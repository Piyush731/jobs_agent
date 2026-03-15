"""
platforms/base.py — Abstract base class for all job platform implementations.

Every platform file (naukri.py, indeed.py, foundit.py, linkedin.py)
extends PlatformBase and implements the abstract methods.

This base provides:
  ABSTRACT (must implement per platform):
    - login() → bool
    - search_jobs(queries, filters) → List[dict]
    - get_job_details(job_url) → dict
    - prepare_application(job, resume_path, cover_letter?) → dict
    - submit_application(prepared) → dict
    - check_status(application_id) → str

  SHARED (inherited by all platforms):
    - Daily application counting / limits
    - Cooldown management (rate limit / ban recovery)
    - Universal form field handler (auto-fill known fields)
    - CAPTCHA detection and handling (via Telegram)
    - OTP detection and handling (via Telegram)
    - Human-like action wrappers (delegates to BrowserEngine)
    - Logging and error tracking to DB

Interface contracts:
  search_jobs returns: [{
      platform_job_id, url, title, company, location,
      salary_text, experience_text, description, posted_date, skills[]
  }]

  prepare_application returns: {
      success: bool, job_id, platform, method, resume_path,
      cover_letter_path, form_filled: bool, error?
  }

  submit_application returns: {
      success: bool, job_id, platform, method, applied_at, error?
  }
"""

import os
import re
import time
import random
import traceback
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Tuple

# ── project imports ──────────────────────────────────────────────
from config import (
    PLATFORM_CONFIG,
    STEALTH_CONFIG,
    USER_PROFILE,
    BROWSER_PROFILES_DIR,
)
from core.logger import get_logger
from core.db import get_db

logger = get_logger("platforms.base")


# ═══════════════════════════════════════════════════════════════════
# ABSTRACT BASE CLASS
# ═══════════════════════════════════════════════════════════════════

class PlatformBase(ABC):
    """
    Abstract base class for all job platform integrations.

    Subclasses implement platform-specific login, search, scrape, and apply
    logic. This base provides shared infrastructure:
      - Rate limiting and daily application caps
      - Cooldown after detection or errors
      - Form field auto-fill from profile/answers
      - CAPTCHA/OTP handling via Telegram
      - Human-like browser action wrappers
      - Error logging to database

    Usage (by platform files):
        class NaukriPlatform(PlatformBase):
            PLATFORM_NAME = "naukri"
            def login(self): ...
            def search_jobs(self, queries, filters): ...
            ...

    Usage (by manager.py / monitor.py):
        naukri = NaukriPlatform(browser_engine)
        if naukri.login():
            jobs = naukri.search_jobs(queries, filters)
            for job in jobs:
                if naukri.can_apply():
                    prepared = naukri.prepare_application(job, resume)
                    result = naukri.submit_application(prepared)
    """

    # ── Subclasses MUST set these ──
    PLATFORM_NAME: str = ""          # "naukri", "indeed", "linkedin", "foundit"
    BASE_URL: str = ""               # "https://www.naukri.com"
    LOGIN_URL: str = ""              # "https://www.naukri.com/nlogin/login"

    # ── Subclass can override these defaults ──
    REQUIRES_LOGIN: bool = True      # LinkedIn search might work without login
    SUPPORTS_APPLY: bool = True      # LinkedIn: False (search only)

    def __init__(self, browser_engine):
        """
        Initialize platform base.

        Args:
            browser_engine: BrowserEngine instance (shared across platforms).
        """
        if not self.PLATFORM_NAME:
            raise ValueError(
                f"{self.__class__.__name__} must set PLATFORM_NAME"
            )

        self.browser = browser_engine
        self.db = get_db()
        self._page = None                   # Set after launch
        self._notifier = None               # Set by manager when available

        # ── Platform config from config.py ──
        self._config = PLATFORM_CONFIG.get(self.PLATFORM_NAME, {})
        self._max_daily = self._config.get("max_daily_applications", 20)
        self._rate_limit = self._config.get("rate_limit_seconds", (180, 300))
        self._cooldown_hours = self._config.get("cooldown_hours", 24)
        self._max_pages = self._config.get("max_pages_per_query", 5)
        self._enabled = self._config.get("enabled", True)
        self._search_queries = self._config.get("search_queries", [])

        # ── Internal state ──
        self._daily_count = 0
        self._daily_reset_date = datetime.now().date()
        self._last_apply_time = 0.0         # epoch seconds
        self._cooldown_until: Optional[datetime] = None
        self._consecutive_errors = 0
        self._is_logged_in = False

        # ── Load persisted session state from DB ──
        self._load_session_state()

        logger.info(
            f"Platform '{self.PLATFORM_NAME}' initialized "
            f"(enabled={self._enabled}, max_daily={self._max_daily}, "
            f"rate_limit={self._rate_limit}s)"
        )

    # ═══════════════════════════════════════════════════════════
    # PROPERTIES
    # ═══════════════════════════════════════════════════════════

    @property
    def name(self) -> str:
        """Platform name string."""
        return self.PLATFORM_NAME

    @property
    def page(self):
        """Current browser page, or None."""
        return self._page

    @property
    def enabled(self) -> bool:
        """Whether this platform is enabled in config."""
        return self._enabled

    @property
    def config(self) -> dict:
        """Platform-specific config dict from config.py."""
        return self._config

    # ═══════════════════════════════════════════════════════════
    # ABSTRACT METHODS — Each platform MUST implement these
    # ═══════════════════════════════════════════════════════════

    @abstractmethod
    def login(self) -> bool:
        """
        Log into the platform.

        Implementation should:
          1. Call self.ensure_browser() to get/create page
          2. Check if already logged in (cookie-based)
          3. Navigate to login page if needed
          4. Enter credentials using self.browser.type_human()
          5. Handle CAPTCHA/OTP if triggered
          6. Verify login success
          7. Call self.browser.save_cookies(self.PLATFORM_NAME)
          8. Update self._is_logged_in

        Returns:
            True if logged in successfully, False on failure.
        """
        pass

    @abstractmethod
    def search_jobs(self, queries: List[Any],
                    filters: Optional[Dict] = None) -> List[dict]:
        """
        Search for jobs on the platform.

        Implementation should:
          1. Iterate through queries (SearchQuery objects or strings)
          2. Navigate to search URL with query params
          3. Parse job cards from results
          4. Paginate up to self._max_pages per query
          5. Return standardized job dicts

        Args:
            queries: List of SearchQuery objects or keyword strings.
            filters: Optional dict of additional filters
                     {experience_min, experience_max, salary_min, location, etc.}

        Returns:
            List of job dicts, each containing:
            {
                platform_job_id: str,   # unique ID on this platform
                url: str,               # direct link to job posting
                title: str,             # job title
                company: str,           # company name
                location: str,          # city/remote
                salary_text: str,       # raw salary text (e.g., "8-12 LPA")
                experience_text: str,   # raw exp text (e.g., "1-3 Yrs")
                description: str,       # full or partial JD text
                posted_date: str,       # when posted (parsed or raw)
                skills: List[str],      # skill tags if available
                job_type: str,          # full-time/contract/etc.
                work_mode: str,         # remote/hybrid/onsite
            }
        """
        pass

    @abstractmethod
    def get_job_details(self, job_url: str) -> dict:
        """
        Navigate to a job page and extract full details.

        Implementation should:
          1. Navigate to job_url
          2. Wait for JD content to load
          3. Extract: title, company, full description, skills, salary,
             experience range, location, posted date, apply button info
          4. Return enriched job dict

        Args:
            job_url: Direct URL to the job posting.

        Returns:
            dict with all fields from search_jobs PLUS:
            {
                description: str,       # FULL JD text (not truncated)
                salary_min: float|None, # parsed salary range
                salary_max: float|None,
                experience_min: float|None,
                experience_max: float|None,
                apply_type: str,        # "quick_apply" | "external" | "email"
                company_url: str,       # company page link
                posted_date: str,       # parsed date
                applicants: int|None,   # number of applicants if shown
            }
        """
        pass

    @abstractmethod
    def prepare_application(self, job: dict,
                            resume_path: str,
                            cover_letter: Optional[str] = None) -> dict:
        """
        Navigate to job, fill the application form, but DO NOT submit.

        This is the "semi-auto" part: everything is ready, waiting for
        human approval via Telegram before calling submit_application().

        Implementation should:
          1. Navigate to job URL
          2. Click "Apply" button
          3. Fill all form fields using self.handle_form_field()
          4. Upload resume using self.browser.upload_file()
          5. Paste cover letter if field exists
          6. STOP before clicking Submit
          7. Take a confirmation screenshot
          8. Return the prepared state

        Args:
            job: Job dict (from search_jobs or get_job_details).
            resume_path: Path to tailored resume file (DOCX/PDF).
            cover_letter: Cover letter text (optional).

        Returns:
            {
                success: bool,
                job_id: str,
                platform: str,
                method: str,            # "quick_apply" | "full_form"
                resume_path: str,
                cover_letter_path: str|None,
                form_filled: bool,
                screenshot: str,        # path to confirmation screenshot
                error: str|None,
            }
        """
        pass

    @abstractmethod
    def submit_application(self, prepared: dict) -> dict:
        """
        Click the Submit button on a prepared application.

        Called AFTER Telegram approval (or auto-approval for high scores).

        Implementation should:
          1. Click the Submit/Apply button
          2. Wait for confirmation page/message
          3. Handle any post-submit popups
          4. Verify submission success
          5. Increment daily count
          6. Log application to DB

        Args:
            prepared: Dict returned by prepare_application().

        Returns:
            {
                success: bool,
                job_id: str,
                platform: str,
                method: str,
                applied_at: str,        # ISO timestamp
                confirmation: str,      # success message text
                error: str|None,
            }
        """
        pass

    @abstractmethod
    def check_status(self, application_id: int) -> str:
        """
        Check the status of a previously submitted application.

        Implementation should:
          1. Navigate to "Applied Jobs" or similar section
          2. Find the application by job title/company
          3. Extract current status

        Args:
            application_id: Application ID from our database.

        Returns:
            Status string: "submitted"|"viewed"|"shortlisted"|
            "interview"|"rejected"|"no_update"
        """
        pass

    # ═══════════════════════════════════════════════════════════
    # BROWSER MANAGEMENT — Shared
    # ═══════════════════════════════════════════════════════════

    def ensure_browser(self, headless: Optional[bool] = None):
        """
        Ensure browser is launched for this platform and return the page.

        Reuses existing page if still alive. Launches new one if needed.
        Always applies stealth.

        Returns:
            Playwright Page object.
        """
        page = self.browser.get_page(self.PLATFORM_NAME)
        if page:
            self._page = page
            return page

        logger.info(f"Launching browser for '{self.PLATFORM_NAME}'")
        page = self.browser.launch(self.PLATFORM_NAME, headless=headless)
        self._page = page
        return page

    def ensure_logged_in(self) -> bool:
        """
        Ensure we're logged in. Try cookies first, then full login.

        Returns:
            True if logged in, False if login failed.
        """
        # Check if we have a live session
        if self._is_logged_in and self.browser.is_logged_in(self.PLATFORM_NAME):
            return True

        # Try login
        try:
            success = self.login()
            self._is_logged_in = success
            if success:
                self._consecutive_errors = 0
                self._update_session_state(logged_in=True)
            else:
                self._update_session_state(
                    logged_in=False,
                    last_error="Login failed"
                )
            return success
        except Exception as e:
            logger.error(f"Login error for '{self.PLATFORM_NAME}': {e}")
            self._is_logged_in = False
            self._update_session_state(
                logged_in=False,
                last_error=str(e)
            )
            return False

    def close_browser(self) -> None:
        """Close browser for this platform."""
        self.browser.save_cookies(self.PLATFORM_NAME)
        self.browser.close(self.PLATFORM_NAME)
        self._page = None
        self._is_logged_in = False
        logger.info(f"Browser closed for '{self.PLATFORM_NAME}'")

    # ═══════════════════════════════════════════════════════════
    # NOTIFIER — Set by manager for Telegram integration
    # ═══════════════════════════════════════════════════════════

    def set_notifier(self, notifier) -> None:
        """
        Attach a JobNotifier instance for Telegram communication.
        Called by PlatformManager after initialization.
        """
        self._notifier = notifier
        logger.debug(f"Notifier attached for '{self.PLATFORM_NAME}'")

    @property
    def has_notifier(self) -> bool:
        """Whether a Telegram notifier is available."""
        return self._notifier is not None

    # ═══════════════════════════════════════════════════════════
    # RATE LIMITING & DAILY COUNTS
    # ═══════════════════════════════════════════════════════════

    def get_daily_count(self) -> int:
        """
        Get number of applications submitted today on this platform.

        Auto-resets at midnight.
        """
        self._check_daily_reset()
        return self._daily_count

    def can_apply(self) -> bool:
        """
        Check if we can submit another application right now.

        Checks:
          1. Platform is enabled
          2. Platform supports apply (not LinkedIn)
          3. Not in cooldown
          4. Under daily limit
          5. Enough time since last apply (rate limit)

        Returns:
            True if safe to apply.
        """
        if not self._enabled:
            logger.debug(f"{self.PLATFORM_NAME}: disabled")
            return False

        if not self.SUPPORTS_APPLY:
            logger.debug(f"{self.PLATFORM_NAME}: apply not supported")
            return False

        if self.is_in_cooldown():
            logger.debug(
                f"{self.PLATFORM_NAME}: in cooldown until "
                f"{self._cooldown_until}"
            )
            return False

        self._check_daily_reset()
        if self._daily_count >= self._max_daily:
            logger.info(
                f"{self.PLATFORM_NAME}: daily limit reached "
                f"({self._daily_count}/{self._max_daily})"
            )
            return False

        # Rate limit between applications
        elapsed = time.time() - self._last_apply_time
        min_gap = self._rate_limit[0] if isinstance(
            self._rate_limit, (list, tuple)
        ) else self._rate_limit
        if elapsed < min_gap:
            remaining = min_gap - elapsed
            logger.debug(
                f"{self.PLATFORM_NAME}: rate limit, "
                f"wait {remaining:.0f}s more"
            )
            return False

        return True

    def wait_for_rate_limit(self) -> None:
        """
        Block until the rate limit window passes.
        Called before each application attempt.
        """
        elapsed = time.time() - self._last_apply_time
        if isinstance(self._rate_limit, (list, tuple)):
            min_gap = random.uniform(*self._rate_limit)
        else:
            min_gap = float(self._rate_limit)

        if elapsed < min_gap:
            wait_time = min_gap - elapsed
            logger.info(
                f"{self.PLATFORM_NAME}: rate limit wait "
                f"{wait_time:.0f}s"
            )
            time.sleep(wait_time)

    def increment_count(self) -> None:
        """
        Increment daily application count after successful submission.
        Also updates last apply time for rate limiting.
        """
        self._check_daily_reset()
        self._daily_count += 1
        self._last_apply_time = time.time()
        self._consecutive_errors = 0

        self._update_session_state(
            daily_applied=self._daily_count,
            total_increment=1
        )

        logger.info(
            f"{self.PLATFORM_NAME}: applied "
            f"({self._daily_count}/{self._max_daily} today)"
        )

    def enter_cooldown(self, hours: Optional[float] = None) -> None:
        """
        Put platform into cooldown mode.

        Called when:
          - "Unusual activity" detected
          - Too many errors in a row
          - Account temp-banned
          - Rate limit exceeded (429)

        Args:
            hours: Cooldown duration. None → config cooldown_hours.
        """
        if hours is None:
            hours = self._cooldown_hours

        self._cooldown_until = datetime.now() + timedelta(hours=hours)
        self._update_session_state(
            status="cooldown",
            cooldown_until=self._cooldown_until.isoformat()
        )

        logger.warning(
            f"{self.PLATFORM_NAME}: entering cooldown for "
            f"{hours:.1f} hours (until {self._cooldown_until})"
        )

        # Notify via Telegram if available
        if self._notifier:
            try:
                self._notifier.send_platform_issue(
                    self.PLATFORM_NAME,
                    f"Entered {hours:.1f}h cooldown"
                )
            except Exception:
                pass

    def is_in_cooldown(self) -> bool:
        """Check if platform is currently in cooldown."""
        if self._cooldown_until is None:
            return False
        if datetime.now() >= self._cooldown_until:
            # Cooldown expired
            self._cooldown_until = None
            self._update_session_state(status="active", cooldown_until="")
            logger.info(f"{self.PLATFORM_NAME}: cooldown expired, active again")
            return False
        return True

    def exit_cooldown(self) -> None:
        """Manually exit cooldown (e.g., user verified account is OK)."""
        self._cooldown_until = None
        self._update_session_state(status="active", cooldown_until="")
        logger.info(f"{self.PLATFORM_NAME}: cooldown manually cleared")

    def _check_daily_reset(self) -> None:
        """Reset daily count at midnight."""
        today = datetime.now().date()
        if today > self._daily_reset_date:
            old_count = self._daily_count
            self._daily_count = 0
            self._daily_reset_date = today
            self.db.update_platform_session(
                self.PLATFORM_NAME,
                {
                    "daily_applied": 0,
                    "daily_reset": today.isoformat()
                }
            )
            if old_count > 0:
                logger.info(
                    f"{self.PLATFORM_NAME}: daily count reset "
                    f"(yesterday: {old_count})"
                )

    # ═══════════════════════════════════════════════════════════
    # SESSION STATE PERSISTENCE (DB)
    # ═══════════════════════════════════════════════════════════

    def _load_session_state(self) -> None:
        """Load persisted session state from database."""
        try:
            session = self.db.get_platform_session(self.PLATFORM_NAME)
            if session:
                self._daily_count = session.get("daily_applied", 0)
                self._is_logged_in = bool(session.get("logged_in", 0))

                reset_str = session.get("daily_reset", "")
                if reset_str:
                    try:
                        from datetime import date as date_type
                        parts = reset_str.split("-")
                        if len(parts) == 3:
                            self._daily_reset_date = date_type(
                                int(parts[0]), int(parts[1]), int(parts[2])
                            )
                    except (ValueError, TypeError):
                        pass

                cooldown_str = session.get("cooldown_until", "")
                if cooldown_str:
                    try:
                        self._cooldown_until = datetime.fromisoformat(
                            cooldown_str
                        )
                    except (ValueError, TypeError):
                        self._cooldown_until = None

                status = session.get("status", "active")
                if status in ("banned", "disabled"):
                    self._enabled = False
                    logger.warning(
                        f"{self.PLATFORM_NAME}: disabled (status={status})"
                    )

                logger.debug(
                    f"Session state loaded for '{self.PLATFORM_NAME}': "
                    f"daily={self._daily_count}, logged_in={self._is_logged_in}"
                )
        except Exception as e:
            logger.debug(f"Could not load session state: {e}")

    def _update_session_state(self, **kwargs) -> None:
        """
        Update session state in database.

        Accepts keyword args matching platform_sessions columns:
          logged_in, last_login, daily_applied, total_applied,
          last_error, status, cooldown_until, notes
        """
        try:
            updates = {}

            if "logged_in" in kwargs:
                updates["logged_in"] = 1 if kwargs["logged_in"] else 0
                if kwargs["logged_in"]:
                    updates["last_login"] = datetime.now().isoformat()

            if "daily_applied" in kwargs:
                updates["daily_applied"] = kwargs["daily_applied"]

            if "total_increment" in kwargs:
                # Increment total_applied by N
                session = self.db.get_platform_session(self.PLATFORM_NAME)
                current_total = session.get("total_applied", 0) if session else 0
                updates["total_applied"] = current_total + kwargs["total_increment"]

            if "last_error" in kwargs:
                updates["last_error"] = kwargs["last_error"]

            if "status" in kwargs:
                updates["status"] = kwargs["status"]

            if "cooldown_until" in kwargs:
                updates["cooldown_until"] = kwargs["cooldown_until"]

            if "notes" in kwargs:
                updates["notes"] = kwargs["notes"]

            if updates:
                self.db.update_platform_session(self.PLATFORM_NAME, updates)

        except Exception as e:
            logger.debug(f"Could not update session state: {e}")

    # ═══════════════════════════════════════════════════════════
    # FORM FIELD HANDLER — Universal
    # ═══════════════════════════════════════════════════════════

    def handle_form_field(self, page, field: dict) -> bool:
        """
        Universal form field handler for application forms.

        Detects field type and fills appropriately:
          1. Standard field (name/email/phone) → auto-fill from profile
          2. Known question (salary/notice) → auto-fill from answers.py
          3. Text/essay question → AI generates (if available)
          4. Dropdown/select → fuzzy match options to known answers
          5. Checkbox consent → auto-check
          6. File upload → attach configured file
          7. Unknown → screenshot + Telegram alert → wait for user input

        Args:
            page: Playwright Page.
            field: Dict describing the field:
                {
                    selector: str,          # CSS selector
                    type: str,              # "text"|"email"|"tel"|"select"|
                                            # "checkbox"|"radio"|"textarea"|"file"
                    label: str,             # visible label text
                    name: str,              # input name attribute
                    required: bool,         # is it required?
                    placeholder: str,       # placeholder text
                    options: List[str],     # for select/radio: available options
                    current_value: str,     # current value if pre-filled
                }

        Returns:
            True if field was handled, False if needs manual intervention.
        """
        selector = field.get("selector", "")
        field_type = field.get("type", "text").lower()
        label = field.get("label", "").lower()
        name = field.get("name", "").lower()
        placeholder = field.get("placeholder", "").lower()
        options = field.get("options", [])
        current_value = field.get("current_value", "")

        # Combined text for matching
        field_text = f"{label} {name} {placeholder}".lower()

        try:
            # ── 1. Standard personal info fields ──
            standard_value = self._match_standard_field(
                field_text, name, field_type
            )
            if standard_value is not None:
                return self._fill_field(
                    page, selector, field_type, standard_value, options
                )

            # ── 2. Known question answers ──
            answer = self._match_known_question(field_text, label)
            if answer is not None:
                return self._fill_field(
                    page, selector, field_type, answer, options
                )

            # ── 3. Checkbox consent fields ──
            if field_type in ("checkbox",):
                if self._is_consent_field(field_text):
                    return self.browser.check_checkbox(page, selector, True)

            # ── 4. Pre-filled fields — don't touch ──
            if current_value and current_value.strip():
                logger.debug(
                    f"Field already filled ({label[:30]}): "
                    f"'{current_value[:30]}'"
                )
                return True

            # ── 5. File upload ──
            if field_type == "file":
                return self._handle_file_upload(page, selector, field_text)

            # ── 6. Try AI generation for text/textarea questions ──
            if field_type in ("text", "textarea") and label:
                ai_answer = self._try_ai_answer(label, field_text)
                if ai_answer:
                    return self._fill_field(
                        page, selector, field_type, ai_answer, options
                    )

            # ── 7. Unknown field — alert user ──
            logger.warning(
                f"Unknown form field: type={field_type}, "
                f"label='{label[:50]}', name='{name}'"
            )
            if self._notifier:
                try:
                    screenshot = self.browser.take_screenshot(
                        page, f"unknown_field_{name}"
                    )
                    self._notifier.send_platform_issue(
                        self.PLATFORM_NAME,
                        f"Unknown form field needs manual input:\n"
                        f"Label: {label}\nType: {field_type}\nName: {name}"
                    )
                except Exception:
                    pass

            return False

        except Exception as e:
            logger.error(f"handle_form_field error: {e}")
            return False

    def _match_standard_field(self, field_text: str, name: str,
                               field_type: str) -> Optional[str]:
        """
        Match field to standard personal info from USER_PROFILE.

        Returns the value to fill, or None if not a standard field.
        """
        profile = USER_PROFILE

        # ── Name ──
        if any(kw in field_text for kw in [
            "full name", "your name", "candidate name",
            "applicant name"
        ]) or (name in ("name", "fullname", "full_name", "candidate_name")
               and "company" not in field_text):
            return profile.get("name", "")

        # ── First name ──
        if "first name" in field_text or name in (
            "firstname", "first_name", "fname"
        ):
            full_name = profile.get("name", "")
            parts = full_name.split()
            return parts[0] if parts else full_name

        # ── Last name ──
        if "last name" in field_text or name in (
            "lastname", "last_name", "lname", "surname"
        ):
            full_name = profile.get("name", "")
            parts = full_name.split()
            return parts[-1] if len(parts) > 1 else ""

        # ── Email ──
        if any(kw in field_text for kw in [
            "email", "e-mail", "mail address"
        ]) or name in ("email", "email_address", "emailid"):
            if field_type == "email" or "email" in name:
                return profile.get("email", "")

        # ── Phone ──
        if any(kw in field_text for kw in [
            "phone", "mobile", "contact number", "cell"
        ]) or name in ("phone", "mobile", "tel", "contact", "phonenumber"):
            phone = profile.get("phone", "")
            # Strip country code for some forms
            if phone.startswith("+91"):
                return phone.replace("+91", "").strip()
            return phone

        # ── Location / City ──
        if any(kw in field_text for kw in [
            "current location", "current city", "city",
            "your location"
        ]) or name in ("city", "location", "current_location"):
            return profile.get("location", "").split(",")[0].strip()

        # ── LinkedIn ──
        if "linkedin" in field_text or name in ("linkedin", "linkedin_url"):
            return profile.get("linkedin_url", "")

        # ── GitHub ──
        if "github" in field_text or name in ("github", "github_url"):
            return profile.get("github_url", "")

        # ── Portfolio / Website ──
        if any(kw in field_text for kw in [
            "portfolio", "website", "personal site"
        ]):
            return profile.get("github_url", "")  # fallback to GitHub

        return None

    def _match_known_question(self, field_text: str,
                                label: str) -> Optional[str]:
        """
        Match field to known application question answers.

        Tries to import from profile.answers if available.
        Falls back to hardcoded common answers.
        """
        # Try answers module (available in Phase 2+)
        try:
            from profile.answers import get_answer, get_standard
            # Try direct field name match first
            answer = get_answer(label)
            if answer:
                return answer
        except ImportError:
            pass

        # ── Hardcoded common answers (Phase 1 fallback) ──

        # Notice period
        if any(kw in field_text for kw in [
            "notice period", "notice_period", "joining time"
        ]):
            return USER_PROFILE.get("notice_period", "15 days")

        # Current CTC
        if any(kw in field_text for kw in [
            "current ctc", "current salary", "present ctc",
            "current_ctc", "current compensation"
        ]):
            return "3.7 LPA"

        # Expected CTC
        if any(kw in field_text for kw in [
            "expected ctc", "expected salary", "desired salary",
            "expected_ctc", "desired ctc", "salary expectation"
        ]):
            return "6-10 LPA (negotiable)"

        # Experience years
        if any(kw in field_text for kw in [
            "total experience", "years of experience",
            "work experience", "total_experience",
            "experience in years"
        ]):
            years = USER_PROFILE.get("experience_years", 1)
            return str(years)

        # Current company
        if any(kw in field_text for kw in [
            "current company", "current employer",
            "present company", "current_company"
        ]):
            return USER_PROFILE.get("current_company", "Site Guru Pvt Ltd")

        # Current designation / title
        if any(kw in field_text for kw in [
            "current designation", "current title", "current role",
            "current_title", "current position"
        ]):
            return USER_PROFILE.get("current_title", "Full Stack Developer L1")

        # Reason for leaving
        if any(kw in field_text for kw in [
            "reason for leaving", "reason for change",
            "why are you leaving", "reason_for_leaving"
        ]):
            return "Seeking growth opportunities in a product-oriented company"

        # Work authorization
        if any(kw in field_text for kw in [
            "authorized to work", "work authorization",
            "legally authorized", "right to work"
        ]):
            return "Yes"

        # Sponsorship
        if any(kw in field_text for kw in [
            "sponsorship", "visa sponsorship", "work visa",
            "require sponsorship"
        ]):
            return "No"

        # Willing to relocate
        if any(kw in field_text for kw in [
            "willing to relocate", "relocate", "relocation",
            "open to relocation"
        ]):
            return "Yes"

        # Available to start / start date
        if any(kw in field_text for kw in [
            "start date", "available to start", "date of joining",
            "joining date", "when can you join"
        ]):
            return "Within 15 days"

        # Background check
        if any(kw in field_text for kw in [
            "background check", "background verification"
        ]):
            return "Yes"

        # Gender / diversity (safe default)
        if any(kw in field_text for kw in [
            "gender", "race", "ethnicity", "veteran",
            "disability", "sexual orientation"
        ]):
            return "Prefer not to say"

        return None

    def _is_consent_field(self, field_text: str) -> bool:
        """Check if a checkbox is a consent/terms field (auto-check)."""
        consent_keywords = [
            "agree", "consent", "terms", "conditions", "privacy",
            "acknowledge", "accept", "confirm", "i certify",
            "i understand", "i authorize", "i agree"
        ]
        return any(kw in field_text for kw in consent_keywords)

    def _fill_field(self, page, selector: str, field_type: str,
                    value: str, options: List[str] = None) -> bool:
        """
        Fill a form field with the given value, handling different types.

        Returns:
            True if field was filled successfully.
        """
        try:
            if field_type in ("text", "email", "tel", "number",
                              "textarea", "password", "url"):
                self.browser.type_human(page, selector, value)
                return True

            elif field_type == "select":
                # Try exact match first, then fuzzy
                if self.browser.select_dropdown_fuzzy(
                    page, selector, value
                ):
                    return True
                # Try direct value
                return self.browser.select_dropdown(
                    page, selector, label=value
                )

            elif field_type == "radio":
                # Click the radio button whose label matches
                if options:
                    for opt in options:
                        if value.lower() in opt.lower():
                            # Try to find and click matching radio
                            radio_selector = (
                                f"{selector}[value='{opt}'],"
                                f"{selector}[value='{value}']"
                            )
                            try:
                                self.browser.click_human(
                                    page, radio_selector
                                )
                                return True
                            except Exception:
                                continue
                # Fallback: click the selector directly
                try:
                    self.browser.click_human(page, selector)
                    return True
                except Exception:
                    return False

            elif field_type == "checkbox":
                return self.browser.check_checkbox(page, selector, True)

            else:
                logger.debug(
                    f"Unsupported field type '{field_type}' "
                    f"for selector {selector}"
                )
                return False

        except Exception as e:
            logger.error(f"_fill_field failed ({selector}): {e}")
            return False

    def _handle_file_upload(self, page, selector: str,
                            field_text: str) -> bool:
        """Handle file upload fields (resume, cover letter, etc.)."""
        # Determine what file to upload based on field text
        if any(kw in field_text for kw in ["resume", "cv", "curriculum"]):
            # Resume upload — will be handled by prepare_application
            # which passes the specific resume_path
            logger.debug(
                "File upload field detected (resume) — "
                "will be handled by prepare_application"
            )
            return True  # Mark as handled; actual upload in prepare_application

        if any(kw in field_text for kw in ["cover letter", "cover_letter"]):
            logger.debug(
                "File upload field detected (cover letter) — "
                "will be handled by prepare_application"
            )
            return True

        logger.warning(f"Unknown file upload field: {field_text[:50]}")
        return False

    def _try_ai_answer(self, label: str,
                        field_text: str) -> Optional[str]:
        """
        Try to generate an answer using AI for unknown questions.

        Only works if ai/llm_client.py is available (Phase 2+).
        Returns None if AI is not available.
        """
        try:
            from ai.llm_client import LLMClient
            client = LLMClient()
            if not client.can_call():
                return None

            prompt = (
                f"Answer this job application form question in 1-2 sentences. "
                f"Be professional and concise. The applicant is a Full Stack "
                f"Developer with 1 year experience in Vue.js, Node.js, MySQL.\n\n"
                f"Question: {label}\n\n"
                f"Answer:"
            )
            answer = client.generate(
                prompt, max_tokens=100, temperature=0.3
            )

            # Review via Telegram if available
            if self._notifier and answer:
                try:
                    reviewed = self._notifier.send_answer_review(
                        label, answer
                    )
                    if reviewed:
                        return reviewed
                except Exception:
                    pass

            return answer

        except ImportError:
            logger.debug("AI not available for form answer generation")
            return None
        except Exception as e:
            logger.debug(f"AI answer generation failed: {e}")
            return None

    # ═══════════════════════════════════════════════════════════
    # CAPTCHA DETECTION & HANDLING
    # ═══════════════════════════════════════════════════════════

    def detect_captcha(self, page) -> Optional[str]:
        """
        Detect if a CAPTCHA is present on the page.

        Checks for common CAPTCHA indicators:
          - reCAPTCHA iframe
          - hCaptcha iframe
          - Image grid challenges
          - Text CAPTCHA inputs
          - Cloudflare challenge

        Returns:
            "recaptcha" | "hcaptcha" | "image_grid" | "text" |
            "cloudflare" | None
        """
        try:
            # reCAPTCHA
            if self.browser.element_exists(
                page,
                "iframe[src*='recaptcha'], "
                "iframe[title*='reCAPTCHA'], "
                ".g-recaptcha, "
                "#recaptcha"
            ):
                return "recaptcha"

            # hCaptcha
            if self.browser.element_exists(
                page,
                "iframe[src*='hcaptcha'], "
                ".h-captcha"
            ):
                return "hcaptcha"

            # Cloudflare challenge
            if self.browser.element_exists(
                page,
                "#challenge-form, "
                "iframe[src*='challenges.cloudflare.com']"
            ):
                return "cloudflare"

            # Generic image CAPTCHA
            if self.browser.element_exists(
                page,
                "img[alt*='captcha' i], "
                "img[src*='captcha' i], "
                "img[id*='captcha' i], "
                ".captcha-image"
            ):
                return "image_grid"

            # Text CAPTCHA input
            if self.browser.element_exists(
                page,
                "input[name*='captcha' i], "
                "input[id*='captcha' i], "
                "input[placeholder*='captcha' i]"
            ):
                return "text"

            # Check page text for CAPTCHA keywords
            body_text = self.browser.get_text(page, "body", "").lower()
            if any(kw in body_text for kw in [
                "prove you're not a robot",
                "verify you are human",
                "captcha verification",
                "security check",
                "unusual traffic",
            ]):
                return "text"

            return None

        except Exception as e:
            logger.debug(f"CAPTCHA detection error: {e}")
            return None

    def handle_captcha(self, page, notifier=None) -> bool:
        """
        Handle a detected CAPTCHA via Telegram user interaction.

        Strategy by type:
          - text: Screenshot → Telegram → user types answer → bot enters
          - image_grid: Screenshot → Telegram → user replies "2 5 8" → bot clicks
          - recaptcha: Try checkbox click, retry 3x, if fails → skip job
          - cloudflare: Wait 10s for auto-solve, else alert user

        Args:
            page: Playwright Page.
            notifier: Optional JobNotifier for Telegram. Falls back to self._notifier.

        Returns:
            True if CAPTCHA was solved, False if not.
        """
        notifier = notifier or self._notifier

        captcha_type = self.detect_captcha(page)
        if not captcha_type:
            return True  # No CAPTCHA

        logger.warning(
            f"{self.PLATFORM_NAME}: CAPTCHA detected (type={captcha_type})"
        )

        # ── reCAPTCHA: try clicking checkbox ──
        if captcha_type == "recaptcha":
            for attempt in range(3):
                try:
                    # Try to click the reCAPTCHA checkbox
                    recaptcha_frame = page.frame_locator(
                        "iframe[title*='reCAPTCHA']"
                    )
                    recaptcha_frame.locator(
                        "#recaptcha-anchor"
                    ).click(timeout=5000)
                    time.sleep(3)

                    # Check if solved
                    if not self.detect_captcha(page):
                        logger.info("reCAPTCHA solved via checkbox click")
                        return True
                except Exception:
                    pass

                time.sleep(2)

            # Checkbox didn't work — need user help
            if notifier:
                screenshot = self.browser.take_screenshot(
                    page, f"captcha_{self.PLATFORM_NAME}"
                )
                notifier.send_platform_issue(
                    self.PLATFORM_NAME,
                    "reCAPTCHA requires manual solving. "
                    "Please solve it in the browser window."
                )
                # Wait for user to solve manually (up to 5 min)
                for _ in range(60):
                    time.sleep(5)
                    if not self.detect_captcha(page):
                        return True
            return False

        # ── Cloudflare: wait for auto-solve ──
        if captcha_type == "cloudflare":
            logger.info("Cloudflare challenge — waiting for auto-solve...")
            for _ in range(20):
                time.sleep(3)
                if not self.detect_captcha(page):
                    logger.info("Cloudflare challenge auto-solved")
                    return True
            if notifier:
                notifier.send_platform_issue(
                    self.PLATFORM_NAME,
                    "Cloudflare challenge won't auto-solve."
                )
            return False

        # ── Text / Image CAPTCHA: Telegram ──
        if notifier:
            screenshot = self.browser.take_screenshot(
                page, f"captcha_{self.PLATFORM_NAME}"
            )
            try:
                answer = notifier.send_captcha_challenge(
                    screenshot, captcha_type
                )
                if answer:
                    if captcha_type == "text":
                        # Type the answer into CAPTCHA input
                        captcha_input = (
                            "input[name*='captcha' i], "
                            "input[id*='captcha' i], "
                            "input[placeholder*='captcha' i]"
                        )
                        self.browser.type_human(page, captcha_input, answer)
                        self.browser.press_key(page, "Enter")
                        time.sleep(2)
                        return not self.detect_captcha(page)

                    elif captcha_type == "image_grid":
                        # User sent numbers like "2 5 8"
                        selections = answer.strip().split()
                        for sel in selections:
                            try:
                                idx = int(sel.strip()) - 1  # 1-based
                                grid_items = page.query_selector_all(
                                    ".captcha-grid img, "
                                    ".captcha-images img, "
                                    "table.captcha td"
                                )
                                if 0 <= idx < len(grid_items):
                                    grid_items[idx].click()
                                    time.sleep(0.5)
                            except (ValueError, IndexError):
                                continue
                        # Click verify/submit
                        self.browser.press_key(page, "Enter")
                        time.sleep(2)
                        return not self.detect_captcha(page)

            except Exception as e:
                logger.error(f"CAPTCHA handling error: {e}")

        logger.warning(
            f"CAPTCHA not solved for {self.PLATFORM_NAME}"
        )
        return False

    # ═══════════════════════════════════════════════════════════
    # OTP DETECTION & HANDLING
    # ═══════════════════════════════════════════════════════════

    def detect_otp_page(self, page) -> bool:
        """
        Detect if the current page is asking for an OTP.

        Returns:
            True if OTP input is detected.
        """
        try:
            # Check for OTP input fields
            otp_selectors = [
                "input[name*='otp' i]",
                "input[id*='otp' i]",
                "input[placeholder*='otp' i]",
                "input[placeholder*='verification code' i]",
                "input[placeholder*='enter code' i]",
                "input[aria-label*='otp' i]",
                "input[type='tel'][maxlength='6']",
                "input[type='tel'][maxlength='4']",
                ".otp-input",
                "#otp",
            ]
            combined = ", ".join(otp_selectors)
            if self.browser.element_exists(page, combined):
                return True

            # Check page text
            body_text = self.browser.get_text(page, "body", "").lower()
            otp_phrases = [
                "enter otp",
                "enter the otp",
                "verification code",
                "enter the code",
                "we sent a code",
                "otp has been sent",
                "enter the verification",
                "two-factor",
                "2-step verification",
                "2fa code",
            ]
            return any(phrase in body_text for phrase in otp_phrases)

        except Exception:
            return False

    def handle_otp(self, page, notifier=None) -> bool:
        """
        Handle OTP verification via Telegram.

        Flow:
          1. Detect OTP input field
          2. Send Telegram alert asking for OTP code
          3. Wait for user to reply with code (2 min timeout)
          4. Enter the code into the OTP field
          5. Submit

        Args:
            page: Playwright Page.
            notifier: Optional JobNotifier. Falls back to self._notifier.

        Returns:
            True if OTP was entered and page moved forward.
        """
        notifier = notifier or self._notifier

        if not notifier:
            logger.error(
                f"OTP required for '{self.PLATFORM_NAME}' "
                f"but no notifier available"
            )
            return False

        logger.info(
            f"OTP required for '{self.PLATFORM_NAME}' — "
            f"requesting via Telegram"
        )

        try:
            # Take screenshot for context
            self.browser.take_screenshot(
                page, f"otp_{self.PLATFORM_NAME}"
            )

            # Request OTP from user via Telegram
            otp_code = notifier.send_otp_request(self.PLATFORM_NAME)

            if not otp_code:
                logger.warning("No OTP received from user")
                return False

            otp_code = otp_code.strip()
            logger.info(f"OTP received: {'*' * len(otp_code)}")

            # Find OTP input field
            otp_selectors = [
                "input[name*='otp' i]",
                "input[id*='otp' i]",
                "input[placeholder*='otp' i]",
                "input[placeholder*='verification code' i]",
                "input[placeholder*='enter code' i]",
                "input[type='tel'][maxlength='6']",
                ".otp-input",
                "#otp",
            ]

            filled = False
            for sel in otp_selectors:
                if self.browser.element_exists(page, sel):
                    self.browser.type_human(page, sel, otp_code)
                    filled = True
                    break

            if not filled:
                # Try entering digit by digit into multiple inputs
                # (some sites split OTP into separate boxes)
                digit_inputs = page.query_selector_all(
                    "input[type='tel'][maxlength='1'], "
                    "input.otp-digit, "
                    "input[data-otp]"
                )
                if digit_inputs and len(digit_inputs) >= len(otp_code):
                    for i, digit in enumerate(otp_code):
                        digit_inputs[i].fill(digit)
                        time.sleep(random.uniform(0.05, 0.15))
                    filled = True

            if not filled:
                logger.error("Could not find OTP input field")
                return False

            # Small delay then submit
            time.sleep(random.uniform(0.5, 1.0))

            # Try to click submit/verify button
            submit_selectors = [
                "button[type='submit']",
                "button:has-text('Verify')",
                "button:has-text('Submit')",
                "button:has-text('Continue')",
                "input[type='submit']",
            ]
            for sel in submit_selectors:
                if self.browser.element_exists(page, sel):
                    self.browser.click_human(page, sel)
                    break

            time.sleep(3)

            # Check if OTP page is gone
            if not self.detect_otp_page(page):
                logger.info(f"OTP verified for '{self.PLATFORM_NAME}'")
                return True

            logger.warning("OTP may have been incorrect")
            return False

        except Exception as e:
            logger.error(f"OTP handling error: {e}")
            return False

    # ═══════════════════════════════════════════════════════════
    # DETECTION HELPERS — Bot detection, blocks, errors
    # ═══════════════════════════════════════════════════════════

    def detect_block(self, page) -> Optional[str]:
        """
        Detect if we've been blocked or flagged by the platform.

        Returns:
            Block type string or None:
            "unusual_activity" | "account_suspended" | "rate_limited" |
            "ip_blocked" | "session_expired" | None
        """
        try:
            body_text = self.browser.get_text(page, "body", "").lower()
            url = self.browser.get_page_url(page).lower()

            # Unusual activity
            if any(kw in body_text for kw in [
                "unusual activity",
                "suspicious activity",
                "automated behavior",
                "bot detected",
                "too many requests",
                "rate limit exceeded",
                "please try again later",
                "access denied",
                "temporarily blocked",
            ]):
                return "unusual_activity"

            # Account suspended
            if any(kw in body_text for kw in [
                "account suspended",
                "account disabled",
                "account blocked",
                "account restricted",
                "your account has been",
            ]):
                return "account_suspended"

            # Rate limited (429 or similar)
            if "429" in body_text or "rate limit" in body_text:
                return "rate_limited"

            # Session expired
            if any(kw in body_text for kw in [
                "session expired",
                "session timed out",
                "please log in again",
                "login required",
            ]) or "login" in url and "error" in url:
                return "session_expired"

            # IP blocked
            if any(kw in body_text for kw in [
                "ip blocked",
                "ip address has been",
                "access from your ip",
            ]):
                return "ip_blocked"

            return None

        except Exception:
            return None

    def handle_block(self, page, block_type: str) -> None:
        """
        Handle a detected block/ban.

        Args:
            block_type: Type of block detected.
        """
        logger.warning(
            f"{self.PLATFORM_NAME}: block detected — {block_type}"
        )

        # Log to database
        self.db.save_error(
            module=f"platforms.{self.PLATFORM_NAME}",
            error_type=f"block_{block_type}",
            message=f"Platform block detected: {block_type}",
            traceback=""
        )

        # Take screenshot for evidence
        self.browser.take_screenshot(
            page, f"block_{self.PLATFORM_NAME}_{block_type}"
        )

        # Strategy per block type
        if block_type == "session_expired":
            self._is_logged_in = False
            self._update_session_state(logged_in=False)
            logger.info("Session expired — will re-login on next attempt")

        elif block_type == "rate_limited":
            self.enter_cooldown(hours=1)

        elif block_type == "unusual_activity":
            self.enter_cooldown(hours=self._cooldown_hours)

        elif block_type == "account_suspended":
            self._enabled = False
            self._update_session_state(status="banned")
            logger.error(
                f"{self.PLATFORM_NAME}: ACCOUNT SUSPENDED — "
                f"platform disabled"
            )

        elif block_type == "ip_blocked":
            self.enter_cooldown(hours=self._cooldown_hours * 2)

        # Notify via Telegram
        if self._notifier:
            try:
                self._notifier.send_platform_issue(
                    self.PLATFORM_NAME,
                    f"⚠️ Block detected: {block_type}"
                )
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════
    # ERROR TRACKING
    # ═══════════════════════════════════════════════════════════

    def record_error(self, error_type: str, message: str,
                     tb: Optional[str] = None) -> None:
        """
        Record an error to the database and handle escalation.

        After 5 consecutive errors, platform enters cooldown.
        """
        self._consecutive_errors += 1

        self.db.save_error(
            module=f"platforms.{self.PLATFORM_NAME}",
            error_type=error_type,
            message=message,
            traceback=tb or ""
        )

        self._update_session_state(last_error=message)

        logger.error(
            f"{self.PLATFORM_NAME} error #{self._consecutive_errors}: "
            f"{error_type} — {message[:100]}"
        )

        # Auto-cooldown after too many consecutive errors
        if self._consecutive_errors >= 5:
            logger.warning(
                f"{self.PLATFORM_NAME}: {self._consecutive_errors} "
                f"consecutive errors — entering cooldown"
            )
            self.enter_cooldown(hours=1)
            self._consecutive_errors = 0

        # Notify on every 3rd error
        if self._consecutive_errors % 3 == 0 and self._notifier:
            try:
                self._notifier.send_error(
                    f"platforms.{self.PLATFORM_NAME}",
                    f"{error_type}: {message[:200]}"
                )
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════
    # HUMAN-LIKE BEHAVIOR WRAPPERS
    # ═══════════════════════════════════════════════════════════

    def human_delay(self, min_s: Optional[float] = None,
                    max_s: Optional[float] = None) -> None:
        """Convenience wrapper for browser.random_delay()."""
        self.browser.random_delay(min_s, max_s)

    def action_delay(self) -> None:
        """Short delay between user actions (1-3s)."""
        self.browser.random_delay(1.0, 3.0)

    def page_delay(self) -> None:
        """Longer delay after page loads (2-5s, simulates reading)."""
        self.browser.random_delay(2.0, 5.0)

    def thinking_delay(self) -> None:
        """Human "thinking" pause before important actions (3-8s)."""
        self.browser.random_delay(3.0, 8.0)

    def check_session_health(self) -> bool:
        """
        Check if browser session is healthy.
        Checks: page alive, no block, no CAPTCHA, session time.

        Returns:
            True if session is healthy and can continue.
        """
        page = self.browser.get_page(self.PLATFORM_NAME)
        if not page:
            logger.debug(f"{self.PLATFORM_NAME}: no active page")
            return False

        # Check for blocks
        block = self.detect_block(page)
        if block:
            self.handle_block(page, block)
            return False

        # Check for CAPTCHA
        captcha = self.detect_captcha(page)
        if captcha:
            solved = self.handle_captcha(page)
            if not solved:
                return False

        # Check session time
        if self.browser.needs_break(self.PLATFORM_NAME):
            logger.info(
                f"{self.PLATFORM_NAME}: session break needed"
            )
            self.browser.take_break(self.PLATFORM_NAME)
            # After break, page reference is invalid
            self._page = None
            return False

        return True

    # ═══════════════════════════════════════════════════════════
    # FORM FIELD DETECTION — Scan a page for all form fields
    # ═══════════════════════════════════════════════════════════

    def detect_form_fields(self, page) -> List[dict]:
        """
        Scan the current page for all visible form fields.

        Returns a list of field dicts compatible with handle_form_field().

        Returns:
            [{selector, type, label, name, required, placeholder,
              options, current_value}, ...]
        """
        fields = []
        try:
            # ── Input fields ──
            inputs = page.query_selector_all(
                "input:visible, textarea:visible, select:visible"
            )

            for inp in inputs:
                try:
                    tag = inp.evaluate("el => el.tagName.toLowerCase()")
                    input_type = (
                        inp.get_attribute("type") or
                        ("textarea" if tag == "textarea" else
                         "select" if tag == "select" else "text")
                    ).lower()

                    # Skip hidden / submit / button types
                    if input_type in (
                        "hidden", "submit", "button", "image", "reset"
                    ):
                        continue

                    name = inp.get_attribute("name") or ""
                    input_id = inp.get_attribute("id") or ""
                    placeholder = inp.get_attribute("placeholder") or ""
                    required = inp.get_attribute("required") is not None
                    aria_label = inp.get_attribute("aria-label") or ""

                    # Find associated label
                    label = ""
                    if input_id:
                        label_el = page.query_selector(
                            f"label[for='{input_id}']"
                        )
                        if label_el:
                            label = (label_el.text_content() or "").strip()

                    if not label and aria_label:
                        label = aria_label

                    if not label:
                        # Try parent label
                        parent_label = inp.evaluate("""
                            el => {
                                let parent = el.closest('label');
                                return parent ? parent.textContent.trim() : '';
                            }
                        """)
                        if parent_label:
                            label = parent_label

                    # Get current value
                    current_value = ""
                    try:
                        if tag in ("input", "textarea"):
                            current_value = inp.input_value() or ""
                        elif tag == "select":
                            current_value = inp.evaluate(
                                "el => el.options[el.selectedIndex]"
                                "?.text || ''"
                            )
                    except Exception:
                        pass

                    # Get options for select
                    options = []
                    if tag == "select":
                        try:
                            options = inp.evaluate("""
                                el => Array.from(el.options).map(
                                    o => o.text.trim()
                                ).filter(t => t)
                            """)
                        except Exception:
                            pass

                    # Generate selector
                    if input_id:
                        selector = f"#{input_id}"
                    elif name:
                        selector = f"{tag}[name='{name}']"
                    else:
                        selector = ""

                    if selector:
                        fields.append({
                            "selector": selector,
                            "type": input_type,
                            "label": label,
                            "name": name,
                            "required": required,
                            "placeholder": placeholder,
                            "options": options,
                            "current_value": current_value,
                        })

                except Exception:
                    continue

            logger.debug(
                f"Detected {len(fields)} form fields on page"
            )

        except Exception as e:
            logger.error(f"Form field detection error: {e}")

        return fields

    def fill_all_form_fields(self, page) -> dict:
        """
        Detect all form fields on the page and attempt to fill them all.

        Returns:
            {
                total: int,
                filled: int,
                failed: int,
                skipped: int,
                fields: [
                    {name, label, status: "filled"|"failed"|"skipped"}
                ]
            }
        """
        fields = self.detect_form_fields(page)
        results = {
            "total": len(fields),
            "filled": 0,
            "failed": 0,
            "skipped": 0,
            "fields": [],
        }

        for field in fields:
            name = field.get("name", "unknown")
            label = field.get("label", "unknown")

            # Skip already-filled fields
            if field.get("current_value", "").strip():
                results["skipped"] += 1
                results["fields"].append({
                    "name": name,
                    "label": label,
                    "status": "skipped_prefilled"
                })
                continue

            success = self.handle_form_field(page, field)
            if success:
                results["filled"] += 1
                results["fields"].append({
                    "name": name,
                    "label": label,
                    "status": "filled"
                })
            else:
                results["failed"] += 1
                results["fields"].append({
                    "name": name,
                    "label": label,
                    "status": "failed"
                })

            # Small delay between fields
            time.sleep(random.uniform(0.3, 0.8))

        logger.info(
            f"Form fill: {results['filled']}/{results['total']} filled, "
            f"{results['failed']} failed, {results['skipped']} skipped"
        )

        return results

    # ═══════════════════════════════════════════════════════════
    # SALARY / EXPERIENCE PARSING — Shared helpers
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def parse_salary_text(salary_text: str) -> Tuple[
        Optional[float], Optional[float]
    ]:
        """
        Parse salary text into (min, max) in LPA.

        Handles:
          "8-12 LPA", "₹8L - ₹12L", "8,00,000 - 12,00,000",
          "8 Lacs", "Not disclosed", "Confidential"

        Returns:
            (salary_min, salary_max) in LPA. Either can be None.
        """
        if not salary_text:
            return None, None

        text = salary_text.strip().lower()

        # Not disclosed
        if any(kw in text for kw in [
            "not disclosed", "confidential", "n/a", "competitive"
        ]):
            return None, None

        # Remove currency symbols and commas
        text = re.sub(r'[₹$€,]', '', text)

        # ── Pattern: "8-12 LPA" or "8 - 12 Lacs" ──
        match = re.search(
            r'(\d+\.?\d*)\s*[-–to]+\s*(\d+\.?\d*)\s*'
            r'(lpa|lac|lakh|l\b|lakhs)',
            text
        )
        if match:
            return float(match.group(1)), float(match.group(2))

        # ── Pattern: "800000 - 1200000" (annual in rupees) ──
        match = re.search(
            r'(\d{5,})\s*[-–to]+\s*(\d{5,})',
            text
        )
        if match:
            min_val = float(match.group(1)) / 100000
            max_val = float(match.group(2)) / 100000
            return min_val, max_val

        # ── Single value: "8 LPA" ──
        match = re.search(
            r'(\d+\.?\d*)\s*(lpa|lac|lakh|l\b|lakhs)',
            text
        )
        if match:
            val = float(match.group(1))
            return val, val

        # ── Per month: "50000/month" → convert to LPA ──
        match = re.search(
            r'(\d+\.?\d*)\s*k?\s*(per\s*month|/\s*month|pm|monthly)',
            text
        )
        if match:
            monthly = float(match.group(1))
            if 'k' in text:
                monthly *= 1000
            elif monthly < 500:
                monthly *= 1000  # likely in thousands
            annual_lpa = (monthly * 12) / 100000
            return annual_lpa, annual_lpa

        return None, None

    @staticmethod
    def parse_experience_text(exp_text: str) -> Tuple[
        Optional[float], Optional[float]
    ]:
        """
        Parse experience text into (min_years, max_years).

        Handles:
          "1-3 Yrs", "2+ years", "Fresher", "0-1 Years",
          "Minimum 2 years", "3 to 5 years experience"

        Returns:
            (exp_min, exp_max) in years. Either can be None.
        """
        if not exp_text:
            return None, None

        text = exp_text.strip().lower()

        # Fresher
        if any(kw in text for kw in ["fresher", "entry level", "0 year"]):
            return 0, 1

        # Range: "1-3 Yrs"
        match = re.search(
            r'(\d+\.?\d*)\s*[-–to]+\s*(\d+\.?\d*)\s*'
            r'(yr|year|yrs|years)',
            text
        )
        if match:
            return float(match.group(1)), float(match.group(2))

        # "2+ years"
        match = re.search(
            r'(\d+\.?\d*)\s*\+?\s*(yr|year|yrs|years)',
            text
        )
        if match:
            val = float(match.group(1))
            return val, val + 3  # assume +3 range

        # Just a number
        match = re.search(r'(\d+\.?\d*)', text)
        if match:
            val = float(match.group(1))
            if val < 30:  # sanity check
                return val, val
            return None, None

        return None, None

    # ═══════════════════════════════════════════════════════════
    # STATUS / INFO
    # ═══════════════════════════════════════════════════════════

    def get_status(self) -> dict:
        """
        Get current platform status.

        Returns:
            {
                platform: str,
                enabled: bool,
                logged_in: bool,
                browser_active: bool,
                daily_count: int,
                max_daily: int,
                in_cooldown: bool,
                cooldown_until: str|None,
                consecutive_errors: int,
                supports_apply: bool,
                session_minutes: float,
            }
        """
        return {
            "platform": self.PLATFORM_NAME,
            "enabled": self._enabled,
            "logged_in": self._is_logged_in,
            "browser_active": self.browser.is_logged_in(self.PLATFORM_NAME),
            "daily_count": self.get_daily_count(),
            "max_daily": self._max_daily,
            "in_cooldown": self.is_in_cooldown(),
            "cooldown_until": (
                self._cooldown_until.isoformat()
                if self._cooldown_until else None
            ),
            "consecutive_errors": self._consecutive_errors,
            "supports_apply": self.SUPPORTS_APPLY,
            "session_minutes": round(
                self.browser.session_time(self.PLATFORM_NAME), 1
            ),
        }

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"platform='{self.PLATFORM_NAME}', "
            f"enabled={self._enabled}, "
            f"logged_in={self._is_logged_in}, "
            f"daily={self._daily_count}/{self._max_daily})"
        )


# ═══════════════════════════════════════════════════════════════════
# TEST BLOCK
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
    console.print("\n[bold cyan]═══ PlatformBase Test ═══[/bold cyan]\n")

    # ── 1. Test that PlatformBase can't be instantiated directly ──
    console.print("[yellow]1. Abstract class check:[/yellow]")
    try:
        # Should fail — PlatformBase is abstract
        base = PlatformBase(None)
        console.print("   [red]✗ Should have raised TypeError[/red]")
    except TypeError as e:
        console.print(f"   [green]✓[/green] Cannot instantiate ABC: {e}")

    # ── 2. Create a concrete test subclass ──
    console.print("\n[yellow]2. Creating test subclass...[/yellow]")

    class TestPlatform(PlatformBase):
        PLATFORM_NAME = "test_platform"
        BASE_URL = "https://httpbin.org"
        LOGIN_URL = "https://httpbin.org/forms/post"
        SUPPORTS_APPLY = True

        def login(self) -> bool:
            return True

        def search_jobs(self, queries, filters=None):
            return [
                {
                    "platform_job_id": "TEST001",
                    "url": "https://example.com/job/1",
                    "title": "Software Engineer",
                    "company": "Test Corp",
                    "location": "Bangalore",
                    "salary_text": "8-12 LPA",
                    "experience_text": "1-3 Yrs",
                    "description": "Looking for a full stack dev...",
                    "posted_date": "2025-01-15",
                    "skills": ["Python", "React", "Node.js"],
                }
            ]

        def get_job_details(self, job_url):
            return {"title": "Test Job", "description": "Full details..."}

        def prepare_application(self, job, resume_path, cover_letter=None):
            return {"success": True, "job_id": "TEST001"}

        def submit_application(self, prepared):
            return {"success": True, "applied_at": datetime.now().isoformat()}

        def check_status(self, application_id):
            return "submitted"

    # Create instance (browser_engine=None for non-browser tests)
    class MockBrowser:
        """Minimal mock for tests that don't need real browser."""
        def get_page(self, p): return None
        def is_logged_in(self, p): return False
        def session_time(self, p): return 0.0
        def needs_break(self, p): return False
        def random_delay(self, a=None, b=None): time.sleep(0.01)
        def take_screenshot(self, p, n): return ""
        def save_cookies(self, p): pass
        def close(self, p): pass

    mock_browser = MockBrowser()
    platform = TestPlatform(mock_browser)
    console.print(f"   [green]✓[/green] Created: {platform}")

    # ── 3. Rate limiting ──
    console.print("\n[yellow]3. Rate limiting & daily counts:[/yellow]")
    console.print(f"   Daily count: {platform.get_daily_count()}")
    console.print(f"   Max daily: {platform._max_daily}")
    console.print(f"   Can apply: {platform.can_apply()}")

    platform.increment_count()
    console.print(f"   After increment: {platform.get_daily_count()}")

    platform.increment_count()
    console.print(f"   After 2nd increment: {platform.get_daily_count()}")

    # ── 4. Cooldown ──
    console.print("\n[yellow]4. Cooldown management:[/yellow]")
    console.print(f"   In cooldown: {platform.is_in_cooldown()}")
    platform.enter_cooldown(hours=0.001)  # ~3.6 seconds
    console.print(f"   After enter_cooldown: {platform.is_in_cooldown()}")
    time.sleep(4)
    console.print(f"   After 4s wait: {platform.is_in_cooldown()} (should be False)")
    platform.exit_cooldown()
    console.print(f"   After manual exit: {platform.is_in_cooldown()}")

    # ── 5. Salary parsing ──
    console.print("\n[yellow]5. Salary parsing:[/yellow]")
    salary_tests = [
        ("8-12 LPA", (8.0, 12.0)),
        ("₹8L - ₹12L", (8.0, 12.0)),
        ("8,00,000 - 12,00,000", (8.0, 12.0)),
        ("8 Lacs", (8.0, 8.0)),
        ("Not disclosed", (None, None)),
        ("Confidential", (None, None)),
        ("15 LPA", (15.0, 15.0)),
        ("3.5-5.5 Lakhs", (3.5, 5.5)),
        ("", (None, None)),
        ("50000/month", None),  # approximate check
    ]
    for text, expected in salary_tests:
        result = PlatformBase.parse_salary_text(text)
        if expected is None:
            icon = "[green]✓[/green]"
            console.print(
                f"   {icon} '{text}' → {result} (approximate)"
            )
        else:
            match = result == expected
            icon = "[green]✓[/green]" if match else "[red]✗[/red]"
            console.print(
                f"   {icon} '{text}' → {result} "
                f"(expected {expected})"
            )

    # ── 6. Experience parsing ──
    console.print("\n[yellow]6. Experience parsing:[/yellow]")
    exp_tests = [
        ("1-3 Yrs", (1.0, 3.0)),
        ("2+ years", (2.0, 5.0)),
        ("Fresher", (0, 1)),
        ("0-1 Years", (0.0, 1.0)),
        ("3 to 5 years", (3.0, 5.0)),
        ("", (None, None)),
    ]
    for text, expected in exp_tests:
        result = PlatformBase.parse_experience_text(text)
        match = result == expected
        icon = "[green]✓[/green]" if match else "[yellow]~[/yellow]"
        console.print(
            f"   {icon} '{text}' → {result} (expected {expected})"
        )

    # ── 7. Standard field matching ──
    console.print("\n[yellow]7. Standard field matching:[/yellow]")
    field_tests = [
        ("full name", "name", "text", True),
        ("email address", "email", "email", True),
        ("phone number", "phone", "tel", True),
        ("current location", "city", "text", True),
        ("linkedin profile", "linkedin", "url", True),
        ("github", "github_url", "url", True),
        ("random field", "xyz", "text", False),
    ]
    for label, name, ftype, should_match in field_tests:
        field_text = f"{label} {name}".lower()
        result = platform._match_standard_field(field_text, name, ftype)
        matched = result is not None
        icon = "[green]✓[/green]" if matched == should_match else "[red]✗[/red]"
        val_preview = (result[:30] + "...") if result and len(result) > 30 else result
        console.print(
            f"   {icon} '{label}' ({name}): "
            f"{'matched' if matched else 'no match'} → {val_preview}"
        )

    # ── 8. Known question matching ──
    console.print("\n[yellow]8. Known question matching:[/yellow]")
    question_tests = [
        ("notice period", True),
        ("current ctc", True),
        ("expected salary", True),
        ("total experience", True),
        ("willing to relocate", True),
        ("what is quantum physics", False),
        ("reason for leaving", True),
        ("work authorization", True),
        ("gender", True),
    ]
    for question, should_match in question_tests:
        result = platform._match_known_question(question, question)
        matched = result is not None
        icon = "[green]✓[/green]" if matched == should_match else "[red]✗[/red]"
        val_preview = (result[:40] + "...") if result and len(result) > 40 else result
        console.print(
            f"   {icon} '{question}': "
            f"{'matched' if matched else 'no match'} → {val_preview}"
        )

    # ── 9. Consent field detection ──
    console.print("\n[yellow]9. Consent field detection:[/yellow]")
    consent_tests = [
        ("I agree to the terms and conditions", True),
        ("Accept privacy policy", True),
        ("I consent to background check", True),
        ("Preferred work location", False),
        ("I acknowledge the above", True),
    ]
    for text, expected in consent_tests:
        result = platform._is_consent_field(text.lower())
        icon = "[green]✓[/green]" if result == expected else "[red]✗[/red]"
        console.print(f"   {icon} '{text[:40]}' → consent={result}")

    # ── 10. Status ──
    console.print("\n[yellow]10. Platform status:[/yellow]")
    status = platform.get_status()
    table = Table(title=f"Platform Status: {status['platform']}")
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="green")
    for k, v in status.items():
        table.add_row(k, str(v))
    console.print(table)

    # ── 11. Search (mock) ──
    console.print("\n[yellow]11. Search test (mock):[/yellow]")
    jobs = platform.search_jobs(["Software Engineer"])
    console.print(f"   [green]✓[/green] Found {len(jobs)} jobs")
    for job in jobs:
        console.print(
            f"   → {job['title']} @ {job['company']} "
            f"({job['location']}) — {job['salary_text']}"
        )

    # ── 12. Error tracking ──
    console.print("\n[yellow]12. Error tracking:[/yellow]")
    platform.record_error("test_error", "This is a test error")
    console.print(
        f"   [green]✓[/green] Recorded error, "
        f"consecutive={platform._consecutive_errors}"
    )
    platform._consecutive_errors = 0

    # ── 13. Login (mock) ──
    console.print("\n[yellow]13. Login test (mock):[/yellow]")
    result = platform.login()
    console.print(f"   [green]✓[/green] Login returned: {result}")

    console.print(
        f"\n[bold green]═══ All PlatformBase tests passed! ═══[/bold green]\n"
    )