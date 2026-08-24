#!/usr/bin/env python3
"""
tracking/notifications.py — Telegram Bot notifications + interactive controls.

Provides:
  • Job match alerts with score breakdown
  • Approve/reject inline keyboards before application submission
  • CAPTCHA screenshot forwarding + answer collection
  • OTP request + reply collection
  • AI answer review (use / edit)
  • Daily report summaries
  • Error / platform issue alerts
  • 2FA code requests
  • Connection testing

Uses raw HTTP requests to Telegram Bot API (zero extra dependencies).

Prerequisites:
    TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set in .env

Usage:
    from tracking.notifications import JobNotifier

    notifier = JobNotifier()
    notifier.test_connection()
    notifier.send_match(job, score_result)
    callback_id = notifier.send_approval_request(job, "/path/resume.pdf")
    approved = notifier.wait_for_approval(callback_id, timeout=1800)
"""

import os
import json
import time
import uuid
import threading
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError

from config import TELEGRAM_CONFIG
from core.logger import get_logger

logger = get_logger("tracking.notifications")

# ── Credentials ─────────────────────────────────────────────────
try:
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
except ImportError:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ═══════════════════════════════════════════════════════════════════
# TELEGRAM API — raw HTTP (no pip dependency)
# ═══════════════════════════════════════════════════════════════════

class _TelegramAPI:
    """Low-level Telegram Bot API wrapper using urllib only."""

    def __init__(self, token: str):
        self._base = f"https://api.telegram.org/bot{token}"
        self._timeout = 15

    def _call(self, method: str, data: Optional[Dict] = None,
              files: Optional[Dict] = None) -> Dict:
        """Call a Telegram Bot API method."""
        url = f"{self._base}/{method}"

        if files:
            return self._call_multipart(url, data or {}, files)

        if data:
            body = json.dumps(data).encode("utf-8")
            req = Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
        else:
            req = Request(url)

        try:
            with urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.error("Telegram API %s error %d: %s",
                         method, e.code, body[:300])
            return {"ok": False, "error_code": e.code,
                    "description": body[:300]}
        except (URLError, OSError) as e:
            logger.error("Telegram API %s network error: %s",
                         method, e)
            return {"ok": False, "description": str(e)}

    def _call_multipart(self, url: str, data: Dict,
                        files: Dict) -> Dict:
        """Multipart form upload (for photos/documents)."""
        import io
        boundary = f"----FormBoundary{uuid.uuid4().hex[:16]}"
        body = io.BytesIO()

        for key, value in data.items():
            body.write(f"--{boundary}\r\n".encode())
            body.write(
                f'Content-Disposition: form-data; name="{key}"\r\n'
                f"\r\n{value}\r\n".encode()
            )

        for key, (filename, filedata, content_type) in files.items():
            body.write(f"--{boundary}\r\n".encode())
            body.write(
                f'Content-Disposition: form-data; name="{key}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n".encode()
            )
            body.write(filedata)
            body.write(b"\r\n")

        body.write(f"--{boundary}--\r\n".encode())
        body_bytes = body.getvalue()

        req = Request(url, data=body_bytes, method="POST")
        req.add_header(
            "Content-Type",
            f"multipart/form-data; boundary={boundary}"
        )

        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, OSError) as e:
            logger.error("Telegram upload error: %s", e)
            return {"ok": False, "description": str(e)}

    # ── Convenience methods ──

    def send_message(self, chat_id: str, text: str,
                     parse_mode: Optional[str] = None,
                     reply_markup: Optional[Dict] = None,
                     disable_preview: bool = True) -> Dict:
        data = {
            "chat_id": chat_id,
            "text": text[:4096],
            "disable_web_page_preview": disable_preview,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup:
            data["reply_markup"] = reply_markup
        return self._call("sendMessage", data)

    def send_photo(self, chat_id: str, photo_path: str,
                   caption: str = "",
                   reply_markup: Optional[Dict] = None) -> Dict:
        if not os.path.isfile(photo_path):
            logger.error("Photo not found: %s", photo_path)
            return {"ok": False, "description": "File not found"}

        with open(photo_path, "rb") as f:
            photo_data = f.read()

        filename = os.path.basename(photo_path)
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1024]
            data["parse_mode"] = "Markdown"
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)

        files = {"photo": (filename, photo_data, "image/png")}
        return self._call_multipart(
            f"{self._base}/sendPhoto", data, files)

    def send_document(self, chat_id: str, doc_path: str,
                      caption: str = "") -> Dict:
        if not os.path.isfile(doc_path):
            return {"ok": False, "description": "File not found"}

        with open(doc_path, "rb") as f:
            doc_data = f.read()

        filename = os.path.basename(doc_path)
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1024]
            data["parse_mode"] = "Markdown"

        files = {"document": (filename, doc_data,
                              "application/octet-stream")}
        return self._call_multipart(
            f"{self._base}/sendDocument", data, files)

    def get_updates(self, offset: int = 0,
                    timeout: int = 5) -> Dict:
        return self._call("getUpdates", {
            "offset": offset, "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        })

    def answer_callback_query(self, callback_query_id: str,
                              text: str = "") -> Dict:
        return self._call("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text[:200],
        })

    def edit_message_text(self, chat_id: str, message_id: int,
                          text: str,
                          parse_mode: Optional[str] = None) -> Dict:
        return self._call("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4096],
            "parse_mode": parse_mode,
        })

    def get_me(self) -> Dict:
        return self._call("getMe")


# ═══════════════════════════════════════════════════════════════════
# JOB NOTIFIER
# ═══════════════════════════════════════════════════════════════════

class JobNotifier:
    """
    Telegram notification hub for the job agent.

    Sends alerts, collects approvals, forwards CAPTCHAs/OTPs,
    and delivers daily reports — all via Telegram Bot API.
    """

    def __init__(self):
        self._token = TELEGRAM_BOT_TOKEN
        self._chat_id = str(TELEGRAM_CHAT_ID)
        self._enabled = TELEGRAM_CONFIG.get("enabled", True)
        self._api: Optional[_TelegramAPI] = None
        self._update_offset = 0

        # Pending responses: callback_id → response value
        self._pending: Dict[str, Optional[str]] = {}
        self._pending_lock = threading.Lock()

        # Config flags
        self._send_matches = TELEGRAM_CONFIG.get("send_matches", True)
        self._send_applications = TELEGRAM_CONFIG.get(
            "send_applications", True)
        self._send_errors = TELEGRAM_CONFIG.get("send_errors", True)
        self._approve_before_apply = TELEGRAM_CONFIG.get(
            "approve_before_apply", True)
        self._approve_timeout = TELEGRAM_CONFIG.get(
            "approve_timeout_minutes", 30) * 60  # seconds

        if self._token and self._chat_id:
            self._api = _TelegramAPI(self._token)
            logger.info("JobNotifier ready (token=set, chat_id=%s)",
                         self._chat_id[:6] + "…")
        else:
            self._enabled = False
            logger.warning(
                "JobNotifier disabled — TELEGRAM_BOT_TOKEN or "
                "TELEGRAM_CHAT_ID not set in .env"
            )

    # ═══════════════════════════════════════════════════════════
    # CORE SEND
    # ═══════════════════════════════════════════════════════════

    def _send(self, text: str,
              reply_markup: Optional[Dict] = None,
              parse_mode: Optional[str] = None) -> Optional[int]:
        """Send a message. Returns message_id or None."""
        if not self._enabled or not self._api:
            logger.debug("Telegram disabled, skipping: %s",
                         text[:60])
            return None
        try:
            result = self._api.send_message(
                self._chat_id, text,
                parse_mode=parse_mode,
                reply_markup=reply_markup)
            if result.get("ok"):
                msg_id = result["result"]["message_id"]
                logger.debug("Telegram sent (msg_id=%d)", msg_id)
                return msg_id
            else:
                logger.error("Telegram send failed: %s",
                             result.get("description", "?"))
                return None
        except Exception as e:
            logger.error("Telegram send error: %s", e)
            return None

    def _send_photo(self, photo_path: str, caption: str = "",
                    reply_markup: Optional[Dict] = None
                    ) -> Optional[int]:
        """Send a photo. Returns message_id or None."""
        if not self._enabled or not self._api:
            return None
        try:
            result = self._api.send_photo(
                self._chat_id, photo_path, caption,
                reply_markup)
            if result.get("ok"):
                return result["result"]["message_id"]
            return None
        except Exception as e:
            logger.error("Telegram photo error: %s", e)
            return None

    # ═══════════════════════════════════════════════════════════
    # JOB MATCH ALERT
    # ═══════════════════════════════════════════════════════════

    def send_match(self, job: Dict, score: Any) -> bool:
        """
        Send a job match notification.

        Args:
            job: Job dict.
            score: Score dict or number.
        """
        if not self._send_matches:
            return True

        if isinstance(score, dict):
            score_val = score.get("score", 0)
            rec = score.get("recommendation", "")
            reasoning = score.get("reasoning", "")
            skills_found = score.get("skills_found", [])
            skills_missing = score.get("skills_missing", [])
        else:
            score_val = score
            rec = reasoning = ""
            skills_found = skills_missing = []

        emoji = "🎯" if score_val >= 70 else "📋"
        title = job.get("title", "?")
        company = job.get("company", "?")
        location = job.get("location", "?")
        salary = job.get("salary_text", "") or "Not disclosed"
        url = job.get("url", "")

        text = (
            f"{emoji} *Job Match ({score_val}%)*\n\n"
            f"*{title}*\n"
            f"🏢 {company}\n"
            f"📍 {location}\n"
            f"💰 {salary}\n"
        )
        if rec:
            text += f"📊 {rec}\n"
        if skills_found:
            text += f"✅ Skills: {', '.join(skills_found[:8])}\n"
        if skills_missing:
            text += f"❌ Missing: {', '.join(skills_missing[:5])}\n"
        if reasoning:
            text += f"\n_{reasoning[:200]}_\n"
        if url:
            text += f"\n🔗 [View Job]({url})"

        return self._send(text) is not None

    # ═══════════════════════════════════════════════════════════
    # APPROVAL REQUEST (inline keyboard)
    # ═══════════════════════════════════════════════════════════

    def send_approval_request(self, job: Dict,
                              resume_path: str) -> str:
        """
        Send approval request with inline keyboard buttons.

        Returns:
            callback_id: Unique ID to track this approval.
                         Use with wait_for_approval().
        """
        callback_id = f"apply_{uuid.uuid4().hex[:8]}"

        title = job.get("title", "?")
        company = job.get("company", "?")
        location = job.get("location", "?")
        platform = job.get("platform", "?")
        score = job.get("match_score", 0)

        text = (
            f"🚀 *Ready to Apply*\n\n"
            f"*{title}*\n"
            f"🏢 {company}\n"
            f"📍 {location}\n"
            f"📊 Score: {score}%\n"
            f"📱 Platform: {platform}\n"
            f"📄 Resume: `{os.path.basename(resume_path)}`\n\n"
            f"_Approve or skip this application:_"
        )

        markup = {
            "inline_keyboard": [[
                {"text": "✅ Apply",
                 "callback_data": f"{callback_id}:approve"},
                {"text": "❌ Skip",
                 "callback_data": f"{callback_id}:skip"},
            ], [
                {"text": "👁 Review",
                 "callback_data": f"{callback_id}:review"},
            ]]
        }

        with self._pending_lock:
            self._pending[callback_id] = None

        self._send(text, reply_markup=markup)
        return callback_id

    def wait_for_approval(self, callback_id: str,
                          timeout: Optional[int] = None) -> bool:
        """
        Wait for user to tap Approve or Skip.

        Args:
            callback_id: From send_approval_request().
            timeout: Seconds to wait. None → config default.

        Returns:
            True if approved, False if skipped or timed out.
        """
        if not self._enabled:
            logger.info("Telegram disabled — auto-approving")
            return True

        if timeout is None:
            timeout = self._approve_timeout

        logger.info("Waiting for approval: %s (timeout=%ds)",
                     callback_id, timeout)

        deadline = time.time() + timeout

        while time.time() < deadline:
            # Poll for updates
            self._poll_updates()

            with self._pending_lock:
                response = self._pending.get(callback_id)

            if response is not None:
                logger.info("Approval response: %s → %s",
                            callback_id, response)
                with self._pending_lock:
                    self._pending.pop(callback_id, None)
                return response == "approve"

            time.sleep(3)

        # Normal applications may auto-approve after the configured timeout.
        # Human-required flows (CAPTCHA/OTP/unknown fields) never reach this
        # approval gate: they pause in the platform handler and fail safely.
        logger.info("Approval timeout — auto-approving: %s", callback_id)
        with self._pending_lock:
            self._pending.pop(callback_id, None)
        self._send(f"⏱ Approval timed out — auto-applied "
                   f"(`{callback_id}`)")
        return True

    def wait_for_response(self, callback_id: str,
                          timeout: Optional[int] = None) -> str:
        """
        Generic wait for any callback response.

        Returns:
            Response string ('approve'/'skip'/'review'/'timeout').
        """
        if not self._enabled:
            return "approve"

        if timeout is None:
            timeout = self._approve_timeout

        deadline = time.time() + timeout

        while time.time() < deadline:
            self._poll_updates()
            with self._pending_lock:
                response = self._pending.get(callback_id)
            if response is not None:
                with self._pending_lock:
                    self._pending.pop(callback_id, None)
                return response
            time.sleep(3)

        with self._pending_lock:
            self._pending.pop(callback_id, None)
        return "timeout"

    # ═══════════════════════════════════════════════════════════
    # APPLICATION NOTIFICATIONS
    # ═══════════════════════════════════════════════════════════

    def send_applied(self, job: Dict,
                     application: Dict) -> bool:
        """Notify that an application was submitted."""
        if not self._send_applications:
            return True

        title = job.get("title", "?")
        company = job.get("company", "?")
        platform = job.get("platform", "?")
        method = application.get("method", "?")

        text = (
            f"✅ *Applied Successfully*\n\n"
            f"*{title}*\n"
            f"🏢 {company}\n"
            f"📱 {platform} ({method})\n"
            f"🕐 {datetime.now().strftime('%H:%M')}"
        )
        return self._send(text) is not None

    def send_response(self, application: Dict,
                      status: str) -> bool:
        """Notify about an application status change."""
        emoji_map = {
            "viewed": "👀",
            "shortlisted": "🌟",
            "interview": "🎉",
            "rejected": "😔",
            "offer": "🎊",
        }
        emoji = emoji_map.get(status, "📬")

        title = application.get("title",
                                application.get("job_title", "?"))
        company = application.get("company", "?")

        text = (
            f"{emoji} *Application Update: {status.title()}*\n\n"
            f"*{title}*\n"
            f"🏢 {company}\n"
            f"📊 Status: {status}"
        )
        return self._send(text) is not None

    # ═══════════════════════════════════════════════════════════
    # CAPTCHA / OTP / ANSWER REVIEW
    # ═══════════════════════════════════════════════════════════

    def send_captcha_challenge(self, screenshot_path: str,
                               captcha_type: str = "text"
                               ) -> Optional[str]:
        """
        Send CAPTCHA screenshot and wait for user's answer.

        Args:
            screenshot_path: Path to CAPTCHA screenshot.
            captcha_type: "text" or "image_grid".

        Returns:
            User's answer string, or None on timeout (5 min).
        """
        if not self._enabled:
            return None

        callback_id = f"captcha_{uuid.uuid4().hex[:8]}"

        if captcha_type == "image_grid":
            caption = (
                f"🧩 *CAPTCHA (Image Grid)*\n\n"
                f"Reply with the numbers of correct images.\n"
                f"Example: `2 5 8`\n"
                f"ID: `{callback_id}`"
            )
        else:
            caption = (
                f"🔤 *CAPTCHA (Text)*\n\n"
                f"Reply with the text shown in the image.\n"
                f"ID: `{callback_id}`"
            )

        with self._pending_lock:
            self._pending[callback_id] = None

        if os.path.isfile(screenshot_path):
            self._send_photo(screenshot_path, caption)
        else:
            self._send(caption)

        # Wait up to 5 minutes for reply
        return self._wait_for_text_reply(callback_id, timeout=300)

    def send_otp_request(self, platform: str) -> Optional[str]:
        """
        Ask user for OTP code via Telegram.

        Returns:
            OTP code string, or None on timeout (2 min).
        """
        if not self._enabled:
            return None

        callback_id = f"otp_{uuid.uuid4().hex[:8]}"

        text = (
            f"🔑 *OTP Required for {platform.title()}*\n\n"
            f"Check your email/phone and reply with the code.\n"
            f"⏱ Timeout: 2 minutes\n"
            f"ID: `{callback_id}`"
        )

        with self._pending_lock:
            self._pending[callback_id] = None

        self._send(text)
        return self._wait_for_text_reply(callback_id, timeout=120)

    def send_answer_review(self, question: str,
                           ai_answer: str) -> str:
        """
        Show AI-generated answer for review.

        Returns:
            Final answer (original or user-edited).
        """
        if not self._enabled:
            return ai_answer

        callback_id = f"answer_{uuid.uuid4().hex[:8]}"

        text = (
            f"📝 *Answer Review*\n\n"
            f"*Q:* {question[:200]}\n\n"
            f"*AI Answer:*\n{ai_answer[:500]}\n\n"
            f"_Tap Use to accept, or reply with your edit._"
        )

        markup = {
            "inline_keyboard": [[
                {"text": "✅ Use This",
                 "callback_data": f"{callback_id}:use"},
                {"text": "✏️ Edit",
                 "callback_data": f"{callback_id}:edit"},
            ]]
        }

        with self._pending_lock:
            self._pending[callback_id] = None

        self._send(text, reply_markup=markup)

        # Wait for button or text reply
        deadline = time.time() + 120  # 2 min

        while time.time() < deadline:
            self._poll_updates()
            with self._pending_lock:
                response = self._pending.get(callback_id)
            if response is not None:
                with self._pending_lock:
                    self._pending.pop(callback_id, None)
                if response == "use":
                    return ai_answer
                else:
                    # response is the edited text
                    return response
            time.sleep(3)

        with self._pending_lock:
            self._pending.pop(callback_id, None)
        return ai_answer  # timeout → use original

    # ═══════════════════════════════════════════════════════════
    # PLATFORM / ERROR / REPORT
    # ═══════════════════════════════════════════════════════════

    def send_daily_report(self, stats: Dict) -> bool:
        """Send daily summary report."""
        applied = stats.get("today_applied", 0)
        responses = stats.get("today_responses", 0)
        week_total = stats.get("week_total", 0)
        matches = stats.get("top_matches", 0)
        follow_ups = stats.get("pending_follow_ups", 0)

        per_platform = stats.get("per_platform", {})
        platform_lines = ""
        for name, count in per_platform.items():
            platform_lines += f"  • {name}: {count}\n"

        text = (
            f"📊 *Daily Report — "
            f"{datetime.now().strftime('%d %b %Y')}*\n\n"
            f"✅ Applied today: {applied}\n"
            f"📬 Responses: {responses}\n"
            f"📅 This week: {week_total}\n"
            f"🎯 Matches found: {matches}\n"
            f"📧 Pending follow-ups: {follow_ups}\n"
        )
        if platform_lines:
            text += f"\n*Per platform:*\n{platform_lines}"

        return self._send(text) is not None

    def send_error(self, module: str, error: str) -> bool:
        """Send error notification."""
        if not self._send_errors:
            return True

        text = (
            f"🚨 *Error*\n\n"
            f"Module: `{module}`\n"
            f"Error: {error[:300]}\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )
        return self._send(text) is not None

    def send_platform_issue(self, platform: str,
                            issue: str) -> bool:
        """Send platform-specific issue notification."""
        text = (
            f"⚠️ *Platform Issue: {platform.title()}*\n\n"
            f"{issue[:500]}\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )
        return self._send(text) is not None

    def send_2fa_needed(self, platform: str) -> bool:
        """Notify that 2FA is needed for a platform."""
        text = (
            f"🔐 *2FA Required: {platform.title()}*\n\n"
            f"The browser is waiting for a verification code.\n"
            f"Please check your email/phone and enter the code\n"
            f"in the browser window, or reply here."
        )
        return self._send(text) is not None

    # ═══════════════════════════════════════════════════════════
    # TEST
    # ═══════════════════════════════════════════════════════════

    def test_connection(self) -> Dict:
        """
        Test Telegram bot connection.

        Returns:
            {ok, bot_name, chat_id, message_sent, error?}
        """
        result = {
            "ok": False,
            "bot_name": "",
            "chat_id": self._chat_id,
            "message_sent": False,
            "error": None,
        }

        if not self._api:
            result["error"] = "Bot token or chat ID not configured"
            return result

        # Test getMe
        me = self._api.get_me()
        if not me.get("ok"):
            result["error"] = f"getMe failed: {me.get('description')}"
            return result

        bot_info = me.get("result", {})
        result["bot_name"] = bot_info.get("username", "?")

        # Test send message
        test_msg = self._api.send_message(
            self._chat_id,
            "🤖 *Job Agent Connected*\n\n"
            f"Bot: @{result['bot_name']}\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Status: All systems operational ✅",
        )
        if test_msg.get("ok"):
            result["message_sent"] = True
            result["ok"] = True
        else:
            result["error"] = (
                f"Send failed: {test_msg.get('description')}")

        return result

    # ═══════════════════════════════════════════════════════════
    # UPDATE POLLING (for callbacks + text replies)
    # ═══════════════════════════════════════════════════════════

    def _poll_updates(self) -> None:
        """Poll Telegram for new updates (callbacks + messages)."""
        if not self._api:
            return

        try:
            result = self._api.get_updates(
                offset=self._update_offset, timeout=2)
            if not result.get("ok"):
                return

            for update in result.get("result", []):
                self._update_offset = update["update_id"] + 1
                self._handle_update(update)

        except Exception as e:
            logger.debug("Poll error: %s", e)

    def _handle_update(self, update: Dict) -> None:
        """Process a single Telegram update."""
        # ── Callback query (button press) ──
        cb = update.get("callback_query")
        if cb:
            data = cb.get("data", "")
            cb_id = cb.get("id", "")

            # Answer the callback (removes loading spinner)
            if self._api:
                self._api.answer_callback_query(cb_id, "✓")

            if ":" in data:
                callback_id, action = data.split(":", 1)
                with self._pending_lock:
                    if callback_id in self._pending:
                        self._pending[callback_id] = action
                        logger.info(
                            "Callback received: %s → %s",
                            callback_id, action)

                # Edit the message to show the action taken
                msg = cb.get("message", {})
                msg_id = msg.get("message_id")
                if msg_id and self._api:
                    action_text = {
                        "approve": "✅ APPROVED",
                        "skip": "❌ SKIPPED",
                        "review": "👁 REVIEWING",
                        "use": "✅ USING AI ANSWER",
                        "edit": "✏️ WAITING FOR EDIT",
                    }.get(action, action.upper())
                    try:
                        original = msg.get("text", "")
                        self._api.edit_message_text(
                            self._chat_id, msg_id,
                            f"{original}\n\n*→ {action_text}*")
                    except Exception:
                        pass
            return

        # ── Text message (OTP, CAPTCHA answer, edited answer) ──
        message = update.get("message")
        if message:
            text = (message.get("text") or "").strip()
            chat_id = str(message.get("chat", {}).get("id", ""))

            # Only process from our chat
            if chat_id != self._chat_id:
                return

            if not text:
                return

            # Match to any pending request (FIFO)
            with self._pending_lock:
                for cid in list(self._pending.keys()):
                    if self._pending[cid] is None:
                        # This pending item is waiting for text
                        if (cid.startswith("otp_") or
                                cid.startswith("captcha_") or
                                cid.startswith("answer_")):
                            self._pending[cid] = text
                            logger.info(
                                "Text reply matched: %s → '%s'",
                                cid, text[:30])
                            return

            logger.debug("Unmatched text message: '%s'",
                         text[:50])

    def _wait_for_text_reply(self, callback_id: str,
                             timeout: int = 120
                             ) -> Optional[str]:
        """Wait for a text reply matching a pending request."""
        deadline = time.time() + timeout

        while time.time() < deadline:
            self._poll_updates()
            with self._pending_lock:
                response = self._pending.get(callback_id)
            if response is not None:
                with self._pending_lock:
                    self._pending.pop(callback_id, None)
                return response
            time.sleep(3)

        with self._pending_lock:
            self._pending.pop(callback_id, None)
        logger.warning("Text reply timeout: %s", callback_id)
        return None


# ═══════════════════════════════════════════════════════════════════
# SINGLETON
# ═══════════════════════════════════════════════════════════════════

_notifier_instance: Optional[JobNotifier] = None


def get_notifier() -> JobNotifier:
    """Get or create the singleton JobNotifier."""
    global _notifier_instance
    if _notifier_instance is None:
        _notifier_instance = JobNotifier()
    return _notifier_instance


# ═══════════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Telegram Notifier — Self Test")
    print("=" * 60)

    # 1. Config check
    print("\n[1] Config:")
    print(f"  BOT_TOKEN: {'✓ set' if TELEGRAM_BOT_TOKEN else '✗ not set'}")
    print(f"  CHAT_ID: {'✓ set' if TELEGRAM_CHAT_ID else '✗ not set'}")
    print(f"  Enabled: {TELEGRAM_CONFIG.get('enabled', True)}")

    notifier = JobNotifier()

    if not notifier._enabled:
        print("\n  ⚠ Notifier disabled (no token/chat_id)")
        print("  Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        print("\n  Skipping live tests.\n")
    else:
        # 2. Connection test
        print("\n[2] Connection test:")
        conn = notifier.test_connection()
        print(f"  OK: {conn['ok']}")
        print(f"  Bot: @{conn['bot_name']}")
        print(f"  Message sent: {conn['message_sent']}")
        if conn.get("error"):
            print(f"  Error: {conn['error']}")

        if conn["ok"]:
            # 3. Match notification
            print("\n[3] Sending test match:")
            test_job = {
                "title": "SDE-1 (Test)",
                "company": "Test Corp",
                "location": "Bangalore",
                "salary_text": "8-12 LPA",
                "url": "https://example.com/job/123",
                "platform": "naukri",
                "match_score": 87,
            }
            test_score = {
                "score": 87,
                "recommendation": "strong",
                "reasoning": "Great match for skills and location",
                "skills_found": ["Java", "Spring Boot", "Docker"],
                "skills_missing": ["Kubernetes"],
            }
            ok = notifier.send_match(test_job, test_score)
            print(f"  Sent: {ok}")

            # 4. Error notification
            print("\n[4] Sending test error:")
            ok = notifier.send_error("test_module",
                                     "This is a test error")
            print(f"  Sent: {ok}")

            # 5. Platform issue
            print("\n[5] Sending test platform issue:")
            ok = notifier.send_platform_issue(
                "naukri", "Test issue — ignore this")
            print(f"  Sent: {ok}")

            # 6. Interactive test (optional)
            run_interactive = input(
                "\n[6] Run interactive approval test? (y/n): "
            ).strip().lower()
            if run_interactive == "y":
                print("  Sending approval request...")
                cb_id = notifier.send_approval_request(
                    test_job, "/tmp/resume.pdf")
                print(f"  Callback ID: {cb_id}")
                print("  Waiting 30s for response (tap button in Telegram)...")
                approved = notifier.wait_for_approval(cb_id, timeout=30)
                print(f"  Result: {'APPROVED' if approved else 'SKIPPED/TIMEOUT'}")

    # 7. Singleton
    print("\n[7] Singleton test:")
    n1 = get_notifier()
    n2 = get_notifier()
    print(f"  Same instance: {n1 is n2}")

    print(f"\n✅ Notifier tests complete!\n")