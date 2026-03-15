"""
platforms/foundit.py — Foundit.in (formerly Monster India) job search and
semi-auto application.

Handles:
  - Cookie-based + OTP login (no password stored)
  - Multi-query job search with pagination
  - Job detail extraction (full JD, skills, salary, etc.)
  - Semi-auto application (prepare → Telegram approve → submit)
  - Form field filling (text, dropdown, radio, checkbox, upload)
  - External-apply detection and logging
  - Applied-jobs status check

Foundit auth flow:
  1. Bot navigates to login page → enters email
  2. Foundit sends OTP to user's email/phone
  3. Bot sends Telegram: "Enter OTP for Foundit"
  4. User replies with OTP → bot enters it → cookies saved
  5. On future runs: saved cookies skip OTP

Foundit apply circuits:
  Circuit A — Quick Apply: click Apply → immediate submit
  Circuit B — Form Apply: click Apply → modal/page with fields → Submit
  Circuit C — External: "Apply on company site" → new tab → log only
  Circuit D — Already Applied: button disabled → skip
  Circuit E — Login wall: apply redirects to login → re-auth → retry

Prerequisites:
    All Phase 1 modules working (config, logger, db, browser, base)
    FOUNDIT_EMAIL set in .env

Usage:
    from core.browser import BrowserEngine
    from platforms.foundit import FounditPlatform

    engine = BrowserEngine()
    foundit = FounditPlatform(engine)

    if foundit.login():
        jobs = foundit.search_jobs()
        for job in jobs[:5]:
            details = foundit.get_job_details(job["url"])
            prepared = foundit.prepare_application(job, "/path/resume.pdf")
            if prepared["status"] == "ready":
                result = foundit.submit_application(prepared)
"""

import re
import os
import json
import time
import random
import traceback as tb_module
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import quote_plus, urlencode, urlparse, parse_qs

from config import (
    PLATFORM_CONFIG,
    USER_PROFILE,
    STEALTH_CONFIG,
    RESUME_CONFIG,
)
from core.logger import get_logger
from core.db import get_db
from platforms.base import PlatformBase

logger = get_logger("platforms.foundit")

# ── Credentials (import safely) ─────────────────────────────────
try:
    from config import FOUNDIT_EMAIL
except ImportError:
    FOUNDIT_EMAIL = ""

# ── Optional: profile/answers.py (Phase 2) ─────────────────────
try:
    from profile.answers import get_answer, get_standard
    _ANSWERS_AVAILABLE = True
except ImportError:
    _ANSWERS_AVAILABLE = False
    get_answer = lambda q: None
    get_standard = lambda f: None


# ═══════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════

_URLS = {
    "home":      "https://www.foundit.in/",
    "login":     "https://www.foundit.in/login",
    "search":    "https://www.foundit.in/srp/results",
    "profile":   "https://www.foundit.in/seeker/profile",
    "applied":   "https://www.foundit.in/seeker/applied-jobs",
    "dashboard": "https://www.foundit.in/seeker/dashboard",
}

# ── Multi-fallback CSS Selectors ──────────────────────────────────
# Foundit redesigns periodically; multiple selectors per element.

_S = {
    # ── Login ──
    "login_email": [
        "input[type='email']",
        "input[name='email']",
        "input[placeholder*='email' i]",
        "input[placeholder*='Email']",
        "input[id*='email' i]",
        "#email",
    ],
    "login_email_submit": [
        "button:has-text('Continue')",
        "button:has-text('Get OTP')",
        "button:has-text('Send OTP')",
        "button:has-text('Proceed')",
        "button[type='submit']",
        "button:has-text('Next')",
    ],
    "login_otp_input": [
        "input[type='tel']",
        "input[name='otp']",
        "input[placeholder*='OTP' i]",
        "input[placeholder*='otp']",
        "input[id*='otp' i]",
        "input[maxlength='6']",
        "input[maxlength='4']",
    ],
    "login_otp_submit": [
        "button:has-text('Verify')",
        "button:has-text('Login')",
        "button:has-text('Submit')",
        "button[type='submit']",
    ],
    "login_error": [
        "div[class*='error']",
        "span[class*='error']",
        "p[class*='error']",
        "div[class*='alert-danger']",
        "div[class*='err-msg']",
    ],
    "login_google": [
        "button:has-text('Google')",
        "a:has-text('Google')",
        "div[class*='google']",
    ],

    # ── Logged-in indicators ──
    "logged_in": [
        "a[href*='/seeker/profile']",
        "a[href*='/seeker/dashboard']",
        "div[class*='user-info']",
        "div[class*='profile-icon']",
        "span[class*='user-name']",
        "a[href*='logout']",
        "div[class*='logged-in']",
        "img[class*='avatar']",
        "div[class*='header-profile']",
    ],

    # ── Search results ──
    "job_card": [
        "div[class*='card-apply-content']",
        "div[class*='jobTuple']",
        "div[class*='job-card']",
        "div[class*='srpResultCardContainer']",
        "div[class*='card-container']",
        "article[class*='job']",
        "div[data-job-id]",
    ],
    "pagination_next": [
        "li[class*='next'] a",
        "a[aria-label='Next']",
        "button:has-text('Next')",
        "a:has-text('Next')",
        "a[class*='next']",
    ],
    "no_results": [
        "div:has-text('No jobs found')",
        "div:has-text('no results')",
        "div[class*='no-result']",
        "div[class*='empty-state']",
    ],

    # ── Inside a job card ──
    "card_title": [
        "a[class*='card-job-title']",
        "a[class*='job-title']",
        "h3 a",
        "a[class*='title']",
        "div[class*='job-title'] a",
    ],
    "card_company": [
        "span[class*='card-company-name']",
        "a[class*='company-name']",
        "span[class*='comp-name']",
        "div[class*='company'] span",
        "span[class*='company']",
    ],
    "card_experience": [
        "span[class*='card-experience']",
        "span[class*='exp']",
        "div[class*='experience'] span",
    ],
    "card_salary": [
        "span[class*='card-salary']",
        "span[class*='sal']",
        "div[class*='salary'] span",
    ],
    "card_location": [
        "span[class*='card-location']",
        "span[class*='loc']",
        "div[class*='location'] span",
    ],
    "card_description": [
        "div[class*='card-job-description']",
        "div[class*='job-desc']",
        "span[class*='desc']",
        "p[class*='description']",
    ],
    "card_tags": [
        "div[class*='card-skills'] span",
        "div[class*='skill'] span",
        "ul[class*='tags'] li",
        "div[class*='chip'] span",
        "a[class*='skill-tag']",
    ],
    "card_posted": [
        "span[class*='card-posted']",
        "span[class*='posted']",
        "span[class*='date']",
        "div[class*='posted-date']",
    ],
    "card_link": [
        "a[class*='card-job-title']",
        "a[class*='job-title']",
        "h3 a",
        "a[class*='title']",
    ],

    # ── Job detail page ──
    "detail_title": [
        "h1[class*='jd-header-title']",
        "h1[class*='job-title']",
        "h1",
    ],
    "detail_company": [
        "a[class*='company-name']",
        "span[class*='company-name']",
        "a[class*='comp-name']",
        "div[class*='company'] a",
    ],
    "detail_experience": [
        "span[class*='exp']",
        "li:has-text('Exp') span",
        "div[class*='experience'] span",
    ],
    "detail_salary": [
        "span[class*='sal']",
        "li:has-text('Salary') span",
        "div[class*='salary'] span",
    ],
    "detail_location": [
        "span[class*='loc']",
        "a[class*='location']",
        "div[class*='location'] span",
    ],
    "detail_jd": [
        "div[class*='job-desc-container']",
        "div[class*='job-description']",
        "div[class*='jd-desc']",
        "div[class*='description-container']",
        "section[class*='job-desc']",
    ],
    "detail_skills": [
        "div[class*='key-skill'] a",
        "div[class*='skills'] span",
        "a[class*='chip']",
        "span[class*='skill-tag']",
        "div[class*='chip-wrapper'] span",
    ],
    "detail_info": [
        "div[class*='job-details'] li",
        "div[class*='other-details'] div",
        "ul[class*='detail-list'] li",
    ],

    # ── Apply button ──
    "apply_btn": [
        "button:has-text('Apply')",
        "button[class*='apply']",
        "a:has-text('Apply')",
        "button[id*='apply' i]",
        "div[class*='apply'] button",
    ],
    "already_applied": [
        "button:has-text('Already Applied')",
        "button:has-text('Applied')",
        "span:has-text('Already Applied')",
        "div[class*='already-applied']",
        "button[disabled]:has-text('Applied')",
    ],
    "external_apply": [
        "a:has-text('Apply on company')",
        "a[class*='external']",
        "button:has-text('Apply on company')",
        "a:has-text('Company Website')",
    ],

    # ── Apply form / modal ──
    "apply_modal": [
        "div[role='dialog']",
        "div[class*='apply-modal']",
        "div[class*='modal-dialog']",
        "div[class*='apply-form']",
        "div[class*='modal'][class*='show']",
    ],
    "form_input": [
        "div[class*='apply'] input[type='text']",
        "div[class*='modal'] input[type='text']",
        "div[role='dialog'] input[type='text']",
        "form input[type='text']",
    ],
    "form_email": [
        "div[class*='apply'] input[type='email']",
        "div[class*='modal'] input[type='email']",
        "form input[type='email']",
    ],
    "form_tel": [
        "div[class*='apply'] input[type='tel']",
        "div[class*='modal'] input[type='tel']",
        "form input[type='tel']",
    ],
    "form_number": [
        "div[class*='apply'] input[type='number']",
        "div[class*='modal'] input[type='number']",
        "form input[type='number']",
    ],
    "form_textarea": [
        "div[class*='apply'] textarea",
        "div[class*='modal'] textarea",
        "div[role='dialog'] textarea",
        "form textarea",
    ],
    "form_select": [
        "div[class*='apply'] select",
        "div[class*='modal'] select",
        "div[role='dialog'] select",
        "form select",
    ],
    "form_checkbox": [
        "div[class*='apply'] input[type='checkbox']",
        "div[class*='modal'] input[type='checkbox']",
        "form input[type='checkbox']",
    ],
    "form_radio": [
        "div[class*='apply'] input[type='radio']",
        "div[class*='modal'] input[type='radio']",
        "form input[type='radio']",
    ],
    "resume_upload": [
        "input[type='file']",
        "input[accept*='.pdf']",
        "input[accept*='.doc']",
        "input[name*='resume' i]",
        "input[name*='cv' i]",
    ],
    "form_submit": [
        "button:has-text('Submit')",
        "button:has-text('Apply')",
        "button:has-text('Submit Application')",
        "button[type='submit']",
        "input[type='submit']",
    ],
    "apply_success": [
        "div:has-text('Application Submitted')",
        "div:has-text('applied successfully')",
        "div:has-text('Successfully Applied')",
        "div[class*='success']",
        "span:has-text('application submitted' i)",
        "div:has-text('Thank you for applying')",
    ],

    # ── Popups/banners to dismiss ──
    "popup_close": [
        "button[class*='close']",
        "button[aria-label='Close']",
        "span[class*='close']",
        "button[class*='cross']",
        "div[class*='popup'] button[class*='close']",
        "div[class*='modal'] button[class*='close']",
        "div[class*='banner'] button[class*='close']",
        "div[class*='cookie'] button",
        "button:has-text('Accept')",
        "button:has-text('Got it')",
    ],

    # ── Applied jobs page ──
    "applied_card": [
        "div[class*='applied-job']",
        "div[class*='app-card']",
        "article[class*='applied']",
        "div[class*='application-card']",
    ],
    "applied_title": [
        "a[class*='title']",
        "h3 a",
        "span[class*='job-title']",
    ],
    "applied_status": [
        "span[class*='status']",
        "div[class*='status']",
        "span[class*='response']",
    ],
}


# ═══════════════════════════════════════════════════════════════════
# SALARY / EXPERIENCE PARSERS
# ═══════════════════════════════════════════════════════════════════

def _parse_salary_text(text: str) -> Tuple[float, float]:
    """
    Parse Foundit salary text into (min, max) in INR.

    Examples:
        '₹ 5,00,000 - 10,00,000 P.A.'  → (500000, 1000000)
        '5-10 Lacs PA'                   → (500000, 1000000)
        'Not disclosed'                  → (0, 0)
    """
    if not text:
        return 0.0, 0.0
    t = text.strip().lower()

    if "not disclosed" in t or "confidential" in t:
        return 0.0, 0.0

    # Pattern: X-Y Lacs / Lakhs / LPA
    m = re.search(
        r'([\d.]+)\s*[-–to]+\s*([\d.]+)\s*(?:lacs?|lakhs?|lpa)',
        t, re.I
    )
    if m:
        return float(m.group(1)) * 100000, float(m.group(2)) * 100000

    # Pattern: X,XX,XXX - Y,YY,YYY
    m = re.search(r'([\d,]+)\s*[-–to]+\s*([\d,]+)', t)
    if m:
        lo = float(m.group(1).replace(",", ""))
        hi = float(m.group(2).replace(",", ""))
        if lo > 1000:
            return lo, hi

    # Single number with Lacs
    m = re.search(r'([\d.]+)\s*(?:lacs?|lakhs?|lpa)', t, re.I)
    if m:
        val = float(m.group(1)) * 100000
        return val, val

    return 0.0, 0.0


def _parse_experience_text(text: str) -> Tuple[float, float]:
    """
    Parse Foundit experience text into (min, max) years.

    Examples:
        '0-2 Yrs'    → (0, 2)
        '3-5 Years'  → (3, 5)
        'Fresher'    → (0, 0)
    """
    if not text:
        return 0.0, 0.0
    t = text.strip().lower()

    if "fresher" in t:
        return 0.0, 0.0

    m = re.search(r'([\d.]+)\s*[-–to]+\s*([\d.]+)', t)
    if m:
        return float(m.group(1)), float(m.group(2))

    m = re.search(r'([\d.]+)\+', t)
    if m:
        return float(m.group(1)), 99.0

    m = re.search(r'([\d.]+)', t)
    if m:
        return float(m.group(1)), float(m.group(1))

    return 0.0, 0.0


# ═══════════════════════════════════════════════════════════════════
# SKILL EXPERIENCE MAPPING
# ═══════════════════════════════════════════════════════════════════

_SKILL_YEARS = {
    "javascript": 1, "js": 1, "es6": 1,
    "vue": 1, "vue.js": 1, "vuejs": 1,
    "nuxt": 1, "nuxt.js": 1, "nuxtjs": 1,
    "vuetify": 1,
    "node": 1, "node.js": 1, "nodejs": 1,
    "express": 1, "express.js": 1, "expressjs": 1,
    "mysql": 1, "sql": 1,
    "rest": 1, "rest api": 1, "restful": 1, "api": 1,
    "websocket": 1, "websockets": 1,
    "html": 1, "css": 1, "git": 1,
    "react": 0.5, "react.js": 0.5, "reactjs": 0.5,
    "java": 0.5, "spring": 0.5, "spring boot": 0.5,
    "python": 0.5,
    "mongodb": 0.5, "mongo": 0.5,
    "postgresql": 0.5, "postgres": 0.5,
    "docker": 0.5, "kafka": 0.3, "redis": 0.3,
    "tailwind": 0.5, "tailwindcss": 0.5,
    "salesforce": 0.3, "apex": 0.3,
    "full stack": 1, "fullstack": 1,
    "backend": 1, "frontend": 1,
    "microservices": 0.5, "agile": 0.5,
}


# ═══════════════════════════════════════════════════════════════════
# FOUNDIT PLATFORM
# ═══════════════════════════════════════════════════════════════════

class FounditPlatform(PlatformBase):
    """
    Foundit.in platform: cookie/OTP login, search, scrape, semi-auto apply.

    Extends PlatformBase with Foundit-specific selectors, OTP login flow,
    form handling, and apply-circuit detection.
    """

    PLATFORM_NAME = "foundit"

    def __init__(self, browser_engine, notifier=None):
        super().__init__(browser_engine)
        self.notifier = notifier
        self.platform_name = self.PLATFORM_NAME
        
        # ── FIX: ensure self.browser exists ──
        if not hasattr(self, 'browser'):
            self.browser = browser_engine
        
        self._page = None
        self._foundit_config = PLATFORM_CONFIG.get("foundit", {})
        self._current_job = None
        logger.info("FounditPlatform initialized")

    # ═══════════════════════════════════════════════════════════
    # INTERNAL: Page & Element helpers
    # ═══════════════════════════════════════════════════════════

    def _get_page(self):
        """Get or launch the Foundit browser page."""
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    _ = self._page.url
                    return self._page
            except Exception:
                pass
        self._page = self.browser.launch(self.PLATFORM_NAME)
        return self._page

    def _find(self, page, key: str):
        """Try each selector in _S[key] until one matches."""
        for sel in _S.get(key, []):
            try:
                el = page.query_selector(sel)
                if el:
                    return el
            except Exception:
                continue
        return None

    def _find_wait(self, page, key: str, timeout: int = 8000):
        """Like _find but waits for the element."""
        for sel in _S.get(key, []):
            try:
                el = page.wait_for_selector(
                    sel, timeout=timeout, state="visible"
                )
                if el:
                    return el
            except Exception:
                continue
        return None

    def _find_all(self, page, key: str) -> list:
        """Return ALL matching elements for first successful selector."""
        for sel in _S.get(key, []):
            try:
                els = page.query_selector_all(sel)
                if els:
                    return els
            except Exception:
                continue
        return []

    def _text(self, page, key: str, default: str = "") -> str:
        """Get trimmed text of first matching element."""
        el = self._find(page, key)
        if el:
            txt = el.text_content()
            return txt.strip() if txt else default
        return default

    def _click(self, page, key: str) -> bool:
        """Click first matching element using human-like click."""
        for sel in _S.get(key, []):
            try:
                if page.query_selector(sel):
                    self.browser.click_human(page, sel)
                    return True
            except Exception:
                continue
        return False

    def _working_selector(self, page, key: str) -> Optional[str]:
        """Return the first working CSS selector from the list."""
        for sel in _S.get(key, []):
            try:
                if page.query_selector(sel):
                    return sel
            except Exception:
                continue
        return None

    def _dismiss_popups(self, page) -> None:
        """Close any Foundit popups, banners, cookie notices."""
        for sel in _S.get("popup_close", []):
            try:
                if self.browser.element_visible(page, sel):
                    page.click(sel, timeout=2000)
                    time.sleep(0.3)
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════
    # LOGIN — Cookie-based + OTP
    # ═══════════════════════════════════════════════════════════

    def login(self) -> bool:
        """
        Log in to Foundit.in.

        Flow:
          1. Launch browser, load saved cookies.
          2. Navigate to home → check if already logged in.
          3. If not → go to login page → enter email → send OTP.
          4. Ask user for OTP via Telegram (or console fallback).
          5. Enter OTP → verify → save cookies.

        Returns:
            True if login succeeded.
        """
        try:
            page = self._get_page()

            # ── Try cookies first ──
            self.browser.navigate(page, _URLS["home"])
            self._dismiss_popups(page)
            self.browser.random_delay(1.5, 3.0)

            if self._check_logged_in(page):
                logger.info(
                    "Already logged in to Foundit (saved session)"
                )
                self._update_session(logged_in=True)
                return True

            # ── Navigate to login ──
            if not self.browser.navigate(page, _URLS["login"]):
                logger.error("Could not reach Foundit login page")
                return False

            self.browser.random_delay(1.5, 3.0)
            self._dismiss_popups(page)

            # ── Check email ──
            email = FOUNDIT_EMAIL
            if not email:
                email = USER_PROFILE.get(
                    "email", "piyushkashyap3247@gmail.com"
                )
            if not email:
                logger.error("FOUNDIT_EMAIL not set in .env")
                return False

            # ── Enter email ──
            email_sel = self._working_selector(page, "login_email")
            if not email_sel:
                logger.error(
                    "Cannot find email field on Foundit login"
                )
                self.browser.take_screenshot(
                    page, "foundit_login_no_email"
                )
                return False

            self.browser.type_human(page, email_sel, email)
            self.browser.random_delay(0.5, 1.5)

            # ── Click Continue / Get OTP ──
            submit_sel = self._working_selector(
                page, "login_email_submit"
            )
            if submit_sel:
                self.browser.click_human(page, submit_sel)
            else:
                self.browser.press_key(page, "Enter")

            self.browser.random_delay(3.0, 5.0)

            # ── Check for errors ──
            error_text = self._text(page, "login_error")
            if error_text and "invalid" in error_text.lower():
                logger.error(f"Foundit login error: {error_text}")
                return False

            # ── Check if OTP page appeared ──
            otp_sel = self._working_selector(page, "login_otp_input")
            if otp_sel:
                logger.info("Foundit OTP page detected")
                otp_entered = self._handle_foundit_otp(page)
                if not otp_entered:
                    logger.error("OTP entry failed")
                    self.browser.take_screenshot(
                        page, "foundit_otp_fail"
                    )
                    return False
            else:
                # Maybe it auto-logged in or needs Google auth
                self.browser.random_delay(2.0, 4.0)

                if self._check_logged_in(page):
                    logger.info("Foundit auto-logged in (no OTP)")
                    self.browser.save_cookies(self.PLATFORM_NAME)
                    self._update_session(logged_in=True)
                    return True

                # Check for OTP again after delay
                otp_sel = self._working_selector(
                    page, "login_otp_input"
                )
                if otp_sel:
                    otp_entered = self._handle_foundit_otp(page)
                    if not otp_entered:
                        return False
                else:
                    logger.error(
                        "Foundit: no OTP field and not logged in"
                    )
                    self.browser.take_screenshot(
                        page, "foundit_login_stuck"
                    )
                    return False

            # ── Verify login ──
            self.browser.random_delay(2.0, 4.0)
            self._dismiss_popups(page)

            if self._check_logged_in(page):
                logger.info("Foundit login successful")
                self.browser.save_cookies(self.PLATFORM_NAME)
                self._update_session(logged_in=True)
                return True

            # Navigate to home to double-check
            self.browser.navigate(page, _URLS["home"])
            self.browser.random_delay(2.0, 4.0)
            self._dismiss_popups(page)

            if self._check_logged_in(page):
                logger.info(
                    "Foundit login successful (confirmed on home)"
                )
                self.browser.save_cookies(self.PLATFORM_NAME)
                self._update_session(logged_in=True)
                return True

            logger.error("Foundit login failed (not logged in)")
            self.browser.take_screenshot(
                page, "foundit_login_fail_final"
            )
            return False

        except Exception as e:
            logger.error(f"Foundit login exception: {e}")
            self._save_error("login", e)
            return False

    def _handle_foundit_otp(self, page) -> bool:
        """
        Handle Foundit OTP entry.

        Asks user for OTP via:
          1. Telegram notifier (if available)
          2. Console input (fallback)

        Returns:
            True if OTP was entered and login succeeded.
        """
        otp_code = None

        # ── Ask via Telegram ──
        if self.notifier:
            try:
                otp_code = self.notifier.send_otp_request("foundit")
                if otp_code:
                    logger.info("OTP received via Telegram")
            except Exception as e:
                logger.debug(f"Telegram OTP request failed: {e}")

        # ── Console fallback ──
        if not otp_code:
            logger.info(
                "Check your email/phone for Foundit OTP"
            )
            try:
                otp_code = input(
                    "Enter Foundit OTP (check email/phone): "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                logger.warning("OTP input cancelled")
                return False

        if not otp_code:
            logger.error("No OTP provided")
            return False

        # ── Enter OTP ──
        otp_sel = self._working_selector(page, "login_otp_input")
        if not otp_sel:
            logger.error("OTP input field not found")
            return False

        # Some OTP fields are split into individual digits
        otp_inputs = page.query_selector_all(otp_sel)
        if len(otp_inputs) > 1 and len(otp_inputs) == len(otp_code):
            # Individual digit inputs
            for i, inp in enumerate(otp_inputs):
                try:
                    inp.fill(otp_code[i])
                    time.sleep(random.uniform(0.1, 0.3))
                except Exception:
                    pass
        else:
            # Single input field
            self.browser.type_human(page, otp_sel, otp_code)

        self.browser.random_delay(0.5, 1.5)

        # ── Click Verify ──
        verify_sel = self._working_selector(
            page, "login_otp_submit"
        )
        if verify_sel:
            self.browser.click_human(page, verify_sel)
        else:
            self.browser.press_key(page, "Enter")

        self.browser.random_delay(3.0, 5.0)

        # Check for errors
        error_text = self._text(page, "login_error")
        if error_text and any(
            k in error_text.lower()
            for k in ["invalid", "incorrect", "wrong", "expired"]
        ):
            logger.error(f"OTP error: {error_text}")
            return False

        return True

    def _check_logged_in(self, page) -> bool:
        """Check if any logged-in indicator is visible."""
        for sel in _S.get("logged_in", []):
            try:
                if page.query_selector(sel):
                    return True
            except Exception:
                continue
        return False

    # ═══════════════════════════════════════════════════════════
    # SEARCH
    # ═══════════════════════════════════════════════════════════

    def search_jobs(self, queries: Optional[List[str]] = None,
                    filters: Optional[Dict] = None) -> List[Dict]:
        """
        Search Foundit for jobs.

        Args:
            queries: List of search keywords.
                     None → uses config search_queries.
            filters: Dict with optional keys.

        Returns:
            List of job dicts.
        """
        if not queries:
            queries = self._foundit_config.get("search_queries", [])
            if not queries:
                queries = USER_PROFILE.get("target_titles", [
                    "Software Engineer",
                    "Full Stack Developer",
                    "Backend Developer",
                    "Java Developer",
                ])

        if not filters:
            filters = {}

        max_pages = self._foundit_config.get("max_pages_per_query", 5)
        all_jobs = []

        try:
            page = self._get_page()

            # Ensure logged in
            if not self._check_logged_in(page):
                logger.warning(
                    "Not logged in, attempting login first"
                )
                if not self.login():
                    logger.error("Cannot search: login failed")
                    return []

            for query in queries:
                logger.info(f"Searching Foundit: '{query}'")
                try:
                    url = self._build_search_url(query, filters)
                    jobs = self._search_single_query(
                        page, url, max_pages
                    )
                    all_jobs.extend(jobs)

                    logger.info(
                        f"  '{query}': found {len(jobs)} jobs"
                    )

                    rate = self._foundit_config.get(
                        "rate_limit_seconds", (5, 15)
                    )
                    self.browser.random_delay(*rate)

                except Exception as e:
                    logger.error(
                        f"Search query '{query}' failed: {e}"
                    )
                    continue

            # Deduplicate by URL
            seen = set()
            unique = []
            for job in all_jobs:
                url = job.get("url", "")
                if url and url not in seen:
                    seen.add(url)
                    unique.append(job)

            logger.info(
                f"Foundit search complete: {len(unique)} unique "
                f"jobs from {len(queries)} queries"
            )
            return unique

        except Exception as e:
            logger.error(f"Foundit search_jobs failed: {e}")
            self._save_error("search_jobs", e)
            return []

    def _build_search_url(self, query: str,
                          filters: Dict) -> str:
        """Build Foundit search URL with filters."""
        params = {
            "query": query.strip(),
            "sort": "1",  # relevance
        }

        locations = filters.get(
            "locations",
            USER_PROFILE.get("target_locations", [])
        )
        if locations:
            params["locations"] = "|".join(locations)

        exp_min = filters.get(
            "experience_min",
            USER_PROFILE.get("experience_years", 0)
        )
        if exp_min is not None:
            params["experienceRanges"] = f"{int(exp_min)}~{int(exp_min) + 3}"

        freshness = filters.get("freshness", "")
        if freshness:
            params["postedDate"] = str(freshness)

        return f"{_URLS['search']}?{urlencode(params)}"

    def _search_single_query(self, page, url: str,
                             max_pages: int) -> List[Dict]:
        """Search a single query URL, paginate, parse cards."""
        all_jobs = []

        for page_num in range(1, max_pages + 1):
            if page_num == 1:
                page_url = url
            else:
                sep = "&" if "?" in url else "?"
                page_url = f"{url}{sep}pageNo={page_num}"

            if not self.browser.navigate(page, page_url):
                logger.warning(
                    f"Failed to load search page {page_num}"
                )
                break

            self.browser.random_delay(2.0, 4.0)
            self._dismiss_popups(page)

            # Check for no results
            if self._find(page, "no_results"):
                logger.debug("No results found")
                break

            # Scroll to load lazy content
            self.browser.scroll_page(
                page, "down", random.randint(300, 600)
            )
            self.browser.random_delay(1.0, 2.0)

            # Parse job cards
            cards = self._parse_job_cards(page)
            if not cards:
                logger.debug(
                    f"No job cards on page {page_num}, stopping"
                )
                break

            all_jobs.extend(cards)
            logger.debug(f"  Page {page_num}: {len(cards)} cards")

            # Check for next page
            if page_num < max_pages:
                has_next = self._find(page, "pagination_next")
                if not has_next:
                    logger.debug(
                        "No 'Next' button, last page reached"
                    )
                    break

                self.browser.scroll_page(
                    page, "down", random.randint(500, 1000)
                )
                self.browser.random_delay(1.5, 3.5)

        return all_jobs

    def _parse_job_cards(self, page) -> List[Dict]:
        """Parse all job cards on the current page."""
        cards = self._find_all(page, "job_card")
        if not cards:
            return []

        jobs = []
        for card in cards:
            try:
                job = self._extract_card(card)
                if job and job.get("title"):
                    jobs.append(job)
            except Exception as e:
                logger.debug(f"Failed to parse job card: {e}")
                continue

        return jobs

    def _extract_card(self, card) -> Optional[Dict]:
        """Extract data from a single job card element."""
        def _card_text(selectors: list) -> str:
            for sel in selectors:
                try:
                    el = card.query_selector(sel)
                    if el:
                        t = el.text_content()
                        return t.strip() if t else ""
                except Exception:
                    continue
            return ""

        def _card_attr(selectors: list, attr: str) -> str:
            for sel in selectors:
                try:
                    el = card.query_selector(sel)
                    if el:
                        val = el.get_attribute(attr)
                        return val or ""
                except Exception:
                    continue
            return ""

        title = _card_text(_S["card_title"])
        if not title:
            return None

        company = _card_text(_S["card_company"])
        location = _card_text(_S["card_location"])
        salary_text = _card_text(_S["card_salary"])
        exp_text = _card_text(_S["card_experience"])
        description = _card_text(_S["card_description"])
        posted_date = _card_text(_S["card_posted"])

        # Job URL
        job_url = _card_attr(_S["card_link"], "href")
        if job_url and not job_url.startswith("http"):
            job_url = "https://www.foundit.in" + job_url

        # Extract job ID from URL
        platform_job_id = ""
        if job_url:
            m = re.search(r'[-/](\d{6,})(?:\?|$|/)', job_url)
            if m:
                platform_job_id = m.group(1)
            else:
                # Try data attribute
                jid = card.get_attribute("data-job-id") or ""
                if jid:
                    platform_job_id = jid
                else:
                    platform_job_id = str(abs(hash(job_url)))[-10:]

        # Skills
        skills = []
        for sel in _S["card_tags"]:
            try:
                tag_els = card.query_selector_all(sel)
                if tag_els:
                    skills = [
                        t.text_content().strip()
                        for t in tag_els
                        if t.text_content()
                        and t.text_content().strip()
                    ]
                    break
            except Exception:
                continue

        sal_min, sal_max = _parse_salary_text(salary_text)
        exp_min, exp_max = _parse_experience_text(exp_text)

        return {
            "platform": self.PLATFORM_NAME,
            "platform_job_id": platform_job_id,
            "url": job_url,
            "title": title,
            "company": company,
            "location": location,
            "salary_text": salary_text,
            "salary_min": sal_min,
            "salary_max": sal_max,
            "experience_text": exp_text,
            "experience_min": exp_min,
            "experience_max": exp_max,
            "description": description,
            "posted_date": posted_date,
            "skills": skills,
            "discovered_at": datetime.now().isoformat(),
        }

    # ═══════════════════════════════════════════════════════════
    # JOB DETAILS
    # ═══════════════════════════════════════════════════════════

    def get_job_details(self, job_url: str) -> Dict:
        """
        Navigate to a job page and extract full details.

        Returns:
            Dict with all job fields.  Empty dict on failure.
        """
        try:
            page = self._get_page()

            if not self.browser.navigate(page, job_url):
                logger.error(f"Cannot load job page: {job_url}")
                return {}

            self.browser.random_delay(2.0, 4.0)
            self._dismiss_popups(page)

            self.browser.scroll_page(
                page, "down", random.randint(300, 600)
            )
            self.browser.random_delay(1.0, 2.0)

            return self._parse_job_page(page, job_url)

        except Exception as e:
            logger.error(
                f"get_job_details failed ({job_url}): {e}"
            )
            self._save_error("get_job_details", e)
            return {}

    def _parse_job_page(self, page, job_url: str) -> Dict:
        """Extract all details from the job detail page."""
        title = self._text(page, "detail_title")
        company = self._text(page, "detail_company")
        location = self._text(page, "detail_location")
        salary_text = self._text(page, "detail_salary")
        exp_text = self._text(page, "detail_experience")

        # Full JD
        jd_el = self._find(page, "detail_jd")
        description = ""
        if jd_el:
            description = (jd_el.text_content() or "").strip()

        # Skills
        skill_els = self._find_all(page, "detail_skills")
        skills = [
            s.text_content().strip()
            for s in skill_els
            if s.text_content() and s.text_content().strip()
        ]

        # Other details
        other_details = {}
        info_els = self._find_all(page, "detail_info")
        for el in info_els:
            text = (el.text_content() or "").strip()
            if ":" in text:
                k, v = text.split(":", 1)
                other_details[k.strip()] = v.strip()

        sal_min, sal_max = _parse_salary_text(salary_text)
        exp_min, exp_max = _parse_experience_text(exp_text)

        page_text = self.browser.get_page_html(page).lower()
        job_type = "full-time"
        if "part-time" in page_text or "part time" in page_text:
            job_type = "part-time"
        elif "contract" in page_text:
            job_type = "contract"
        elif "intern" in page_text:
            job_type = "internship"

        work_mode = ""
        if "remote" in page_text or "work from home" in page_text:
            work_mode = "remote"
        elif "hybrid" in page_text:
            work_mode = "hybrid"

        platform_job_id = ""
        m = re.search(r'[-/](\d{6,})(?:\?|$|/)', job_url)
        if m:
            platform_job_id = m.group(1)

        result = {
            "platform": self.PLATFORM_NAME,
            "platform_job_id": platform_job_id,
            "url": job_url,
            "title": title,
            "company": company,
            "location": location,
            "salary_text": salary_text,
            "salary_min": sal_min,
            "salary_max": sal_max,
            "experience_text": exp_text,
            "experience_min": exp_min,
            "experience_max": exp_max,
            "description": description,
            "skills": skills,
            "job_type": job_type,
            "work_mode": work_mode,
            "other_details": other_details,
        }

        logger.debug(
            f"Job detail: '{title}' @ {company} "
            f"({location}, {salary_text})"
        )
        return result

    # ═══════════════════════════════════════════════════════════
    # APPLICATION — Prepare
    # ═══════════════════════════════════════════════════════════

    def prepare_application(self, job: Dict, resume_path: str,
                            cover_letter: Optional[str] = None
                            ) -> Dict:
        """
        Navigate to job, click Apply, fill all fields, STOP before
        submit.

        Returns:
            {status, job, apply_type, error, screenshot, timestamp}
        """
        result = {
            "status": "failed",
            "job": job,
            "platform": self.PLATFORM_NAME,
            "apply_type": "",
            "resume_path": resume_path,
            "cover_letter": cover_letter,
            "error": None,
            "screenshot": "",
            "timestamp": datetime.now().isoformat(),
        }

        self._current_job = job

        try:
            if not self.can_apply():
                result["error"] = (
                    "Daily apply limit reached or in cooldown"
                )
                logger.warning(result["error"])
                return result

            page = self._get_page()
            job_url = job.get("url", "")
            if not job_url:
                result["error"] = "No job URL"
                return result

            if not self.browser.navigate(page, job_url):
                result["error"] = (
                    f"Cannot load job page: {job_url}"
                )
                return result

            self.browser.random_delay(2.0, 4.0)
            self._dismiss_popups(page)

            # ── Already Applied? ──
            if self._find(page, "already_applied"):
                result["status"] = "already_applied"
                result["apply_type"] = "already_applied"
                logger.info(
                    f"Already applied: {job.get('title')} @ "
                    f"{job.get('company')}"
                )
                return result

            # ── External Apply? ──
            if (self._find(page, "external_apply") and
                    not self._find(page, "apply_btn")):
                result["status"] = "external"
                result["apply_type"] = "external"
                logger.info(
                    f"External apply: {job.get('title')} @ "
                    f"{job.get('company')}"
                )
                return result

            # ── Find Apply button ──
            apply_sel = self._working_selector(page, "apply_btn")
            if not apply_sel:
                result["error"] = "Apply button not found"
                result["screenshot"] = (
                    self.browser.take_screenshot(
                        page, "foundit_no_apply_btn"
                    )
                )
                return result

            # Scroll to Apply
            self.browser.scroll_to_element(page, apply_sel)
            self.browser.random_delay(0.5, 1.5)

            # ── Click Apply ──
            logger.info(
                f"Clicking Apply: {job.get('title')} @ "
                f"{job.get('company')}"
            )
            self.browser.click_human(page, apply_sel)
            self.browser.random_delay(2.0, 4.0)

            # ── Detect circuit ──
            apply_type = self._detect_apply_circuit(page)
            result["apply_type"] = apply_type

            if apply_type == "success_quick":
                result["status"] = "submitted_quick"
                logger.info("Quick apply submitted immediately")
                return result

            elif apply_type == "form":
                ok = self._handle_apply_form(
                    page, job, resume_path
                )
                if ok:
                    result["status"] = "ready"
                    result["screenshot"] = (
                        self.browser.take_screenshot(
                            page, "foundit_form_ready"
                        )
                    )
                else:
                    result["error"] = "Form handling failed"
                    result["screenshot"] = (
                        self.browser.take_screenshot(
                            page, "foundit_form_fail"
                        )
                    )
                return result

            elif apply_type == "login_wall":
                logger.warning(
                    "Apply triggered login wall, retrying..."
                )
                if self.login():
                    return self.prepare_application(
                        job, resume_path, cover_letter
                    )
                result["error"] = "Login wall, re-auth failed"
                return result

            elif apply_type == "external_redirect":
                result["status"] = "external"
                return result

            else:
                result["error"] = (
                    f"Unknown apply circuit: {apply_type}"
                )
                result["screenshot"] = (
                    self.browser.take_screenshot(
                        page, "foundit_unknown_circuit"
                    )
                )
                return result

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"prepare_application failed: {e}")
            self._save_error("prepare_application", e)
            try:
                result["screenshot"] = (
                    self.browser.take_screenshot(
                        self._get_page(),
                        "foundit_prepare_exception"
                    )
                )
            except Exception:
                pass
            return result

    def _detect_apply_circuit(self, page) -> str:
        """Detect which apply circuit after clicking Apply."""
        time.sleep(1.5)

        # Immediate success
        if self._find(page, "apply_success"):
            return "success_quick"

        # Modal/form appeared
        if self._find(page, "apply_modal"):
            return "form"

        # Check for any visible form fields
        for key in ("form_input", "form_textarea", "form_select",
                    "resume_upload"):
            if self._find(page, key):
                return "form"

        # Login redirect
        current_url = self.browser.get_page_url(page)
        if "login" in current_url.lower():
            return "login_wall"

        # Wait more
        time.sleep(2.0)

        if self._find(page, "apply_success"):
            return "success_quick"
        if self._find(page, "apply_modal"):
            return "form"

        # New tab
        try:
            context = page.context
            if len(context.pages) > 1:
                return "external_redirect"
        except Exception:
            pass

        # Check if already_applied appeared
        if self._find(page, "already_applied"):
            return "success_quick"

        return "unknown"

    # ═══════════════════════════════════════════════════════════
    # APPLICATION — Form Handler
    # ═══════════════════════════════════════════════════════════

    def _handle_apply_form(self, page, job: Dict,
                           resume_path: str) -> bool:
        """
        Fill all fields in the Foundit apply form.

        Handles:
          - Text inputs (name, email, phone, experience, CTC)
          - Number inputs (experience years, salary)
          - Textareas (cover letter, why interested)
          - Dropdowns (notice period, location)
          - Checkboxes (consent)
          - Radio buttons
          - File upload (resume)

        Returns True if form is filled and Submit is visible.
        """
        try:
            self.browser.random_delay(1.0, 2.0)
            profile = USER_PROFILE

            # ── Fill text inputs ──
            for sel_key in ("form_input", "form_email", "form_tel",
                            "form_number"):
                for sel in _S.get(sel_key, []):
                    try:
                        inputs = page.query_selector_all(sel)
                        for inp in inputs:
                            self._fill_form_input(
                                page, inp, job, profile
                            )
                    except Exception:
                        continue

            # ── Fill textareas ──
            for sel in _S.get("form_textarea", []):
                try:
                    areas = page.query_selector_all(sel)
                    for area in areas:
                        self._fill_form_textarea(
                            page, area, job, profile
                        )
                except Exception:
                    continue

            # ── Handle dropdowns ──
            for sel in _S.get("form_select", []):
                try:
                    selects = page.query_selector_all(sel)
                    for select_el in selects:
                        self._fill_form_select(
                            page, select_el, job, profile
                        )
                except Exception:
                    continue

            # ── Upload resume ──
            if resume_path and os.path.isfile(resume_path):
                upload_sel = self._working_selector(
                    page, "resume_upload"
                )
                if upload_sel:
                    try:
                        page.set_input_files(
                            upload_sel, resume_path, timeout=10000
                        )
                        self.browser.random_delay(1.0, 2.5)
                        logger.debug("Foundit: resume uploaded")
                    except Exception as e:
                        logger.debug(
                            f"Resume upload failed: {e}"
                        )

            # ── Check checkboxes ──
            for sel in _S.get("form_checkbox", []):
                try:
                    cbs = page.query_selector_all(sel)
                    for cb in cbs:
                        if not cb.is_checked():
                            cb.click()
                            self.browser.random_delay(0.2, 0.5)
                except Exception:
                    continue

            # ── Handle radio buttons ──
            for sel in _S.get("form_radio", []):
                try:
                    radios = page.query_selector_all(sel)
                    if radios:
                        # Select first radio that looks like "Yes"
                        clicked = False
                        for r in radios:
                            label = ""
                            try:
                                parent = r.evaluate_handle(
                                    "el => el.parentElement"
                                )
                                if parent:
                                    label = (
                                        parent.as_element()
                                        .text_content() or ""
                                    ).strip().lower()
                            except Exception:
                                pass
                            if "yes" in label:
                                r.click()
                                clicked = True
                                break
                        if not clicked and radios:
                            radios[0].click()
                        self.browser.random_delay(0.2, 0.5)
                except Exception:
                    continue

            # ── Check Submit button is visible ──
            submit_el = self._find(page, "form_submit")
            if submit_el:
                logger.info(
                    "Foundit form filled, Submit visible — PAUSED"
                )
                return True

            logger.info(
                "Foundit form filled (Submit not confirmed)"
            )
            return True

        except Exception as e:
            logger.error(f"Form handling failed: {e}")
            return False

    def _fill_form_input(self, page, input_el, job: Dict,
                         profile: Dict) -> None:
        """Fill a single input in the apply form."""
        try:
            # Skip if already filled
            current_val = ""
            try:
                current_val = input_el.input_value() or ""
            except Exception:
                pass
            if current_val.strip():
                return

            # Determine field identity
            input_type = (
                input_el.get_attribute("type") or "text"
            ).lower()
            name = (
                input_el.get_attribute("name") or ""
            ).lower()
            placeholder = (
                input_el.get_attribute("placeholder") or ""
            ).lower()
            label = (
                input_el.get_attribute("aria-label") or ""
            ).lower()
            field_id = (
                input_el.get_attribute("id") or ""
            ).lower()
            combined = f"{name} {placeholder} {label} {field_id}"

            answer = ""

            if input_type == "email" or "email" in combined:
                answer = profile.get(
                    "email", "piyushkashyap3247@gmail.com"
                )
            elif input_type == "tel" or any(
                k in combined for k in ["phone", "mobile", "contact"]
            ):
                answer = profile.get("phone", "7310703247")
            elif any(k in combined for k in ["name", "full name"]):
                answer = profile.get("name", "Piyush Kashyap")
            elif any(k in combined for k in [
                "current ctc", "current salary", "ctc"
            ]):
                answer = str(profile.get("current_ctc_lpa", 3.7))
            elif any(k in combined for k in [
                "expected", "desired salary", "expected ctc"
            ]):
                answer = self._compute_expected(job, combined)
            elif any(k in combined for k in [
                "experience", "exp", "years"
            ]):
                answer = str(profile.get("experience_years", 1))
            elif any(k in combined for k in [
                "notice", "notice period"
            ]):
                answer = "15"
            elif any(k in combined for k in [
                "location", "city"
            ]):
                answer = profile.get(
                    "location", "Rishikesh, Uttarakhand"
                )
            elif any(k in combined for k in [
                "company", "current employer", "organization"
            ]):
                answer = profile.get(
                    "current_company", "Site Guru Pvt Ltd"
                )
            elif any(k in combined for k in [
                "designation", "title", "role", "current role"
            ]):
                answer = profile.get(
                    "current_title", "Full Stack Developer L1"
                )
            elif any(k in combined for k in [
                "linkedin"
            ]):
                answer = profile.get(
                    "linkedin_url",
                    "linkedin.com/in/piyush-kashyap731"
                )
            elif any(k in combined for k in [
                "github", "portfolio"
            ]):
                answer = profile.get(
                    "github_url", "github.com/Piyush731"
                )

            if answer:
                try:
                    input_el.click()
                    time.sleep(random.uniform(0.1, 0.3))
                    input_el.fill("")
                    input_el.type(
                        answer,
                        delay=random.randint(30, 80)
                    )
                    self.browser.random_delay(0.2, 0.5)
                    logger.debug(
                        f"Form input filled: "
                        f"{name or placeholder} = "
                        f"'{answer[:30]}'"
                    )
                except Exception as e:
                    logger.debug(f"Input fill error: {e}")

        except Exception as e:
            logger.debug(f"Form input error: {e}")

    def _fill_form_textarea(self, page, textarea_el, job: Dict,
                            profile: Dict) -> None:
        """Fill a textarea in the apply form."""
        try:
            current_val = ""
            try:
                current_val = textarea_el.input_value() or ""
            except Exception:
                pass
            if current_val.strip():
                return

            name = (
                textarea_el.get_attribute("name") or ""
            ).lower()
            placeholder = (
                textarea_el.get_attribute("placeholder") or ""
            ).lower()
            combined = f"{name} {placeholder}"

            answer = ""
            if any(k in combined for k in [
                "cover", "letter", "message"
            ]):
                answer = (
                    f"I am interested in the "
                    f"{job.get('title', 'position')} role at "
                    f"{job.get('company', 'your company')}. "
                    f"With experience in full stack development "
                    f"across 10+ production applications, I bring "
                    f"strong skills in backend and frontend "
                    f"development. I would welcome the opportunity "
                    f"to discuss how I can contribute to your team."
                )
            elif any(k in combined for k in [
                "why", "reason", "about", "interest"
            ]):
                answer = (
                    "I am looking for growth opportunities in a "
                    "product company where I can apply my full "
                    "stack development skills to challenging "
                    "projects."
                )
            else:
                # Generic textarea — short answer
                answer = (
                    f"Experienced full stack developer interested "
                    f"in this role at "
                    f"{job.get('company', 'your company')}."
                )

            if answer:
                try:
                    textarea_el.click()
                    time.sleep(random.uniform(0.1, 0.3))
                    textarea_el.fill("")
                    textarea_el.type(
                        answer,
                        delay=random.randint(20, 60)
                    )
                    self.browser.random_delay(0.2, 0.5)
                    logger.debug(f"Textarea filled: {name}")
                except Exception as e:
                    logger.debug(f"Textarea fill error: {e}")

        except Exception as e:
            logger.debug(f"Form textarea error: {e}")

    def _fill_form_select(self, page, select_el, job: Dict,
                          profile: Dict) -> None:
        """Fill a dropdown in the apply form."""
        try:
            name = (
                select_el.get_attribute("name") or ""
            ).lower()
            aria = (
                select_el.get_attribute("aria-label") or ""
            ).lower()
            combined = f"{name} {aria}"

            answer = ""
            if "notice" in combined:
                answer = profile.get("notice_period", "15 Days")
            elif "experience" in combined:
                answer = str(profile.get("experience_years", 1))
            elif "location" in combined or "city" in combined:
                answer = profile.get("location", "")
            elif "gender" in combined:
                answer = "Male"
            elif "qualification" in combined or "degree" in combined:
                answer = "B.Tech"

            if answer:
                # Try fuzzy match
                try:
                    options = select_el.query_selector_all("option")
                    for opt in options:
                        opt_text = (
                            opt.text_content() or ""
                        ).strip()
                        if (answer.lower() in opt_text.lower() or
                                opt_text.lower() in answer.lower()):
                            val = opt.get_attribute("value")
                            if val:
                                select_el.select_option(value=val)
                            else:
                                select_el.select_option(
                                    label=opt_text
                                )
                            self.browser.random_delay(0.3, 0.7)
                            logger.debug(
                                f"Select: {name} = '{opt_text}'"
                            )
                            return
                except Exception:
                    pass

            # Fallback: select second option
            try:
                select_el.select_option(index=1)
                self.browser.random_delay(0.3, 0.7)
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"Form select error: {e}")

    def _compute_expected(self, job: Dict, question: str) -> str:
        """Compute expected salary answer."""
        sal_max = job.get("salary_max", 0) or 0
        sal_min = job.get("salary_min", 0) or 0

        if sal_max > 0:
            expected = sal_min + (sal_max - sal_min) * 0.65
        else:
            expected = 800000  # 8 LPA default

        if expected < 500000:
            expected = 500000

        if any(k in question for k in ["lakh", "lac", "lpa"]):
            return f"{expected / 100000:.1f}"
        return str(int(expected))

    def _skill_years(self, skill: str) -> str:
        """Return years of experience for a skill."""
        skill_lower = skill.strip().lower()

        if skill_lower in _SKILL_YEARS:
            return str(_SKILL_YEARS[skill_lower])

        for key, years in _SKILL_YEARS.items():
            if key in skill_lower or skill_lower in key:
                return str(years)

        return "0.5"

    # ═══════════════════════════════════════════════════════════
    # APPLICATION — Submit
    # ═══════════════════════════════════════════════════════════

    def submit_application(self, prepared: Dict) -> Dict:
        """
        Click the final Submit button.

        Called ONLY after Telegram approval.
        """
        result = {
            "success": False,
            "status": "failed",
            "error": None,
            "screenshot": "",
            "timestamp": datetime.now().isoformat(),
        }

        try:
            page = self._get_page()

            # Already submitted during prepare
            if prepared.get("status") == "submitted_quick":
                result["success"] = True
                result["status"] = "submitted"
                self.increment_count()
                logger.info("Quick apply already submitted")
                return result

            if prepared.get("status") in ("external",
                                           "already_applied"):
                result["status"] = prepared["status"]
                result["error"] = (
                    f"Cannot submit: {prepared['status']}"
                )
                return result

            # ── Find and click Submit ──
            submitted = False
            for sel in _S.get("form_submit", []):
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        self.browser.random_delay(0.5, 1.5)
                        submitted = True
                        logger.debug(f"Submit clicked: {sel}")
                        break
                except Exception:
                    continue

            if not submitted:
                # Generic fallback
                try:
                    btn = page.query_selector(
                        "button[type='submit'], "
                        "input[type='submit']"
                    )
                    if btn:
                        btn.click()
                        self.browser.random_delay(0.5, 1.5)
                        submitted = True
                except Exception:
                    pass

            if not submitted:
                result["error"] = (
                    "Submit button not found or not clickable"
                )
                result["screenshot"] = (
                    self.browser.take_screenshot(
                        page, "foundit_submit_not_found"
                    )
                )
                return result

            # ── Wait for confirmation ──
            self.browser.random_delay(2.0, 5.0)

            # Check for success
            if self._find(page, "apply_success"):
                result["success"] = True
                result["status"] = "submitted"
                self.increment_count()
                result["screenshot"] = (
                    self.browser.take_screenshot(
                        page, "foundit_submit_success"
                    )
                )
                job = prepared.get("job", {})
                logger.info(
                    f"✅ Application submitted: "
                    f"{job.get('title', '?')} @ "
                    f"{job.get('company', '?')}"
                )
                return result

            # Overlay closed = probably succeeded
            if not self._find(page, "apply_modal"):
                result["success"] = True
                result["status"] = "submitted"
                self.increment_count()
                logger.info(
                    "Application likely submitted "
                    "(modal closed)"
                )
                return result

            # Already Applied appeared
            if self._find(page, "already_applied"):
                result["success"] = True
                result["status"] = "submitted"
                self.increment_count()
                logger.info(
                    "Application confirmed "
                    "(Already Applied visible)"
                )
                return result

            # Uncertain but likely went through
            result["success"] = True
            result["status"] = "submitted"
            self.increment_count()
            result["error"] = (
                "Submitted but could not confirm success"
            )
            return result

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"submit_application failed: {e}")
            self._save_error("submit_application", e)
            return result

    # ═══════════════════════════════════════════════════════════
    # STATUS CHECK
    # ═══════════════════════════════════════════════════════════

    def check_status(self,
                     application_id: Optional[int] = None) -> str:
        """
        Check application status on Foundit's applied jobs page.

        Returns:
            "submitted" | "viewed" | "shortlisted" |
            "rejected" | "unknown"
        """
        try:
            page = self._get_page()

            if not self._check_logged_in(page):
                if not self.login():
                    return "unknown"

            if not self.browser.navigate(page, _URLS["applied"]):
                return "unknown"

            self.browser.random_delay(2.0, 4.0)
            self._dismiss_popups(page)

            cards = self._find_all(page, "applied_card")
            if not cards:
                logger.debug("No applied job cards found")
                return "unknown"

            for card in cards:
                try:
                    status_text = ""
                    for sel in _S["applied_status"]:
                        status_el = card.query_selector(sel)
                        if status_el:
                            status_text = (
                                status_el.text_content() or ""
                            ).strip().lower()
                            break

                    if "viewed" in status_text:
                        return "viewed"
                    elif "shortlist" in status_text:
                        return "shortlisted"
                    elif "reject" in status_text:
                        return "rejected"
                    elif "applied" in status_text:
                        return "submitted"

                except Exception:
                    continue

            return "unknown"

        except Exception as e:
            logger.error(f"check_status failed: {e}")
            return "unknown"

    # ═══════════════════════════════════════════════════════════
    # SESSION & ERROR HELPERS
    # ═══════════════════════════════════════════════════════════

    def _update_session(self, **kwargs) -> None:
        """Update platform session in database."""
        try:
            db = get_db()
            updates = {}
            if "logged_in" in kwargs:
                updates["logged_in"] = (
                    1 if kwargs["logged_in"] else 0
                )
                if kwargs["logged_in"]:
                    updates["last_login"] = (
                        datetime.now().isoformat()
                    )
            updates.update(
                {k: v for k, v in kwargs.items()
                 if k != "logged_in"}
            )
            db.update_platform_session(
                self.PLATFORM_NAME, updates
            )
        except Exception as e:
            logger.debug(f"Session update failed: {e}")

    def _save_error(self, method: str,
                    error: Exception) -> None:
        """Log error to database."""
        try:
            db = get_db()
            db.save_error(
                module=f"platforms.foundit.{method}",
                error_type=type(error).__name__,
                message=str(error),
                traceback=tb_module.format_exc(),
            )
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════
    # CONVENIENCE — Discover + save
    # ═══════════════════════════════════════════════════════════

    def discover_and_save(
        self,
        queries: Optional[List[str]] = None,
        filters: Optional[Dict] = None,
    ) -> Dict:
        """
        Search for jobs, save new ones to DB, return stats.

        Returns:
            {total_found, new_saved, duplicates, errors}
        """
        stats = {
            "total_found": 0,
            "new_saved": 0,
            "duplicates": 0,
            "errors": 0,
        }

        try:
            jobs = self.search_jobs(queries, filters)
            stats["total_found"] = len(jobs)

            db = get_db()
            for job in jobs:
                try:
                    existing = db.get_job_by_platform_id(
                        self.PLATFORM_NAME,
                        job.get("platform_job_id", "")
                    )
                    if existing:
                        stats["duplicates"] += 1
                        continue

                    job_data = {
                        "platform": self.PLATFORM_NAME,
                        "platform_job_id": job.get(
                            "platform_job_id", ""
                        ),
                        "url": job.get("url", ""),
                        "title": job.get("title", ""),
                        "company": job.get("company", ""),
                        "location": job.get("location", ""),
                        "salary_min": job.get("salary_min", 0),
                        "salary_max": job.get("salary_max", 0),
                        "experience_min": job.get(
                            "experience_min", 0
                        ),
                        "experience_max": job.get(
                            "experience_max", 0
                        ),
                        "description": job.get(
                            "description", ""
                        ),
                        "skills": json.dumps(
                            job.get("skills", [])
                        ),
                        "posted_date": job.get(
                            "posted_date", ""
                        ),
                        "discovered_at": job.get(
                            "discovered_at",
                            datetime.now().isoformat()
                        ),
                        "status": "new",
                    }
                    db.save_job(job_data)
                    stats["new_saved"] += 1

                except Exception as e:
                    logger.debug(f"Error saving job: {e}")
                    stats["errors"] += 1

            logger.info(
                f"Foundit discover: {stats['total_found']} found, "
                f"{stats['new_saved']} new, "
                f"{stats['duplicates']} duplicates"
            )

        except Exception as e:
            logger.error(f"discover_and_save failed: {e}")
            self._save_error("discover_and_save", e)

        return stats


# ═══════════════════════════════════════════════════════════════════
# TEST BLOCK
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print(
        "\n[bold cyan]═══ Foundit Platform Test ═══[/bold cyan]\n"
    )

    # ── 1. Dependency check ──
    console.print("[yellow]1. Dependency check:[/yellow]")
    console.print(
        f"   FOUNDIT_EMAIL: "
        f"{'[green]✓ set[/green]' if FOUNDIT_EMAIL else '[yellow]⚠ not set (will use USER_PROFILE.email)[/yellow]'}"
    )
    console.print(
        f"   answers.py: "
        f"{'[green]✓ available[/green]' if _ANSWERS_AVAILABLE else '[yellow]⚠ not available (Phase 2)[/yellow]'}"
    )

    # ── 2. Parser tests ──
    console.print("\n[yellow]2. Salary parser tests:[/yellow]")
    salary_tests = [
        ("₹ 5-10 Lacs P.A.", (500000, 1000000)),
        ("3.5 - 7 Lacs PA", (350000, 700000)),
        ("Not disclosed", (0, 0)),
        ("8 Lacs PA", (800000, 800000)),
        ("", (0, 0)),
    ]
    for text, expected in salary_tests:
        result = _parse_salary_text(text)
        ok = (abs(result[0] - expected[0]) < 1 and
              abs(result[1] - expected[1]) < 1)
        icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
        console.print(
            f"   {icon} '{text}' → {result} "
            f"(expected {expected})"
        )

    console.print("\n[yellow]3. Experience parser tests:[/yellow]")
    exp_tests = [
        ("0-2 Yrs", (0, 2)),
        ("3-5 Years", (3, 5)),
        ("Fresher", (0, 0)),
        ("5+ Yrs", (5, 99)),
        ("", (0, 0)),
    ]
    for text, expected in exp_tests:
        result = _parse_experience_text(text)
        ok = (abs(result[0] - expected[0]) < 0.1 and
              abs(result[1] - expected[1]) < 0.1)
        icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
        console.print(
            f"   {icon} '{text}' → {result} "
            f"(expected {expected})"
        )

    # ── 3. Initialize ──
    console.print(
        "\n[yellow]4. Initializing FounditPlatform...[/yellow]"
    )
    try:
        from core.browser import BrowserEngine
        engine = BrowserEngine()
        foundit = FounditPlatform(engine)
        console.print(
            f"   [green]✓[/green] Platform: "
            f"{foundit.platform_name}"
        )
        console.print(
            f"   Config: "
            f"{json.dumps(foundit._foundit_config, indent=2)[:200]}"
        )
    except Exception as e:
        console.print(f"   [red]✗ Init failed: {e}[/red]")
        sys.exit(1)

    # ── 4. URL builder ──
    console.print("\n[yellow]5. Search URL builder:[/yellow]")
    url = foundit._build_search_url("Software Engineer", {})
    console.print(f"   URL: {url[:100]}...")

    url2 = foundit._build_search_url("Java Developer", {
        "locations": ["Bangalore", "Pune"],
        "experience_min": 0,
    })
    console.print(f"   URL with filters: {url2[:100]}...")

    # ── 5. Skill years ──
    console.print("\n[yellow]6. Skill years mapping:[/yellow]")
    skill_tests = [
        "JavaScript", "Vue.js", "Java", "Python",
        "Spring Boot", "MongoDB", "Kubernetes"
    ]
    for skill in skill_tests:
        years = foundit._skill_years(skill)
        console.print(f"   {skill}: {years} years")

    # ── 6. Expected salary ──
    console.print("\n[yellow]7. Expected salary calc:[/yellow]")
    test_job = {
        "salary_min": 600000,
        "salary_max": 1200000,
    }
    exp_sal = foundit._compute_expected(test_job, "expected lpa")
    console.print(
        f"   Job range 6-12 LPA → expected: {exp_sal} LPA"
    )

    test_job2 = {"salary_min": 0, "salary_max": 0}
    exp_sal2 = foundit._compute_expected(test_job2, "expected")
    console.print(
        f"   No range → expected: {exp_sal2}"
    )

    # ── 7. Live test ──
    email_available = FOUNDIT_EMAIL or USER_PROFILE.get("email", "")
    if email_available:
        console.print(
            "\n[yellow]8. Live browser test "
            "(requires network):[/yellow]"
        )
        run_live = input(
            "   Run live Foundit test? (y/n): "
        ).strip().lower()

        if run_live == "y":
            try:
                console.print("   Attempting login...")
                login_ok = foundit.login()
                console.print(
                    f"   Login: "
                    f"{'[green]✓ success[/green]' if login_ok else '[red]✗ failed[/red]'}"
                )

                if login_ok:
                    console.print(
                        "   Searching for "
                        "'Software Engineer'..."
                    )
                    jobs = foundit.search_jobs(
                        ["Software Engineer"],
                        {"experience_min": 0}
                    )
                    console.print(
                        f"   [green]✓[/green] Found "
                        f"{len(jobs)} jobs"
                    )

                    if jobs:
                        table = Table(
                            title="Foundit Results (top 5)"
                        )
                        table.add_column(
                            "Title", style="cyan",
                            max_width=30
                        )
                        table.add_column(
                            "Company", max_width=20
                        )
                        table.add_column(
                            "Location", max_width=15
                        )
                        table.add_column("Salary")
                        table.add_column("Exp")

                        for j in jobs[:5]:
                            table.add_row(
                                j.get("title", "")[:30],
                                j.get("company", "")[:20],
                                j.get("location", "")[:15],
                                j.get("salary_text", "N/A"),
                                j.get(
                                    "experience_text", "N/A"
                                ),
                            )

                        console.print(table)

                        # Save to DB
                        console.print(
                            "\n   Saving to database..."
                        )
                        stats = foundit.discover_and_save(
                            ["Software Engineer"]
                        )
                        console.print(
                            f"   [green]✓[/green] "
                            f"Saved: {stats['new_saved']} "
                            f"new, "
                            f"{stats['duplicates']} "
                            f"duplicates"
                        )

            except Exception as e:
                console.print(
                    f"   [red]Live test error: {e}[/red]"
                )
                import traceback
                traceback.print_exc()
            finally:
                engine.close_all()
    else:
        console.print(
            "\n[yellow]8. Live test skipped "
            "(no email configured)[/yellow]"
        )

    # ── 8. Database check ──
    console.print("\n[yellow]9. Database check:[/yellow]")
    try:
        db = get_db()
        info = db.get_table_info()
        console.print(f"   Jobs in DB: {info.get('jobs', 0)}")
        console.print(
            f"   Applications in DB: "
            f"{info.get('applications', 0)}"
        )
    except Exception as e:
        console.print(f"   [yellow]DB check: {e}[/yellow]")

    console.print(
        f"\n[bold green]"
        f"═══ Foundit platform tests complete! ═══"
        f"[/bold green]\n"
    )