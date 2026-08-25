#!/usr/bin/env python3
"""
discovery/monitor.py  –  Main autonomous job monitoring & application pipeline.

Orchestrates:
    Discovery  →  Dedup  →  Filter  →  AI Score  →  Queue
    →  Tailor Resume  →  Telegram Approve  →  Platform Apply
    →  Email HR  →  Schedule Follow-ups  →  Track & Report

Runs continuously with configurable intervals.
Handles errors gracefully – one failed cycle never kills the agent.
"""

import re
import time
import json
import random
import signal
import threading
import traceback as tb_module
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple

from config import (
    BASE_DIR,
    RESUME_OUTPUT_DIR,
    USER_PROFILE,
    PLATFORM_CONFIG,
    AI_CONFIG,
    RESUME_CONFIG,
    EMAIL_CONFIG,
    MATCH_CONFIG,
    STEALTH_CONFIG,
    TELEGRAM_CONFIG,
    SCHEDULE_CONFIG,
)
from core.logger import get_logger
from core.db import get_db

logger = get_logger("monitor")

# Platforms that support browser-based auto-apply
_APPLY_PLATFORMS = {"naukri", "indeed", "foundit"}
# Search / scrape only – NEVER auto-apply
_SEARCH_ONLY_PLATFORMS = {"linkedin"}


# ═══════════════════════════════════════════════════════════════════════
class JobMonitor:
    """
    Central controller that runs discover / apply / follow-up / report
    cycles on configurable intervals.  Every public method is safe to
    call independently from the CLI or from the continuous loop.
    """

    # ──────────────────────────────────────────────────────────────────
    #  INIT
    # ──────────────────────────────────────────────────────────────────
    def __init__(self):
        self.logger = logger
        self.db = get_db()
        self.running = False
        self._shutdown = threading.Event()

        # pipeline components – lazy-initialised by _init_components()
        self.platform_manager = None
        self.llm = None
        self.matcher = None
        self.tailor = None
        self.cover_gen = None
        self.builder = None
        self.optimizer = None
        self.notifier = None
        self.email_sender = None
        self.email_finder = None
        self.follow_up_sched = None
        self.report_gen = None
        self.dedup = None
        self.job_filter = None
        self.base_resume = None
        self.preferences = None
        self._components_ready = False

        # epoch-second timestamps for last cycle run
        self._t_discover: float = 0.0
        self._t_apply: float = 0.0
        self._t_follow_up: float = 0.0
        self._t_email: float = 0.0
        self._last_report_date: Optional[str] = None
        self._last_reset_date: Optional[str] = None

        # cumulative session stats
        self.session: Dict[str, Any] = {
            "started": None,
            "cycles": 0,
            "discovered": 0,
            "matched": 0,
            "applied": 0,
            "emailed": 0,
            "errors": 0,
        }
        self.logger.info("JobMonitor created (components not yet initialised)")

    # ──────────────────────────────────────────────────────────────────
    #  SIGNAL HANDLER
    # ──────────────────────────────────────────────────────────────────
    def _signal_handler(self, signum, frame):
        """Handle SIGINT/SIGTERM for graceful shutdown."""
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else signum
        self.logger.info("Signal %s received — initiating shutdown", sig_name)
        self.stop()

    # ──────────────────────────────────────────────────────────────────
    #  COMPONENT INITIALISATION
    # ──────────────────────────────────────────────────────────────────
    def _init_components(self):
        """Create every pipeline component; tolerate individual failures."""
        if self._components_ready:
            self.logger.debug("Components already initialised – skipping")
            return
        self.logger.info("Initialising pipeline components …")

        # ── Platform Manager ──────────────────────────────────────────
        try:
            from platforms.manager import PlatformManager

            self.platform_manager = PlatformManager()
            self.logger.info("  ✔ PlatformManager ready")
        except Exception as exc:
            self.logger.error("  ✘ PlatformManager: %s", exc)

        # ── AI ────────────────────────────────────────────────────────
        try:
            from ai.llm_client import LLMClient
            from ai.job_matcher import JobMatcher
            from ai.resume_tailor import ResumeTailor
            from ai.cover_letter import CoverLetterGenerator

            self.llm = LLMClient()
            self.matcher = JobMatcher(self.llm)
            self.tailor = ResumeTailor(self.llm)
            self.cover_gen = CoverLetterGenerator(self.llm)
            self.logger.info("  ✔ AI (LLM / Matcher / Tailor / CoverLetter)")
        except Exception as exc:
            self.logger.warning("  ✘ AI: %s – heuristic scoring only", exc)

        # ── Resume ────────────────────────────────────────────────────
        try:
            from resume.builder import ResumeBuilder
            from resume.optimizer import ATSOptimizer

            self.builder = ResumeBuilder()
            self.optimizer = ATSOptimizer()
            self.logger.info("  ✔ Resume builder + optimiser")
        except Exception as exc:
            self.logger.warning("  ✘ Resume tools: %s", exc)

        # ── Notifications ─────────────────────────────────────────────
        try:
            from tracking.notifications import JobNotifier

            self.notifier = JobNotifier()
            test = self.notifier.test_connection()
            # test_connection may return {"ok": True} or {"success": True}
            if test and (test.get("success") or test.get("ok")):
                self.logger.info("  ✔ Telegram notifier connected")
            else:
                self.logger.warning("  ⚠ Telegram test failed – auto-approve mode")
                self.notifier = None
        except Exception as exc:
            self.logger.warning("  ✘ Telegram: %s – auto-approve mode", exc)
            self.notifier = None

        # ── Email / Outreach ──────────────────────────────────────────
        try:
            from outreach.email_sender import EmailSender
            from outreach.email_finder import EmailFinder
            from outreach.scheduler import FollowUpScheduler

            self.email_sender = EmailSender()
            self.email_finder = EmailFinder()
            self.follow_up_sched = FollowUpScheduler()
            self.logger.info("  ✔ Email / follow-up")
        except Exception as exc:
            self.logger.warning("  ✘ Email tools: %s", exc)

        # ── Reports (Phase 4 – optional) ──────────────────────────────
        try:
            from tracking.reports import ReportGenerator

            self.report_gen = ReportGenerator()
            self.logger.info("  ✔ ReportGenerator")
        except Exception as exc:
            self.logger.info("  ⚠ ReportGenerator not available (Phase 4): %s", exc)

        # ── Discovery helpers ─────────────────────────────────────────
        try:
            from discovery.dedup import Deduplicator
            from discovery.filters import JobFilter

            self.dedup = Deduplicator()
            self.job_filter = JobFilter()
            self.logger.info("  ✔ Dedup + filter")
        except Exception as exc:
            self.logger.warning("  ✘ Dedup/filter: %s", exc)

        # ── Profile ───────────────────────────────────────────────────
        try:
            from profile.resume_data import get_base_resume
            from profile.preferences import get_preferences

            self.base_resume = get_base_resume()
            self.preferences = get_preferences()
            self.logger.info("  ✔ Profile / preferences loaded")
        except Exception as exc:
            self.logger.warning("  ✘ Profile: %s", exc)

        self._components_ready = True
        self.logger.info("Component initialisation complete")

    # ──────────────────────────────────────────────────────────────────
    #  PLATFORM LOGIN
    # ──────────────────────────────────────────────────────────────────
    def _login_platforms(self) -> dict:
        """Attempt login on every enabled platform via PlatformManager."""
        if not self.platform_manager:
            self.logger.warning("No PlatformManager – cannot log in")
            return {"error": "PlatformManager unavailable"}
        try:
            result = self.platform_manager.login_all()
            if isinstance(result, dict):
                for plat, status in result.items():
                    ok = status if isinstance(status, bool) else bool(status)
                    sym = "✔" if ok else "✘"
                    self.logger.info("  %s Login %s: %s", sym, plat, status)
                    if not ok and self.notifier:
                        try:
                            self.notifier.send_platform_issue(
                                plat, f"Login failed: {status}"
                            )
                        except Exception:
                            pass
            return result or {}
        except Exception as exc:
            self.logger.error("Login sweep failed: %s", exc)
            self._save_error("login_platforms", exc)
            return {"error": str(exc)}

    # ──────────────────────────────────────────────────────────────────
    #  START / STOP
    # ──────────────────────────────────────────────────────────────────
    def start(self):
        """
        Public entry-point.
        Initialises → logins → enters main loop.  Blocks until stop().
        """
        self.logger.info("=" * 60)
        self.logger.info("  JOB APPLICATION AGENT  –  STARTING")
        self.logger.info("=" * 60)

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self.running = True
        self._shutdown.clear()
        self.session["started"] = datetime.now().isoformat()

        # 1. init
        self._init_components()

        # 2. login
        self.logger.info("Logging in to platforms …")
        login_result = self._login_platforms()
        self.logger.info("Login results: %s", login_result)

        # 3. notify
        plat_names = (
            ", ".join(login_result.keys())
            if isinstance(login_result, dict)
            else "N/A"
        )
        self._safe_notify(f"🚀 *Job Agent started*\nPlatforms: {plat_names}")

        # 4. main loop
        try:
            self._run_loop()
        except KeyboardInterrupt:
            self.logger.info("KeyboardInterrupt – shutting down")
        except Exception as exc:
            self.logger.critical(
                "Fatal error in main loop: %s", exc, exc_info=True
            )
            self._save_error("run_loop", exc)
        finally:
            self.stop()

    def stop(self):
        """Graceful shutdown – close browsers, notify, flip flag."""
        if not self.running:
            return
        self.logger.info("Shutting down agent …")
        self.running = False
        self._shutdown.set()

        # close browser sessions
        if self.platform_manager:
            try:
                if hasattr(self.platform_manager, "close_all"):
                    self.platform_manager.close_all()
                elif hasattr(self.platform_manager, "browser") and hasattr(
                    self.platform_manager.browser, "close_all"
                ):
                    self.platform_manager.browser.close_all()
            except Exception as exc:
                self.logger.debug("Browser close error: %s", exc)

        elapsed = ""
        if self.session["started"]:
            try:
                dt = datetime.fromisoformat(self.session["started"])
                elapsed = str(datetime.now() - dt).split(".")[0]
            except Exception:
                pass
        self._safe_notify(
            f"🛑 *Job Agent stopped*\n"
            f"Uptime: {elapsed}\n"
            f"Cycles: {self.session['cycles']}  |  "
            f"Applied: {self.session['applied']}  |  "
            f"Errors: {self.session['errors']}"
        )
        self.logger.info(
            "Shutdown complete  (applied=%d)", self.session["applied"]
        )

    # ──────────────────────────────────────────────────────────────────
    #  MAIN LOOP
    # ──────────────────────────────────────────────────────────────────
    def _run_loop(self):
        """
        Continuous event loop.
        Runs discovery / apply / email / follow-up / report cycles at
        their configured intervals.  Sleeps 60 s between ticks.
        """
        discover_iv = SCHEDULE_CONFIG.get("discovery_interval_minutes", 30) * 60
        follow_up_iv = SCHEDULE_CONFIG.get("follow_up_check_hours", 12) * 3600
        email_iv = 1800  # process email queue every 30 min
        report_hour = SCHEDULE_CONFIG.get("daily_report_hour", 20)

        self.logger.info(
            "Loop params: discover=%dm  follow_up=%dh  report@%d:00",
            discover_iv // 60,
            follow_up_iv // 3600,
            report_hour,
        )

        while self.running and not self._shutdown.is_set():
            today = datetime.now().strftime("%Y-%m-%d")
            cur_hour = datetime.now().hour
            now = time.time()

            # ── midnight reset ────────────────────────────────────────
            if self._last_reset_date != today:
                self._reset_daily_counts()
                self._last_reset_date = today

            # ── active-hours gate (8 AM – 11 PM) ─────────────────────
            if not self._is_active_hours():
                self.logger.debug("Outside active hours – sleeping 5 min")
                self._shutdown.wait(300)
                continue

            # ── DISCOVERY  +  APPLY ───────────────────────────────────
            if now - self._t_discover >= discover_iv:
                try:
                    d_stats = self.discover_cycle()
                    self.session["cycles"] += 1
                    self.logger.info("Discovery stats: %s", d_stats)
                except Exception as exc:
                    self._handle_error("discover_cycle", exc)
                self._t_discover = time.time()

                # apply immediately after discovery
                try:
                    a_stats = self.apply_cycle()
                    self.logger.info("Apply stats: %s", a_stats)
                except Exception as exc:
                    self._handle_error("apply_cycle", exc)
                self._t_apply = time.time()

            # ── EMAIL QUEUE ───────────────────────────────────────────
            if now - self._t_email >= email_iv:
                try:
                    self.email_cycle()
                except Exception as exc:
                    self._handle_error("email_cycle", exc)
                self._t_email = time.time()

            # ── FOLLOW-UPS ────────────────────────────────────────────
            if now - self._t_follow_up >= follow_up_iv:
                try:
                    f_stats = self.follow_up_cycle()
                    self.logger.info("Follow-up stats: %s", f_stats)
                except Exception as exc:
                    self._handle_error("follow_up_cycle", exc)
                self._t_follow_up = time.time()

            # ── DAILY REPORT (once, at report_hour) ───────────────────
            if cur_hour >= report_hour and self._last_report_date != today:
                try:
                    self.daily_report()
                except Exception as exc:
                    self._handle_error("daily_report", exc)
                self._last_report_date = today

            # ── sleep 60 s (interruptible) ────────────────────────────
            self._shutdown.wait(60)

    # ══════════════════════════════════════════════════════════════════
    #  DISCOVERY CYCLE  (FIXED — no double-dedup, no double-save)
    # ══════════════════════════════════════════════════════════════════
    def discover_cycle(self) -> dict:
        """
        Search all platforms → dedup → filter → save → score → queue.
        Returns {new_jobs, matched, queued, skipped, errors, time_s}.

        KEY FIX: manager.discover_all() already saves jobs to DB.
        We must NOT re-dedup against DB here — that would drop everything.
        Instead we:
          1. Tell manager to return raw jobs WITHOUT saving
             OR
          2. After manager saves, pick up status='new' jobs from DB
             and score them.

        We use approach 2: just score whatever is status='new' in DB.
        """
        t0 = time.time()
        stats = dict(
            new_jobs=0, matched=0, queued=0, skipped=0, errors=0, time_s=0.0
        )

        self.logger.info("═" * 55)
        self.logger.info("  DISCOVERY CYCLE")
        self.logger.info("═" * 55)

        # ─── STEP 1: Run platform discovery ───────────────────────────
        # manager.discover_all() searches, deduplicates, and saves to DB.
        # It returns the list of jobs it found, but they're ALREADY in DB.
        platform_count = 0
        if self.platform_manager:
            try:
                raw = self.platform_manager.discover_all() or []
                platform_count = len(raw)
                self.logger.info("Platforms returned %d jobs (already saved to DB by manager)", platform_count)
            except Exception as exc:
                self.logger.error("discover_all() failed: %s", exc)
                self._save_error("discover_all", exc)
                stats["errors"] += 1

        # ─── STEP 2: Score ALL unscored jobs in DB (status='new') ─────
        # This is the KEY difference from the old code.
        # Instead of re-deduping the returned list, we go straight to DB
        # and pick up everything that hasn't been scored yet.
        try:
            unscored_jobs = self.db.get_jobs(status="new", limit=50)
        except Exception as exc:
            self.logger.error("Failed to fetch unscored jobs: %s", exc)
            unscored_jobs = []

        if not unscored_jobs:
            self.logger.info("No unscored jobs to process")
            stats["time_s"] = round(time.time() - t0, 1)
            self.logger.info(
                "Discovery complete: new=%d matched=%d queued=%d "
                "skipped=%d errors=%d (%.1fs)",
                stats["new_jobs"], stats["matched"], stats["queued"],
                stats["skipped"], stats["errors"], stats["time_s"],
            )
            return stats

        stats["new_jobs"] = len(unscored_jobs)
        self.logger.info("Scoring %d unscored jobs from DB", len(unscored_jobs))

        # ─── STEP 3: AI scoring ──────────────────────────────────────
        min_score = MATCH_CONFIG.get("min_score_to_apply", 40)
        auto_score = MATCH_CONFIG.get("auto_apply_score", 70)

        for i, job in enumerate(unscored_jobs, 1):
            if not self.running:
                break

            job_title = (job.get("title") or "?")[:40]
            job_company = (job.get("company") or "?")[:25]
            job_id = job.get("id")

            try:
                # Parse skills if stored as JSON string
                skills_raw = job.get("skills")
                if isinstance(skills_raw, str):
                    try:
                        job["skills"] = json.loads(skills_raw)
                    except (json.JSONDecodeError, TypeError):
                        job["skills"] = []

                score_result = self._score_single_job(job)
                score = score_result.get("score", 0)
                rec = score_result.get("recommendation", "?")

                # Determine new status + priority
                new_status = "skipped"
                priority = 0
                if score >= auto_score:
                    new_status = "matched"
                    priority = 2  # high priority – auto-apply eligible
                    stats["matched"] += 1
                    stats["queued"] += 1
                elif score >= min_score:
                    new_status = "matched"
                    priority = 1  # normal – needs review / Telegram
                    stats["matched"] += 1
                    stats["queued"] += 1
                else:
                    stats["skipped"] += 1

                # Persist status + score
                score_result["_match_score"] = score
                score_result["_priority"] = priority
                self.db.update_job_status(
                    job_id, new_status, notes=json.dumps(score_result)
                )

                # Persist score through the public database API.
                # Do not access private connection attributes.
                try:
                    self.db.update_job_score(
                        job_id,
                        float(score),
                        details=score_result,
                    )
                except Exception as exc:
                    self.logger.warning(
                        "Could not persist match score for job %s: %s",
                        job_id,
                        exc,
                    )

                # Log progress
                if score >= auto_score:
                    tag = "🟢"
                elif score >= min_score:
                    tag = "🟡"
                else:
                    tag = "🔴"
                self.logger.info(
                    "  [%d/%d] %s %3.0f%%  %-40s  %-25s  %s",
                    i, len(unscored_jobs), tag, score,
                    job_title, job_company, rec,
                )

                # Telegram notification for strong matches
                if score >= auto_score and self.notifier:
                    try:
                        self.notifier.send_match(job, score)
                    except Exception:
                        pass

                if score >= min_score:
                    self.session["matched"] += 1

                # Rate limit pause for AI calls (4.5s to stay under 15 RPM)
                if i < len(unscored_jobs):
                    time.sleep(4.5)

            except Exception as exc:
                self.logger.debug(
                    "Scoring job '%s' failed: %s", job_title, exc,
                )
                stats["errors"] += 1

        stats["time_s"] = round(time.time() - t0, 1)
        self.logger.info(
            "Discovery complete: new=%d matched=%d queued=%d "
            "skipped=%d errors=%d (%.1fs)",
            stats["new_jobs"],
            stats["matched"],
            stats["queued"],
            stats["skipped"],
            stats["errors"],
            stats["time_s"],
        )
        self.session["discovered"] += stats["new_jobs"]
        return stats

    # ══════════════════════════════════════════════════════════════════
    #  APPLY CYCLE
    # ══════════════════════════════════════════════════════════════════
    def apply_cycle(self) -> dict:
        """
        Take top-N matched jobs → tailor resume → approve → apply.
        Returns {attempted, successful, failed, skipped, time_s}.
        """
        t0 = time.time()
        stats = dict(
            attempted=0, successful=0, failed=0, skipped=0, time_s=0.0
        )

        self.logger.info("═" * 55)
        self.logger.info("  APPLY CYCLE")
        self.logger.info("═" * 55)

        batch_size = SCHEDULE_CONFIG.get("apply_batch_size", 10)
        auto_score = MATCH_CONFIG.get("auto_apply_score", 70)
        approve_before = TELEGRAM_CONFIG.get("approve_before_apply", True)
        approve_timeout = TELEGRAM_CONFIG.get("approve_timeout_minutes", 30)

        # get matched jobs ordered by priority desc, score desc
        try:
            queued = self.db.get_jobs(status="matched", limit=batch_size * 2)
        except Exception as exc:
            self.logger.error("get_jobs(matched) failed: %s", exc)
            stats["time_s"] = round(time.time() - t0, 1)
            return stats

        if not queued:
            self.logger.info("No matched jobs in queue")
            stats["time_s"] = round(time.time() - t0, 1)
            return stats

        # sort by priority desc, then match_score desc
        queued.sort(
            key=lambda j: (
                j.get("priority", 0) or 0,
                j.get("match_score", 0) or 0,
            ),
            reverse=True,
        )
        queued = queued[:batch_size]

        self.logger.info("Processing %d jobs from queue", len(queued))

        for job in queued:
            if not self.running:
                break

            job_label = (
                f"{job.get('title', '?')} @ {job.get('company', '?')}"
            )
            job_id = job.get("id")
            platform = job.get("platform", "")
            score = job.get("match_score", 0) or 0

            # if score not populated in column, try parsing from notes
            if score == 0:
                try:
                    notes = json.loads(job.get("notes", "{}") or "{}")
                    score = notes.get("_match_score", notes.get("score", 0))
                except Exception:
                    pass

            self.logger.info(
                "─" * 50 + "\nJob: %s  (score=%s, platform=%s)",
                job_label,
                score,
                platform,
            )
            stats["attempted"] += 1

            # 1 ── check platform can accept more today ────────────────
            if platform in _SEARCH_ONLY_PLATFORMS:
                self.logger.info(
                    "  → %s is search-only, skipping apply", platform
                )
                stats["skipped"] += 1
                continue

            if not self._platform_can_apply(platform):
                self.logger.info(
                    "  → %s daily limit reached, skipping", platform
                )
                stats["skipped"] += 1
                continue

            # 2 ── tailor resume ───────────────────────────────────────
            resume_path = None
            cover_letter_text = ""
            try:
                resume_path, cover_letter_text = self._tailor_and_build(job)
                self.logger.info("  ✔ Resume tailored: %s", resume_path)
            except Exception as exc:
                self.logger.warning(
                    "  ✘ Tailoring failed (%s) – using base resume", exc
                )
                resume_path = RESUME_CONFIG.get("base_resume_path", "")

            # 3 ── approval gate ───────────────────────────────────────
            approved = False
            if score >= auto_score:
                # auto-approve for high-score jobs
                approved = True
                self.logger.info(
                    "  → Auto-approved (score=%s ≥ %s)", score, auto_score
                )
            elif self.notifier and approve_before:
                # ask Telegram
                try:
                    approved = self._request_approval(
                        job, resume_path, approve_timeout
                    )
                except Exception as exc:
                    self.logger.warning(
                        "  ⚠ Approval request failed (%s) – auto-approving",
                        exc,
                    )
                    approved = True  # fail-open after error
            else:
                # no notifier → auto-approve
                approved = True
                self.logger.info("  → Auto-approved (no Telegram)")

            if not approved:
                self.logger.info("  ✘ User rejected – skipping")
                self.db.update_job_status(
                    job_id, "skipped", notes="User rejected via Telegram"
                )
                stats["skipped"] += 1
                continue

            # 4 ── apply via platform manager ──────────────────────────
            apply_result = self._apply_single_job(
                job, resume_path, cover_letter_text
            )

            if apply_result.get("success"):
                self.logger.info("  ✔ Applied successfully")
                stats["successful"] += 1
                self.session["applied"] += 1
                self.db.update_job_status(job_id, "applied")

                # save application record
                app_id = self._save_application(
                    job, resume_path, cover_letter_text, apply_result
                )

                # Telegram notification
                if self.notifier:
                    try:
                        self.notifier.send_applied(job, apply_result)
                    except Exception:
                        pass

                # 5 ── email HR for high-match jobs ────────────────────
                if score >= 80:
                    self._try_email_hr(
                        job, resume_path, cover_letter_text, app_id
                    )

                # 6 ── schedule follow-ups ─────────────────────────────
                if app_id and self.follow_up_sched:
                    try:
                        follow_days = EMAIL_CONFIG.get(
                            "follow_up_schedule", [3, 7, 14]
                        )
                        self.follow_up_sched.schedule(app_id, follow_days)
                        self.logger.info(
                            "  ✔ Follow-ups scheduled: D%s", follow_days
                        )
                    except Exception as exc:
                        self.logger.debug(
                            "Follow-up schedule failed: %s", exc
                        )

            else:
                self.logger.warning(
                    "  ✘ Apply failed: %s",
                    apply_result.get("error", "unknown"),
                )
                stats["failed"] += 1
                # Mark as 'apply_failed' so we don't retry forever
                self.db.update_job_status(
                    job_id,
                    "skipped",
                    notes=f"Apply failed: {apply_result.get('error', '')}",
                )

            # inter-job delay (human-like)
            delay_range = STEALTH_CONFIG.get("random_delay_range", (3, 12))
            if not isinstance(delay_range, (list, tuple)) or len(delay_range) < 2:
                delay_range = (3, 12)
            delay = random.uniform(delay_range[0], delay_range[1])
            self.logger.debug("  … sleeping %.1fs before next job", delay)
            time.sleep(delay)

        stats["time_s"] = round(time.time() - t0, 1)
        self.logger.info(
            "Apply complete: attempted=%d success=%d fail=%d skip=%d (%.1fs)",
            stats["attempted"],
            stats["successful"],
            stats["failed"],
            stats["skipped"],
            stats["time_s"],
        )
        return stats

    # ══════════════════════════════════════════════════════════════════
    #  EMAIL CYCLE
    # ══════════════════════════════════════════════════════════════════
    def email_cycle(self) -> dict:
        """Process any queued outreach emails (business hours only)."""
        stats = dict(sent=0, failed=0, skipped=0)

        if not self.email_sender:
            return stats

        if not self._is_business_hours():
            self.logger.debug("Outside business hours – skipping email queue")
            return stats

        self.logger.info("Processing email queue …")
        try:
            result = self.email_sender.process_queue()
            if isinstance(result, dict):
                stats.update(result)
            self.logger.info("Email queue result: %s", stats)
        except Exception as exc:
            self.logger.warning("Email queue processing failed: %s", exc)
            self._save_error("email_cycle", exc)
            stats["failed"] += 1

        self.session["emailed"] += stats.get("sent", 0)
        return stats

    # ══════════════════════════════════════════════════════════════════
    #  FOLLOW-UP CYCLE
    # ══════════════════════════════════════════════════════════════════
    def follow_up_cycle(self) -> dict:
        """Check and send due follow-up emails."""
        stats = dict(due=0, sent=0, failed=0)

        if not self.follow_up_sched:
            return stats
        if not self._is_business_hours():
            return stats

        self.logger.info("Checking follow-ups …")

        try:
            result = self.follow_up_sched.process()
            if isinstance(result, dict):
                stats.update(result)
            self.logger.info("Follow-up result: %s", stats)
        except Exception as exc:
            self.logger.warning("Follow-up processing failed: %s", exc)
            self._save_error("follow_up_cycle", exc)

        return stats

    # ══════════════════════════════════════════════════════════════════
    #  DAILY REPORT
    # ══════════════════════════════════════════════════════════════════
    def daily_report(self) -> dict:
        """Generate daily stats and send via Telegram."""
        self.logger.info("Generating daily report …")

        # Gather stats from DB
        report_data: Dict[str, Any] = {}
        try:
            report_data = self.db.get_stats("today") or {}
        except Exception as exc:
            self.logger.debug("get_stats failed: %s", exc)

        # Add session stats
        report_data["session"] = dict(self.session)

        # Try ReportGenerator for richer stats
        if self.report_gen:
            try:
                rich_report = self.report_gen.daily_report()
                if isinstance(rich_report, dict):
                    report_data.update(rich_report)
            except Exception as exc:
                self.logger.debug("ReportGenerator failed: %s", exc)

        # Build message
        sess = report_data.get("session", {})
        today_applied = report_data.get("today_applied", sess.get("applied", 0))
        today_discovered = report_data.get(
            "today_discovered", sess.get("discovered", 0)
        )
        today_responses = report_data.get("today_responses", 0)
        total_applied = report_data.get("total_applied", 0)
        pending_follow = report_data.get("pending_follow_ups", 0)

        msg = (
            f"📊 *Daily Report* — {datetime.now().strftime('%d %b %Y')}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔍 Discovered today: {today_discovered}\n"
            f"📝 Applied today: {today_applied}\n"
            f"💬 Responses today: {today_responses}\n"
            f"📨 Total applied: {total_applied}\n"
            f"⏳ Pending follow-ups: {pending_follow}\n"
            f"⚡ Session cycles: {self.session['cycles']}\n"
            f"❌ Errors today: {self.session['errors']}\n"
        )

        # Pipeline funnel
        try:
            pipeline = self.db.get_pipeline()
            if pipeline:
                msg += (
                    f"\n📈 *Pipeline*\n"
                    f"New → Matched → Applied → Response\n"
                    f"{pipeline.get('new', 0)} → {pipeline.get('matched', 0)} → "
                    f"{pipeline.get('applied', 0)} → {pipeline.get('response', 0)}\n"
                )
        except Exception:
            pass

        # Send via Telegram
        if self.notifier:
            try:
                self.notifier.send_daily_report(report_data)
            except Exception:
                # fallback: send as plain message
                self._safe_notify(msg)
        else:
            self.logger.info("Daily report (no Telegram):\n%s", msg)

        self.logger.info("Daily report sent")
        return report_data

    # ══════════════════════════════════════════════════════════════════
    #  HELPER: Score a single job
    # ══════════════════════════════════════════════════════════════════
    def _score_single_job(self, job: dict) -> dict:
        """Score a job using AI matcher, falling back to heuristic."""
        profile = USER_PROFILE or {}

        # Try AI matcher
        if self.matcher:
            try:
                result = self.matcher.score_job(job, profile)
                if isinstance(result, dict) and "score" in result:
                    return result
            except Exception as exc:
                self.logger.debug(
                    "AI scoring failed (%s) – heuristic fallback", exc
                )

        # Heuristic scoring fallback
        return self._heuristic_score(job, profile)

    def _heuristic_score(self, job: dict, profile: dict) -> dict:
        """
        Quick keyword-based scoring when AI is unavailable.
        Returns {score: 0-100, reasoning: str, ...}
        """
        score = 0.0
        reasons: List[str] = []

        title = (job.get("title") or "").lower()
        desc = (job.get("description") or "").lower()
        location = (job.get("location") or "").lower()

        # Title match (25 pts)
        target_titles = [t.lower() for t in profile.get("target_titles", [])]
        for tt in target_titles:
            tt_words = tt.split()
            if all(w in title for w in tt_words):
                score += 25
                reasons.append(f"Title match: {tt}")
                break
            elif any(w in title for w in tt_words if len(w) > 3):
                score += 12
                reasons.append(f"Partial title match: {tt}")
                break

        # Skills match (30 pts)
        user_skills = [s.lower() for s in profile.get("skills", [])]
        job_skills_raw = job.get("skills", [])
        if isinstance(job_skills_raw, str):
            try:
                job_skills_raw = json.loads(job_skills_raw)
            except Exception:
                job_skills_raw = [
                    s.strip() for s in job_skills_raw.split(",")
                ]

        job_skills = [s.lower() for s in (job_skills_raw or [])]
        # also scan description for skills
        all_job_text = " ".join(job_skills) + " " + desc
        matched_skills = [s for s in user_skills if s in all_job_text]
        skill_ratio = len(matched_skills) / max(len(user_skills), 1)
        skill_pts = min(30, skill_ratio * 40)
        score += skill_pts
        if matched_skills:
            reasons.append(f"Skills: {', '.join(matched_skills[:5])}")

        # Location match (15 pts)
        target_locs = [loc.lower() for loc in profile.get("target_locations", [])]
        for tl in target_locs:
            if tl in location or location in tl:
                score += 15
                reasons.append(f"Location: {tl}")
                break
        if "remote" in location:
            score += 15
            reasons.append("Remote OK")

        # Experience match (15 pts)
        exp_min = job.get("experience_min", 0) or 0
        exp_max = job.get("experience_max", 99) or 99
        user_exp = profile.get("experience_years", 1)
        if exp_min <= user_exp <= exp_max:
            score += 15
            reasons.append("Experience in range")
        elif user_exp < exp_min and (exp_min - user_exp) <= 1:
            score += 8
            reasons.append("Slightly under experience")

        # Salary (10 pts)
        sal_min = job.get("salary_min", 0) or 0
        user_min_sal = profile.get("min_salary", 5)
        if sal_min >= user_min_sal or sal_min == 0:
            score += 10
            reasons.append("Salary OK")

        # Cap at 100
        score = min(100, max(0, round(score)))

        if score >= 70:
            rec = "strong"
        elif score >= 55:
            rec = "good"
        elif score >= 40:
            rec = "partial"
        else:
            rec = "weak"

        return {
            "score": score,
            "recommendation": rec,
            "reasoning": "; ".join(reasons) if reasons else "Low match",
            "skills_found": matched_skills[:10],
            "skills_missing": [],
            "method": "heuristic",
        }

    # ══════════════════════════════════════════════════════════════════
    #  HELPER: Tailor resume + build file
    # ══════════════════════════════════════════════════════════════════
    def _tailor_and_build(self, job: dict) -> Tuple[str, str]:
        """
        Tailor the base resume for this job and build DOCX.
        Returns (resume_path, cover_letter_text).
        """
        resume_path = RESUME_CONFIG.get("base_resume_path", "")
        cover_letter = ""
        tailor_mode = RESUME_CONFIG.get("tailor_mode", "light")

        # Tailor
        tailored_data = self.base_resume
        if self.tailor and self.base_resume:
            try:
                tailored_data = self.tailor.tailor(
                    self.base_resume, job, tailor_mode
                )
                self.logger.debug("  Resume tailored (mode=%s)", tailor_mode)
            except Exception as exc:
                self.logger.debug("  Tailoring failed: %s – using base", exc)
                tailored_data = self.base_resume

        # Build DOCX
        if self.builder and tailored_data:
            try:
                resume_path = self.builder.build_docx(tailored_data)
                self.logger.debug("  DOCX built: %s", resume_path)
            except Exception as exc:
                self.logger.debug(
                    "  DOCX build failed: %s – using base", exc
                )

        # Cover letter
        if self.cover_gen and RESUME_CONFIG.get("include_cover_letter", True):
            try:
                cover_letter = self.cover_gen.generate(
                    job, USER_PROFILE, tone="professional"
                )
                self.logger.debug(
                    "  Cover letter generated (%d chars)", len(cover_letter)
                )
            except Exception as exc:
                self.logger.debug(
                    "  Cover letter generation failed: %s", exc
                )

        return resume_path, cover_letter

    # ══════════════════════════════════════════════════════════════════
    #  HELPER: Telegram approval
    # ══════════════════════════════════════════════════════════════════
    def _request_approval(
        self, job: dict, resume_path: str, timeout_min: int
    ) -> bool:
        """
        Send Telegram approval request.  Returns True if approved
        or auto-approved on timeout.
        """
        if not self.notifier:
            return True

        try:
            callback_id = self.notifier.send_approval_request(
                job, resume_path
            )
            if not callback_id:
                self.logger.debug("  No callback_id – auto-approving")
                return True

            self.logger.info(
                "  ⏳ Waiting for Telegram approval (timeout=%dm) …",
                timeout_min,
            )
            result = self.notifier.wait_for_approval(
                callback_id, timeout=timeout_min * 60
            )

            if result is True or result == "approve":
                self.logger.info("  ✅ User approved")
                return True
            elif result is False or result == "skip":
                self.logger.info("  ❌ User rejected")
                return False
            else:
                # Timeout is the configured normal-flow auto-approval.
                self.logger.info(
                    "  ⏰ Timeout / unknown (%s) – auto-approving", result
                )
                return True
        except Exception as exc:
            self.logger.warning(
                "  Approval flow error: %s – skipping safely", exc
            )
            return False

    # ══════════════════════════════════════════════════════════════════
    #  HELPER: Apply to a single job
    # ══════════════════════════════════════════════════════════════════
    def _apply_single_job(
        self, job: dict, resume_path: str, cover_letter: str
    ) -> dict:
        """
        Route application to the correct platform via PlatformManager.
        Returns {success: bool, method: str, error: str?}.
        """
        if not self.platform_manager:
            return {
                "success": False,
                "error": "PlatformManager unavailable",
                "method": "none",
            }

        try:
            result = self.platform_manager.apply_to_job(
                job,
                resume_path=resume_path,
                cover_letter=cover_letter,
            )
            if isinstance(result, dict):
                return result
            # if it returns True/False
            return {
                "success": bool(result),
                "method": "platform",
                "error": "" if result else "Unknown",
            }
        except Exception as exc:
            err_msg = str(exc)
            self.logger.error("  apply_to_job exception: %s", err_msg)
            self._save_error("apply_single_job", exc)

            # check for ban / captcha indicators
            err_lower = err_msg.lower()
            ban_keywords = (
                "captcha",
                "unusual activity",
                "blocked",
                "banned",
            )
            if any(kw in err_lower for kw in ban_keywords):
                platform = job.get("platform", "unknown")
                if self.notifier:
                    try:
                        self.notifier.send_platform_issue(platform, err_msg)
                    except Exception:
                        pass

            return {"success": False, "error": err_msg, "method": "platform"}

    # ══════════════════════════════════════════════════════════════════
    #  HELPER: Save application record to DB
    # ══════════════════════════════════════════════════════════════════
    def _save_application(
        self,
        job: dict,
        resume_path: str,
        cover_letter: str,
        apply_result: dict,
    ) -> Optional[int]:
        """Save an application to the DB. Returns application_id or None."""
        try:
            app_data = {
                "job_id": job.get("id"),
                "platform": job.get("platform", ""),
                "method": apply_result.get("method", "quick_apply"),
                "resume_version": resume_path or "",
                "cover_letter": (cover_letter[:2000] if cover_letter else ""),
                "tailoring_mode": RESUME_CONFIG.get("tailor_mode", "light"),
                "applied_at": datetime.now().isoformat(),
                "status": "submitted",
                "follow_up_count": 0,
                "notes": json.dumps(apply_result)[:500],
            }
            app_id = self.db.save_application(app_data)
            self.logger.debug("  Application saved: id=%s", app_id)
            return app_id
        except Exception as exc:
            self.logger.warning("  save_application failed: %s", exc)
            return None

    # ══════════════════════════════════════════════════════════════════
    #  HELPER: Email HR for high-match jobs
    # ══════════════════════════════════════════════════════════════════
    def _try_email_hr(
        self,
        job: dict,
        resume_path: str,
        cover_letter: str,
        app_id: Optional[int],
    ):
        """Find HR email and queue application email."""
        if not self.email_sender or not self.email_finder:
            return
        if not EMAIL_CONFIG.get("enabled", True):
            return

        company = job.get("company", "")
        if not company:
            return

        self.logger.info("  📧 Attempting HR email for %s …", company)

        try:
            contacts = self.email_finder.find_hr_email(company)
            if not contacts:
                self.logger.debug("  No HR email found for %s", company)
                return

            # use first verified or highest-confidence contact
            contact = contacts[0]
            hr_email = contact.get("email", "")
            if not hr_email:
                return

            self.logger.info(
                "  Found HR: %s (%s)", hr_email, contact.get("name", "")
            )

            # save contact
            try:
                self.db.save_contact(
                    {
                        "company": company,
                        "name": contact.get("name", ""),
                        "title": contact.get("title", ""),
                        "email": hr_email,
                        "source": contact.get("source", "finder"),
                        "verified": 1 if contact.get("verified") else 0,
                    }
                )
            except Exception:
                pass

            # generate email body if we have cover letter gen
            email_body = cover_letter
            if self.cover_gen:
                try:
                    email_body = self.cover_gen.generate_email_body(
                        job, USER_PROFILE, "application"
                    )
                except Exception:
                    pass

            # queue the email
            title = job.get("title", "Software Engineer")
            applicant_name = USER_PROFILE.get("name", "Applicant")
            subject = f"Application for {title} – {applicant_name}"

            email_data = {
                "application_id": app_id,
                "email_type": "application",
                "to_email": hr_email,
                "subject": subject,
                "body": email_body,
                "attachments": (
                    json.dumps([resume_path]) if resume_path else "[]"
                ),
                "status": "queued",
            }
            self.db.save_email(email_data)
            self.logger.info("  ✔ HR email queued: %s", hr_email)
            self.session["emailed"] += 1

        except Exception as exc:
            self.logger.debug("  HR email attempt failed: %s", exc)

    # ══════════════════════════════════════════════════════════════════
    #  HELPER: Normalize raw platform job → DB format
    # ══════════════════════════════════════════════════════════════════
    def _normalize_job(self, job: dict) -> dict:
        """Convert raw platform scrape data into DB-ready dict."""
        sal_min, sal_max = self._parse_salary(job.get("salary_text", ""))
        exp_min, exp_max = self._parse_experience(
            job.get("experience_text", "")
        )

        skills = job.get("skills", [])
        if isinstance(skills, list):
            skills_json = json.dumps(skills)
        elif isinstance(skills, str):
            skills_json = skills
        else:
            skills_json = "[]"

        return {
            "platform": job.get("platform", "unknown"),
            "platform_job_id": job.get("platform_job_id", ""),
            "url": job.get("url", ""),
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "salary_min": sal_min,
            "salary_max": sal_max,
            "currency": "INR",
            "experience_min": exp_min,
            "experience_max": exp_max,
            "job_type": job.get("job_type", ""),
            "work_mode": job.get("work_mode", ""),
            "description": (job.get("description") or "")[:10000],
            "skills": skills_json,
            "posted_date": job.get("posted_date", ""),
            "discovered_at": datetime.now().isoformat(),
            "match_score": 0,
            "match_details": "{}",
            "status": "new",
            "priority": 0,
            "notes": "",
        }

    # ══════════════════════════════════════════════════════════════════
    #  HELPER: Parse salary text → (min_lpa, max_lpa)
    # ══════════════════════════════════════════════════════════════════
    @staticmethod
    def _parse_salary(text: str) -> Tuple[float, float]:
        if not text or not isinstance(text, str):
            return 0.0, 0.0

        text = (
            text.strip()
            .lower()
            .replace(",", "")
            .replace("₹", "")
            .replace("inr", "")
        )

        m = re.search(
            r"(\d+\.?\d*)\s*(?:lpa|l|lac|lakh)?\s*[-–to]+\s*"
            r"(\d+\.?\d*)\s*(?:lpa|l|lac|lakh)",
            text,
        )
        if m:
            return float(m.group(1)), float(m.group(2))

        m = re.search(
            r"(\d+\.?\d*)\s*[-–to]+\s*(\d+\.?\d*)\s*(lpa|l|lac|lakh)", text
        )
        if m:
            return float(m.group(1)), float(m.group(2))

        m = re.search(r"(\d+\.?\d*)\s*(lpa|l|lac|lakh)", text)
        if m:
            val = float(m.group(1))
            return val, val

        m = re.search(r"(\d{5,})\s*[-–to]+\s*(\d{5,})", text)
        if m:
            return round(float(m.group(1)) / 100000, 2), round(
                float(m.group(2)) / 100000, 2
            )

        m = re.search(r"(\d{5,})", text)
        if m:
            val = round(float(m.group(1)) / 100000, 2)
            return val, val

        return 0.0, 0.0

    @staticmethod
    def _parse_experience(text: str) -> Tuple[float, float]:
        if not text or not isinstance(text, str):
            return 0.0, 0.0

        text = text.strip().lower()

        if "fresher" in text:
            return 0.0, 1.0

        m = re.search(
            r"(\d+\.?\d*)\s*[-–to]+\s*(\d+\.?\d*)\s*(year|yr)", text
        )
        if m:
            return float(m.group(1)), float(m.group(2))

        m = re.search(r"(\d+\.?\d*)\s*(year|yr)", text)
        if m:
            val = float(m.group(1))
            return max(0, val - 1), val + 1

        m = re.search(r"(\d+)", text)
        if m:
            val = float(m.group(1))
            return max(0, val - 1), val + 1

        return 0.0, 0.0

    def _platform_can_apply(self, platform: str) -> bool:
        try:
            session = self.db.get_platform_session(platform)
            if not session:
                return True
            if session.get("status") in ("cooldown", "banned", "disabled"):
                return False
            daily = session.get("daily_applied", 0)
            plat_cfg = PLATFORM_CONFIG.get(platform, {})
            max_daily = plat_cfg.get("max_daily_applications", 25)
            return daily < max_daily
        except Exception:
            return True

    @staticmethod
    def _is_active_hours() -> bool:
        hour = datetime.now().hour
        return 8 <= hour <= 23

    @staticmethod
    def _is_business_hours() -> bool:
        hour = datetime.now().hour
        return 9 <= hour <= 18

    def _reset_daily_counts(self):
        self.logger.info("Midnight reset – clearing daily counts")
        try:
            self.db.reset_daily_counts()
        except Exception as exc:
            self.logger.debug("reset_daily_counts failed: %s", exc)
        self.session["errors"] = 0

    def _handle_error(self, module: str, exc: Exception):
        self.session["errors"] += 1
        self.logger.error("%s error: %s", module, exc, exc_info=True)
        self._save_error(module, exc)
        if self.notifier:
            try:
                self.notifier.send_error(module, str(exc))
            except Exception:
                pass

    def _save_error(self, module: str, exc: Exception):
        try:
            self.db.save_error(
                module=module,
                error_type=type(exc).__name__,
                message=str(exc),
                traceback=tb_module.format_exc(),
            )
        except Exception:
            pass

    def _safe_notify(self, message: str):
        if not self.notifier:
            return
        try:
            if hasattr(self.notifier, "send_message"):
                self.notifier.send_message(message)
            else:
                self.notifier.send_error("system", message)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
#  STANDALONE TEST
# ═══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("  JobMonitor – standalone test")
    print("=" * 60)

    monitor = JobMonitor()
    monitor._init_components()

    print("\n--- Component status ---")
    components = {
        "PlatformManager": monitor.platform_manager,
        "LLM": monitor.llm,
        "Matcher": monitor.matcher,
        "Tailor": monitor.tailor,
        "CoverLetter": monitor.cover_gen,
        "Builder": monitor.builder,
        "Optimiser": monitor.optimizer,
        "Notifier": monitor.notifier,
        "EmailSender": monitor.email_sender,
        "EmailFinder": monitor.email_finder,
        "FollowUpScheduler": monitor.follow_up_sched,
        "ReportGen": monitor.report_gen,
        "Dedup": monitor.dedup,
        "JobFilter": monitor.job_filter,
        "BaseResume": monitor.base_resume,
        "Preferences": monitor.preferences,
    }
    for name, obj in components.items():
        sym = "✔" if obj else "✘"
        print(f"  {sym} {name}")

    # Test parsers
    print("\n--- Salary parser ---")
    salary_tests = [
        "5-10 LPA",
        "₹5,00,000 - ₹10,00,000",
        "5L - 10L",
        "Not disclosed",
        "800000",
    ]
    for t in salary_tests:
        print(f"  '{t}' → {JobMonitor._parse_salary(t)}")

    print("\n--- Experience parser ---")
    exp_tests = ["1-3 years", "0-2 Yrs", "Fresher", "5 years"]
    for t in exp_tests:
        print(f"  '{t}' → {JobMonitor._parse_experience(t)}")

    # Test heuristic scoring
    print("\n--- Heuristic scorer ---")
    dummy_job = {
        "title": "Full Stack Developer",
        "company": "TestCo",
        "location": "Bangalore",
        "description": "Looking for Java Spring Boot Node.js React developer",
        "skills": ["Java", "Spring Boot", "React", "Node.js"],
        "salary_min": 8,
        "salary_max": 15,
        "experience_min": 0,
        "experience_max": 3,
    }
    score_result = monitor._heuristic_score(dummy_job, USER_PROFILE)
    print(f"  Score: {score_result.get('score')} ({score_result.get('recommendation')})")
    print(f"  Reasoning: {score_result.get('reasoning')}")

    print("\n--- Checking unscored jobs in DB ---")
    db = get_db()
    unscored = db.get_jobs(status="new", limit=5)
    print(f"  {len(unscored)} unscored (showing first 5)")
    for j in unscored[:5]:
        print(f"    id={j.get('id')} | {j.get('title','?')[:35]} | {j.get('company','?')[:20]}")

    print("\nDone. To start full agent: JobMonitor().start()")