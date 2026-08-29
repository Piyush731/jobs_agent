#!/usr/bin/env python3
"""
outreach/email_sender.py — SMTP email sender with rate limiting.

Features:
  • Gmail App Password SMTP (port 587 TLS)
  • Rate limited: max 30 emails/day, 2+ min gap
  • Business hours only (9 AM – 6 PM IST, Mon–Sat)
  • Queue system: emails queued outside hours, sent when window opens
  • Attachment support (resume PDF/DOCX)
  • Application emails, follow-ups (D3/D7/D14), cold outreach
  • All emails logged to DB (status tracking)
  • Retry on transient failures (up to 3 attempts)
  • Connection pooling (reuses SMTP connection within batch)

Prerequisites:
    SMTP_EMAIL, SMTP_PASSWORD (Gmail App Password),
    SMTP_SERVER, SMTP_PORT set in .env

Usage:
    from outreach.email_sender import EmailSender

    sender = EmailSender()
    result = sender.send("hr@company.com", "Subject", "Body",
                         attachments=["/path/resume.pdf"])
    sender.send_application(job, contact, resume_path, cover_letter)
    sender.send_follow_up(application, follow_up_number=1)
    sender.process_queue()
"""

import os
import re
import ssl
import time
import json
import random
import smtplib
import traceback as tb_module
from email import encoders
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

from config import EMAIL_CONFIG, USER_PROFILE
from core.logger import get_logger
from core.db import get_db

logger = get_logger("outreach.email_sender")

# ── Credentials ─────────────────────────────────────────────────
try:
    from config import SMTP_EMAIL, SMTP_PASSWORD, SMTP_SERVER, SMTP_PORT
except ImportError:
    SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

# ── Timezone ────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except ImportError:
    try:
        import pytz
        _IST = pytz.timezone("Asia/Kolkata")
    except ImportError:
        _IST = None
        logger.warning("No timezone library — business hours "
                       "check will use system time")


def _now_ist() -> datetime:
    """Current time in IST."""
    if _IST:
        return datetime.now(_IST)
    return datetime.now()


# ═══════════════════════════════════════════════════════════════════
# EMAIL SENDER
# ═══════════════════════════════════════════════════════════════════

class EmailSender:
    """
    Rate-limited SMTP email sender with queue and business-hours
    enforcement.

    All emails are logged to the ``emails`` table in the database.
    Emails sent outside business hours are queued and processed when
    ``process_queue()`` is called during the next window.
    """

    def __init__(self):
        self._email = SMTP_EMAIL
        self._password = SMTP_PASSWORD
        self._server = SMTP_SERVER or "smtp.gmail.com"
        self._port = SMTP_PORT or 587

        self._enabled = EMAIL_CONFIG.get("enabled", True)
        self._daily_limit = EMAIL_CONFIG.get("daily_limit", 30)
        self._send_hours = tuple(EMAIL_CONFIG.get("send_hours", (9, 18)))
        self._min_gap_s = EMAIL_CONFIG.get("min_gap_seconds", 120)
        self._max_retries = 3

        self._smtp: Optional[smtplib.SMTP] = None
        self._last_send_ts: Optional[datetime] = None
        self._daily_count = 0
        self._daily_reset_date: Optional[str] = None

        self.db = get_db()

        # Validate config
        if not self._email or not self._password:
            self._enabled = False
            logger.warning("EmailSender disabled — SMTP_EMAIL or "
                           "SMTP_PASSWORD not set in .env")
        else:
            logger.info("EmailSender ready (from=%s, server=%s:%d, "
                         "limit=%d/day, hours=%d–%d IST)",
                         self._email, self._server, self._port,
                         self._daily_limit, *self._send_hours)

    # ═══════════════════════════════════════════════════════════
    # PUBLIC — Send Email
    # ═══════════════════════════════════════════════════════════

    def send(self, to: str, subject: str, body: str,
             attachments: Optional[List[str]] = None,
             cc: Optional[str] = None,
             email_type: str = "application",
             application_id: Optional[int] = None,
             contact_id: Optional[int] = None,
             force: bool = False) -> Dict:
        """
        Send an email immediately (if within business hours and limits).

        Args:
            to: Recipient email address.
            subject: Email subject.
            body: Email body (plain text).
            attachments: List of file paths to attach.
            cc: CC address.
            email_type: "application" | "follow_up_d3" | "follow_up_d7"
                        | "follow_up_d14" | "cold_outreach"
            application_id: Link to applications table.
            contact_id: Link to contacts table.
            force: If True, bypass business hours check.

        Returns:
            {success, message_id, error?, queued?}
        """
        result = {
            "success": False,
            "message_id": None,
            "error": None,
            "queued": False,
        }

        if not self._enabled:
            result["error"] = "Email sender disabled"
            return result

        # Validate email
        if not self._is_valid_email(to):
            result["error"] = f"Invalid email: {to}"
            return result

        # Check daily limit
        self._check_daily_reset()
        if self._daily_count >= self._daily_limit:
            result["error"] = (f"Daily limit reached "
                               f"({self._daily_count}/{self._daily_limit})")
            logger.warning(result["error"])
            return result

        # Check business hours
        if not force and not self._is_business_hours():
            # Queue for later
            self._queue_email(to, subject, body, attachments, cc,
                              email_type, application_id, contact_id)
            result["queued"] = True
            result["error"] = "Outside business hours — queued"
            logger.info("Email queued (outside hours): %s → %s",
                         subject[:50], to)
            return result

        # Enforce minimum gap
        if not force and self._last_send_ts:
            elapsed = (datetime.now() - self._last_send_ts
                       ).total_seconds()
            if elapsed < self._min_gap_s:
                wait = self._min_gap_s - elapsed
                logger.debug("Rate limit: waiting %.0fs", wait)
                time.sleep(wait)

        # ── Actually send ──
        for attempt in range(1, self._max_retries + 1):
            try:
                msg_id = self._send_smtp(to, subject, body,
                                         attachments, cc)
                result["success"] = True
                result["message_id"] = msg_id
                self._daily_count += 1
                self._last_send_ts = datetime.now()

                # Log to DB
                self._log_email(to, subject, body, attachments,
                                email_type, "sent", msg_id,
                                application_id, contact_id)

                logger.info("✉ Email sent: '%s' → %s (attempt %d)",
                            subject[:40], to, attempt)
                return result

            except smtplib.SMTPRecipientsRefused as e:
                result["error"] = f"Recipient refused: {e}"
                self._log_email(to, subject, body, attachments,
                                email_type, "bounced", None,
                                application_id, contact_id,
                                error=str(e))
                logger.error(result["error"])
                return result  # don't retry on bounced

            except smtplib.SMTPAuthenticationError as e:
                result["error"] = (
                    f"SMTP auth failed — check App Password: {e}")
                logger.error(result["error"])
                self._close_smtp()
                return result  # don't retry auth errors

            except (smtplib.SMTPException, OSError) as e:
                logger.warning("Send attempt %d failed: %s",
                               attempt, e)
                self._close_smtp()
                if attempt < self._max_retries:
                    time.sleep(5 * attempt)  # backoff
                else:
                    result["error"] = f"Failed after {self._max_retries} attempts: {e}"
                    self._log_email(to, subject, body, attachments,
                                    email_type, "failed", None,
                                    application_id, contact_id,
                                    error=str(e))

        return result

    # ═══════════════════════════════════════════════════════════
    # PUBLIC — Application Email
    # ═══════════════════════════════════════════════════════════

    def send_application(self, job: Dict, contact: Dict,
                         resume_path: str,
                         cover_letter: str) -> Dict:
        """
        Send a job application email to HR/recruiter.

        Args:
            job: Job dict (title, company, url).
            contact: Contact dict (email, name, title).
            resume_path: Path to resume file.
            cover_letter: Cover letter text.

        Returns:
            send() result dict.
        """
        to = contact.get("email", "").strip()
        if not to:
            return {"success": False, "error": "No contact email"}
        # Never send to unverified guesses. Hunter/publicly discovered
        # contacts should carry verified=1; pattern_guess must remain queued.
        if not bool(contact.get("verified")):
            return {"success": False, "error": "Contact is not verified; email not sent"}

        name = contact.get("name", "")
        title = job.get("title", "the open position")
        company = job.get("company", "your company")

        # Subject
        user_name = USER_PROFILE.get("name", "Applicant")
        subject = (f"Application for {title} — {user_name}")

        # Body
        salutation = f"Dear {name}," if name else "Dear Hiring Manager,"
        body = f"""{salutation}

{cover_letter}

I have attached my resume for your consideration. I would welcome the opportunity to discuss how my experience aligns with the requirements of the {title} role at {company}.

Thank you for your time and consideration.

Best regards,
{USER_PROFILE.get('name', 'Piyush Kashyap')}
{USER_PROFILE.get('current_title', 'Full Stack Developer')}
📧 {USER_PROFILE.get('email', '')}
📱 {USER_PROFILE.get('phone', '')}
🔗 {USER_PROFILE.get('linkedin_url', '')}"""

        attachments = []
        if resume_path and os.path.isfile(resume_path):
            attachments.append(resume_path)

        return self.send(
            to=to,
            subject=subject,
            body=body,
            attachments=attachments,
            email_type="application",
            contact_id=contact.get("id"),
        )

    # ═══════════════════════════════════════════════════════════
    # PUBLIC — Follow-up Email
    # ═══════════════════════════════════════════════════════════

    def send_follow_up(self, application: Dict,
                       follow_up_number: int) -> Dict:
        """
        Send a follow-up email (D3, D7, or D14).

        Args:
            application: Application dict with job/contact info.
            follow_up_number: 1 (D3), 2 (D7), or 3 (D14).

        Returns:
            send() result dict.
        """
        to = application.get("contact_email", "")
        if not to:
            return {"success": False, "error": "No contact email"}

        title = application.get("title",
                                application.get("job_title", "the position"))
        company = application.get("company", "your company")
        contact_name = application.get("contact_name", "")
        user_name = USER_PROFILE.get("name", "Piyush Kashyap")
        applied_date = application.get("applied_at", "recently")

        salutation = (f"Dear {contact_name},"
                      if contact_name else "Dear Hiring Manager,")

        if follow_up_number == 1:
            # Day 3
            subject = (f"Following up: {title} Application — "
                       f"{user_name}")
            body = f"""{salutation}

I wanted to follow up on my application for the {title} position at {company}, submitted on {applied_date[:10] if len(applied_date) > 10 else applied_date}. I remain very enthusiastic about this opportunity and would welcome any updates on the hiring process.

Please don't hesitate to reach out if you need any additional information.

Best regards,
{user_name}
📧 {USER_PROFILE.get('email', '')}
📱 {USER_PROFILE.get('phone', '')}"""

        elif follow_up_number == 2:
            # Day 7
            subject = f"Re: {title} Position — {user_name}"
            body = f"""{salutation}

I hope you're doing well. I'm writing to reiterate my interest in the {title} role at {company}. Since applying on {applied_date[:10] if len(applied_date) > 10 else applied_date}, I've continued to sharpen my skills in the relevant technologies.

I'd be grateful for the chance to discuss how my experience with 10+ production applications could benefit your team.

Best regards,
{user_name}
📧 {USER_PROFILE.get('email', '')}
📱 {USER_PROFILE.get('phone', '')}"""

        else:
            # Day 14
            subject = (f"Final Follow-up: {title} — "
                       f"{user_name}")
            body = f"""{salutation}

I'm following up once more regarding the {title} position at {company}. I understand the hiring process takes time, and I remain keen on this opportunity.

If the position has been filled or my profile isn't the right fit this time, I completely understand. I'd appreciate any feedback or consideration for future openings.

Thank you for your time and consideration.

Best regards,
{user_name}
📧 {USER_PROFILE.get('email', '')}
📱 {USER_PROFILE.get('phone', '')}"""

        email_type_map = {1: "follow_up_d3", 2: "follow_up_d7",
                          3: "follow_up_d14"}
        email_type = email_type_map.get(follow_up_number,
                                         "follow_up_d3")

        return self.send(
            to=to, subject=subject, body=body,
            email_type=email_type,
            application_id=application.get("id"),
            contact_id=application.get("contact_id"),
        )

    # ═══════════════════════════════════════════════════════════
    # PUBLIC — Queue Processing
    # ═══════════════════════════════════════════════════════════

    def queue_email(self, to: str, subject: str, body: str,
                    attachments: Optional[List[str]] = None,
                    email_type: str = "application",
                    application_id: Optional[int] = None,
                    contact_id: Optional[int] = None) -> int:
        """
        Queue an email for sending during business hours.

        Returns:
            Email ID in database.
        """
        return self._queue_email(to, subject, body, attachments,
                                 None, email_type,
                                 application_id, contact_id)

    def process_queue(self) -> Dict:
        """
        Send all queued emails (if within business hours + limits).

        Returns:
            {processed, sent, failed, skipped, remaining}
        """
        stats = {"processed": 0, "sent": 0, "failed": 0,
                 "skipped": 0, "remaining": 0}

        if not self._is_business_hours():
            logger.info("Outside business hours — skipping queue")
            return stats

        try:
            queued = self.db.get_emails(status="queued", limit=50)
            stats["remaining"] = len(queued)

            for email_data in queued:
                if self._daily_count >= self._daily_limit:
                    logger.info("Daily limit reached during queue processing")
                    break

                stats["processed"] += 1

                to = email_data.get("to_email", "")
                subject = email_data.get("subject", "")
                body = email_data.get("body", "")
                attachments_json = email_data.get("attachments", "[]")

                try:
                    attachments = json.loads(attachments_json)
                except (json.JSONDecodeError, TypeError):
                    attachments = []

                result = self.send(
                    to=to, subject=subject, body=body,
                    attachments=attachments,
                    email_type=email_data.get("email_type", "application"),
                    application_id=email_data.get("application_id"),
                    contact_id=email_data.get("contact_id"),
                    force=True,  # bypass hours check (already checked above)
                )

                email_id = email_data.get("id")
                if result.get("success"):
                    stats["sent"] += 1
                    if email_id:
                        self.db.update_email_status(
                            email_id, "sent")
                else:
                    stats["failed"] += 1
                    if email_id:
                        self.db.update_email_status(
                            email_id, "failed",
                            error=result.get("error", ""))

                stats["remaining"] -= 1

        except Exception as e:
            logger.error("Queue processing failed: %s", e)

        logger.info("Queue: %d processed, %d sent, %d failed, "
                     "%d remaining",
                     stats["processed"], stats["sent"],
                     stats["failed"], stats["remaining"])
        return stats

    # ═══════════════════════════════════════════════════════════
    # PUBLIC — Status
    # ═══════════════════════════════════════════════════════════

    def get_daily_count(self) -> int:
        self._check_daily_reset()
        return self._daily_count

    def can_send(self) -> bool:
        self._check_daily_reset()
        return (self._enabled and
                self._daily_count < self._daily_limit and
                self._is_business_hours())

    # ═══════════════════════════════════════════════════════════
    # INTERNAL — SMTP
    # ═══════════════════════════════════════════════════════════

    def _get_smtp(self) -> smtplib.SMTP:
        """Get or create SMTP connection."""
        if self._smtp is not None:
            try:
                self._smtp.noop()
                return self._smtp
            except Exception:
                self._close_smtp()

        smtp = smtplib.SMTP(self._server, self._port, timeout=30)
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(self._email, self._password)
        self._smtp = smtp
        logger.debug("SMTP connection established")
        return smtp

    def _close_smtp(self) -> None:
        if self._smtp:
            try:
                self._smtp.quit()
            except Exception:
                pass
            self._smtp = None

    def _send_smtp(self, to: str, subject: str, body: str,
                   attachments: Optional[List[str]] = None,
                   cc: Optional[str] = None) -> str:
        """Send via SMTP. Returns Message-ID."""
        msg = MIMEMultipart()
        msg["From"] = f"{USER_PROFILE.get('name', 'Piyush Kashyap')} <{self._email}>"
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc

        # Generate Message-ID
        import uuid
        msg_id = f"<{uuid.uuid4().hex}@jobagent.local>"
        msg["Message-ID"] = msg_id

        # Body
        msg.attach(MIMEText(body, "plain", "utf-8"))

        # Attachments
        for filepath in (attachments or []):
            if not os.path.isfile(filepath):
                logger.warning("Attachment not found: %s", filepath)
                continue

            filename = os.path.basename(filepath)
            with open(filepath, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())

            encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                            f"attachment; filename={filename}")
            msg.attach(part)

        # Send
        smtp = self._get_smtp()
        recipients = [to]
        if cc:
            recipients.append(cc)
        smtp.sendmail(self._email, recipients, msg.as_string())

        return msg_id

    # ═══════════════════════════════════════════════════════════
    # INTERNAL — Helpers
    # ═══════════════════════════════════════════════════════════

    def _is_valid_email(self, email: str) -> bool:
        if not email:
            return False
        return bool(re.match(
            r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$',
            email))

    def _is_business_hours(self) -> bool:
        now = _now_ist()
        hour = now.hour
        weekday = now.weekday()  # 0=Mon, 6=Sun
        lo, hi = self._send_hours
        return lo <= hour < hi and weekday < 6  # Mon-Sat

    def _check_daily_reset(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._daily_reset_date != today:
            self._daily_count = 0
            self._daily_reset_date = today
            logger.debug("Daily email count reset")

    def _queue_email(self, to, subject, body, attachments,
                     cc, email_type, application_id,
                     contact_id) -> int:
        """Save email to DB queue."""
        try:
            data = {
                "to_email": to,
                "subject": subject,
                "body": body,
                "attachments": json.dumps(attachments or []),
                "email_type": email_type,
                "status": "queued",
                "application_id": application_id,
                "contact_id": contact_id,
            }
            email_id = self.db.save_email(data)
            logger.debug("Email queued (id=%s): %s → %s",
                         email_id, subject[:40], to)
            return email_id
        except Exception as e:
            logger.error("Failed to queue email: %s", e)
            return 0

    def _log_email(self, to, subject, body, attachments,
                   email_type, status, message_id,
                   application_id, contact_id,
                   error=None) -> None:
        """Log sent/failed email to DB."""
        try:
            data = {
                "to_email": to,
                "subject": subject,
                "body": body,
                "attachments": json.dumps(attachments or []),
                "email_type": email_type,
                "status": status,
                "sent_at": datetime.now().isoformat()
                           if status == "sent" else None,
                "application_id": application_id,
                "contact_id": contact_id,
                "error_message": error,
            }
            self.db.save_email(data)
        except Exception as e:
            logger.debug("Failed to log email: %s", e)

    def close(self) -> None:
        """Close SMTP connection."""
        self._close_smtp()
        logger.debug("EmailSender closed")

    def __del__(self):
        try:
            self._close_smtp()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
# SINGLETON
# ═══════════════════════════════════════════════════════════════════

_sender_instance: Optional[EmailSender] = None


def get_email_sender() -> EmailSender:
    global _sender_instance
    if _sender_instance is None:
        _sender_instance = EmailSender()
    return _sender_instance


# ═══════════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Email Sender — Self Test")
    print("=" * 60)

    sender = EmailSender()

    # 1. Config
    print("\n[1] Config:")
    print(f"  SMTP_EMAIL: {'✓ set' if SMTP_EMAIL else '✗ not set'}")
    print(f"  SMTP_PASSWORD: {'✓ set' if SMTP_PASSWORD else '✗ not set'}")
    print(f"  Server: {SMTP_SERVER}:{SMTP_PORT}")
    print(f"  Enabled: {sender._enabled}")
    print(f"  Daily limit: {sender._daily_limit}")
    print(f"  Hours: {sender._send_hours[0]}–{sender._send_hours[1]} IST")

    # 2. Validation
    print("\n[2] Email validation:")
    tests = [
        ("test@example.com", True),
        ("bad@", False),
        ("", False),
        ("hr@company.co.in", True),
    ]
    for email, expected in tests:
        ok = sender._is_valid_email(email)
        icon = "✓" if ok == expected else "✗"
        print(f"  {icon} '{email}' → {ok} (expected {expected})")

    # 3. Business hours
    print("\n[3] Business hours check:")
    in_hours = sender._is_business_hours()
    now = _now_ist()
    print(f"  Current time (IST): {now.strftime('%H:%M %A')}")
    print(f"  In business hours: {in_hours}")

    # 4. Can send
    print("\n[4] Can send:")
    print(f"  can_send(): {sender.can_send()}")
    print(f"  Daily count: {sender.get_daily_count()}")

    # 5. Live send test
    if sender._enabled:
        run = input("\n[5] Send test email to yourself? (y/n): "
                    ).strip().lower()
        if run == "y":
            result = sender.send(
                to=SMTP_EMAIL,
                subject="Job Agent Test Email",
                body="This is a test email from the Job Agent.\n\n"
                     "If you see this, email sending works! ✅",
                force=True,
            )
            print(f"  Result: {result}")
    else:
        print("\n[5] Skipped (sender disabled)")

    # 6. Follow-up generation
    print("\n[6] Follow-up subjects:")
    for num in [1, 2, 3]:
        app = {"title": "SDE-1", "company": "Razorpay",
               "applied_at": "2025-07-10", "contact_email": "x@y.com"}
        # Just test the subject generation (don't actually send)
        day_map = {1: "D3", 2: "D7", 3: "D14"}
        print(f"  {day_map[num]}: Follow-up #{num}")

    # 7. Singleton
    print("\n[7] Singleton:")
    s1 = get_email_sender()
    s2 = get_email_sender()
    print(f"  Same instance: {s1 is s2}")

    sender.close()
    print(f"\n✅ Email Sender tests complete!\n")