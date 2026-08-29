#!/usr/bin/env python3
"""One-shot SMTP smoke test. Sends only to the configured sender mailbox and reports to Telegram."""
from pathlib import Path
import os
import json
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from outreach.email_sender import EmailSender
from tracking.notifications import JobNotifier

recipient = os.getenv("EMAIL_SMOKE_TEST_TO", "").strip() or os.getenv("SMTP_EMAIL", "").strip()
if not recipient or "@" not in recipient:
    raise SystemExit("Set EMAIL_SMOKE_TEST_TO to a real inbox you control, or configure SMTP_EMAIL")

result = EmailSender().send(
    to=recipient,
    subject="Job Agent SMTP smoke test",
    body="This automated smoke test verifies that the Job Agent SMTP queue and Gmail app password are working.",
)

summary = json.dumps(result, default=str)
print(summary)

try:
    JobNotifier().send_error("email_smoke_test", f"SMTP test to {recipient}: {summary}")
except Exception as exc:
    print(f"Telegram report failed: {exc}")

if not result.get("success") and not result.get("queued"):
    raise SystemExit(1)
