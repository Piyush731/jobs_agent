#!/usr/bin/env python3
"""
ai/llm_client.py — Multi-provider LLM client with auto-fallback chain.

Provider priority:
  1. Google Gemini 2.0 Flash  (free: 15 RPM, 1500 RPD, 1M tokens/day)
  2. Groq Cloud               (free: Llama 3.1 70B, 30 RPM, 14400 RPD)
  3. Local Ollama              (Llama 3.2 3B, unlimited, no API key)

Features:
  - Automatic fallback on rate-limit / error / empty response
  - Per-provider rate limiting (thread-safe)
  - JSON generation with robust extraction + retry
  - Usage tracking across all providers
"""

import json
import os
import re
import time
from datetime import date
from threading import Lock
from typing import Any, Dict, List, Optional

from config import AI_CONFIG
from core.logger import get_logger

logger = get_logger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Optional provider libraries (graceful if missing)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

try:
    from groq import Groq as _GroqClient
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False

import requests as _http          # always available — used for Ollama


def _resolve_key(name: str) -> str:
    """Resolve an API key from config module attrs → AI_CONFIG dict → env."""
    try:
        import config as _cfg_mod
        val = getattr(_cfg_mod, name.upper(), None)
        if val:
            return str(val)
    except Exception:
        pass
    val = AI_CONFIG.get(name.lower(), '') or AI_CONFIG.get(name, '')
    if val:
        return str(val)
    return os.getenv(name.upper(), '')


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Rate Limiter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class RateLimiter:
    """Thread-safe sliding-window rate limiter (per-minute + per-day)."""

    def __init__(self, rpm: int = 15, rpd: int = 1500):
        self.rpm = rpm
        self.rpd = rpd
        self._minute_ts: List[float] = []
        self._daily_count: int = 0
        self._daily_date: date = date.today()
        self._lock = Lock()

    # ── public ────────────────────────────────────────────
    def can_call(self) -> bool:
        with self._lock:
            self._tick()
            return (len(self._minute_ts) < self.rpm
                    and self._daily_count < self.rpd)

    def record(self):
        with self._lock:
            self._tick()
            self._minute_ts.append(time.time())
            self._daily_count += 1

    def wait_seconds(self) -> float:
        """Seconds to wait before next call; -1 if daily exhausted."""
        with self._lock:
            self._tick()
            if self._daily_count >= self.rpd:
                return -1.0
            if len(self._minute_ts) >= self.rpm:
                return max(0.0, 60.0 - (time.time() - self._minute_ts[0]) + 0.5)
            return 0.0

    @property
    def usage(self) -> Dict[str, int]:
        with self._lock:
            self._tick()
            return {
                'rpm_used': len(self._minute_ts),
                'rpm_limit': self.rpm,
                'daily_used': self._daily_count,
                'daily_limit': self.rpd,
                'daily_remaining': max(0, self.rpd - self._daily_count),
            }

    # ── internal ──────────────────────────────────────────
    def _tick(self):
        now = time.time()
        self._minute_ts = [t for t in self._minute_ts if now - t < 60]
        today = date.today()
        if today != self._daily_date:
            self._daily_count = 0
            self._daily_date = today


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLM Client
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class LLMClient:
    """
    Unified LLM interface with Gemini → Groq → Ollama fallback chain.

    Usage
    -----
        client = LLMClient()
        text   = client.generate("Summarise this JD …")
        data   = client.generate_json("Extract skills as JSON …")
        stats  = client.get_usage()
    """

    def __init__(self):
        self._cfg = AI_CONFIG
        self._chain: List[str] = []          # ordered provider names
        self._total_calls: int = 0
        self._total_errors: int = 0
        self._last_provider: Optional[str] = None

        # ── 1. Gemini ─────────────────────────────────────
        self._gemini_model = None
        gemini_key = _resolve_key('GEMINI_API_KEY')
        if GEMINI_AVAILABLE and gemini_key:
            try:
                genai.configure(api_key=gemini_key)
                model_name = self._cfg.get('model', 'gemini-2.0-flash')
                self._gemini_model = genai.GenerativeModel(model_name=model_name)
                self._chain.append('gemini')
                logger.info("✓ Gemini ready  (%s)", model_name)
            except Exception as exc:
                logger.error("Gemini init failed: %s", exc)
        elif not GEMINI_AVAILABLE:
            logger.warning("google-generativeai not installed — Gemini skipped")
        else:
            logger.warning("GEMINI_API_KEY not set — Gemini skipped")

        self._gemini_rl = RateLimiter(
            rpm=int(self._cfg.get('rpm_limit', 15)),
            rpd=int(self._cfg.get('daily_limit', 1500)),
        )

        # ── 2. Groq ──────────────────────────────────────
        self._groq_client = None
        groq_key = _resolve_key('GROQ_API_KEY')
        if GROQ_AVAILABLE and groq_key:
            try:
                self._groq_client = _GroqClient(api_key=groq_key)
                self._chain.append('groq')
                logger.info("✓ Groq ready    (%s)",
                            self._cfg.get('backup_model', 'llama-3.1-70b-versatile'))
            except Exception as exc:
                logger.error("Groq init failed: %s", exc)
        elif not GROQ_AVAILABLE:
            logger.warning("groq library not installed — Groq skipped")
        else:
            logger.warning("GROQ_API_KEY not set — Groq skipped")

        self._groq_rl = RateLimiter(rpm=30, rpd=14400)

        # ── 3. Ollama (always last) ──────────────────────
        self._ollama_url = self._cfg.get('ollama_url', 'http://localhost:11434')
        self._ollama_model = self._cfg.get('local_model', 'llama3.2:3b')
        self._chain.append('ollama')
        self._ollama_rl = RateLimiter(rpm=9999, rpd=999999)

        if not self._chain:
            logger.error("No LLM providers available at all!")
        else:
            logger.info("LLM fallback chain: %s", " → ".join(self._chain))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC — generate text
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """
        Generate a text completion.  Walks the fallback chain on failure.
        Returns empty string only if every provider fails.
        """
        max_tokens = max_tokens or int(self._cfg.get('max_tokens', 1000))
        temperature = temperature if temperature is not None else float(self._cfg.get('temperature', 0.3))

        for provider in self._chain:
            rl = self._limiter(provider)

            # rate-limit gate
            wait = rl.wait_seconds()
            if wait < 0:
                logger.warning("%s daily limit hit — skipping", provider)
                continue
            if wait > 0:
                logger.debug("%s RPM limit — sleeping %.1fs", provider, wait)
                time.sleep(wait)

            try:
                text = self._dispatch(provider, prompt, system_prompt,
                                      max_tokens, temperature)
                if text and text.strip():
                    rl.record()
                    self._last_provider = provider
                    self._total_calls += 1
                    logger.debug("%s returned %d chars", provider, len(text))
                    return text.strip()
                logger.warning("%s returned empty — trying next", provider)
            except Exception as exc:
                self._total_errors += 1
                logger.warning("%s error (%s): %s — trying next",
                               provider, type(exc).__name__, exc)

        logger.error("ALL providers failed for prompt: %.120s…", prompt)
        return ""

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC — generate JSON
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def generate_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        retries: int = 2,
    ) -> Dict[str, Any]:
        """
        Generate and parse a JSON response.  Retries with stronger
        instructions on parse failure.  Returns ``{}`` on total failure.
        """
        suffix = ("\n\nIMPORTANT: Respond ONLY with valid JSON. "
                  "No markdown, no explanation, no code fences. "
                  "Start with { and end with }.")

        for attempt in range(retries + 1):
            raw = self.generate(prompt + suffix, system_prompt,
                                max_tokens, temperature)
            if not raw:
                continue
            try:
                return self._extract_json(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("JSON parse attempt %d/%d failed: %s",
                               attempt + 1, retries + 1, exc)
                # tighten instruction for next attempt
                suffix = ("\n\nYou MUST reply with ONLY valid JSON. "
                          "No text before or after. Start with { end with }.")

        logger.error("Failed to get valid JSON after %d attempts", retries + 1)
        return {}

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC — status helpers
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def can_call(self) -> bool:
        """True if at least one provider can take a request right now."""
        return any(self._limiter(p).can_call() for p in self._chain)

    def get_usage(self) -> Dict[str, Any]:
        return {
            'total_calls': self._total_calls,
            'total_errors': self._total_errors,
            'last_provider': self._last_provider,
            'providers': {p: self._limiter(p).usage for p in self._chain},
        }

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PRIVATE — provider dispatchers
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _dispatch(self, provider: str, prompt: str,
                  system_prompt: Optional[str],
                  max_tokens: int, temperature: float) -> str:
        fn = {'gemini': self._call_gemini,
              'groq':   self._call_groq,
              'ollama': self._call_ollama}.get(provider)
        if fn is None:
            raise ValueError(f"Unknown provider: {provider}")
        return fn(prompt, system_prompt, max_tokens, temperature)

    # ── Gemini ────────────────────────────────────────────
    def _call_gemini(self, prompt: str, system_prompt: Optional[str],
                     max_tokens: int, temperature: float) -> str:
        if self._gemini_model is None:
            raise RuntimeError("Gemini not initialised")

        gen_cfg = genai.GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
        )

        # Prepend system prompt to user prompt (avoids creating new Model
        # instance on every call just for system_instruction).
        full = f"Instructions: {system_prompt}\n\n{prompt}" if system_prompt else prompt

        response = self._gemini_model.generate_content(full,
                                                       generation_config=gen_cfg)
        # Handle safety blocks
        try:
            return response.text
        except ValueError:
            feedback = getattr(response, 'prompt_feedback', None)
            logger.warning("Gemini safety block: %s", feedback)
            return ""

    # ── Groq ──────────────────────────────────────────────
    def _call_groq(self, prompt: str, system_prompt: Optional[str],
                   max_tokens: int, temperature: float) -> str:
        if self._groq_client is None:
            raise RuntimeError("Groq not initialised")

        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        resp = self._groq_client.chat.completions.create(
            model=self._cfg.get('backup_model', 'llama-3.1-70b-versatile'),
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""

    # ── Ollama (local) ────────────────────────────────────
    def _call_ollama(self, prompt: str, system_prompt: Optional[str],
                     max_tokens: int, temperature: float) -> str:
        full = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        try:
            resp = _http.post(
                f"{self._ollama_url}/api/generate",
                json={
                    'model': self._ollama_model,
                    'prompt': full,
                    'stream': False,
                    'options': {
                        'temperature': temperature,
                        'num_predict': max_tokens,
                    },
                },
                timeout=180,
            )
            resp.raise_for_status()
            return resp.json().get('response', '')
        except _http.ConnectionError:
            raise RuntimeError(
                f"Ollama not running at {self._ollama_url} — "
                "start with: ollama serve")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PRIVATE — helpers
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _limiter(self, provider: str) -> RateLimiter:
        return {'gemini': self._gemini_rl,
                'groq':   self._groq_rl,
                'ollama': self._ollama_rl}.get(provider, self._ollama_rl)

    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        """
        Robustly pull JSON from LLM output that may contain markdown
        fences, leading prose, or trailing commentary.
        """
        text = text.strip()

        # 1) Direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 2) Markdown code fence  ```json … ```
        m = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 3) Find outermost braces { … }
        result = LLMClient._find_balanced(text, '{', '}')
        if result is not None:
            return result

        # 4) Find outermost brackets [ … ]
        result = LLMClient._find_balanced(text, '[', ']')
        if result is not None:
            return result

        raise ValueError(f"No valid JSON in: {text[:300]}…")

    @staticmethod
    def _find_balanced(text: str, open_ch: str, close_ch: str):
        depth = 0
        start = None
        for i, ch in enumerate(text):
            if ch == open_ch:
                if depth == 0:
                    start = i
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        start = None
                        depth = 0
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Quick self-test
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("  LLM Client — Self-Test")
    print("=" * 60)

    client = LLMClient()
    print(f"\nFallback chain : {client._chain}")
    print(f"Can call now   : {client.can_call()}")
    print(f"Usage          : {json.dumps(client.get_usage(), indent=2)}")

    if not client.can_call():
        print("\n⚠  No provider available — set GEMINI_API_KEY or GROQ_API_KEY "
              "in .env, or start Ollama.")
        sys.exit(1)

    # ── Text generation ───────────────────────────────────
    print("\n─── Text Generation ───")
    result = client.generate(
        "Say exactly: 'LLM client is working!' — nothing else.",
        system_prompt="You are a test bot. Follow instructions precisely."
    )
    print(f"Response : {result}")
    assert result, "Text generation returned empty!"

    # ── JSON generation ───────────────────────────────────
    print("\n─── JSON Generation ───")
    data = client.generate_json(
        'Return a JSON object with keys "status" (string "ok") '
        'and "languages" (array of 3 programming language names).'
    )
    print(f"Parsed   : {json.dumps(data, indent=2)}")
    assert isinstance(data, dict) and data, "JSON generation returned empty!"

    # ── Usage after tests ─────────────────────────────────
    print(f"\nFinal usage: {json.dumps(client.get_usage(), indent=2)}")
    print("\n✅  All LLM client tests passed!")