"""
platforms/manager.py — Orchestrates all job platforms (round-robin).

Provides a single interface to:
  - Login to all enabled platforms
  - Discover jobs across all platforms (deduplicated)
  - Route applications to the correct platform
  - Track per-platform health, daily counts, cooldowns
  - Refresh stale sessions
  - Enforce session breaks and rate limits

Usage:
    from platforms.manager import PlatformManager

    manager = PlatformManager()
    manager.login_all()

    # Discover across all platforms
    jobs = manager.discover_all()

    # Apply to a specific job (routes to correct platform)
    result = manager.apply_to_job(job, "/path/resume.pdf")

    # Check status
    status = manager.get_status()

Prerequisites:
    config.py, core/logger.py, core/db.py, core/browser.py,
    platforms/base.py, and at least one platform file.
"""

import json
import time
import random
import traceback as tb_module
from datetime import datetime
from typing import Optional, List, Dict, Any

from config import PLATFORM_CONFIG, USER_PROFILE, STEALTH_CONFIG
from core.logger import get_logger
from core.db import get_db
from core.browser import BrowserEngine, get_browser_engine

logger = get_logger("platforms.manager")


# ═══════════════════════════════════════════════════════════════════
# PLATFORM IMPORTS — graceful, each platform is optional
# ═══════════════════════════════════════════════════════════════════

_PLATFORM_CLASSES: Dict[str, Any] = {}

try:
    from platforms.naukri import NaukriPlatform
    _PLATFORM_CLASSES["naukri"] = NaukriPlatform
    logger.debug("Naukri platform loaded")
except ImportError as e:
    logger.debug(f"Naukri platform not available: {e}")

try:
    from platforms.foundit import FounditPlatform
    _PLATFORM_CLASSES["foundit"] = FounditPlatform
    logger.debug("Foundit platform loaded")
except ImportError as e:
    logger.debug(f"Foundit platform not available: {e}")

try:
    from platforms.indeed import IndeedPlatform
    _PLATFORM_CLASSES["indeed"] = IndeedPlatform
    logger.debug("Indeed platform loaded")
except ImportError as e:
    logger.debug(f"Indeed platform not available: {e}")

try:
    from platforms.linkedin import LinkedInPlatform
    _PLATFORM_CLASSES["linkedin"] = LinkedInPlatform
    logger.debug("LinkedIn platform loaded")
except ImportError as e:
    logger.debug(f"LinkedIn platform not available: {e}")


# ═══════════════════════════════════════════════════════════════════
# SIMPLE DEDUPLICATION (cross-platform)
# ═══════════════════════════════════════════════════════════════════

def _normalize(text: str) -> str:
    """Lowercase, strip, collapse whitespace."""
    import re
    if not text:
        return ""
    return re.sub(r'\s+', ' ', text.strip().lower())


def _dedup_key(job: Dict) -> str:
    """
    Generate a dedup key from company + title.
    Same company + same title (fuzzy) = duplicate.
    """
    company = _normalize(job.get("company", ""))
    title = _normalize(job.get("title", ""))
    # Remove common suffixes that vary across platforms
    for suffix in [" pvt ltd", " pvt. ltd.", " private limited",
                   " limited", " ltd", " inc", " inc.",
                   " technologies", " tech", " solutions",
                   " software", " services", " india"]:
        company = company.replace(suffix, "")
    return f"{company}|||{title}"


def _deduplicate_jobs(jobs: List[Dict]) -> List[Dict]:
    """
    Remove cross-platform duplicates.

    Keeps the first occurrence (preserves platform priority order).
    Uses company+title as the dedup key.
    Also deduplicates by URL.
    """
    seen_keys = set()
    seen_urls = set()
    unique = []

    for job in jobs:
        url = job.get("url", "")
        key = _dedup_key(job)

        # Skip if same URL already seen
        if url and url in seen_urls:
            continue

        # Skip if same company+title already seen
        if key in seen_keys and key != "|||":
            continue

        if url:
            seen_urls.add(url)
        if key != "|||":
            seen_keys.add(key)
        unique.append(job)

    return unique


# ═══════════════════════════════════════════════════════════════════
# PLATFORM MANAGER
# ═══════════════════════════════════════════════════════════════════

class PlatformManager:
    """
    Orchestrates all job platforms.

    Manages browser engine lifecycle, platform instances, login state,
    discovery round-robin, application routing, and health tracking.

    Platform priority order (for discovery):
        1. Naukri   (most jobs in India)
        2. Foundit  (good variety)
        3. Indeed   (international + India)
        4. LinkedIn (search only, no apply)

    Usage:
        manager = PlatformManager()
        manager.login_all()
        jobs = manager.discover_all()
        result = manager.apply_to_job(jobs[0], "resume.pdf")
        status = manager.get_status()
    """

    # Platform priority order for round-robin discovery
    PLATFORM_ORDER = ["naukri", "foundit", "indeed", "linkedin"]

    def __init__(self, browser_engine: Optional[BrowserEngine] = None,
                 notifier=None):
        """
        Initialize PlatformManager.

        Args:
            browser_engine: Shared BrowserEngine instance.
                            None → creates/uses the singleton.
            notifier: Optional Telegram notifier (for OTP/CAPTCHA/approval).
        """
        self.browser = browser_engine or get_browser_engine()
        self.notifier = notifier
        self._platforms: Dict[str, Any] = {}  # name → platform instance
        self._last_discover: Dict[str, float] = {}  # name → timestamp
        self._errors: Dict[str, int] = {}  # name → consecutive error count

        # Initialize enabled platforms
        self._init_platforms()

        logger.info(
            f"PlatformManager initialized: "
            f"{list(self._platforms.keys())} "
            f"({len(self._platforms)} active)"
        )

    def _init_platforms(self) -> None:
        """Create platform instances for all enabled platforms."""
        for name in self.PLATFORM_ORDER:
            config = PLATFORM_CONFIG.get(name, {})

            # Skip if disabled in config
            if not config.get("enabled", True):
                logger.debug(f"Platform '{name}' disabled in config")
                continue

            # Skip if class not available (not built yet)
            cls = _PLATFORM_CLASSES.get(name)
            if cls is None:
                logger.debug(
                    f"Platform '{name}' class not available "
                    f"(module not built yet)"
                )
                continue

            # Create instance
            try:
                instance = cls(self.browser, notifier=self.notifier)
                self._platforms[name] = instance
                self._errors[name] = 0
                logger.debug(f"Platform '{name}' initialized")
            except Exception as e:
                logger.error(
                    f"Failed to initialize platform '{name}': {e}"
                )

    # ═══════════════════════════════════════════════════════════
    # LOGIN
    # ═══════════════════════════════════════════════════════════

    def login_all(self) -> Dict[str, bool]:
        """
        Log in to all enabled platforms.

        Attempts each platform sequentially.  If one fails, continues
        to the next (graceful degradation).

        Returns:
            {platform_name: login_success_bool}
        """
        results = {}

        for name, platform in self._platforms.items():
            logger.info(f"Logging in to '{name}'...")
            try:
                success = platform.login()
                results[name] = success

                if success:
                    logger.info(f"  ✓ '{name}' login OK")
                    self._errors[name] = 0
                else:
                    logger.warning(f"  ✗ '{name}' login failed")
                    self._errors[name] = self._errors.get(name, 0) + 1

                # Delay between platform logins (anti-detection)
                self.browser.random_delay(2.0, 5.0)

            except Exception as e:
                logger.error(f"  ✗ '{name}' login error: {e}")
                results[name] = False
                self._errors[name] = self._errors.get(name, 0) + 1
                self._save_error(f"login_all.{name}", e)

        logged_in = [k for k, v in results.items() if v]
        failed = [k for k, v in results.items() if not v]

        logger.info(
            f"Login complete: {len(logged_in)} OK "
            f"({', '.join(logged_in) or 'none'}), "
            f"{len(failed)} failed "
            f"({', '.join(failed) or 'none'})"
        )

        return results

    def login_platform(self, platform_name: str) -> bool:
        """Login to a specific platform."""
        platform = self._platforms.get(platform_name)
        if not platform:
            logger.error(
                f"Platform '{platform_name}' not available. "
                f"Available: {list(self._platforms.keys())}"
            )
            return False

        try:
            success = platform.login()
            if success:
                self._errors[platform_name] = 0
            return success
        except Exception as e:
            logger.error(f"Login failed for '{platform_name}': {e}")
            self._save_error(f"login_platform.{platform_name}", e)
            return False

    # ═══════════════════════════════════════════════════════════
    # DISCOVER — Search all platforms, deduplicate
    # ═══════════════════════════════════════════════════════════

    def discover_all(self,
                     platforms: Optional[List[str]] = None,
                     queries: Optional[List[str]] = None,
                     filters: Optional[Dict] = None,
                     save_to_db: bool = True) -> List[Dict]:
        """
        Search all enabled platforms for jobs, deduplicate results.

        Platforms are searched in priority order.  Each platform's
        search is wrapped in try/except — one failure doesn't stop
        the others.

        Args:
            platforms: List of platform names to search.
                       None → all enabled platforms.
            queries: Search queries (shared across platforms).
                     None → each platform uses its own config queries.
            filters: Search filters.
                     None → each platform uses defaults.
            save_to_db: If True, save new jobs to database.

        Returns:
            List of deduplicated job dicts (sorted by platform priority).
        """
        if platforms is None:
            platforms = list(self._platforms.keys())
        else:
            # Filter to only available platforms
            platforms = [
                p for p in platforms if p in self._platforms
            ]

        if not platforms:
            logger.warning("No platforms available for discovery")
            return []

        all_jobs = []
        platform_counts = {}
        total_start = time.time()

        for name in platforms:
            platform = self._platforms.get(name)
            if not platform:
                continue

            # Skip if in cooldown
            if platform.is_in_cooldown():
                logger.info(f"Skipping '{name}' (in cooldown)")
                platform_counts[name] = {"status": "cooldown", "count": 0}
                continue

            # Skip if too many consecutive errors
            if self._errors.get(name, 0) >= 5:
                logger.warning(
                    f"Skipping '{name}' "
                    f"(5+ consecutive errors)"
                )
                platform_counts[name] = {"status": "error_limit", "count": 0}
                continue

            # Skip LinkedIn apply (search only)
            if name == "linkedin":
                logger.debug(
                    f"LinkedIn: search-only mode "
                    f"(no apply)"
                )

            # ── Session break check ──
            if self.browser.needs_break(name):
                logger.info(
                    f"Session break needed for '{name}', "
                    f"skipping this cycle"
                )
                platform_counts[name] = {"status": "break", "count": 0}
                continue

            # ── Search ──
            logger.info(f"Discovering jobs on '{name}'...")
            try:
                search_start = time.time()

                kwargs = {}
                if queries:
                    kwargs["queries"] = queries
                if filters:
                    kwargs["filters"] = filters

                jobs = platform.search_jobs(**kwargs)

                elapsed = time.time() - search_start
                logger.info(
                    f"  '{name}': {len(jobs)} jobs "
                    f"in {elapsed:.1f}s"
                )

                # Tag each job with platform
                for job in jobs:
                    job["platform"] = name

                all_jobs.extend(jobs)
                platform_counts[name] = {
                    "status": "ok",
                    "count": len(jobs),
                    "time_s": round(elapsed, 1),
                }
                self._errors[name] = 0
                self._last_discover[name] = time.time()

            except Exception as e:
                logger.error(
                    f"  '{name}' discovery failed: {e}"
                )
                platform_counts[name] = {
                    "status": "error",
                    "count": 0,
                    "error": str(e),
                }
                self._errors[name] = self._errors.get(name, 0) + 1
                self._save_error(f"discover_all.{name}", e)

            # ── Rate limit between platforms ──
            if name != platforms[-1]:  # not the last one
                self.browser.random_delay(3.0, 8.0)

        # ── Deduplicate ──
        before_dedup = len(all_jobs)
        unique_jobs = _deduplicate_jobs(all_jobs)
        dedup_removed = before_dedup - len(unique_jobs)

        total_elapsed = time.time() - total_start

        logger.info(
            f"Discovery complete: {len(unique_jobs)} unique jobs "
            f"({before_dedup} total, {dedup_removed} duplicates removed) "
            f"in {total_elapsed:.1f}s"
        )
        for name, info in platform_counts.items():
            logger.debug(f"  {name}: {info}")

        # ── Save to database ──
        db_stats = {"new": 0, "existing": 0, "errors": 0}
        if save_to_db and unique_jobs:
            db_stats = self._save_jobs_to_db(unique_jobs)
            logger.info(
                f"DB: {db_stats['new']} new, "
                f"{db_stats['existing']} existing, "
                f"{db_stats['errors']} errors"
            )

        return unique_jobs

    def discover_platform(self, platform_name: str,
                          queries: Optional[List[str]] = None,
                          filters: Optional[Dict] = None,
                          save_to_db: bool = True) -> List[Dict]:
        """
        Discover jobs on a single specific platform.

        Args:
            platform_name: Which platform to search.
            queries: Search queries.
            filters: Search filters.
            save_to_db: Save results to database.

        Returns:
            List of job dicts.
        """
        return self.discover_all(
            platforms=[platform_name],
            queries=queries,
            filters=filters,
            save_to_db=save_to_db,
        )

    def _save_jobs_to_db(self, jobs: List[Dict]) -> Dict:
        """Save jobs to database, skip duplicates."""
        stats = {"new": 0, "existing": 0, "errors": 0}
        db = get_db()

        for job in jobs:
            try:
                platform = job.get("platform", "")
                job_id = job.get("platform_job_id", "")

                if not platform or not job_id:
                    stats["errors"] += 1
                    continue

                # Check if already exists
                existing = db.get_job_by_platform_id(platform, job_id)
                if existing:
                    stats["existing"] += 1
                    continue

                # Prepare job data for DB
                job_data = {
                    "platform": platform,
                    "platform_job_id": job_id,
                    "url": job.get("url", ""),
                    "title": job.get("title", ""),
                    "company": job.get("company", ""),
                    "location": job.get("location", ""),
                    "salary_min": job.get("salary_min", 0),
                    "salary_max": job.get("salary_max", 0),
                    "experience_min": job.get("experience_min", 0),
                    "experience_max": job.get("experience_max", 0),
                    "description": job.get("description", ""),
                    "skills": json.dumps(job.get("skills", [])),
                    "posted_date": job.get("posted_date", ""),
                    "discovered_at": job.get(
                        "discovered_at",
                        datetime.now().isoformat()
                    ),
                    "status": "new",
                }

                # Optional fields
                if job.get("job_type"):
                    job_data["job_type"] = job["job_type"]
                if job.get("work_mode"):
                    job_data["work_mode"] = job["work_mode"]

                db.save_job(job_data)
                stats["new"] += 1

            except Exception as e:
                logger.debug(f"Error saving job to DB: {e}")
                stats["errors"] += 1

        return stats

    # ═══════════════════════════════════════════════════════════
    # APPLY — Route to correct platform
    # ═══════════════════════════════════════════════════════════

    def apply_to_job(self, job: Dict, resume_path: str,
                     cover_letter: Optional[str] = None,
                     auto_submit: bool = False) -> Dict:
        """
        Apply to a job by routing to the correct platform.

        Flow:
          1. Identify platform from job dict.
          2. Check daily limits and cooldowns.
          3. Call platform.prepare_application() (fills form, pauses).
          4. If auto_submit=True, immediately submit.
             Otherwise, return prepared state for Telegram approval.
          5. Log result to database.

        Args:
            job: Job dict (must have 'platform' key).
            resume_path: Path to resume file.
            cover_letter: Optional cover letter text.
            auto_submit: If True, submit without waiting for approval.

        Returns:
            {
                success: bool,
                status: str (ready/submitted/failed/...),
                platform: str,
                apply_type: str,
                error: str|None,
                screenshot: str,
                prepared: dict (the full prepared result),
            }
        """
        result = {
            "success": False,
            "status": "failed",
            "platform": job.get("platform", "unknown"),
            "apply_type": "",
            "error": None,
            "screenshot": "",
            "prepared": None,
        }

        platform_name = job.get("platform", "")
        if not platform_name:
            result["error"] = "Job has no 'platform' field"
            logger.error(result["error"])
            return result

        # ── LinkedIn: no apply ──
        if platform_name == "linkedin":
            result["error"] = (
                "LinkedIn is search-only (no auto-apply)"
            )
            result["status"] = "skipped"
            logger.info(result["error"])
            return result

        # ── Get platform instance ──
        platform = self._platforms.get(platform_name)
        if not platform:
            result["error"] = (
                f"Platform '{platform_name}' not available. "
                f"Available: {list(self._platforms.keys())}"
            )
            logger.error(result["error"])
            return result

        # ── Check limits ──
        if not platform.can_apply():
            result["error"] = (
                f"'{platform_name}' daily limit reached "
                f"or in cooldown"
            )
            result["status"] = "limit_reached"
            logger.warning(result["error"])
            return result

        # ── Session break ──
        if self.browser.needs_break(platform_name):
            result["error"] = (
                f"'{platform_name}' needs session break"
            )
            result["status"] = "break_needed"
            logger.info(result["error"])
            return result

        # ── Prepare application ──
        try:
            logger.info(
                f"Preparing application: "
                f"{job.get('title', '?')} @ "
                f"{job.get('company', '?')} "
                f"on {platform_name}"
            )

            prepared = platform.prepare_application(
                job, resume_path, cover_letter
            )
            result["prepared"] = prepared
            result["apply_type"] = prepared.get("apply_type", "")
            result["screenshot"] = prepared.get("screenshot", "")

            prep_status = prepared.get("status", "failed")

            # ── Handle different prepare outcomes ──

            if prep_status == "submitted_quick":
                # Already submitted during prepare
                result["success"] = True
                result["status"] = "submitted"
                self._log_application(job, prepared, "submitted")
                logger.info(
                    f"✅ Quick-applied: "
                    f"{job.get('title')} @ {job.get('company')}"
                )
                return result

            elif prep_status == "already_applied":
                result["status"] = "already_applied"
                result["error"] = "Already applied to this job"
                logger.info(result["error"])
                # Update job status in DB
                self._update_job_status(job, "applied",
                                        "Already applied (detected)")
                return result

            elif prep_status == "external":
                result["status"] = "external"
                result["error"] = (
                    "External apply (company website)"
                )
                logger.info(result["error"])
                self._update_job_status(job, "skipped",
                                        "External apply only")
                return result

            elif prep_status == "ready":
                # Form filled, waiting for approval
                if auto_submit:
                    # Submit immediately
                    submit_result = platform.submit_application(
                        prepared
                    )
                    result["success"] = submit_result.get(
                        "success", False
                    )
                    result["status"] = submit_result.get(
                        "status", "failed"
                    )
                    result["screenshot"] = submit_result.get(
                        "screenshot", ""
                    )
                    if result["success"]:
                        self._log_application(
                            job, prepared, "submitted"
                        )
                        logger.info(
                            f"✅ Applied: {job.get('title')} "
                            f"@ {job.get('company')}"
                        )
                    else:
                        result["error"] = submit_result.get(
                            "error", "Submit failed"
                        )
                else:
                    # Return ready state — caller handles
                    # Telegram approval
                    result["status"] = "ready"
                    result["success"] = True
                    logger.info(
                        f"Application ready (awaiting approval): "
                        f"{job.get('title')} @ "
                        f"{job.get('company')}"
                    )

                return result

            else:
                # Failed
                result["error"] = prepared.get(
                    "error", f"Prepare failed: {prep_status}"
                )
                logger.error(result["error"])
                self._errors[platform_name] = (
                    self._errors.get(platform_name, 0) + 1
                )
                return result

        except Exception as e:
            result["error"] = str(e)
            logger.error(
                f"apply_to_job failed ({platform_name}): {e}"
            )
            self._save_error(
                f"apply_to_job.{platform_name}", e
            )
            self._errors[platform_name] = (
                self._errors.get(platform_name, 0) + 1
            )
            return result

    def submit_prepared(self, prepared: Dict) -> Dict:
        """
        Submit a previously prepared application.

        Called after Telegram approval.

        Args:
            prepared: The dict returned by prepare_application()
                      (stored in apply_to_job result['prepared']).

        Returns:
            {success, status, error, screenshot}
        """
        result = {
            "success": False,
            "status": "failed",
            "error": None,
            "screenshot": "",
        }

        platform_name = prepared.get("platform", "")
        platform = self._platforms.get(platform_name)

        if not platform:
            result["error"] = (
                f"Platform '{platform_name}' not available"
            )
            return result

        try:
            submit_result = platform.submit_application(prepared)
            result["success"] = submit_result.get("success", False)
            result["status"] = submit_result.get("status", "failed")
            result["screenshot"] = submit_result.get("screenshot", "")
            result["error"] = submit_result.get("error")

            if result["success"]:
                job = prepared.get("job", {})
                self._log_application(job, prepared, "submitted")
                logger.info(
                    f"✅ Submitted (post-approval): "
                    f"{job.get('title')} @ {job.get('company')}"
                )

            return result

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"submit_prepared failed: {e}")
            self._save_error("submit_prepared", e)
            return result

    def apply_batch(self, jobs: List[Dict], resume_path: str,
                    cover_letter: Optional[str] = None,
                    auto_submit: bool = False,
                    max_count: Optional[int] = None) -> Dict:
        """
        Apply to a batch of jobs with rate limiting.

        Args:
            jobs: List of job dicts to apply to.
            resume_path: Path to resume file.
            cover_letter: Optional cover letter text.
            auto_submit: If True, submit without approval.
            max_count: Max number of applications this batch.
                       None → config SCHEDULE_CONFIG.apply_batch_size.

        Returns:
            {
                attempted: int,
                submitted: int,
                ready: int (awaiting approval),
                skipped: int,
                failed: int,
                results: [per-job result dicts],
            }
        """
        if max_count is None:
            from config import SCHEDULE_CONFIG
            max_count = SCHEDULE_CONFIG.get("apply_batch_size", 10)

        stats = {
            "attempted": 0,
            "submitted": 0,
            "ready": 0,
            "skipped": 0,
            "failed": 0,
            "results": [],
        }

        for job in jobs[:max_count]:
            platform_name = job.get("platform", "")

            # Check if platform can still apply
            platform = self._platforms.get(platform_name)
            if platform and not platform.can_apply():
                logger.info(
                    f"'{platform_name}' limit reached, "
                    f"skipping remaining"
                )
                stats["skipped"] += 1
                continue

            stats["attempted"] += 1

            result = self.apply_to_job(
                job, resume_path, cover_letter, auto_submit
            )
            stats["results"].append(result)

            status = result.get("status", "failed")
            if status == "submitted":
                stats["submitted"] += 1
            elif status == "ready":
                stats["ready"] += 1
            elif status in ("already_applied", "external",
                           "limit_reached", "skipped",
                           "break_needed"):
                stats["skipped"] += 1
            else:
                stats["failed"] += 1

            # Rate limit between applications
            rate = PLATFORM_CONFIG.get(platform_name, {}).get(
                "rate_limit_seconds", (60, 180)
            )
            if stats["attempted"] < len(jobs[:max_count]):
                delay = random.uniform(*rate)
                logger.debug(
                    f"Rate limit delay: {delay:.0f}s "
                    f"before next application"
                )
                time.sleep(delay)

        logger.info(
            f"Batch complete: {stats['attempted']} attempted, "
            f"{stats['submitted']} submitted, "
            f"{stats['ready']} ready, "
            f"{stats['skipped']} skipped, "
            f"{stats['failed']} failed"
        )

        return stats

    # ═══════════════════════════════════════════════════════════
    # STATUS
    # ═══════════════════════════════════════════════════════════

    def get_status(self) -> Dict:
        """
        Get status of all platforms.

        Returns:
            {
                platforms: {
                    name: {
                        available: bool,
                        enabled: bool,
                        logged_in: bool,
                        daily_applied: int,
                        daily_limit: int,
                        can_apply: bool,
                        in_cooldown: bool,
                        consecutive_errors: int,
                        last_discover: str (ISO) or None,
                        session_minutes: float,
                        needs_break: bool,
                    }
                },
                summary: {
                    total_platforms: int,
                    active: int,
                    logged_in: int,
                    in_cooldown: int,
                    total_applied_today: int,
                },
                browser: {browser engine status},
            }
        """
        platforms_status = {}
        summary = {
            "total_platforms": 0,
            "active": 0,
            "logged_in": 0,
            "in_cooldown": 0,
            "total_applied_today": 0,
        }

        for name in self.PLATFORM_ORDER:
            config = PLATFORM_CONFIG.get(name, {})
            enabled = config.get("enabled", True)
            available = name in _PLATFORM_CLASSES
            platform = self._platforms.get(name)

            info = {
                "available": available,
                "enabled": enabled,
                "initialized": platform is not None,
                "logged_in": False,
                "daily_applied": 0,
                "daily_limit": config.get(
                    "max_daily_applications", 25
                ),
                "can_apply": False,
                "in_cooldown": False,
                "consecutive_errors": self._errors.get(name, 0),
                "last_discover": None,
                "session_minutes": 0.0,
                "needs_break": False,
            }

            if platform:
                try:
                    info["logged_in"] = self.browser.is_logged_in(name)
                    info["daily_applied"] = platform.get_daily_count()
                    info["can_apply"] = platform.can_apply()
                    info["in_cooldown"] = platform.is_in_cooldown()
                    info["session_minutes"] = round(
                        self.browser.session_time(name), 1
                    )
                    info["needs_break"] = self.browser.needs_break(name)
                except Exception:
                    pass

                summary["active"] += 1
                if info["logged_in"]:
                    summary["logged_in"] += 1
                if info["in_cooldown"]:
                    summary["in_cooldown"] += 1
                summary["total_applied_today"] += info["daily_applied"]

            last_disc = self._last_discover.get(name)
            if last_disc:
                info["last_discover"] = datetime.fromtimestamp(
                    last_disc
                ).isoformat()

            platforms_status[name] = info
            summary["total_platforms"] += 1

        return {
            "platforms": platforms_status,
            "summary": summary,
            "browser": self.browser.get_status(),
        }

    def get_platform(self, name: str):
        """
        Get a specific platform instance.

        Returns:
            Platform instance or None.
        """
        return self._platforms.get(name)

    def get_active_platforms(self) -> List[str]:
        """Return names of platforms that are initialized."""
        return list(self._platforms.keys())

    def get_apply_ready_platforms(self) -> List[str]:
        """
        Return names of platforms that can accept applications
        right now.
        """
        ready = []
        for name, platform in self._platforms.items():
            if name == "linkedin":
                continue  # search only
            try:
                if (platform.can_apply() and
                        not self.browser.needs_break(name) and
                        self._errors.get(name, 0) < 5):
                    ready.append(name)
            except Exception:
                continue
        return ready

    # ═══════════════════════════════════════════════════════════
    # SESSION MANAGEMENT
    # ═══════════════════════════════════════════════════════════

    def refresh_sessions(self) -> Dict[str, str]:
        """
        Refresh all platform sessions.

        For each platform:
          - If cookies are still valid → skip.
          - If session expired → re-login.
          - If in cooldown → skip.

        Returns:
            {platform: "ok" | "refreshed" | "failed" | "cooldown"}
        """
        results = {}

        for name, platform in self._platforms.items():
            try:
                if platform.is_in_cooldown():
                    results[name] = "cooldown"
                    continue

                if self.browser.is_logged_in(name):
                    # Session still alive
                    results[name] = "ok"
                    continue

                # Try to re-login
                logger.info(f"Refreshing session for '{name}'...")
                success = platform.login()
                if success:
                    results[name] = "refreshed"
                    self._errors[name] = 0
                else:
                    results[name] = "failed"
                    self._errors[name] = (
                        self._errors.get(name, 0) + 1
                    )

                self.browser.random_delay(2.0, 5.0)

            except Exception as e:
                logger.error(
                    f"Session refresh failed for '{name}': {e}"
                )
                results[name] = "failed"

        logger.info(f"Session refresh: {results}")
        return results

    def take_breaks_if_needed(self) -> List[str]:
        """
        Check all platforms for session breaks, close those that
        need it.

        Returns:
            List of platform names that were closed for break.
        """
        closed = []
        for name in list(self._platforms.keys()):
            if self.browser.needs_break(name):
                logger.info(
                    f"Closing '{name}' for session break "
                    f"({self.browser.session_time(name):.0f}min)"
                )
                self.browser.save_cookies(name)
                self.browser.close(name)
                closed.append(name)

        if closed:
            logger.info(
                f"Session breaks: {', '.join(closed)}"
            )
        return closed

    # ═══════════════════════════════════════════════════════════
    # PROFILE UPDATES
    # ═══════════════════════════════════════════════════════════

    def update_profiles(self) -> Dict[str, bool]:
        """
        Update profiles on platforms that support it.

        Currently only Naukri has update_profile() to boost visibility.

        Returns:
            {platform: success_bool}
        """
        results = {}

        for name, platform in self._platforms.items():
            if hasattr(platform, "update_profile"):
                try:
                    success = platform.update_profile()
                    results[name] = success
                    logger.info(
                        f"Profile update '{name}': "
                        f"{'✓' if success else '✗'}"
                    )
                except Exception as e:
                    results[name] = False
                    logger.error(
                        f"Profile update '{name}' failed: {e}"
                    )

        return results

    # ═══════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ═══════════════════════════════════════════════════════════

    def _log_application(self, job: Dict, prepared: Dict,
                         status: str) -> None:
        """Log a completed application to the database."""
        try:
            db = get_db()

            # Find job_id in DB
            job_id = None
            platform = job.get("platform", "")
            pjid = job.get("platform_job_id", "")
            if platform and pjid:
                existing = db.get_job_by_platform_id(platform, pjid)
                if existing:
                    job_id = existing.get("id")
                    # Update job status to 'applied'
                    db.update_job_status(
                        job_id, "applied",
                        f"Applied via {prepared.get('apply_type', 'unknown')}"
                    )

            app_data = {
                "job_id": job_id,
                "platform": platform,
                "method": prepared.get("apply_type", "unknown"),
                "resume_version": prepared.get("resume_path", ""),
                "cover_letter": prepared.get("cover_letter", ""),
                "tailoring_mode": "generic",
                "applied_at": datetime.now().isoformat(),
                "status": status,
            }
            db.save_application(app_data)

            # Update platform session daily count
            db.update_platform_session(platform, {
                "daily_applied": db.get_platform_session(platform)
                .get("daily_applied", 0) + 1,
                "total_applied": db.get_platform_session(platform)
                .get("total_applied", 0) + 1,
            })

        except Exception as e:
            logger.debug(f"Failed to log application: {e}")

    def _update_job_status(self, job: Dict, status: str,
                           notes: str = "") -> None:
        """Update job status in database."""
        try:
            db = get_db()
            platform = job.get("platform", "")
            pjid = job.get("platform_job_id", "")
            if platform and pjid:
                existing = db.get_job_by_platform_id(platform, pjid)
                if existing:
                    db.update_job_status(
                        existing["id"], status, notes
                    )
        except Exception as e:
            logger.debug(f"Failed to update job status: {e}")

    def _save_error(self, method: str,
                    error: Exception) -> None:
        """Log error to database."""
        try:
            db = get_db()
            db.save_error(
                module=f"platforms.manager.{method}",
                error_type=type(error).__name__,
                message=str(error),
                traceback=tb_module.format_exc(),
            )
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════
    # CLOSE / CLEANUP
    # ═══════════════════════════════════════════════════════════

    def close_all(self) -> None:
        """Close all platform browser sessions."""
        for name in list(self._platforms.keys()):
            try:
                self.browser.save_cookies(name)
                self.browser.close(name)
            except Exception:
                pass

        logger.info("PlatformManager: all sessions closed")

    def close_platform(self, name: str) -> None:
        """Close a specific platform's browser session."""
        try:
            self.browser.save_cookies(name)
            self.browser.close(name)
            logger.info(f"Platform '{name}' session closed")
        except Exception as e:
            logger.debug(f"Error closing '{name}': {e}")

    def __repr__(self):
        active = list(self._platforms.keys())
        return (
            f"PlatformManager(platforms={active}, "
            f"available_classes="
            f"{list(_PLATFORM_CLASSES.keys())})"
        )


# ═══════════════════════════════════════════════════════════════════
# MODULE-LEVEL SINGLETON
# ═══════════════════════════════════════════════════════════════════

_manager_instance: Optional[PlatformManager] = None


def get_platform_manager(
    browser_engine: Optional[BrowserEngine] = None,
    notifier=None,
) -> PlatformManager:
    """Get or create the singleton PlatformManager."""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = PlatformManager(
            browser_engine, notifier
        )
    return _manager_instance


# ═══════════════════════════════════════════════════════════════════
# TEST BLOCK
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
    console.print(
        "\n[bold cyan]═══ Platform Manager Test ═══[/bold cyan]\n"
    )

    # ── 1. Available platform classes ──
    console.print("[yellow]1. Available platform classes:[/yellow]")
    for name in PlatformManager.PLATFORM_ORDER:
        available = name in _PLATFORM_CLASSES
        icon = "[green]✓[/green]" if available else "[yellow]⚠ not built[/yellow]"
        console.print(f"   {icon} {name}")

    # ── 2. Platform configs ──
    console.print("\n[yellow]2. Platform configs:[/yellow]")
    for name in PlatformManager.PLATFORM_ORDER:
        config = PLATFORM_CONFIG.get(name, {})
        enabled = config.get("enabled", True)
        max_daily = config.get("max_daily_applications", "?")
        queries = config.get("search_queries", [])
        icon = "[green]✓[/green]" if enabled else "[red]✗[/red]"
        console.print(
            f"   {icon} {name}: enabled={enabled}, "
            f"max_daily={max_daily}, "
            f"queries={len(queries)}"
        )

    # ── 3. Initialize manager ──
    console.print(
        "\n[yellow]3. Initializing PlatformManager...[/yellow]"
    )
    try:
        engine = BrowserEngine()
        manager = PlatformManager(engine)
        console.print(f"   [green]✓[/green] {manager}")
        console.print(
            f"   Active platforms: "
            f"{manager.get_active_platforms()}"
        )
    except Exception as e:
        console.print(f"   [red]✗ Init failed: {e}[/red]")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # ── 4. Status ──
    console.print("\n[yellow]4. Platform status:[/yellow]")
    status = manager.get_status()

    table = Table(title="Platform Status")
    table.add_column("Platform", style="cyan")
    table.add_column("Available", justify="center")
    table.add_column("Enabled", justify="center")
    table.add_column("Initialized", justify="center")
    table.add_column("Logged In", justify="center")
    table.add_column("Applied", justify="right")
    table.add_column("Limit", justify="right")
    table.add_column("Can Apply", justify="center")
    table.add_column("Errors", justify="right")

    for name, info in status["platforms"].items():
        table.add_row(
            name,
            "✓" if info["available"] else "✗",
            "✓" if info["enabled"] else "✗",
            "✓" if info["initialized"] else "✗",
            "✓" if info["logged_in"] else "✗",
            str(info["daily_applied"]),
            str(info["daily_limit"]),
            "✓" if info["can_apply"] else "✗",
            str(info["consecutive_errors"]),
        )

    console.print(table)

    summary = status["summary"]
    console.print(
        f"\n   Summary: {summary['active']} active, "
        f"{summary['logged_in']} logged in, "
        f"{summary['in_cooldown']} in cooldown, "
        f"{summary['total_applied_today']} applied today"
    )

    # ── 5. Dedup tests ──
    console.print("\n[yellow]5. Deduplication tests:[/yellow]")
    test_jobs = [
        {"title": "Software Engineer", "company": "Razorpay Pvt Ltd",
         "url": "https://naukri.com/job/1", "platform": "naukri"},
        {"title": "Software Engineer", "company": "Razorpay",
         "url": "https://foundit.in/job/2", "platform": "foundit"},
        {"title": "Backend Developer", "company": "Razorpay",
         "url": "https://naukri.com/job/3", "platform": "naukri"},
        {"title": "SDE-1", "company": "Google India",
         "url": "https://naukri.com/job/4", "platform": "naukri"},
        {"title": "SDE-1", "company": "Google India Pvt. Ltd.",
         "url": "https://foundit.in/job/5", "platform": "foundit"},
        {"title": "Full Stack Developer", "company": "Flipkart",
         "url": "https://naukri.com/job/6", "platform": "naukri"},
    ]

    deduped = _deduplicate_jobs(test_jobs)
    console.print(
        f"   Input: {len(test_jobs)} jobs → "
        f"Output: {len(deduped)} unique"
    )
    for j in deduped:
        console.print(
            f"   ✓ {j['title']} @ {j['company']} "
            f"({j['platform']})"
        )

    # ── 6. Apply-ready platforms ──
    console.print(
        "\n[yellow]6. Apply-ready platforms:[/yellow]"
    )
    ready = manager.get_apply_ready_platforms()
    console.print(f"   Ready to apply: {ready or 'none'}")

    # ── 7. Singleton test ──
    console.print("\n[yellow]7. Singleton test:[/yellow]")
    m1 = get_platform_manager()
    m2 = get_platform_manager()
    console.print(
        f"   Same instance: {m1 is m2} (should be True)"
    )

    # ── 8. Live test ──
    console.print(
        "\n[yellow]8. Live test (optional):[/yellow]"
    )
    if manager.get_active_platforms():
        run_live = input(
            "   Run live login + discover test? (y/n): "
        ).strip().lower()

        if run_live == "y":
            try:
                # Login
                console.print("\n   Logging in to all platforms...")
                login_results = manager.login_all()
                for name, ok in login_results.items():
                    icon = (
                        "[green]✓[/green]" if ok
                        else "[red]✗[/red]"
                    )
                    console.print(f"   {icon} {name}: {'OK' if ok else 'FAILED'}")

                logged_in = [
                    k for k, v in login_results.items() if v
                ]
                if logged_in:
                    # Discover
                    console.print(
                        f"\n   Discovering jobs on "
                        f"{logged_in}..."
                    )
                    jobs = manager.discover_all(
                        platforms=logged_in,
                        queries=["Software Engineer"],
                    )
                    console.print(
                        f"   [green]✓[/green] Found "
                        f"{len(jobs)} unique jobs"
                    )

                    if jobs:
                        table = Table(
                            title="Discovered Jobs (top 10)"
                        )
                        table.add_column(
                            "Platform", style="cyan"
                        )
                        table.add_column(
                            "Title", max_width=30
                        )
                        table.add_column(
                            "Company", max_width=20
                        )
                        table.add_column(
                            "Location", max_width=15
                        )

                        for j in jobs[:10]:
                            table.add_row(
                                j.get("platform", ""),
                                j.get("title", "")[:30],
                                j.get("company", "")[:20],
                                j.get("location", "")[:15],
                            )

                        console.print(table)

                    # Updated status
                    console.print(
                        "\n   Updated status:"
                    )
                    new_status = manager.get_status()
                    console.print(
                        f"   Applied today: "
                        f"{new_status['summary']['total_applied_today']}"
                    )

            except Exception as e:
                console.print(
                    f"   [red]Live test error: {e}[/red]"
                )
                import traceback
                traceback.print_exc()
            finally:
                manager.close_all()
                engine.close_all()
    else:
        console.print(
            "   No platforms available for live test"
        )

    # ── 9. DB check ──
    console.print("\n[yellow]9. Database check:[/yellow]")
    try:
        db = get_db()
        info = db.get_table_info()
        console.print(f"   Jobs: {info.get('jobs', 0)}")
        console.print(
            f"   Applications: "
            f"{info.get('applications', 0)}"
        )
        console.print(
            f"   Platform sessions: "
            f"{info.get('platform_sessions', 0)}"
        )
    except Exception as e:
        console.print(f"   [yellow]DB check: {e}[/yellow]")

    console.print(
        f"\n[bold green]"
        f"═══ Platform Manager tests complete! ═══"
        f"[/bold green]\n"
    )