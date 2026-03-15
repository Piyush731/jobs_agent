#!/usr/bin/env python3
"""
outreach/scheduler.py — Follow-up email scheduling and execution.

Manages the follow-up cadence after job applications:
  • D3  — Polite check-in (3 days after application)
  • D7  — Value reinforcement (7 days)
  • D14 — Final graceful follow-up (14 days)

Automatically:
  • Schedules follow-ups when an application is logged
  • Checks for due follow-ups each cycle
  • Sends follow-ups via EmailSender (respects rate limits + hours)
  • Cancels follow-ups when a response is received
  • Skips follow-ups if daily email limit is reached
  • Tracks follow-up count per application

Usage:
    from outreach.scheduler import FollowUpScheduler

    scheduler = FollowUpScheduler()

    # After submitting an application:
    scheduler.schedule(application_id, follow_up_days=[3, 7, 14])

    # Periodic check (called by discovery/monitor.py):
    result = scheduler.process()
    # → {checked, sent, failed, skipped, cancelled}

    # When response received:
    scheduler.cancel(application_id)
"""

import json
import time
import traceback as tb_module
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from config import EMAIL_CONFIG
from core.logger import get_logger
from core.db import get_db

logger = get_logger("outreach.scheduler")

# ── Default follow-up schedule (days after application) ─────────
_DEFAULT_SCHEDULE = EMAIL_CONFIG.get("follow_up_schedule", [3, 7, 14])


# ═══════════════════════════════════════════════════════════════════
# FOLLOW-UP SCHEDULER
# ═══════════════════════════════════════════════════════════════════

class FollowUpScheduler:
    """
    Manages follow-up email scheduling for job applications.

    Workflow:
      1. ``schedule(app_id)`` → creates follow-up entries in DB
         (next_follow_up date on the application row + email queue).
      2. ``process()`` → checks all applications for due follow-ups,
         generates email from template, sends via EmailSender.
      3. ``cancel(app_id)`` → stops follow-ups (got response).

    The scheduler does NOT run in its own thread — it's called
    periodically by the main monitor loop or via CLI.
    """

    def __init__(self, email_sender=None):
        """
        Args:
            email_sender: EmailSender instance. None → lazy-import.
        """
        self._sender = email_sender
        self._schedule_days = list(_DEFAULT_SCHEDULE)
        self.db = get_db()

        logger.info("FollowUpScheduler ready (schedule=%s days)",
                     self._schedule_days)

    def _get_sender(self):
        """Lazy-load EmailSender to avoid circular imports."""
        if self._sender is None:
            try:
                from outreach.email_sender import get_email_sender
                self._sender = get_email_sender()
            except ImportError:
                logger.error("EmailSender not available")
                return None
        return self._sender

    # ═══════════════════════════════════════════════════════════
    # PUBLIC — Schedule Follow-ups
    # ═══════════════════════════════════════════════════════════

    def schedule(self, application_id: int,
                 follow_up_days: Optional[List[int]] = None,
                 contact_email: Optional[str] = None,
                 contact_name: Optional[str] = None) -> bool:
        """
        Schedule follow-up emails for an application.

        Creates the first follow-up date on the application record.
        Subsequent follow-ups are scheduled after each one is sent.

        Args:
            application_id: ID in the applications table.
            follow_up_days: Days after application to follow up.
                            None → config default [3, 7, 14].
            contact_email: HR email (if not on the application record).
            contact_name: HR name (if not on the application record).

        Returns:
            True if follow-up was scheduled successfully.
        """
        days = follow_up_days or list(self._schedule_days)
        if not days:
            logger.debug("No follow-up days configured")
            return False

        try:
            # Get application info
            apps = self.db.get_applications(limit=1000)
            app = None
            for a in apps:
                if a.get("id") == application_id:
                    app = a
                    break

            if not app:
                logger.warning("Application %d not found",
                               application_id)
                return False

            # Check if already has follow-ups scheduled
            current_next = app.get("next_follow_up", "")
            if current_next:
                logger.debug("Application %d already has follow-up "
                             "scheduled: %s", application_id,
                             current_next)
                return True

            # Calculate first follow-up date
            applied_at = app.get("applied_at", "")
            if applied_at:
                try:
                    applied_dt = datetime.fromisoformat(
                        applied_at.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    applied_dt = datetime.now()
            else:
                applied_dt = datetime.now()

            first_day = min(days)
            next_follow_up = (applied_dt + timedelta(days=first_day)
                              ).strftime("%Y-%m-%d")

            # Store follow-up schedule as JSON in notes
            schedule_info = {
                "follow_up_days": days,
                "contact_email": contact_email or "",
                "contact_name": contact_name or "",
                "current_index": 0,
            }

            # Update application record
            updates = {
                "next_follow_up": next_follow_up,
                "notes": json.dumps(schedule_info),
            }

            self.db.update_application_status(
                application_id,
                app.get("status", "submitted"),
                notes=json.dumps(schedule_info),
            )

            # Also update next_follow_up directly
            try:
                self.db.execute(
                    "UPDATE applications SET next_follow_up = ? "
                    "WHERE id = ?",
                    (next_follow_up, application_id),
                )
            except AttributeError:
                # DB might not have raw execute — use available method
                pass

            logger.info("Scheduled follow-ups for app %d: "
                         "first on %s (days=%s)",
                         application_id, next_follow_up, days)
            return True

        except Exception as e:
            logger.error("Failed to schedule follow-up for "
                         "app %d: %s", application_id, e)
            return False

    # ═══════════════════════════════════════════════════════════
    # PUBLIC — Check & Send Due Follow-ups
    # ═══════════════════════════════════════════════════════════

    def check_due(self) -> List[Dict]:
        """
        Find all applications with due follow-ups.

        Returns:
            List of application dicts where next_follow_up <= today.
        """
        try:
            due = self.db.get_due_follow_ups()
            logger.debug("Found %d due follow-ups", len(due))
            return due
        except Exception as e:
            logger.error("Failed to check due follow-ups: %s", e)
            return []

    def process(self) -> Dict:
        """
        Process all due follow-ups: generate emails and send.

        This is the main method called periodically by the monitor.

        Returns:
            {checked, sent, failed, skipped, cancelled}
        """
        stats = {
            "checked": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "cancelled": 0,
        }

        sender = self._get_sender()
        if not sender:
            logger.warning("EmailSender not available — "
                           "skipping follow-ups")
            return stats

        # Check if sender can send (business hours + daily limit)
        if not sender.can_send():
            logger.info("Email sending not available right now "
                         "(hours/limit) — skipping follow-ups")
            return stats

        due_apps = self.check_due()
        stats["checked"] = len(due_apps)

        if not due_apps:
            logger.debug("No follow-ups due")
            return stats

        for app in due_apps:
            try:
                result = self._process_single(app, sender)

                if result == "sent":
                    stats["sent"] += 1
                elif result == "failed":
                    stats["failed"] += 1
                elif result == "skipped":
                    stats["skipped"] += 1
                elif result == "cancelled":
                    stats["cancelled"] += 1

                # Check if sender can still send
                if not sender.can_send():
                    logger.info("Daily email limit reached during "
                                 "follow-up processing")
                    stats["skipped"] += (
                        len(due_apps) - stats["checked"])
                    break

                # Small delay between follow-ups
                time.sleep(2)

            except Exception as e:
                logger.error("Follow-up processing error for "
                             "app %s: %s",
                             app.get("id", "?"), e)
                stats["failed"] += 1

        logger.info("Follow-ups: %d checked, %d sent, %d failed, "
                     "%d skipped, %d cancelled",
                     stats["checked"], stats["sent"],
                     stats["failed"], stats["skipped"],
                     stats["cancelled"])
        return stats

    def _process_single(self, app: Dict,
                        sender) -> str:
        """
        Process a single due follow-up.

        Returns: "sent" | "failed" | "skipped" | "cancelled"
        """
        app_id = app.get("id")
        status = app.get("status", "")

        # Skip if application got a response
        if status in ("interview", "offer", "rejected",
                      "shortlisted", "ghosted"):
            logger.info("App %s: status=%s — cancelling follow-ups",
                         app_id, status)
            self.cancel(app_id)
            return "cancelled"

        # Get follow-up info from notes
        notes = app.get("notes", "")
        schedule_info = {}
        try:
            if notes:
                schedule_info = json.loads(notes)
        except (json.JSONDecodeError, TypeError):
            pass

        follow_up_days = schedule_info.get(
            "follow_up_days", list(self._schedule_days))
        current_index = schedule_info.get("current_index", 0)
        contact_email = schedule_info.get(
            "contact_email",
            app.get("contact_email", ""))
        contact_name = schedule_info.get(
            "contact_name",
            app.get("contact_name", ""))

        # No contact email — skip
        if not contact_email:
            logger.debug("App %s: no contact email — skipping",
                          app_id)
            return "skipped"

        # Determine follow-up number (1-based)
        follow_up_num = min(current_index + 1, 3)
        follow_up_count = app.get("follow_up_count", 0)

        # Max follow-ups reached
        if follow_up_count >= len(follow_up_days):
            logger.info("App %s: max follow-ups (%d) reached",
                         app_id, follow_up_count)
            self._clear_next_follow_up(app_id)
            return "skipped"

        # ── Generate and send follow-up ──
        try:
            from outreach.templates import get_follow_up_template

            app_data = {
                "title": app.get("title",
                                 app.get("job_title", "")),
                "company": app.get("company", ""),
                "applied_at": app.get("applied_at", ""),
                "contact_name": contact_name,
                "contact_email": contact_email,
                "id": app_id,
                "contact_id": app.get("contact_id"),
            }

            template = get_follow_up_template(
                app_data, follow_up_num, contact_name)

            result = sender.send(
                to=contact_email,
                subject=template["subject"],
                body=template["body"],
                email_type=f"follow_up_d{follow_up_days[current_index]}",
                application_id=app_id,
                contact_id=app.get("contact_id"),
            )

            if result.get("success"):
                logger.info("✉ Follow-up #%d sent for app %s → %s",
                            follow_up_num, app_id, contact_email)

                # Update application: increment count, set next date
                new_count = follow_up_count + 1
                new_index = current_index + 1

                # Schedule next follow-up (if any remaining)
                next_date = None
                if new_index < len(follow_up_days):
                    applied_at = app.get("applied_at", "")
                    try:
                        applied_dt = datetime.fromisoformat(
                            applied_at.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        applied_dt = datetime.now()

                    next_day = follow_up_days[new_index]
                    next_date = (applied_dt + timedelta(days=next_day)
                                 ).strftime("%Y-%m-%d")

                # Update schedule info
                schedule_info["current_index"] = new_index
                new_notes = json.dumps(schedule_info)

                # Update DB
                try:
                    self.db.update_application_status(
                        app_id, status or "submitted",
                        notes=new_notes)
                except Exception:
                    pass

                try:
                    self.db.execute(
                        "UPDATE applications SET "
                        "follow_up_count = ?, "
                        "next_follow_up = ? "
                        "WHERE id = ?",
                        (new_count, next_date, app_id),
                    )
                except (AttributeError, Exception):
                    pass

                return "sent"

            else:
                error = result.get("error", "Unknown")
                logger.warning("Follow-up failed for app %s: %s",
                                app_id, error)
                if result.get("queued"):
                    return "skipped"  # will retry later
                return "failed"

        except ImportError:
            logger.error("outreach.templates not available")
            return "failed"
        except Exception as e:
            logger.error("Follow-up generation failed for "
                          "app %s: %s", app_id, e)
            return "failed"

    # ═══════════════════════════════════════════════════════════
    # PUBLIC — Cancel Follow-ups
    # ═══════════════════════════════════════════════════════════

    def cancel(self, application_id: int) -> None:
        """
        Cancel all pending follow-ups for an application.

        Called when:
          • Application gets a response (interview/rejection)
          • User manually cancels
          • Application is withdrawn
        """
        self._clear_next_follow_up(application_id)
        logger.info("Follow-ups cancelled for app %d",
                     application_id)

    def cancel_all(self) -> int:
        """
        Cancel all pending follow-ups across all applications.

        Returns:
            Number of applications affected.
        """
        count = 0
        try:
            apps = self.db.get_applications(limit=10000)
            for app in apps:
                if app.get("next_follow_up"):
                    self._clear_next_follow_up(app["id"])
                    count += 1
        except Exception as e:
            logger.error("cancel_all failed: %s", e)
        logger.info("Cancelled follow-ups for %d applications",
                     count)
        return count

    def _clear_next_follow_up(self, application_id: int) -> None:
        """Clear next_follow_up in DB."""
        try:
            self.db.execute(
                "UPDATE applications SET next_follow_up = NULL "
                "WHERE id = ?",
                (application_id,),
            )
        except (AttributeError, Exception):
            # Fallback: try update_application_status
            try:
                self.db.update_application_status(
                    application_id, "submitted",
                    notes="Follow-ups cancelled")
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════
    # PUBLIC — Reschedule
    # ═══════════════════════════════════════════════════════════

    def reschedule(self, application_id: int,
                   days_from_now: int = 3) -> bool:
        """
        Reschedule next follow-up to a specific number of days
        from today.

        Useful when:
          • HR says "check back in a week"
          • User wants to delay follow-up

        Args:
            application_id: Application ID.
            days_from_now: Days from today.

        Returns:
            True if rescheduled.
        """
        try:
            new_date = (datetime.now() + timedelta(days=days_from_now)
                        ).strftime("%Y-%m-%d")
            self.db.execute(
                "UPDATE applications SET next_follow_up = ? "
                "WHERE id = ?",
                (new_date, application_id),
            )
            logger.info("Rescheduled app %d follow-up to %s",
                         application_id, new_date)
            return True
        except (AttributeError, Exception) as e:
            logger.error("Reschedule failed: %s", e)
            return False

    # ═══════════════════════════════════════════════════════════
    # PUBLIC — Status
    # ═══════════════════════════════════════════════════════════

    def get_status(self) -> Dict:
        """
        Get follow-up scheduler status.

        Returns:
            {
                schedule_days: [3, 7, 14],
                total_pending: int,
                due_today: int,
                due_this_week: int,
                by_follow_up_num: {1: count, 2: count, 3: count},
                sender_available: bool,
            }
        """
        result = {
            "schedule_days": self._schedule_days,
            "total_pending": 0,
            "due_today": 0,
            "due_this_week": 0,
            "by_follow_up_num": {1: 0, 2: 0, 3: 0},
            "sender_available": False,
        }

        try:
            sender = self._get_sender()
            result["sender_available"] = (
                sender is not None and sender.can_send())
        except Exception:
            pass

        try:
            apps = self.db.get_applications(limit=10000)
            today = datetime.now().strftime("%Y-%m-%d")
            week_end = (datetime.now() + timedelta(days=7)
                        ).strftime("%Y-%m-%d")

            for app in apps:
                next_fu = app.get("next_follow_up", "")
                if not next_fu:
                    continue

                result["total_pending"] += 1

                if next_fu <= today:
                    result["due_today"] += 1
                if next_fu <= week_end:
                    result["due_this_week"] += 1

                fu_count = app.get("follow_up_count", 0)
                fu_num = min(fu_count + 1, 3)
                result["by_follow_up_num"][fu_num] = (
                    result["by_follow_up_num"].get(fu_num, 0) + 1)

        except Exception as e:
            logger.debug("Status check failed: %s", e)

        return result


# ═══════════════════════════════════════════════════════════════════
# SINGLETON
# ═══════════════════════════════════════════════════════════════════

_scheduler_instance: Optional[FollowUpScheduler] = None


def get_follow_up_scheduler(
    email_sender=None,
) -> FollowUpScheduler:
    """Get or create the singleton FollowUpScheduler."""
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = FollowUpScheduler(email_sender)
    return _scheduler_instance


# ═══════════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Follow-Up Scheduler — Self Test")
    print("=" * 60)

    scheduler = FollowUpScheduler()

    # 1. Config
    print("\n[1] Config:")
    print(f"  Schedule: {scheduler._schedule_days} days")

    # 2. Status
    print("\n[2] Current status:")
    status = scheduler.get_status()
    for key, val in status.items():
        print(f"  {key}: {val}")

    # 3. Check due
    print("\n[3] Due follow-ups:")
    due = scheduler.check_due()
    print(f"  Found: {len(due)} due")
    for d in due[:5]:
        print(f"    • App #{d.get('id')}: "
              f"{d.get('title', '?')} @ {d.get('company', '?')} "
              f"(next: {d.get('next_follow_up', '?')})")

    # 4. Process (dry run — only sends if EmailSender is configured)
    print("\n[4] Process:")
    result = scheduler.process()
    for key, val in result.items():
        print(f"  {key}: {val}")

    # 5. Schedule test (only if we have an application in DB)
    print("\n[5] Schedule test:")
    try:
        apps = get_db().get_applications(limit=1)
        if apps:
            app_id = apps[0]["id"]
            ok = scheduler.schedule(app_id)
            print(f"  Scheduled for app #{app_id}: {ok}")
        else:
            print("  No applications in DB — skipping")
    except Exception as e:
        print(f"  Skipped: {e}")

    # 6. Singleton
    print("\n[6] Singleton:")
    s1 = get_follow_up_scheduler()
    s2 = get_follow_up_scheduler()
    print(f"  Same instance: {s1 is s2}")

    print(f"\n✅ Scheduler tests complete!\n")