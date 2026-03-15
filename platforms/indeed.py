#!/usr/bin/env python3
"""
platforms/indeed.py — Indeed Platform Integration

Auth: Cookie-based ONLY.  No password stored.
  • First run → visible browser + Telegram alert → user logs in via Google /
    magic-link → cookies saved.
  • Subsequent runs → saved cookies are loaded automatically.
  • Re-auth typically needed every 2-4 weeks.

Capabilities:
  • Search jobs across queries × locations with pagination
  • Extract full job details (JD, skills, salary, experience)
  • Semi-auto Indeed Apply: fill form → pause → Telegram approve → submit
  • Resume upload (file input OR file-chooser dialog OR "resume on file")
  • Multi-step form traversal with smart field detection and answering
  • CAPTCHA detection + Telegram-assisted solving
  • OTP handling via Telegram
  • Rate limiting: 20 apps/day, 3+ min gap, cooldown on issues
  • Full anti-ban: human-like delays, scrolling, typing, mouse movement
"""

from __future__ import annotations

import os
import re
import json
import time
import random
import asyncio
import traceback as tb_module
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from platforms.base import PlatformBase
from core.logger import get_logger
from core.db import get_db
from config import (
    BASE_DIR,
    BROWSER_PROFILES_DIR,
    CACHE_DIR,
    PLATFORM_CONFIG,
    STEALTH_CONFIG,
    USER_PROFILE,
)

# Optional — gracefully degrade if profile/answers isn't built yet
try:
    from profile.answers import get_answer, get_salary_answer, get_standard
except ImportError:
    get_answer = get_salary_answer = get_standard = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://in.indeed.com"
_SEARCH_URL = f"{_BASE_URL}/jobs"

# Multiple selectors per element — tried in order until one succeeds.
# Keeps the scraper resilient against Indeed's frequent DOM changes.
_SEL: Dict[str, List[str]] = {
    # ── Search page ────────────────────────────────────────────────────
    "search_input": [
        "input#text-input-what",
        'input[name="q"]',
        "#what",
        'input[aria-label*="what"]',
        'input[placeholder*="job title"]',
    ],
    "location_input": [
        "input#text-input-where",
        'input[name="l"]',
        "#where",
        'input[aria-label*="where"]',
        'input[placeholder*="location"]',
    ],
    "search_button": [
        'button[type="submit"]',
        "button.yosegi-InlineWhatWhere-primaryButton",
        'button:has-text("Find jobs")',
        'button:has-text("Search")',
    ],
    # ── Job cards ──────────────────────────────────────────────────────
    "job_cards": [
        ".job_seen_beacon",
        ".jobsearch-ResultsList > li",
        '[data-testid="slider_item"]',
        ".resultContent",
        "div.cardOutline",
        "li.css-5lfssm",
        "td.resultContent",
    ],
    "job_title": [
        "h2.jobTitle a",
        "h2.jobTitle span",
        '[data-testid="jobTitle"]',
        ".jobTitle > a",
        ".jcs-JobTitle",
        "a[data-jk]",
    ],
    "company_name": [
        '[data-testid="company-name"]',
        ".companyName",
        "span.css-92r8pb",
        ".company_location .companyName",
        'span[data-testid="company-name"]',
    ],
    "location": [
        '[data-testid="text-location"]',
        ".companyLocation",
        "div.css-1p0sjhy",
        ".company_location .companyLocation",
    ],
    "salary": [
        ".salary-snippet-container",
        '[data-testid="attribute_snippet_testid"]',
        ".salaryText",
        ".metadata.salary-snippet-container",
        "div.css-1cvvo1b",
    ],
    "date_posted": [
        ".date",
        "span.css-qvloho",
        '[data-testid="myJobsStateDate"]',
        ".result-footer .date",
    ],
    "job_snippet": [
        ".job-snippet",
        ".underShelfFooter",
        'div[class*="job-snippet"]',
        "table.jobCardShelfContainer",
    ],
    # ── Job detail page ────────────────────────────────────────────────
    "job_description": [
        "#jobDescriptionText",
        ".jobsearch-jobDescriptionText",
        '[data-testid="jobDescriptionText"]',
        "#jobDescription",
        ".jobsearch-JobComponent-description",
    ],
    "detail_title": [
        "h1.jobsearch-JobInfoHeader-title",
        'h1[data-testid="jobsearch-JobInfoHeader-title"]',
        "h2.jobsearch-JobInfoHeader-title",
        "h1.icl-u-xs-mb--xs",
        ".jobsearch-JobInfoHeader-title",
        "h1",
    ],
    "detail_company": [
        '[data-testid="inlineHeader-companyName"] a',
        '[data-testid="inlineHeader-companyName"]',
        ".jobsearch-InlineCompanyRating a",
        ".jobsearch-InlineCompanyRating",
        'div[data-company-name="true"] a',
        ".css-1saizt3 a",
    ],
    "detail_location": [
        '[data-testid="inlineHeader-companyLocation"]',
        ".jobsearch-JobInfoHeader-subtitle > div:last-child",
        ".css-6z8o9s",
        'div[data-testid="job-location"]',
    ],
    "detail_salary": [
        "#salaryInfoAndJobType",
        '[data-testid="attribute_snippet_testid"]',
        ".jobsearch-JobMetadataHeader-item",
        ".salary-snippet-container",
        "span.css-19j1a75",
    ],
    "detail_date": [
        ".jobsearch-HiringInsights-entry--bullet",
        'span:has-text("Posted")',
        'span:has-text("days ago")',
        'span:has-text("Just posted")',
        'span:has-text("Today")',
    ],
    # ── Apply button ───────────────────────────────────────────────────
    "apply_button": [
        "button#indeedApplyButton",
        "button.indeed-apply-button",
        'button:has-text("Apply now")',
        'button:has-text("Apply on Indeed")',
        'a:has-text("Apply now")',
        "#applyButtonLinkContainer button",
        'button[data-testid="indeedApplyButton"]',
    ],
    "external_apply": [
        'button:has-text("Apply on company site")',
        'a:has-text("Apply on company site")',
    ],
    # ── Apply form ─────────────────────────────────────────────────────
    "resume_upload": [
        'input[type="file"]',
        'input[accept*="pdf"]',
        'input[accept*="doc"]',
        "#resume-upload",
        'input[name="resume"]',
    ],
    "continue_button": [
        'button:has-text("Continue")',
        'button:has-text("Next")',
        'button[data-testid="continue-button"]',
        "button.ia-continueButton",
        "#ia-continueButton",
    ],
    "submit_button": [
        'button:has-text("Submit your application")',
        'button:has-text("Submit application")',
        'button:has-text("Submit")',
        'button[data-testid="submit-button"]',
        "button.ia-submitButton",
    ],
    "review_button": [
        'button:has-text("Review")',
        'button:has-text("Review your application")',
    ],
    # ── Pagination ─────────────────────────────────────────────────────
    "next_page": [
        'a[data-testid="pagination-page-next"]',
        'a[aria-label="Next Page"]',
        'a:has-text("Next")',
        "nav a:last-child",
    ],
    # ── Auth / Misc ────────────────────────────────────────────────────
    "sign_in_prompt": [
        'a:has-text("Sign in")',
        'button:has-text("Sign in")',
        '[data-testid="sign-in"]',
    ],
    "email_input": [
        "input#ifl-InputFormField-3",
        'input[type="email"]',
        'input[name="__email"]',
        "#email-input",
    ],
    "captcha_frame": [
        'iframe[src*="captcha"]',
        'iframe[src*="recaptcha"]',
        "#recaptcha",
        ".g-recaptcha",
    ],
    "close_popup": [
        'button[aria-label="Close"]',
        "button.icl-CloseButton",
        '[data-testid="close-button"]',
        "button.popover-x-button-close",
    ],
    "cookie_accept": [
        "button#onetrust-accept-btn-handler",
        'button:has-text("Accept")',
        'button:has-text("Accept all")',
        'button:has-text("I accept")',
    ],
}

# Skills regex patterns for extraction from JD text
_SKILL_PATTERNS: List[str] = [
    r"\bJava\b",
    r"\bPython\b",
    r"\bJavaScript\b",
    r"\bTypeScript\b",
    r"\bReact(?:\.js|JS)?\b",
    r"\bAngular(?:\.js|JS)?\b",
    r"\bVue(?:\.js|JS)?\b",
    r"\bNode(?:\.js|JS)?\b",
    r"\bExpress(?:\.js|JS)?\b",
    r"\bNuxt(?:\.js|JS)?\b",
    r"\bNext(?:\.js|JS)?\b",
    r"\bSpring\s*Boot\b",
    r"\bSpring\b",
    r"\bDjango\b",
    r"\bFlask\b",
    r"\bFastAPI\b",
    r"\bMySQL\b",
    r"\bPostgreSQL\b",
    r"\bMongoDB\b",
    r"\bRedis\b",
    r"\bDocker\b",
    r"\bKubernetes\b",
    r"\bAWS\b",
    r"\bAzure\b",
    r"\bGCP\b",
    r"\bGit\b",
    r"\bLinux\b",
    r"\bREST\s*API[s]?\b",
    r"\bGraphQL\b",
    r"\bKafka\b",
    r"\bRabbitMQ\b",
    r"\bElasticsearch\b",
    r"\bHTML\b",
    r"\bCSS\b",
    r"\bTailwind\b",
    r"\bBootstrap\b",
    r"\bSQL\b",
    r"\bNoSQL\b",
    r"\bC\+\+\b",
    r"\bC#\b",
    r"\bGo(?:lang)?\b",
    r"\bRust\b",
    r"\bScala\b",
    r"\bKotlin\b",
    r"\bSwift\b",
    r"\bMicroservices?\b",
    r"\bCI/CD\b",
    r"\bJenkins\b",
    r"\bTerraform\b",
    r"\bAnsible\b",
    r"\bAgile\b",
    r"\bScrum\b",
    r"\bJIRA\b",
    r"\bConfluence\b",
    r"\bMachine\s*Learning\b",
    r"\bDeep\s*Learning\b",
    r"\bNLP\b",
    r"\bTensorFlow\b",
    r"\bPyTorch\b",
    r"\bWebSocket[s]?\b",
    r"\bJWT\b",
    r"\bOAuth\b",
    r"\bHibernate\b",
    r"\bJPA\b",
    r"\bMaven\b",
    r"\bGradle\b",
    r"\bRazorpay\b",
    r"\bStripe\b",
]


# ═══════════════════════════════════════════════════════════════════════════
# IndeedPlatform
# ═══════════════════════════════════════════════════════════════════════════


class IndeedPlatform(PlatformBase):
    """
    Indeed job-platform adapter.

    Authentication
    ──────────────
    Cookie-based only — **no** password is stored.  On first run (or when
    cookies expire) the agent opens a *visible* browser window and sends a
    Telegram alert so the user can log in via Google / magic-link.  Cookies
    are persisted to ``browser_profiles/indeed/`` for subsequent runs.

    Limits
    ──────
    • 20 applications / day
    • ≥3-minute gap between applications
    • Auto-cooldown on CAPTCHA / ban detection
    """

    PLATFORM = "indeed"

    # ─── init ──────────────────────────────────────────────────────────

    def __init__(self, browser_engine, notifier=None):
        """
        Parameters
        ----------
        browser_engine : BrowserEngine
            Shared browser engine (from ``core/browser.py``).
        notifier : JobNotifier | None
            Telegram notifier (from ``tracking/notifications.py``).
        """
        super().__init__(browser_engine, notifier)
        self.platform_name: str = self.PLATFORM
        self.logger = get_logger("IndeedPlatform")
        self.db = get_db()

        # Platform config from config.py
        cfg = PLATFORM_CONFIG.get("indeed", {})
        self.max_daily: int = cfg.get("max_daily_applications", 20)
        _rl = cfg.get("rate_limit_seconds", {"min": 180, "max": 360})
        if isinstance(_rl, dict):
            self._gap_min: int = _rl.get("min", 180)
            self._gap_max: int = _rl.get("max", 360)
        elif isinstance(_rl, (int, float)):
            self._gap_min = int(_rl)
            self._gap_max = int(_rl) + 120
        else:
            self._gap_min, self._gap_max = 180, 360

        self.search_queries: List[str] = cfg.get(
            "search_queries",
            [
                "software developer",
                "full stack developer",
                "backend developer",
                "java developer",
                "node.js developer",
                "SDE",
                "python developer",
            ],
        )
        self.max_pages: int = cfg.get("max_pages_per_query", 3)

        # Runtime state
        self.page = None  # current Playwright Page
        self._last_apply_ts: Optional[datetime] = None
        self._prepared: Dict[str, Dict] = {}  # job_id → state

        self.logger.info("IndeedPlatform initialised  (max %d/day, gap %d-%ds)",
                         self.max_daily, self._gap_min, self._gap_max)

    # ═══════════════════════════════════════════════════════════════════
    #  INTERNAL HELPERS — element lookup, text extraction, popups
    # ═══════════════════════════════════════════════════════════════════

    async def _el(self, page, key: str, timeout: int = 5000):
        """Return first visible element matching any selector in ``_SEL[key]``."""
        for sel in _SEL.get(key, []):
            try:
                el = await page.wait_for_selector(sel, timeout=timeout, state="visible")
                if el:
                    return el
            except Exception:
                continue
        return None

    async def _els(self, page, key: str, timeout: int = 5000) -> List:
        """Return **all** visible elements for the first working selector."""
        for sel in _SEL.get(key, []):
            try:
                await page.wait_for_selector(sel, timeout=timeout, state="visible")
                els = await page.query_selector_all(sel)
                if els:
                    return els
            except Exception:
                continue
        return []

    @staticmethod
    async def _txt(el) -> str:
        """Safely extract trimmed inner-text from an element handle."""
        if el is None:
            return ""
        try:
            t = await el.inner_text()
            return (t or "").strip()
        except Exception:
            try:
                t = await el.text_content()
                return (t or "").strip()
            except Exception:
                return ""

    @staticmethod
    async def _attr(el, name: str) -> str:
        """Safely get an attribute value."""
        if el is None:
            return ""
        try:
            v = await el.get_attribute(name)
            return (v or "").strip()
        except Exception:
            return ""

    async def _css_for(self, el) -> str:
        """Best-effort CSS selector for an element (for ``click_human`` etc.)."""
        try:
            eid = await el.get_attribute("id")
            if eid:
                return f"#{eid}"
            dtid = await el.get_attribute("data-testid")
            if dtid:
                return f'[data-testid="{dtid}"]'
            tag = await el.evaluate("e=>e.tagName.toLowerCase()")
            name = await el.get_attribute("name")
            if name:
                return f'{tag}[name="{name}"]'
            txt = await self._txt(el)
            if txt and len(txt) < 40:
                safe = txt.replace('"', '\\"')[:35]
                return f'{tag}:has-text("{safe}")'
            cls = await el.get_attribute("class")
            if cls:
                return f"{tag}.{cls.split()[0]}"
        except Exception:
            pass
        return "button:visible"

    async def _dismiss_popups(self, page):
        """Click away cookie banners, overlays, "no-thanks" dialogs."""
        targets = (
            _SEL.get("close_popup", [])
            + _SEL.get("cookie_accept", [])
            + [
                'button:has-text("No thanks")',
                'button:has-text("Not now")',
                'button:has-text("Dismiss")',
                'button:has-text("Skip")',
                '[aria-label="Close"]',
                "button.gnav-LoggedOutAccountLink-close",
            ]
        )
        for sel in targets:
            try:
                el = await page.wait_for_selector(sel, timeout=1200, state="visible")
                if el:
                    await el.click()
                    await self._rand(0.3, 1.0)
            except Exception:
                continue

    # ── tiny helpers ───────────────────────────────────────────────────

    async def _rand(self, lo: float = 1.0, hi: float = 3.0):
        """Async random sleep — delegates to browser_engine when possible."""
        try:
            await self.browser_engine.random_delay(lo, hi)
        except Exception:
            await asyncio.sleep(random.uniform(lo, hi))

    async def _screenshot(self, page, label: str) -> str:
        """Take a screenshot; swallow errors."""
        try:
            return await self.browser_engine.take_screenshot(page, label)
        except Exception:
            return ""

    def _stealth_range(self) -> Tuple[float, float]:
        rng = STEALTH_CONFIG.get("random_delay_range", (3, 12))
        return (rng[0], rng[1]) if isinstance(rng, (list, tuple)) else (3, 12)

    async def _human_scroll(self, page, rounds: int = 3):
        """Scroll page incrementally like a human reading."""
        for _ in range(rounds):
            dy = random.randint(200, 550)
            await page.evaluate(f"window.scrollBy(0,{dy})")
            await self._rand(0.6, 2.0)

    async def _close_extra_tabs(self):
        """Keep only the first tab open."""
        try:
            for p in self.page.context.pages[1:]:
                await p.close()
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════════
    #  LOGIN  (cookie-based)
    # ═══════════════════════════════════════════════════════════════════

    async def login(self) -> bool:
        """
        Authenticate to Indeed.

        1. Launch visible browser for ``indeed`` profile.
        2. Load saved cookies and navigate to homepage.
        3. If logged in → done.
        4. Otherwise → send Telegram alert, pre-fill email, wait ≤5 min
           for the user to complete manual Google / magic-link login.
        5. Save cookies on success.
        """
        self.logger.info("═══ Indeed Login ═══")
        try:
            # 1. Launch
            self.page = await self.browser_engine.launch("indeed", headless=False)
            if not self.page:
                self.logger.error("Browser launch failed")
                return False

            # 2. Load cookies + navigate
            await self.browser_engine.load_cookies("indeed")
            await self.page.goto(_BASE_URL, wait_until="domcontentloaded", timeout=30_000)
            await self._rand(2, 4)
            await self._dismiss_popups(self.page)

            # 3. Already logged in?
            if await self._is_logged_in(self.page):
                self.logger.info("✅ Indeed: logged in via saved cookies")
                await self.browser_engine.save_cookies("indeed")
                self._update_session(logged_in=True)
                return True

            # 4. Manual login flow
            self.logger.info("Not logged in — requesting manual auth")
            self._telegram_login_alert()
            await self.page.goto(
                f"{_BASE_URL}/account/login",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await self._rand(2, 3)

            # Pre-fill email
            email = USER_PROFILE.get("email", "")
            if email:
                email_el = await self._el(self.page, "email_input", timeout=4000)
                if email_el:
                    css = await self._css_for(email_el)
                    await self.browser_engine.type_human(self.page, css, email)

            # 5. Wait
            ok = await self._wait_for_login(timeout_s=300)
            if ok:
                self.logger.info("✅ Indeed: manual login succeeded")
                await self.browser_engine.save_cookies("indeed")
                self._update_session(logged_in=True)
                self._tg("indeed", "✅ Indeed login successful!")
                return True

            self.logger.error("❌ Indeed login timed out")
            self._update_session(logged_in=False, error="Manual login timed out",
                                 cooldown_h=1)
            return False

        except Exception as exc:
            self.logger.error("Login exception: %s", exc)
            self.db.save_error("indeed.login", type(exc).__name__,
                               str(exc), tb_module.format_exc())
            self._update_session(logged_in=False, error=str(exc))
            return False

    # ── login sub-routines ─────────────────────────────────────────────

    async def _is_logged_in(self, page) -> bool:
        """Heuristic check for logged-in state."""
        pos = [
            "#AccountMenu",
            'button[data-gnav-element-name="AccountMenu"]',
            '[data-testid="account-menu"]',
            'a[href*="/account"]',
            "#gnav-header-account",
            'a[href*="/myjobs"]',
            "#profileMenuButton",
        ]
        for sel in pos:
            try:
                if await page.wait_for_selector(sel, timeout=2000):
                    return True
            except Exception:
                continue

        # Negative: sign-in link visible → NOT logged in
        sign = await self._el(page, "sign_in_prompt", timeout=1500)
        if sign:
            return False

        url = page.url
        if "/account/login" in url or "/registration" in url:
            return False
        return False  # conservative default

    async def _wait_for_login(self, timeout_s: int = 300) -> bool:
        """Poll every 5 s until login is detected or timeout."""
        t0 = time.monotonic()
        while (time.monotonic() - t0) < timeout_s:
            try:
                if await self._is_logged_in(self.page):
                    return True
                url = self.page.url
                if any(p in url for p in ["/jobs", "/myjobs", "/?from=", "/myaccount"]):
                    await self._rand(2, 3)
                    if await self._is_logged_in(self.page):
                        return True
                    await self.page.goto(_BASE_URL, wait_until="domcontentloaded",
                                         timeout=15_000)
                    await self._rand(2, 3)
                    return await self._is_logged_in(self.page)
            except Exception:
                pass
            await asyncio.sleep(5)
        return False

    def _telegram_login_alert(self):
        if not self.notifier:
            return
        try:
            self.notifier.send_platform_issue(
                "indeed",
                "🔐 *Indeed Login Required*\n\n"
                "A browser window is open.\n"
                "Log in via Google or email magic-link.\n"
                "⏱ Timeout: 5 minutes",
            )
        except Exception:
            pass

    # ── session DB helpers ─────────────────────────────────────────────

    def _update_session(self, *, logged_in: bool, error: str = None,
                        cooldown_h: float = 0):
        updates: Dict[str, Any] = {
            "logged_in": int(logged_in),
            "last_login": datetime.now().isoformat() if logged_in else None,
            "status": "active" if logged_in else ("cooldown" if cooldown_h else "active"),
            "last_error": error,
        }
        if cooldown_h:
            updates["cooldown_until"] = (
                datetime.now() + timedelta(hours=cooldown_h)
            ).isoformat()
        self.db.update_platform_session("indeed", updates)

    def _tg(self, platform: str, msg: str):
        if self.notifier:
            try:
                self.notifier.send_platform_issue(platform, msg)
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════════════
    #  SEARCH JOBS
    # ═══════════════════════════════════════════════════════════════════

    async def search_jobs(
        self,
        queries: List[str] | None = None,
        filters: Dict | None = None,
    ) -> List[Dict]:
        """
        Search Indeed across *queries × locations* with pagination.

        Parameters
        ----------
        queries : list[str] | None
            Search strings.  Falls back to ``config.PLATFORM_CONFIG["indeed"]["search_queries"]``.
        filters : dict | None
            Optional keys: ``locations`` (list[str]), ``fromage`` (int, days),
            ``job_type`` (str), ``experience`` (str), ``sort`` (str).

        Returns
        -------
        list[dict]
            Unique jobs — each dict has at minimum:
            ``platform_job_id, url, title, company, location, salary_text,
            experience_text, description, posted_date, skills, work_mode,
            job_type, discovered_at``.
        """
        self.logger.info("═══ Indeed Search ═══")
        if not self.page:
            self.logger.error("Browser not ready — call login() first")
            return []

        queries = queries or self.search_queries
        filters = filters or {}
        locations: List[str] = filters.get(
            "locations",
            USER_PROFILE.get(
                "target_locations",
                ["Bangalore", "Hyderabad", "Pune", "Remote", "Delhi NCR", "Mumbai"],
            ),
        )
        fromage: int = filters.get("fromage", 3)
        job_type: str = filters.get("job_type", "fulltime")
        experience: str = filters.get("experience", "")
        sort_by: str = filters.get("sort", "date")

        all_jobs: List[Dict] = []

        for q in queries:
            for loc in locations:
                try:
                    self.logger.info("→ '%s' in '%s'", q, loc)
                    batch = await self._run_query(
                        q, loc, fromage, job_type, experience, sort_by,
                    )
                    self.logger.info("  found %d jobs", len(batch))
                    all_jobs.extend(batch)
                    lo, hi = self._stealth_range()
                    await self._rand(lo, hi)
                except Exception as exc:
                    self.logger.error("Search error '%s' / '%s': %s", q, loc, exc)
                    self.db.save_error("indeed.search", type(exc).__name__,
                                       str(exc), tb_module.format_exc())

        # Deduplicate by platform_job_id within this batch
        # Deduplicate by platform_job_id within this batch
        seen: set = set()
        unique: List[Dict] = []
        for j in all_jobs:
            jid = j.get("platform_job_id", "")
            if jid and jid not in seen:
                seen.add(jid)
                unique.append(j)

        self.logger.info("═══ Indeed Search Complete: %d unique jobs ═══", len(unique))
        return unique

    # ── single query runner ────────────────────────────────────────────

    async def _run_query(
        self,
        query: str,
        location: str,
        fromage: int,
        job_type: str,
        experience: str,
        sort_by: str,
    ) -> List[Dict]:
        """Execute a single (query, location) search with pagination."""
        jobs: List[Dict] = []

        for page_num in range(self.max_pages):
            params: Dict[str, Any] = {
                "q": query,
                "l": location,
                "fromage": fromage,
                "sort": sort_by,
            }
            if job_type:
                params["jt"] = job_type
            if experience:
                params["explvl"] = experience
            if page_num > 0:
                params["start"] = page_num * 10

            url = f"{_SEARCH_URL}?{urlencode(params)}"
            self.logger.debug("  page %d → %s", page_num + 1, url)

            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await self._rand(2, 4)
                await self._dismiss_popups(self.page)
                await self._human_scroll(self.page, rounds=random.randint(2, 4))

                # Captcha check
                cap = await self.detect_captcha(self.page)
                if cap:
                    self.logger.warning("CAPTCHA detected on search page")
                    solved = await self.handle_captcha(self.page, self.notifier)
                    if not solved:
                        self.logger.error("CAPTCHA unsolved — aborting query")
                        break

                # No results?
                no_results_sels = [
                    '.jobsearch-NoResult-messageContainer',
                    'p:has-text("did not match any jobs")',
                    'p:has-text("No matching jobs found")',
                    'div:has-text("0 results")',
                ]
                no_res = False
                for sel in no_results_sels:
                    try:
                        el = await self.page.wait_for_selector(sel, timeout=1500)
                        if el:
                            no_res = True
                            break
                    except Exception:
                        continue
                if no_res:
                    self.logger.info("  no results for '%s' in '%s'", query, location)
                    break

                batch = await self._parse_job_cards(self.page, query, location)
                if not batch:
                    self.logger.debug("  0 cards parsed — stopping pagination")
                    break
                jobs.extend(batch)

                # Check next page exists
                if page_num < self.max_pages - 1:
                    nxt = await self._el(self.page, "next_page", timeout=2000)
                    if not nxt:
                        break
                    await self._rand(1.5, 3.0)

            except Exception as exc:
                self.logger.error("  page %d error: %s", page_num + 1, exc)
                break

        return jobs

    # ── card parsing ───────────────────────────────────────────────────

    async def _parse_job_cards(self, page, query: str, location: str) -> List[Dict]:
        """Parse all job cards on the current search-results page."""
        cards = await self._els(page, "job_cards", timeout=8000)
        if not cards:
            self.logger.debug("  no job cards found")
            return []

        results: List[Dict] = []
        for card in cards:
            try:
                data = await self._extract_card_data(card, query, location)
                if data and data.get("platform_job_id"):
                    results.append(data)
            except Exception as exc:
                self.logger.debug("  card parse error: %s", exc)
        return results

    async def _extract_card_data(self, card, query: str, location: str) -> Optional[Dict]:
        """Extract structured data from a single job card element."""
        now_iso = datetime.now().isoformat()

        # ── Job ID ─────────────────────────────────────────────────────
        jid = ""
        for attr in ("data-jk", "id", "data-id"):
            jid = await self._attr(card, attr)
            if jid:
                break
        if not jid:
            # Try extracting from child <a> href
            link_el = await card.query_selector("a[data-jk]")
            if link_el:
                jid = await self._attr(link_el, "data-jk")
            if not jid:
                link_el = await card.query_selector("a[href*='jk=']")
                if link_el:
                    href = await self._attr(link_el, "href")
                    m = re.search(r"jk=([a-f0-9]+)", href)
                    if m:
                        jid = m.group(1)
        if not jid:
            return None

        # ── Title ──────────────────────────────────────────────────────
        title = ""
        for sel in _SEL["job_title"]:
            el = await card.query_selector(sel)
            if el:
                title = await self._txt(el)
                if title:
                    break
        if not title:
            return None

        # ── URL ────────────────────────────────────────────────────────
        url = f"{_BASE_URL}/viewjob?jk={jid}"
        for sel in _SEL["job_title"]:
            el = await card.query_selector(sel)
            if el:
                href = await self._attr(el, "href")
                if href:
                    if href.startswith("/"):
                        href = f"{_BASE_URL}{href}"
                    url = href
                    break

        # ── Company ────────────────────────────────────────────────────
        company = ""
        for sel in _SEL["company_name"]:
            el = await card.query_selector(sel)
            if el:
                company = await self._txt(el)
                if company:
                    break

        # ── Location ───────────────────────────────────────────────────
        loc = ""
        for sel in _SEL["location"]:
            el = await card.query_selector(sel)
            if el:
                loc = await self._txt(el)
                if loc:
                    break

        # ── Salary ─────────────────────────────────────────────────────
        salary = ""
        for sel in _SEL["salary"]:
            el = await card.query_selector(sel)
            if el:
                salary = await self._txt(el)
                if salary:
                    break

        # ── Posted date ────────────────────────────────────────────────
        posted = ""
        for sel in _SEL["date_posted"]:
            el = await card.query_selector(sel)
            if el:
                posted = await self._txt(el)
                if posted:
                    break

        # ── Snippet ────────────────────────────────────────────────────
        snippet = ""
        for sel in _SEL["job_snippet"]:
            el = await card.query_selector(sel)
            if el:
                snippet = await self._txt(el)
                if snippet:
                    break

        # ── Work mode detection ────────────────────────────────────────
        combined = f"{title} {loc} {snippet}".lower()
        if "remote" in combined:
            work_mode = "remote"
        elif "hybrid" in combined:
            work_mode = "hybrid"
        else:
            work_mode = "onsite"

        # ── Experience hint ────────────────────────────────────────────
        exp_text = ""
        exp_match = re.search(r"(\d+)\s*[-–to]+\s*(\d+)\s*(?:years?|yrs?)", combined)
        if not exp_match:
            exp_match = re.search(r"(\d+)\+?\s*(?:years?|yrs?)", combined)
        if exp_match:
            exp_text = exp_match.group(0)

        # ── Skills from snippet ────────────────────────────────────────
        skills_found: List[str] = []
        for pat in _SKILL_PATTERNS:
            if re.search(pat, f"{title} {snippet}", re.IGNORECASE):
                match_str = re.search(pat, f"{title} {snippet}", re.IGNORECASE)
                if match_str:
                    skills_found.append(match_str.group(0))

        return {
            "platform": "indeed",
            "platform_job_id": jid,
            "url": url,
            "title": title,
            "company": company,
            "location": loc,
            "salary_text": salary,
            "experience_text": exp_text,
            "description": snippet,
            "posted_date": posted,
            "skills": list(set(skills_found)),
            "work_mode": work_mode,
            "job_type": "full-time",
            "discovered_at": now_iso,
            "search_query": query,
            "search_location": location,
        }

    # ═══════════════════════════════════════════════════════════════════
    #  GET JOB DETAILS
    # ═══════════════════════════════════════════════════════════════════

    async def get_job_details(self, job_url: str) -> Dict:
        """
        Navigate to a job page and extract the full JD, salary, skills, etc.

        Returns
        -------
        dict
            Keys: ``title, company, location, salary_text, experience_text,
            description, skills, work_mode, job_type, has_indeed_apply,
            posted_date``.
        """
        self.logger.info("Fetching details: %s", job_url)
        if not self.page:
            self.logger.error("Browser not ready")
            return {}

        try:
            await self.page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
            await self._rand(2, 4)
            await self._dismiss_popups(self.page)
            await self._human_scroll(self.page, rounds=random.randint(2, 3))

            result: Dict[str, Any] = {"url": job_url}

            # ── Title ──────────────────────────────────────────────────
            el = await self._el(self.page, "detail_title", timeout=5000)
            result["title"] = await self._txt(el)

            # ── Company ────────────────────────────────────────────────
            el = await self._el(self.page, "detail_company", timeout=3000)
            result["company"] = await self._txt(el)

            # ── Location ───────────────────────────────────────────────
            el = await self._el(self.page, "detail_location", timeout=3000)
            result["location"] = await self._txt(el)

            # ── Salary ─────────────────────────────────────────────────
            el = await self._el(self.page, "detail_salary", timeout=3000)
            result["salary_text"] = await self._txt(el)

            # ── Posted date ────────────────────────────────────────────
            posted = ""
            for sel in _SEL["detail_date"]:
                try:
                    el2 = await self.page.wait_for_selector(sel, timeout=2000)
                    if el2:
                        posted = await self._txt(el2)
                        if posted:
                            break
                except Exception:
                    continue
            result["posted_date"] = posted

            # ── Full JD ────────────────────────────────────────────────
            desc_el = await self._el(self.page, "job_description", timeout=6000)
            description = await self._txt(desc_el)
            result["description"] = description

            # ── Skills from JD ─────────────────────────────────────────
            skills: List[str] = []
            for pat in _SKILL_PATTERNS:
                m = re.search(pat, description, re.IGNORECASE)
                if m:
                    skills.append(m.group(0))
            result["skills"] = list(set(skills))

            # ── Experience from JD ─────────────────────────────────────
            exp_text = ""
            exp_m = re.search(
                r"(\d+)\s*[-–to]+\s*(\d+)\s*(?:years?|yrs?)", description, re.IGNORECASE
            )
            if not exp_m:
                exp_m = re.search(r"(\d+)\+?\s*(?:years?|yrs?)", description, re.IGNORECASE)
            if exp_m:
                exp_text = exp_m.group(0)
            result["experience_text"] = exp_text

            # ── Work mode ──────────────────────────────────────────────
            combined_lower = f"{result.get('title','')} {result.get('location','')} {description}".lower()
            if "remote" in combined_lower:
                result["work_mode"] = "remote"
            elif "hybrid" in combined_lower:
                result["work_mode"] = "hybrid"
            else:
                result["work_mode"] = "onsite"

            result["job_type"] = "full-time"

            # ── Indeed Apply available? ────────────────────────────────
            apply_btn = await self._el(self.page, "apply_button", timeout=3000)
            external_btn = await self._el(self.page, "external_apply", timeout=1500)
            result["has_indeed_apply"] = apply_btn is not None and external_btn is None

            self.logger.info("  ✅ '%s' @ '%s' — %d skills, apply=%s",
                             result.get("title", "?"), result.get("company", "?"),
                             len(result.get("skills", [])),
                             result.get("has_indeed_apply"))
            return result

        except Exception as exc:
            self.logger.error("get_job_details error: %s", exc)
            self.db.save_error("indeed.get_job_details", type(exc).__name__,
                               str(exc), tb_module.format_exc())
            return {}

    # ═══════════════════════════════════════════════════════════════════
    #  PREPARE APPLICATION  (fill form — DO NOT SUBMIT)
    # ═══════════════════════════════════════════════════════════════════

    async def prepare_application(
        self,
        job: Dict,
        resume_path: str,
        cover_letter: Optional[str] = None,
    ) -> Dict:
        """
        Navigate to the job, click Apply, fill the multi-step form, and
        **pause before final submission**.

        Returns
        -------
        dict
            ``{success: bool, job_id, method, state, error?}``
            ``state`` is stored internally so ``submit_application`` can
            continue from the same point.
        """
        jid = str(job.get("id", job.get("platform_job_id", "unknown")))
        self.logger.info("═══ Prepare Application: %s @ %s ═══",
                         job.get("title", "?"), job.get("company", "?"))

        if not self.page:
            return {"success": False, "job_id": jid, "error": "Browser not ready"}

        if not self._can_apply_now():
            return {"success": False, "job_id": jid, "error": "Rate limit / daily cap"}

        if not os.path.isfile(resume_path):
            return {"success": False, "job_id": jid, "error": f"Resume not found: {resume_path}"}

        url = job.get("url", "")
        if not url:
            return {"success": False, "job_id": jid, "error": "No job URL"}

        try:
            # Navigate to job page
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await self._rand(2, 4)
            await self._dismiss_popups(self.page)
            await self._human_scroll(self.page, rounds=random.randint(1, 3))

            # CAPTCHA check
            cap = await self.detect_captcha(self.page)
            if cap:
                solved = await self.handle_captcha(self.page, self.notifier)
                if not solved:
                    return {"success": False, "job_id": jid, "error": "CAPTCHA unsolved"}

            # Click Apply
            apply_btn = await self._el(self.page, "apply_button", timeout=5000)
            if not apply_btn:
                # Maybe external apply
                ext = await self._el(self.page, "external_apply", timeout=2000)
                if ext:
                    return {"success": False, "job_id": jid,
                            "error": "External apply — not Indeed Apply",
                            "method": "external"}
                return {"success": False, "job_id": jid,
                        "error": "Apply button not found"}

            css = await self._css_for(apply_btn)
            await self.browser_engine.click_human(self.page, css)
            await self._rand(3, 5)

            # Indeed Apply opens in modal or new page — handle both
            apply_page = self.page
            pages = self.page.context.pages
            if len(pages) > 1:
                apply_page = pages[-1]
                await apply_page.wait_for_load_state("domcontentloaded", timeout=15_000)
                await self._rand(2, 3)

            # Walk through multi-step form
            form_result = await self._walk_apply_form(
                apply_page, resume_path, cover_letter, job
            )

            if form_result.get("ready_to_submit"):
                # Store state for submit_application
                self._prepared[jid] = {
                    "apply_page": apply_page,
                    "job": job,
                    "resume_path": resume_path,
                    "timestamp": datetime.now().isoformat(),
                }
                self.logger.info("  ✅ Application PREPARED — awaiting approval")
                return {
                    "success": True,
                    "job_id": jid,
                    "method": "indeed_apply",
                    "state": "prepared",
                    "steps_completed": form_result.get("steps", 0),
                }
            else:
                err = form_result.get("error", "Form walk failed")
                self.logger.warning("  ❌ Prepare failed: %s", err)
                await self._screenshot(apply_page, f"indeed_prepare_fail_{jid}")
                await self._close_extra_tabs()
                return {"success": False, "job_id": jid, "error": err}

        except Exception as exc:
            self.logger.error("prepare_application error: %s", exc)
            self.db.save_error("indeed.prepare_application", type(exc).__name__,
                               str(exc), tb_module.format_exc())
            await self._close_extra_tabs()
            return {"success": False, "job_id": jid, "error": str(exc)}

    # ── multi-step form walker ─────────────────────────────────────────

    async def _walk_apply_form(
        self,
        page,
        resume_path: str,
        cover_letter: Optional[str],
        job: Dict,
    ) -> Dict:
        """
        Walk through Indeed's multi-step apply form.

        Steps typically: Contact → Resume → Questions → Review → Submit.
        This method stops at the **Submit** / **Review** step and returns
        ``{ready_to_submit: True}`` so the caller can wait for Telegram
        approval.
        """
        MAX_STEPS = 12  # safety: never loop more than this
        steps = 0

        for step in range(MAX_STEPS):
            await self._rand(1.5, 3.0)
            await self._dismiss_popups(page)

            page_html = ""
            try:
                page_html = await page.content()
            except Exception:
                pass
            page_lower = page_html.lower()

            # ── Check for submit / review button = we're done ──────────
            submit_btn = await self._el(page, "submit_button", timeout=2000)
            if submit_btn:
                self.logger.info("  step %d: SUBMIT button found — ready", step + 1)
                return {"ready_to_submit": True, "steps": step + 1}

            review_btn = await self._el(page, "review_button", timeout=1500)
            if review_btn:
                self.logger.info("  step %d: REVIEW button — clicking", step + 1)
                css = await self._css_for(review_btn)
                await self.browser_engine.click_human(page, css)
                await self._rand(2, 4)
                # After review, submit should appear
                submit_btn2 = await self._el(page, "submit_button", timeout=5000)
                if submit_btn2:
                    return {"ready_to_submit": True, "steps": step + 2}
                continue

            # ── Check for confirmation / success ───────────────────────
            if self._is_success_page(page_lower, page.url):
                self.logger.info("  step %d: already submitted? success page detected", step + 1)
                return {"ready_to_submit": False, "error": "Already submitted (success page)"}

            # ── CAPTCHA ────────────────────────────────────────────────
            cap = await self.detect_captcha(page)
            if cap:
                solved = await self.handle_captcha(page, self.notifier)
                if not solved:
                    return {"ready_to_submit": False, "error": "CAPTCHA unsolved"}
                continue

            # ── Resume upload step ─────────────────────────────────────
            uploaded = await self._upload_resume(page, resume_path)
            if uploaded:
                self.logger.info("  step %d: resume uploaded", step + 1)

            # ── Fill form fields ───────────────────────────────────────
            fields_handled = await self._handle_form_fields(page, job, cover_letter)
            if fields_handled:
                self.logger.info("  step %d: %d fields handled", step + 1, fields_handled)
                steps = step + 1

            # ── Click Continue / Next ──────────────────────────────────
            cont_btn = await self._el(page, "continue_button", timeout=3000)
            if cont_btn:
                self.logger.info("  step %d: clicking Continue", step + 1)
                css = await self._css_for(cont_btn)
                await self.browser_engine.click_human(page, css)
                await self._rand(2, 4)
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                continue

            # ── No continue and no submit — stuck ──────────────────────
            # Try any visible button that looks like progress
            fallback_sels = [
                'button:has-text("Apply")',
                'button:has-text("Save")',
                'button:has-text("Confirm")',
                'button[type="submit"]',
            ]
            clicked = False
            for sel in fallback_sels:
                try:
                    el = await page.wait_for_selector(sel, timeout=1500, state="visible")
                    if el:
                        await self.browser_engine.click_human(page, sel)
                        await self._rand(2, 4)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                self.logger.warning("  step %d: stuck — no button found", step + 1)
                await self._screenshot(page, f"indeed_stuck_step{step}")
                return {"ready_to_submit": False, "steps": steps,
                        "error": f"Stuck at step {step + 1}"}

        return {"ready_to_submit": False, "error": "Max steps exceeded", "steps": steps}

    @staticmethod
    def _is_success_page(html_lower: str, url: str) -> bool:
        """Return True if the page looks like a post-submit confirmation."""
        markers = [
            "application has been submitted",
            "your application was sent",
            "you have applied",
            "application submitted",
            "successfully applied",
            "thank you for applying",
        ]
        for m in markers:
            if m in html_lower:
                return True
        if "/post-apply" in url or "applied=true" in url:
            return True
        return False

    # ── resume upload ──────────────────────────────────────────────────

    async def _upload_resume(self, page, resume_path: str) -> bool:
        """Upload resume via <input type='file'> or file-chooser dialog."""
        abs_path = os.path.abspath(resume_path)
        if not os.path.isfile(abs_path):
            self.logger.warning("Resume file not found: %s", abs_path)
            return False

        # Strategy 1: direct <input type="file">
        for sel in _SEL["resume_upload"]:
            try:
                inp = await page.wait_for_selector(sel, timeout=2000)
                if inp:
                    await inp.set_input_files(abs_path)
                    self.logger.info("    resume uploaded via input[type=file]")
                    await self._rand(1.5, 3.0)
                    return True
            except Exception:
                continue

        # Strategy 2: click upload button → file chooser dialog
        upload_btn_sels = [
            'button:has-text("Upload resume")',
            'button:has-text("Upload")',
            'label:has-text("Upload resume")',
            'span:has-text("Upload resume")',
            '[data-testid="upload-resume"]',
        ]
        for sel in upload_btn_sels:
            try:
                btn = await page.wait_for_selector(sel, timeout=1500, state="visible")
                if btn:
                    async with page.expect_file_chooser(timeout=5000) as fc_info:
                        await btn.click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(abs_path)
                    self.logger.info("    resume uploaded via file chooser")
                    await self._rand(1.5, 3.0)
                    return True
            except Exception:
                continue

        # Strategy 3: "use resume on file" — skip upload
        use_existing_sels = [
            'button:has-text("Use last uploaded")',
            'div:has-text("Resume on Indeed")',
            ':has-text("resume on file")',
        ]
        for sel in use_existing_sels:
            try:
                el = await page.wait_for_selector(sel, timeout=1500)
                if el:
                    self.logger.info("    Indeed resume on file — skipping upload")
                    return True
            except Exception:
                continue

        return False

    # ── form field handling ────────────────────────────────────────────

    async def _handle_form_fields(
        self, page, job: Dict, cover_letter: Optional[str] = None,
    ) -> int:
        """Detect and fill all form fields on the current step."""
        handled = 0

        # ── Standard text/email/tel/number inputs ──────────────────────
        input_sels = [
            'input[type="text"]:visible',
            'input[type="email"]:visible',
            'input[type="tel"]:visible',
            'input[type="number"]:visible',
            'input:not([type="file"]):not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):visible',
        ]
        for sel in input_sels:
            try:
                inputs = await page.query_selector_all(sel)
                for inp in inputs:
                    ok = await self._handle_single_input(page, inp, job)
                    if ok:
                        handled += 1
            except Exception:
                continue

        # ── Textareas (cover letter, "why apply", etc.) ────────────────
        try:
            textareas = await page.query_selector_all("textarea:visible")
            for ta in textareas:
                ok = await self._handle_textarea(page, ta, job, cover_letter)
                if ok:
                    handled += 1
        except Exception:
            pass

        # ── Selects (dropdowns) ────────────────────────────────────────
        try:
            selects = await page.query_selector_all("select:visible")
            for sel_el in selects:
                ok = await self._handle_select(page, sel_el, job)
                if ok:
                    handled += 1
        except Exception:
            pass

        # ── Radio buttons ──────────────────────────────────────────────
        try:
            radio_groups = await page.query_selector_all(
                'fieldset:visible, div[role="radiogroup"]:visible, div[role="group"]:visible'
            )
            for rg in radio_groups:
                ok = await self._handle_radio_group(page, rg, job)
                if ok:
                    handled += 1
        except Exception:
            pass

        # ── Checkboxes (consent / terms) ───────────────────────────────
        try:
            checkboxes = await page.query_selector_all(
                'input[type="checkbox"]:visible'
            )
            for cb in checkboxes:
                ok = await self._handle_checkbox(page, cb)
                if ok:
                    handled += 1
        except Exception:
            pass

        return handled

    async def _handle_single_input(self, page, inp, job: Dict) -> bool:
        """Fill a single <input> based on its label / name / type."""
        try:
            # Skip if already filled
            val = await inp.input_value()
            if val and len(val.strip()) > 0:
                return False

            name = (await self._attr(inp, "name")).lower()
            ph = (await self._attr(inp, "placeholder")).lower()
            label_text = await self._get_label_for(page, inp)
            label_lower = label_text.lower()
            inp_type = (await self._attr(inp, "type")).lower() or "text"
            aria = (await self._attr(inp, "aria-label")).lower()
            combined = f"{name} {ph} {label_lower} {aria}"

            answer = self._resolve_field_answer(combined, inp_type, job)
            if not answer:
                return False

            css = await self._css_for(inp)
            await self.browser_engine.type_human(page, css, answer)
            await self._rand(0.5, 1.2)
            return True
        except Exception:
            return False

    async def _handle_textarea(self, page, ta, job: Dict,
                                cover_letter: Optional[str]) -> bool:
        """Fill a <textarea>."""
        try:
            val = await ta.input_value()
            if val and len(val.strip()) > 5:
                return False

            label_text = await self._get_label_for(page, ta)
            label_lower = label_text.lower()
            name = (await self._attr(ta, "name")).lower()
            ph = (await self._attr(ta, "placeholder")).lower()
            combined = f"{name} {ph} {label_lower}"

            answer = ""

            # Cover letter field?
            if any(kw in combined for kw in
                   ["cover letter", "cover_letter", "coverletter",
                    "why are you interested", "why apply", "message to hiring",
                    "additional information"]):
                answer = cover_letter or self._default_cover(job)
            # General text question
            elif get_answer:
                answer = get_answer(label_text) or get_answer(combined)

            if not answer:
                answer = self._fallback_text_answer(combined, job)

            if not answer:
                return False

            css = await self._css_for(ta)
            await ta.click()
            await self._rand(0.3, 0.8)
            await self.browser_engine.type_human(page, css, answer[:2000])
            return True
        except Exception:
            return False

    async def _handle_select(self, page, sel_el, job: Dict) -> bool:
        """Handle a <select> dropdown."""
        try:
            label_text = await self._get_label_for(page, sel_el)
            label_lower = label_text.lower()
            name = (await self._attr(sel_el, "name")).lower()
            combined = f"{name} {label_lower}"

            # Get all options
            options = await sel_el.query_selector_all("option")
            opt_values: List[Tuple[str, str]] = []  # (value, text)
            for opt in options:
                ov = await self._attr(opt, "value")
                ot = await self._txt(opt)
                if ov or ot:
                    opt_values.append((ov, ot))

            if not opt_values:
                return False

            desired = self._resolve_dropdown_answer(combined, opt_values, job)
            if desired:
                await sel_el.select_option(value=desired)
                await self._rand(0.3, 0.8)
                return True
            return False
        except Exception:
            return False

    async def _handle_radio_group(self, page, group, job: Dict) -> bool:
        """Handle a radio-button group — pick the best option."""
        try:
            label_text = await self._txt(group)
            label_lower = label_text.lower()

            radios = await group.query_selector_all('input[type="radio"]')
            if not radios:
                return False

            # Check if any is already selected
            for r in radios:
                checked = await r.get_attribute("checked")
                if checked is not None:
                    return False  # already answered

            # Collect options
            radio_opts: List[Tuple[Any, str]] = []
            for r in radios:
                r_label = await self._get_label_for(page, r)
                if not r_label:
                    parent = await r.evaluate_handle("e => e.parentElement")
                    r_label = await self._txt(parent)
                radio_opts.append((r, r_label.lower()))

            # Decide which to pick
            pick = self._resolve_radio_answer(label_lower, radio_opts, job)
            if pick is not None:
                css = await self._css_for(pick)
                await self.browser_engine.click_human(page, css)
                await self._rand(0.3, 0.8)
                return True
            return False
        except Exception:
            return False

    async def _handle_checkbox(self, page, cb) -> bool:
        """Auto-check consent / terms checkboxes."""
        try:
            is_checked = await cb.is_checked()
            if is_checked:
                return False

            label = await self._get_label_for(page, cb)
            label_lower = label.lower()

            # Auto-check typical consent fields
            auto_check_keywords = [
                "agree", "consent", "acknowledge", "terms", "privacy",
                "confirm", "certify", "authorize", "i have read",
                "accept", "understand",
            ]
            for kw in auto_check_keywords:
                if kw in label_lower:
                    css = await self._css_for(cb)
                    await self.browser_engine.click_human(page, css)
                    await self._rand(0.2, 0.5)
                    return True
            return False
        except Exception:
            return False

    # ── label / question detection ─────────────────────────────────────

    async def _get_label_for(self, page, el) -> str:
        """Find the label associated with a form element."""
        try:
            # 1. <label for="id">
            eid = await self._attr(el, "id")
            if eid:
                lbl = await page.query_selector(f'label[for="{eid}"]')
                if lbl:
                    return await self._txt(lbl)

            # 2. aria-label
            aria = await self._attr(el, "aria-label")
            if aria:
                return aria

            # 3. aria-labelledby
            lblby = await self._attr(el, "aria-labelledby")
            if lblby:
                ref = await page.query_selector(f"#{lblby}")
                if ref:
                    return await self._txt(ref)

            # 4. ancestor label
            ancestor_label = await el.evaluate_handle(
                "e => e.closest('label') || e.parentElement?.querySelector('label')"
            )
            t = await self._txt(ancestor_label)
            if t:
                return t

            # 5. placeholder
            ph = await self._attr(el, "placeholder")
            if ph:
                return ph

            # 6. preceding sibling text
            prev_text = await el.evaluate(
                """e => {
                    let p = e.previousElementSibling;
                    if (p) return p.textContent || '';
                    let par = e.parentElement;
                    if (par) {
                        let label = par.querySelector('label, span, p, div');
                        if (label && label !== e) return label.textContent || '';
                    }
                    return '';
                }"""
            )
            if prev_text and isinstance(prev_text, str):
                return prev_text.strip()

        except Exception:
            pass
        return ""

    # ── answer resolution ──────────────────────────────────────────────

    def _resolve_field_answer(self, combined: str, inp_type: str, job: Dict) -> str:
        """Return the answer for a text input based on label context."""
        c = combined

        # ── Direct field matches ───────────────────────────────────────
        if any(k in c for k in ["first name", "firstname", "first_name", "given name"]):
            return USER_PROFILE.get("name", "Piyush Kashyap").split()[0]
        if any(k in c for k in ["last name", "lastname", "last_name", "surname", "family name"]):
            parts = USER_PROFILE.get("name", "Piyush Kashyap").split()
            return parts[-1] if len(parts) > 1 else parts[0]
        if any(k in c for k in ["full name", "fullname", "your name", "name"]) and "company" not in c:
            return USER_PROFILE.get("name", "Piyush Kashyap")
        if any(k in c for k in ["email", "e-mail"]) and "company" not in c:
            return USER_PROFILE.get("email", "piyushkashyap3247@gmail.com")
        if any(k in c for k in ["phone", "mobile", "contact number", "telephone"]):
            return USER_PROFILE.get("phone", "+91 73107 03247")
        if any(k in c for k in ["city", "location", "address"]) and "company" not in c:
            return USER_PROFILE.get("location", "Rishikesh, Uttarakhand")
        if any(k in c for k in ["linkedin"]):
            return USER_PROFILE.get("linkedin_url", "https://linkedin.com/in/piyush-kashyap731")
        if any(k in c for k in ["github", "portfolio", "website"]):
            return USER_PROFILE.get("github_url", "https://github.com/Piyush731")
        if any(k in c for k in ["current ctc", "current salary", "current compensation"]):
            return "3.7 LPA"
        if any(k in c for k in ["expected ctc", "expected salary", "desired salary",
                                  "salary expectation"]):
            if get_salary_answer:
                return get_salary_answer(job)
            return "6-10 LPA (negotiable)"
        if any(k in c for k in ["notice period", "notice_period"]):
            return "15 days"
        if any(k in c for k in ["experience", "total experience", "years of experience",
                                  "work experience"]) and "relevant" not in c:
            return str(USER_PROFILE.get("experience_years", 1))
        if any(k in c for k in ["relevant experience"]):
            return str(USER_PROFILE.get("experience_years", 1))
        if any(k in c for k in ["current company", "current employer", "current organization"]):
            return "Site Guru Pvt Ltd"
        if any(k in c for k in ["current title", "current role", "current designation",
                                  "job title"]) and "desired" not in c:
            return USER_PROFILE.get("current_title", "Full Stack Developer L1")
        if any(k in c for k in ["pincode", "zip code", "postal code", "zip"]):
            return "249201"
        if any(k in c for k in ["date of birth", "dob", "birth date"]):
            return "2003-07-31"

        # ── profile.answers fallback ───────────────────────────────────
        if get_answer:
            ans = get_answer(combined)
            if ans:
                return str(ans)
        if get_standard:
            # Try matching known field names
            for fname in ["name", "email", "phone", "location"]:
                if fname in c:
                    val = get_standard(fname)
                    if val:
                        return str(val)

        return ""

    def _resolve_dropdown_answer(
        self, combined: str, opts: List[Tuple[str, str]], job: Dict
    ) -> Optional[str]:
        """Fuzzy-match the best dropdown option for a question."""
        c = combined

        # Map question keyword → preferred option keywords
        preference_map: Dict[str, List[str]] = {
            "experience": ["0-1", "1-2", "0", "1", "fresher", "entry"],
            "notice": ["immediate", "15", "less than", "0-15", "1-15", "within"],
            "relocat": ["yes", "true", "willing"],
            "work auth": ["yes", "authorized", "citizen"],
            "sponsor": ["no", "do not"],
            "gender": ["prefer not", "male"],
            "job type": ["full-time", "full time", "permanent"],
            "shift": ["day", "general", "any"],
            "education": ["bachelor", "b.tech", "btech", "engineering"],
            "salary": ["negotiable"],
        }

        for keyword, prefs in preference_map.items():
            if keyword in c:
                for pref in prefs:
                    for val, text in opts:
                        if pref in text.lower() or pref in val.lower():
                            return val
                break

        # Fallback: pick first non-empty, non-placeholder option
        for val, text in opts:
            tl = text.lower().strip()
            if tl and tl not in ("select", "choose", "--", "select one",
                                  "please select", "", "choose one"):
                return val

        return None

    def _resolve_radio_answer(
        self, label_lower: str, opts: List[Tuple[Any, str]], job: Dict
    ) -> Any:
        """Pick the best radio button for a question."""
        # Positive-preference keywords per question type
        positive_map: Dict[str, List[str]] = {
            "authorized": ["yes"],
            "legally": ["yes"],
            "sponsorship": ["no"],
            "relocat": ["yes"],
            "willing to travel": ["yes"],
            "background check": ["yes"],
            "drug test": ["yes"],
            "18 years": ["yes"],
            "shift": ["yes"],
            "currently employed": ["yes"],
            "gender": ["prefer not", "male"],
            "veteran": ["no", "prefer not"],
            "disability": ["no", "prefer not"],
            "race": ["prefer not"],
            "ethnicity": ["prefer not"],
            "hear about": ["job board", "indeed", "online"],
        }

        for keyword, prefs in positive_map.items():
            if keyword in label_lower:
                for pref in prefs:
                    for el, text in opts:
                        if pref in text:
                            return el
                break

        # Default: pick "Yes" if available, else first option
        for el, text in opts:
            if text.strip().lower() == "yes":
                return el
        return opts[0][0] if opts else None

    def _fallback_text_answer(self, combined: str, job: Dict) -> str:
        """Last-resort generic answer for unmatched text fields."""
        c = combined
        if any(kw in c for kw in ["why", "interest", "motivation", "reason"]):
            company = job.get("company", "your company")
            title = job.get("title", "this role")
            return (
                f"I am excited about the {title} role at {company}. "
                f"With hands-on experience building 10+ production applications "
                f"as a sole developer, I bring strong full-stack skills and "
                f"ownership mentality. I am eager to contribute and grow."
            )
        if any(kw in c for kw in ["strength", "skill", "qualification"]):
            return (
                "Full-stack development (Vue.js, Node.js, Java Spring Boot), "
                "end-to-end project ownership, 10+ production apps, "
                "database design, REST APIs, WebSocket integrations."
            )
        return ""

    def _default_cover(self, job: Dict) -> str:
        """Short default cover letter when none is provided."""
        company = job.get("company", "your company")
        title = job.get("title", "the open position")
        return (
            f"Dear Hiring Team,\n\n"
            f"I am writing to express my strong interest in the {title} role at {company}. "
            f"As a Full Stack Developer with experience building 10+ production applications "
            f"independently — spanning fintech, ERP, CRM, and edtech domains — I am confident "
            f"I can contribute meaningfully to your team.\n\n"
            f"Key highlights:\n"
            f"• Sole developer on multi-tenant ERP with 57+ DB tables\n"
            f"• Real-time WebSocket integrations, third-party API work (META, Razorpay, RTO)\n"
            f"• Published app on Google Play Store\n"
            f"• Proficient in JavaScript, Java, Python, Vue.js, Node.js, Spring Boot\n\n"
            f"I am available to join within 15 days and am eager to discuss how my background "
            f"aligns with your needs.\n\n"
            f"Best regards,\nPiyush Kashyap\n"
            f"+91 73107 03247 | piyushkashyap3247@gmail.com"
        )

    # ═══════════════════════════════════════════════════════════════════
    #  SUBMIT APPLICATION
    # ═══════════════════════════════════════════════════════════════════

    async def submit_application(self, prepared: Dict) -> Dict:
        """
        Click the final Submit button after Telegram approval.

        Parameters
        ----------
        prepared : dict
            The dict returned by ``prepare_application``.

        Returns
        -------
        dict
            ``{success: bool, job_id, method, applied_at?, error?}``
        """
        jid = str(prepared.get("job_id", "unknown"))
        self.logger.info("═══ Submitting application %s ═══", jid)

        state = self._prepared.pop(jid, None)
        if not state:
            return {"success": False, "job_id": jid,
                    "error": "No prepared state found — call prepare_application first"}

        apply_page = state.get("apply_page")
        if not apply_page:
            return {"success": False, "job_id": jid, "error": "Apply page lost"}

        try:
            # Find submit button
            submit_btn = await self._el(apply_page, "submit_button", timeout=8000)
            if not submit_btn:
                await self._screenshot(apply_page, f"indeed_no_submit_{jid}")
                return {"success": False, "job_id": jid,
                        "error": "Submit button not found on page"}

            # Click submit
            css = await self._css_for(submit_btn)
            await self.browser_engine.click_human(apply_page, css)
            await self._rand(3, 6)

            # Verify submission
            try:
                html = (await apply_page.content()).lower()
            except Exception:
                html = ""

            if self._is_success_page(html, apply_page.url):
                now = datetime.now().isoformat()
                self._last_apply_ts = datetime.now()
                self.increment_count()
                self.logger.info("  ✅ Application SUBMITTED for %s", jid)
                await self._close_extra_tabs()
                return {
                    "success": True,
                    "job_id": jid,
                    "method": "indeed_apply",
                    "applied_at": now,
                }

            # Maybe redirect / page changed — check once more
            await self._rand(2, 4)
            try:
                html2 = (await apply_page.content()).lower()
            except Exception:
                html2 = ""
            if self._is_success_page(html2, apply_page.url):
                now = datetime.now().isoformat()
                self._last_apply_ts = datetime.now()
                self.increment_count()
                await self._close_extra_tabs()
                return {
                    "success": True,
                    "job_id": jid,
                    "method": "indeed_apply",
                    "applied_at": now,
                }

            # Not confirmed — screenshot for debug
            await self._screenshot(apply_page, f"indeed_submit_unclear_{jid}")
            self.logger.warning("  ⚠ Submit clicked but confirmation unclear")
            # Optimistic — count it
            self._last_apply_ts = datetime.now()
            self.increment_count()
            await self._close_extra_tabs()
            return {
                "success": True,
                "job_id": jid,
                "method": "indeed_apply",
                "applied_at": datetime.now().isoformat(),
                "note": "Submit clicked, confirmation unclear",
            }

        except Exception as exc:
            self.logger.error("submit_application error: %s", exc)
            self.db.save_error("indeed.submit_application", type(exc).__name__,
                               str(exc), tb_module.format_exc())
            await self._close_extra_tabs()
            return {"success": False, "job_id": jid, "error": str(exc)}

    # ═══════════════════════════════════════════════════════════════════
    #  CHECK STATUS
    # ═══════════════════════════════════════════════════════════════════

    async def check_status(self, application_id: int = None) -> str:
        """
        Check application status on Indeed's "My Jobs" page.

        Returns one of: submitted, viewed, interview, rejected, unknown.
        """
        self.logger.info("Checking Indeed application status")
        if not self.page:
            return "unknown"

        try:
            await self.page.goto(
                f"{_BASE_URL}/myjobs?advn=1",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await self._rand(2, 4)
            await self._dismiss_popups(self.page)

            # Look for status indicators on applied jobs
            status_indicators = {
                "viewed": ["employer viewed", "viewed by employer", "application viewed"],
                "interview": ["interview", "scheduled"],
                "rejected": ["not selected", "position filled", "not moving forward"],
            }

            html = ""
            try:
                html = (await self.page.content()).lower()
            except Exception:
                pass

            for status, keywords in status_indicators.items():
                for kw in keywords:
                    if kw in html:
                        return status

            return "submitted"

        except Exception as exc:
            self.logger.error("check_status error: %s", exc)
            return "unknown"

    # ═══════════════════════════════════════════════════════════════════
    #  CAPTCHA + OTP
    # ═══════════════════════════════════════════════════════════════════

    async def detect_captcha(self, page) -> Optional[str]:
        """Return captcha type if detected, else None."""
        try:
            url = page.url.lower()
            html = ""
            try:
                html = (await page.content()).lower()
            except Exception:
                pass

            # URL-based detection
            if "captcha" in url or "challenge" in url:
                return "page_captcha"

            # reCAPTCHA iframe
            for sel in _SEL.get("captcha_frame", []):
                try:
                    el = await page.wait_for_selector(sel, timeout=1500)
                    if el:
                        return "recaptcha"
                except Exception:
                    continue

            # Text CAPTCHA
            text_captcha_sels = [
                'input[name*="captcha"]',
                'input[id*="captcha"]',
                'img[src*="captcha"]',
                '#captcha',
            ]
            for sel in text_captcha_sels:
                try:
                    el = await page.wait_for_selector(sel, timeout=1000)
                    if el:
                        return "text"
                except Exception:
                    continue

            # Cloudflare / bot detection
            if any(kw in html for kw in [
                "verify you are human",
                "just a moment",
                "checking your browser",
                "unusual traffic",
                "are you a robot",
            ]):
                return "bot_detection"

        except Exception:
            pass
        return None

    async def handle_captcha(self, page, notifier=None) -> bool:
        """
        Handle CAPTCHA:
        - text → screenshot + Telegram → user provides answer
        - recaptcha → retry clicks, if stuck → screenshot + Telegram
        - bot detection → wait + retry
        """
        cap_type = await self.detect_captcha(page)
        if not cap_type:
            return True  # no captcha

        self.logger.warning("Handling CAPTCHA type: %s", cap_type)
        notifier = notifier or self.notifier

        if cap_type == "bot_detection":
            # Wait and hope it resolves
            for attempt in range(3):
                await self._rand(5, 10)
                new_cap = await self.detect_captcha(page)
                if not new_cap:
                    return True
            # Still stuck — ask user
            if notifier:
                ss = await self._screenshot(page, "indeed_bot_detection")
                try:
                    notifier.send_platform_issue(
                        "indeed",
                        "🤖 *Indeed Bot Detection*\n\n"
                        "Please solve it in the open browser window.\n"
                        "The agent will continue after detection clears.",
                    )
                except Exception:
                    pass
            # Wait up to 3 minutes
            for _ in range(36):
                await asyncio.sleep(5)
                new_cap = await self.detect_captcha(page)
                if not new_cap:
                    return True
            return False

        if cap_type == "recaptcha":
            # Try clicking the checkbox
            try:
                frames = page.frames
                for frame in frames:
                    try:
                        checkbox = await frame.wait_for_selector(
                            ".recaptcha-checkbox-border", timeout=3000
                        )
                        if checkbox:
                            await checkbox.click()
                            await self._rand(3, 6)
                            new_cap = await self.detect_captcha(page)
                            if not new_cap:
                                return True
                    except Exception:
                        continue
            except Exception:
                pass

            # Couldn't auto-solve — ask user
            if notifier:
                ss = await self._screenshot(page, "indeed_recaptcha")
                try:
                    notifier.send_platform_issue(
                        "indeed",
                        "🧩 *reCAPTCHA on Indeed*\n\n"
                        "Please solve it in the open browser window.\n"
                        "⏱ Waiting up to 3 minutes.",
                    )
                except Exception:
                    pass
            for _ in range(36):
                await asyncio.sleep(5)
                new_cap = await self.detect_captcha(page)
                if not new_cap:
                    return True
            return False

        if cap_type == "text":
            if notifier:
                ss = await self._screenshot(page, "indeed_text_captcha")
                try:
                    answer = notifier.send_captcha_challenge(ss, "text")
                    if answer:
                        # Find captcha input and enter
                        inp_sels = [
                            'input[name*="captcha"]',
                            'input[id*="captcha"]',
                            '#captchaInput',
                            'input[type="text"]:visible',
                        ]
                        for sel in inp_sels:
                            try:
                                inp = await page.wait_for_selector(sel, timeout=2000)
                                if inp:
                                    await self.browser_engine.type_human(page, sel, answer)
                                    # Submit
                                    sub = await page.query_selector(
                                        'button[type="submit"], input[type="submit"]'
                                    )
                                    if sub:
                                        await sub.click()
                                    await self._rand(2, 4)
                                    new_cap = await self.detect_captcha(page)
                                    return new_cap is None
                            except Exception:
                                continue
                except Exception:
                    pass
            return False

        # page_captcha or unknown — wait for user
        if notifier:
            await self._screenshot(page, "indeed_captcha_unknown")
            try:
                notifier.send_platform_issue(
                    "indeed",
                    f"⚠️ *CAPTCHA ({cap_type}) on Indeed*\n"
                    f"Please solve in browser. Waiting 3 min.",
                )
            except Exception:
                pass
        for _ in range(36):
            await asyncio.sleep(5)
            if not await self.detect_captcha(page):
                return True
        return False

    async def detect_otp_page(self, page) -> bool:
        """Check if current page is asking for OTP / verification code."""
        try:
            html = ""
            try:
                html = (await page.content()).lower()
            except Exception:
                pass
            otp_keywords = [
                "verification code",
                "enter code",
                "enter the code",
                "one-time",
                "otp",
                "sent to your email",
                "sent a code",
                "verify your identity",
            ]
            for kw in otp_keywords:
                if kw in html:
                    return True
        except Exception:
            pass
        return False

    async def handle_otp(self, page, notifier=None) -> bool:
        """Ask user for OTP via Telegram and enter it."""
        notifier = notifier or self.notifier
        if not notifier:
            self.logger.error("OTP needed but no notifier configured")
            return False

        self.logger.info("OTP page detected — requesting from user")
        try:
            otp = notifier.send_otp_request("indeed")
            if not otp:
                self.logger.error("OTP not received in time")
                return False

            # Find OTP input
            otp_sels = [
                'input[name*="code"]',
                'input[name*="otp"]',
                'input[name*="verification"]',
                'input[type="tel"]',
                'input[type="number"]',
                'input[inputmode="numeric"]',
                'input[type="text"]:visible',
            ]
            for sel in otp_sels:
                try:
                    inp = await page.wait_for_selector(sel, timeout=2000, state="visible")
                    if inp:
                        await self.browser_engine.type_human(page, sel, otp)
                        await self._rand(0.5, 1.0)
                        # Submit
                        sub_sels = [
                            'button[type="submit"]',
                            'button:has-text("Verify")',
                            'button:has-text("Continue")',
                            'button:has-text("Submit")',
                            'input[type="submit"]',
                        ]
                        for ss in sub_sels:
                            try:
                                btn = await page.wait_for_selector(ss, timeout=2000)
                                if btn:
                                    await btn.click()
                                    break
                            except Exception:
                                continue
                        await self._rand(3, 5)
                        # Check if OTP page is gone
                        if not await self.detect_otp_page(page):
                            self.logger.info("✅ OTP accepted")
                            return True
                except Exception:
                    continue

            self.logger.error("Could not enter OTP")
            return False
        except Exception as exc:
            self.logger.error("OTP handling error: %s", exc)
            return False

    # ═══════════════════════════════════════════════════════════════════
    #  RATE LIMITING
    # ═══════════════════════════════════════════════════════════════════

    def _can_apply_now(self) -> bool:
        """Check daily cap + min gap between applications."""
        if not self.can_apply():
            self.logger.info("Daily limit reached (%d/%d)", self.get_daily_count(),
                             self.max_daily)
            return False

        if self.is_in_cooldown():
            self.logger.info("Platform in cooldown")
            return False

        if self._last_apply_ts:
            elapsed = (datetime.now() - self._last_apply_ts).total_seconds()
            if elapsed < self._gap_min:
                wait = self._gap_min - elapsed
                self.logger.info("Rate limit: wait %.0fs more", wait)
                return False

        return True

    async def _enforce_gap(self):
        """Sleep the remaining gap time if needed."""
        if self._last_apply_ts:
            elapsed = (datetime.now() - self._last_apply_ts).total_seconds()
            needed = random.uniform(self._gap_min, self._gap_max)
            if elapsed < needed:
                wait = needed - elapsed
                self.logger.info("Enforcing %.0fs gap between applications", wait)
                await asyncio.sleep(wait)

    # ═══════════════════════════════════════════════════════════════════
    #  CLEANUP
    # ═══════════════════════════════════════════════════════════════════

    async def close(self):
        """Save cookies and close browser."""
        try:
            if self.page:
                await self.browser_engine.save_cookies("indeed")
                self.logger.info("Cookies saved for indeed")
        except Exception as exc:
            self.logger.warning("Error saving cookies: %s", exc)

        try:
            await self.browser_engine.close("indeed")
        except Exception:
            pass

        self.page = None
        self.logger.info("IndeedPlatform closed")


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN — standalone test
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Quick smoke test:
      python platforms/indeed.py

    Tests login → search → fetch details for the first result.
    Requires .env with GEMINI / TELEGRAM / etc. and a working browser.
    """
    import sys

    async def _test():
        from core.browser import BrowserEngine

        # Optional notifier (won't crash if not built yet)
        notifier = None
        try:
            from tracking.notifications import JobNotifier
            notifier = JobNotifier()
        except Exception:
            print("[WARN] Notifier not available — running without Telegram")

        engine = BrowserEngine()
        indeed = IndeedPlatform(engine, notifier)

        print("\n══════════════════════════════════════════")
        print("  Indeed Platform — Smoke Test")
        print("══════════════════════════════════════════\n")

        # ── Login ──────────────────────────────────────────────────────
        print("[1/3] Logging in …")
        ok = await indeed.login()
        if not ok:
            print("❌ Login failed. Exiting.")
            await indeed.close()
            return

        # ── Search ─────────────────────────────────────────────────────
        print("\n[2/3] Searching …")
        jobs = await indeed.search_jobs(
            queries=["full stack developer"],
            filters={"locations": ["Bangalore"], "fromage": 7},
        )
        print(f"  → Found {len(jobs)} unique jobs")

        if jobs:
            for j in jobs[:5]:
                print(f"     • {j['title']} @ {j['company']} — {j['location']}")

            # ── Details ────────────────────────────────────────────────
            print(f"\n[3/3] Fetching details for first job …")
            details = await indeed.get_job_details(jobs[0]["url"])
            if details:
                print(f"  Title   : {details.get('title')}")
                print(f"  Company : {details.get('company')}")
                print(f"  Location: {details.get('location')}")
                print(f"  Skills  : {', '.join(details.get('skills', []))}")
                print(f"  Apply?  : {details.get('has_indeed_apply')}")
                desc = details.get("description", "")
                print(f"  JD      : {desc[:200]}…" if len(desc) > 200 else f"  JD: {desc}")
            else:
                print("  ⚠ Could not fetch details")
        else:
            print("  No jobs found — try different queries / locations")

        # ── Cleanup ────────────────────────────────────────────────────
        print("\nCleaning up …")
        await indeed.close()
        print("\n✅ Test complete.\n")

    try:
        asyncio.run(_test())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)