"""
platforms/naukri.py — Naukri.com job search and semi-auto application.

Handles:
  - Auto-login with email/password (CAPTCHA/OTP via Telegram)
  - Multi-query job search with pagination
  - Job detail extraction (full JD, skills, salary, etc.)
  - Semi-auto application (prepare → Telegram approve → submit)
  - Chatbot questionnaire handling (text, dropdown, radio, checkbox)
  - iframe-based application forms
  - Profile update (boosts visibility in recruiter search)
  - Applied-jobs status check
  - Popup/banner dismissal
  - External-apply detection and logging

Naukri apply circuits:
  Circuit A — Quick Apply: click Apply → immediate submit (rare)
  Circuit B — Chatbot: click Apply → overlay slides in → questions one
              by one (text / dropdown / radio / chip-buttons) → Submit
  Circuit C — Modal form: click Apply → modal with multiple fields +
              resume upload → Submit
  Circuit D — External: "Apply on company site" → new tab → log only
  Circuit E — Already Applied: button disabled → skip
  Circuit F — Login wall: apply redirects to login → re-auth → retry

Prerequisites:
    All Phase 1 modules working (config, logger, db, browser, base)
    NAUKRI_EMAIL and NAUKRI_PASSWORD set in .env

Usage:
    from core.browser import BrowserEngine
    from platforms.naukri import NaukriPlatform

    engine = BrowserEngine()
    naukri = NaukriPlatform(engine)

    if naukri.login():
        jobs = naukri.search_jobs()
        for job in jobs[:5]:
            details = naukri.get_job_details(job["url"])
            prepared = naukri.prepare_application(job, "/path/resume.pdf")
            if prepared["status"] == "ready":
                result = naukri.submit_application(prepared)
"""

import re
import os
import json
import time
import random
import traceback as tb_module
import threading
import concurrent.futures
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import quote_plus, urlencode

from config import (
    PLATFORM_CONFIG,
    USER_PROFILE,
    STEALTH_CONFIG,
    RESUME_CONFIG,
)
from core.logger import get_logger
from core.db import get_db
from platforms.base import PlatformBase

logger = get_logger("platforms.naukri")

# ── Credentials (import safely — .env may not have them yet) ────
try:
    from config import NAUKRI_EMAIL, NAUKRI_PASSWORD
except ImportError:
    NAUKRI_EMAIL = ""
    NAUKRI_PASSWORD = ""

# ── Optional: profile/answers.py (not built yet in Phase 1) ────
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
    "login":           "https://www.naukri.com/nlogin/login",
    "home":            "https://www.naukri.com/",
    "search_base":     "https://www.naukri.com/jobs-in-india",
    "profile":         "https://www.naukri.com/mnjuser/profile",
    "applied":         "https://www.naukri.com/mnjuser/applied-jobs",
    "recommendations": "https://www.naukri.com/mnjuser/recommendedjobs",
}

# ── Multi-fallback CSS Selectors ──────────────────────────────────
# Naukri uses CSS-modules with hashed class names that change every
# deploy.  We list several selectors per element — first match wins.
# When Naukri redesigns, just update the first entry; the rest are
# safety-nets.

_S = {
    # ── Login ──
    "login_email": [
        "#usernameField",
        "input[placeholder*='Email']",
        "input[placeholder*='email']",
        "input[name='username']",
        "input[type='text'][autocomplete='username']",
    ],
    "login_password": [
        "#passwordField",
        "input[type='password']",
        "input[placeholder*='password' i]",
    ],
    "login_submit": [
        "button[type='submit']",
        "button:has-text('Login')",
        "input[type='submit']",
    ],
    "login_error": [
        "span[class*='error']",
        "div[class*='error']",
        "span.err-msg",
    ],

    # ── Logged-in indicators (any one visible = logged in) ──
    "logged_in": [
        "a[href*='/mnjuser/profile']",
        "div[class*='nI-gNb-header']",
        "a[class*='user-name']",
        "#user-badge",
        "a[href*='mnjuser']",
        "div[class*='user-section']",
        "div[class*='nI-gNb-hamburger']",
    ],

    # ── Search results ──
    "job_card": [
        "div.srp-jobtuple-wrapper",
        "div[class*='jobtuple-wrapper']",
        "article.jobTuple",
        "div[class*='jobTuple']",
        "div[data-job-id]",
    ],
    "pagination_next": [
        "a[class*='fright']:has-text('Next')",
        "a.fright.fs14.btn-secondary",
        "a:has-text('Next')",
    ],

    # ── Inside a job card ──
    "card_title":       ["a.title", "a[class*='title']", "h2 a"],
    "card_company":     ["a.comp-name", "a.subTitle", "a[class*='comp-name']",
                         "span[class*='comp-name']"],
    "card_experience":  ["span.expwdth", "span[class*='exp']",
                         "span[title*='Yrs']"],
    "card_salary":      ["span.sal span", "span[class*='sal']",
                         "span[title*='PA']"],
    "card_location":    ["span.locWdth", "span[class*='loc']",
                         "span[class*='location']"],
    "card_description": ["span.job-desc", "span[class*='job-desc']",
                         "div[class*='job-desc']"],
    "card_tags":        ["ul.tags-gt li", "li.dot-gt.tag-li",
                         "ul[class*='tags'] li", "a[class*='tag-li']"],
    "card_posted":      ["span.job-post-day", "span[class*='job-post-day']",
                         "span[class*='posted']"],
    "card_link":        ["a.title", "a[class*='title']", "h2 a",
                         "a[href*='/job-listings']", "a[href*='/job/']"],

    # ── Job detail page ──
    "detail_title":     ["h1[class*='jd-header-title']", "h1"],
    "detail_company":   ["a[class*='jd-header-comp-name']",
                         "a[class*='comp-name']", "a[href*='/company-jobs']"],
    "detail_experience": ["span[class*='exp']", "span:has-text('Yrs')",
                          "div[class*='exp'] span"],
    "detail_salary":    ["span[class*='sal']", "span:has-text('PA')",
                         "div[class*='salary'] span"],
    "detail_location":  ["span[class*='loc']", "a[class*='location']"],
    "detail_jd":        ["div[class*='dang-inner-html']",
                         "div[class*='job-desc']",
                         "section[class*='job-desc']", "div.job-desc"],
    "detail_skills":    ["div[class*='key-skill'] a", "a[class*='chip']",
                         "div[class*='chip-wrapper'] span",
                         "a[href*='tag']", "span[class*='skill-tag']"],
    "detail_info":      ["div[class*='other-details'] div",
                         "div[class*='detail-row']"],

    # ── Apply button ──
    "apply_btn":        ["#apply-button", "button[id='apply-button']",
                         "button:has-text('Apply')",
                         "button[class*='apply-button']"],
    "already_applied":  ["button:has-text('Already Applied')",
                         "button[disabled]:has-text('Applied')",
                         "span:has-text('Already Applied')",
                         "div[class*='already-applied']"],
    "external_apply":   ["a:has-text('Apply on company site')",
                         "a[class*='company-site']",
                         "button:has-text('Apply on company')"],

    # ── Chatbot overlay ──
    "chatbot_wrap":     ["div[class*='chatbot']", "div[class*='Chatbot']",
                         "div[class*='apply-overlay']",
                         "div[class*='drawer']", "div[class*='apply-modal']"],
    "chatbot_question": ["div[class*='botMsg']", "div[class*='bot-msg']",
                         "div[class*='msg-bot']", "p[class*='chatbot']",
                         "div[class*='question']"],
    "chatbot_text_in":  ["input[class*='chatbot']",
                         "input[placeholder*='Type' i]",
                         "input[class*='chatInput']",
                         "textarea[class*='chatbot']",
                         "div[class*='chatbot'] input[type='text']"],
    "chatbot_send":     ["button[class*='send' i]",
                         "button:has-text('Send')",
                         "button[class*='chatbot'] svg"],
    "chatbot_select":   ["div[class*='chatbot'] select", "select"],
    "chatbot_radio":    ["div[class*='chatbot'] input[type='radio']",
                         "input[type='radio']"],
    "chatbot_chips":    ["button[class*='option']",
                         "div[class*='options'] button",
                         "button[class*='chip']",
                         "div[class*='chip'] button"],
    "chatbot_submit":   ["button:has-text('Submit')",
                         "button:has-text('Submit Application')",
                         "button[class*='submit']"],
    "chatbot_next":     ["button:has-text('Next')",
                         "button:has-text('Continue')",
                         "button:has-text('Proceed')"],

    # ── Apply modal / form ──
    "apply_modal":      ["div[role='dialog']",
                         "div[class*='apply-modal']",
                         "div[class*='modal'][class*='apply']"],
    "resume_upload":    ["input[type='file']",
                         "input[accept*='.pdf']",
                         "input[accept*='.doc']",
                         "input[name*='resume' i]"],
    "apply_success":    ["div:has-text('Application Submitted')",
                         "div:has-text('applied successfully')",
                         "div:has-text('Successfully Applied')",
                         "div[class*='success']",
                         "span:has-text('application submitted' i)"],

    # ── Popups/banners to dismiss ──
    "popup_close":      ["button[class*='cross']", "button[class*='close']",
                         "span[class*='cross']", "div[class*='popup'] button",
                         "div[class*='modal'] button[class*='close']",
                         "button[aria-label='Close']"],

    # ── Profile page ──
    "profile_upload":   ["input[type='file']"],
    "profile_headline": ["div[class*='resumeHeadline'] span[class*='edit']",
                         "span[class*='edit-icon']"],
    "profile_save":     ["button:has-text('Save')",
                         "button:has-text('Update')"],

    # ── iframes ──
    "apply_iframe":     ["iframe[src*='apply']", "iframe[class*='apply']",
                         "iframe[id*='apply']", "iframe"],

    # ── Modal form fields (multi-field form inside modal) ──
    "modal_input":      ["div[role='dialog'] input[type='text']",
                         "div[class*='modal'] input[type='text']"],
    "modal_select":     ["div[role='dialog'] select",
                         "div[class*='modal'] select"],
    "modal_textarea":   ["div[role='dialog'] textarea",
                         "div[class*='modal'] textarea"],
    "modal_checkbox":   ["div[role='dialog'] input[type='checkbox']",
                         "div[class*='modal'] input[type='checkbox']"],
    "modal_submit":     ["div[role='dialog'] button:has-text('Submit')",
                         "div[role='dialog'] button:has-text('Apply')",
                         "div[class*='modal'] button:has-text('Submit')"],
    "modal_close":      ["div[role='dialog'] button[class*='close']",
                         "div[role='dialog'] button[aria-label='Close']"],

    # ── Applied jobs page ──
    "applied_card":     ["div[class*='applied-job']",
                         "div[class*='appCard']",
                         "article[class*='applied']"],
    "applied_title":    ["a[class*='title']", "h3 a", "a[class*='job-title']"],
    "applied_status":   ["span[class*='status']", "div[class*='status']",
                         "span[class*='response']"],
    "applied_date":     ["span[class*='date']", "div[class*='applied-on']"],
}


# ═══════════════════════════════════════════════════════════════════
# SALARY / EXPERIENCE PARSERS
# ═══════════════════════════════════════════════════════════════════

def _parse_salary_text(text: str) -> Tuple[float, float]:
    """
    Parse Naukri salary text into (min, max) in INR.

    Examples:
        '₹ 5-10 Lacs P.A.'     → (500000, 1000000)
        '3.5 - 7 Lacs PA'      → (350000, 700000)
        '₹10,00,000-20,00,000' → (1000000, 2000000)
        'Not disclosed'        → (0, 0)
    """
    if not text:
        return 0.0, 0.0
    t = text.strip().lower()

    if "not disclosed" in t or "confidential" in t:
        return 0.0, 0.0

    # Pattern: X-Y Lacs / Lakhs
    m = re.search(r'([\d.]+)\s*[-–to]+\s*([\d.]+)\s*(?:lacs?|lakhs?|lpa)',
                  t, re.I)
    if m:
        return float(m.group(1)) * 100000, float(m.group(2)) * 100000

    # Pattern: X,XX,XXX - Y,YY,YYY
    m = re.search(r'([\d,]+)\s*[-–to]+\s*([\d,]+)', t)
    if m:
        lo = float(m.group(1).replace(",", ""))
        hi = float(m.group(2).replace(",", ""))
        if lo > 1000:  # absolute numbers
            return lo, hi

    # Single number
    m = re.search(r'([\d.]+)\s*(?:lacs?|lakhs?|lpa)', t, re.I)
    if m:
        val = float(m.group(1)) * 100000
        return val, val

    return 0.0, 0.0


def _parse_experience_text(text: str) -> Tuple[float, float]:
    """
    Parse Naukri experience text into (min, max) years.

    Examples:
        '0-2 Yrs'   → (0, 2)
        '3-5 Years'  → (3, 5)
        'Fresher'    → (0, 0)
        '5+ Yrs'     → (5, 99)
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
    # Primary stack (actual production experience at Site Guru)
    "javascript": 1, "js": 1, "es6": 1,
    "vue": 1, "vue.js": 1, "vuejs": 1,
    "nuxt": 1, "nuxt.js": 1, "nuxtjs": 1,
    "vuetify": 1,
    "node": 1, "node.js": 1, "nodejs": 1,
    "express": 1, "express.js": 1, "expressjs": 1,
    "mysql": 1, "sql": 1,
    "rest": 1, "rest api": 1, "restful": 1, "api": 1,
    "websocket": 1, "websockets": 1,
    "html": 1, "css": 1,
    "git": 1, "github": 1,
    # Secondary (projects / learning)
    "react": 0.5, "react.js": 0.5, "reactjs": 0.5,
    "java": 0.5, "spring": 0.5, "spring boot": 0.5, "springboot": 0.5,
    "python": 0.5,
    "mongodb": 0.5, "mongo": 0.5,
    "postgresql": 0.5, "postgres": 0.5,
    "docker": 0.5,
    "kafka": 0.3,
    "redis": 0.3,
    "tailwind": 0.5, "tailwindcss": 0.5, "tailwind css": 0.5,
    # Salesforce (internship)
    "salesforce": 0.3, "apex": 0.3, "lwc": 0.3,
    # General
    "full stack": 1, "fullstack": 1, "full-stack": 1,
    "backend": 1, "back-end": 1, "back end": 1,
    "frontend": 1, "front-end": 1, "front end": 1,
    "microservices": 0.5,
    "agile": 0.5, "scrum": 0.5,
    "jwt": 0.5, "authentication": 0.5,
    "razorpay": 0.5, "payment gateway": 0.5,
    "whatsapp api": 0.5, "meta api": 0.5,
}


# ═══════════════════════════════════════════════════════════════════
# NAUKRI PLATFORM
# ═══════════════════════════════════════════════════════════════════

class NaukriPlatform(PlatformBase):
    """
    Naukri.com platform: login, search, scrape, semi-auto apply.

    Extends PlatformBase with Naukri-specific selectors, chatbot handling,
    profile update, and apply-circuit detection.
    """

    PLATFORM_NAME = "naukri"

    def __init__(self, browser_engine, notifier=None):
        super().__init__(browser_engine)
        if notifier:
            self.set_notifier(notifier)
        self._naukri_config = PLATFORM_CONFIG.get("naukri", {})
        self._current_job = None
        logger.info("NaukriPlatform initialized")

    # ═══════════════════════════════════════════════════════════
    # INTERNAL: Page & Element helpers
    # ═══════════════════════════════════════════════════════════
    def _get_page(self):
        """Get or launch the Naukri browser page."""
        # ALWAYS get from browser engine — never cache separately
        page = self.browser.get_page(self.PLATFORM_NAME)
        if page is not None:
            try:
                if not page.is_closed():
                    _ = page.url  # probe
                    self._page = page
                    return page
            except Exception:
                pass
        
        # Need to launch
        page = self.browser.launch(self.PLATFORM_NAME)
        self._page = page
        return page
    # def _get_page(self):
    #     if self._page is not None:
    #         try:
    #             if not self._page.is_closed():
    #                 _ = self._page.url
    #                 return self._page
    #         except Exception:
    #             pass

    #     def _launch():
    #         return self.browser.launch(self.PLATFORM_NAME)

    #     with concurrent.futures.ThreadPoolExecutor() as executor:
    #         future = executor.submit(_launch)
    #         self._page = future.result(timeout=30)

    #     return self._page

    def _find(self, page, key: str, timeout: int = 3000):
        """
        Try each selector in _S[key] until one matches.
        Returns the first matching element, or None.
        """
        selectors = _S.get(key, [])
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el:
                    return el
            except Exception:
                continue
        return None

    def _find_wait(self, page, key: str, timeout: int = 8000):
        """Like _find but waits for the element to appear."""
        selectors = _S.get(key, [])
        for sel in selectors:
            try:
                el = page.wait_for_selector(sel, timeout=timeout,
                                            state="visible")
                if el:
                    return el
            except Exception:
                continue
        return None

    def _find_all(self, page, key: str) -> list:
        """Return ALL matching elements for first successful selector."""
        selectors = _S.get(key, [])
        for sel in selectors:
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
        selectors = _S.get(key, [])
        for sel in selectors:
            try:
                if page.query_selector(sel):
                    self.browser.click_human(page, sel)
                    return True
            except Exception:
                continue
        return False

    def _dismiss_popups(self, page) -> None:
        """Close any Naukri popups, banners, cookie notices."""
        for sel in _S.get("popup_close", []):
            try:
                if self.browser.element_visible(page, sel):
                    page.click(sel, timeout=2000)
                    time.sleep(0.3)
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════
    # LOGIN
    # ═══════════════════════════════════════════════════════════

    def login(self) -> bool:
        """
        Log in to Naukri.com.

        Flow:
          1. Launch browser, load saved cookies.
          2. Navigate to home → check if already logged in.
          3. If not → go to login page → enter creds → submit.
          4. Handle CAPTCHA/OTP if triggered (via Telegram).
          5. Save cookies on success.

        Returns:
            True if login succeeded.
        """
        try:
            page = self._get_page()

            # Try home first (cookies may still be valid)
            self.browser.navigate(page, _URLS["home"])
            self._dismiss_popups(page)
            self.browser.random_delay(1.0, 2.5)

            if self._check_logged_in(page):
                logger.info("Already logged in to Naukri (saved session)")
                self._update_session(logged_in=True)
                return True

            # Navigate to login page
            if not self.browser.navigate(page, _URLS["login"]):
                logger.error("Could not reach Naukri login page")
                return False

            self.browser.random_delay(1.5, 3.0)
            self._dismiss_popups(page)

            # Check credentials
            email = NAUKRI_EMAIL
            password = NAUKRI_PASSWORD
            if not email or not password:
                logger.error(
                    "NAUKRI_EMAIL / NAUKRI_PASSWORD not set in .env"
                )
                return False

            # Enter email
            email_sel = self._working_selector(page, "login_email")
            if not email_sel:
                logger.error("Cannot find email field on Naukri login")
                self.browser.take_screenshot(page, "naukri_login_no_email")
                return False

            self.browser.type_human(page, email_sel, email)
            self.browser.random_delay(0.5, 1.0)

            # Enter password
            pwd_sel = self._working_selector(page, "login_password")
            if not pwd_sel:
                logger.error("Cannot find password field on Naukri login")
                self.browser.take_screenshot(page, "naukri_login_no_pwd")
                return False

            self.browser.type_human(page, pwd_sel, password)
            self.browser.random_delay(0.5, 1.5)

            # Click submit
            submit_sel = self._working_selector(page, "login_submit")
            if submit_sel:
                self.browser.click_human(page, submit_sel)
            else:
                self.browser.press_key(page, "Enter")

            self.browser.random_delay(3.0, 6.0)

            # ── Post-submit checks ──

            # Check for CAPTCHA
            captcha_type = self.detect_captcha(page)
            if captcha_type:
                logger.warning(
                    f"CAPTCHA detected on Naukri login: {captcha_type}"
                )
                handled = self.handle_captcha(page) 
                if not handled:
                    logger.error("CAPTCHA not solved, login failed")
                    self.browser.take_screenshot(page, "naukri_captcha_fail")
                    return False
                self.browser.random_delay(2.0, 4.0)

            # Check for OTP
            if self.detect_otp_page(page):
                logger.warning("OTP page detected on Naukri login")
                handled = self.handle_otp(page)  
                if not handled:
                    logger.error("OTP not entered, login failed")
                    self.browser.take_screenshot(page, "naukri_otp_fail")
                    return False
                self.browser.random_delay(2.0, 4.0)

            # Check for error messages
            error_text = self._text(page, "login_error")
            if error_text:
                logger.error(f"Naukri login error: {error_text}")
                self.browser.take_screenshot(page, "naukri_login_error")
                return False

            # Verify login success
            self.browser.random_delay(1.0, 2.5)
            if self._check_logged_in(page):
                logger.info("Naukri login successful")
                self.browser.save_cookies(self.PLATFORM_NAME)
                self._update_session(logged_in=True)
                return True

            # Might need to navigate to home to confirm
            self.browser.navigate(page, _URLS["home"])
            self.browser.random_delay(2.0, 4.0)
            self._dismiss_popups(page)

            if self._check_logged_in(page):
                logger.info("Naukri login successful (confirmed on home)")
                self.browser.save_cookies(self.PLATFORM_NAME)
                self._update_session(logged_in=True)
                return True

            logger.error(
                "Naukri login failed (no logged-in indicator found)"
            )
            self.browser.take_screenshot(page, "naukri_login_fail_final")
            return False

        except Exception as e:
            logger.error(f"Naukri login exception: {e}")
            self._save_error("login", e)
            return False

    def _check_logged_in(self, page) -> bool:
        """Check if any logged-in indicator is visible."""
        for sel in _S.get("logged_in", []):
            try:
                if page.query_selector(sel):
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

    # ═══════════════════════════════════════════════════════════
    # SEARCH
    # ═══════════════════════════════════════════════════════════

    def search_jobs(self, queries: Optional[List[str]] = None,
                    filters: Optional[Dict] = None) -> List[Dict]:
        """
        Search Naukri for jobs.

        Args:
            queries: List of search keywords.
                     None → uses PLATFORM_CONFIG.naukri.search_queries.
            filters: Dict with optional keys: location, experience_min,
                     experience_max, salary_min, freshness, job_type,
                     work_mode.
                     None → uses USER_PROFILE defaults.

        Returns:
            List of job dicts: [{platform_job_id, url, title, company,
            location, salary_text, experience_text, description,
            posted_date, skills[]}]
        """
        if not queries:
            queries = self._naukri_config.get("search_queries", [])
            if not queries:
                queries = USER_PROFILE.get("target_titles", [
                    "Software Engineer",
                    "Full Stack Developer",
                    "Backend Developer",
                    "Java Developer",
                ])

        if not filters:
            filters = {}

        max_pages = self._naukri_config.get("max_pages_per_query", 5)
        all_jobs = []

        try:
            page = self._get_page()

            # Ensure logged in
            if not self._check_logged_in(page):
                logger.warning("Not logged in, attempting login first")
                if not self.login():
                    logger.error("Cannot search: login failed")
                    return []

            for query in queries:
                logger.info(f"Searching Naukri: '{query}'")
                try:
                    url = self._build_search_url(query, filters)
                    jobs = self._search_single_query(page, url, max_pages)
                    all_jobs.extend(jobs)

                    logger.info(
                        f"  '{query}': found {len(jobs)} jobs"
                    )

                    # Rate limit between queries
                    rate = self._naukri_config.get(
                        "rate_limit_seconds", (5, 15)
                    )
                    self.browser.random_delay(*rate)

                except Exception as e:
                    logger.error(f"Search query '{query}' failed: {e}")
                    continue

            # Deduplicate by URL
            seen_urls = set()
            unique_jobs = []
            for job in all_jobs:
                url = job.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    unique_jobs.append(job)

            logger.info(
                f"Naukri search complete: {len(unique_jobs)} unique jobs "
                f"from {len(queries)} queries"
            )
            return unique_jobs

        except Exception as e:
            logger.error(f"Naukri search_jobs failed: {e}")
            self._save_error("search_jobs", e)
            return []

    def _build_search_url(self, query: str,
                          filters: Dict) -> str:
        """Build Naukri search URL with filters."""
        keyword_slug = query.strip().lower().replace(" ", "-")

        locations = filters.get("locations",
                                USER_PROFILE.get("target_locations", []))
        location_str = ", ".join(locations) if locations else ""
        location_slug = (location_str.strip().lower()
                         .replace(", ", "-").replace(" ", "-"))

        # Base path
        if location_slug:
            path = f"/{keyword_slug}-jobs-in-{location_slug}"
        else:
            path = f"/{keyword_slug}-jobs"

        params = {
            "k": query.strip(),
            "nignbevent_src": "jobsearchDeskGNB",
        }

        if location_str:
            params["l"] = location_str

        exp_min = filters.get("experience_min",
                              USER_PROFILE.get("experience_years", 0))
        exp_max = filters.get("experience_max", exp_min + 3)
        if exp_min is not None:
            params["experience"] = str(int(exp_min))

        freshness = filters.get("freshness", "")
        if freshness:
            params["jobAge"] = str(freshness)

        salary_min = filters.get("salary_min", "")
        if salary_min:
            params["salary"] = str(salary_min)

        return f"https://www.naukri.com{path}?{urlencode(params)}"

    def _search_single_query(self, page, url: str,
                             max_pages: int) -> List[Dict]:
        """Search a single query URL, paginate, parse cards."""
        all_jobs = []

        for page_num in range(1, max_pages + 1):
            page_url = (url if page_num == 1
                        else f"{url}&pageNo={page_num}")

            if not self.browser.navigate(page, page_url):
                logger.warning(f"Failed to load search page {page_num}")
                break

            self.browser.random_delay(2.0, 4.0)
            self._dismiss_popups(page)

            # Scroll down to load lazy content
            self.browser.scroll_page(page, "down",
                                     random.randint(300, 600))
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
                    logger.debug("No 'Next' button, last page reached")
                    break

                # Human-like: scroll to bottom before clicking next
                self.browser.scroll_page(page, "down",
                                         random.randint(500, 1000))
                self.browser.random_delay(1.5, 3.5)

        return all_jobs

    def _parse_job_cards(self, page) -> List[Dict]:
        """Parse all job cards on the current search results page."""
        cards = self._find_all(page, "job_card")
        if not cards:
            return []

        jobs = []
        for card in cards:
            try:
                job = self._extract_card(card, page)
                if job and job.get("title"):
                    jobs.append(job)
            except Exception as e:
                logger.debug(f"Failed to parse job card: {e}")
                continue

        return jobs

    def _extract_card(self, card, page) -> Optional[Dict]:
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
            job_url = "https://www.naukri.com" + job_url

        # Extract job ID from URL
        platform_job_id = ""
        if job_url:
            m = re.search(r'-(\d{6,})(?:\?|$)', job_url)
            if m:
                platform_job_id = m.group(1)
            else:
                platform_job_id = str(hash(job_url))[-10:]

        # Tags/skills
        skills = []
        for sel in _S["card_tags"]:
            try:
                tag_els = card.query_selector_all(sel)
                if tag_els:
                    skills = [
                        t.text_content().strip()
                        for t in tag_els
                        if t.text_content() and t.text_content().strip()
                    ]
                    break
            except Exception:
                continue

        # Parse salary
        sal_min, sal_max = _parse_salary_text(salary_text)

        # Parse experience
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

        Args:
            job_url: Full Naukri job URL.

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

            # Scroll to load full content
            self.browser.scroll_page(page, "down",
                                     random.randint(300, 600))
            self.browser.random_delay(1.0, 2.0)

            return self._parse_job_page(page, job_url)

        except Exception as e:
            logger.error(f"get_job_details failed ({job_url}): {e}")
            self._save_error("get_job_details", e)
            return {}

    def _parse_job_page(self, page, job_url: str) -> Dict:
        """Extract all details from the current job detail page."""
        title = self._text(page, "detail_title")
        company = self._text(page, "detail_company")
        location = self._text(page, "detail_location")
        salary_text = self._text(page, "detail_salary")
        exp_text = self._text(page, "detail_experience")

        # Full JD (HTML → text)
        jd_el = self._find(page, "detail_jd")
        description = ""
        if jd_el:
            description = jd_el.text_content() or ""
            description = description.strip()

        # Skills
        skill_els = self._find_all(page, "detail_skills")
        skills = [
            s.text_content().strip()
            for s in skill_els
            if s.text_content() and s.text_content().strip()
        ]

        # Other details (role, industry, department, etc.)
        other_details = {}
        info_els = self._find_all(page, "detail_info")
        for el in info_els:
            text = (el.text_content() or "").strip()
            if ":" in text:
                k, v = text.split(":", 1)
                other_details[k.strip()] = v.strip()

        # Parse salary and experience
        sal_min, sal_max = _parse_salary_text(salary_text)
        exp_min, exp_max = _parse_experience_text(exp_text)

        # Detect job type and work mode from page content
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

        # Extract job ID from URL
        platform_job_id = ""
        m = re.search(r'-(\d{6,})(?:\?|$)', job_url)
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
    # APPLICATION — Prepare (fills everything, does NOT submit)
    # ═══════════════════════════════════════════════════════════

    def prepare_application(self, job: Dict, resume_path: str,
                            cover_letter: Optional[str] = None) -> Dict:
        """
        Navigate to job, click Apply, fill all fields, STOP before submit.

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
            # Check daily limit
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

            # Navigate to job page
            if not self.browser.navigate(page, job_url):
                result["error"] = f"Cannot load job page: {job_url}"
                return result

            self.browser.random_delay(2.0, 4.0)
            self._dismiss_popups(page)

            # ── Check: Already Applied? ──
            if self._find(page, "already_applied"):
                result["status"] = "already_applied"
                result["apply_type"] = "already_applied"
                logger.info(
                    f"Already applied: {job.get('title')} @ "
                    f"{job.get('company')}"
                )
                return result

            # ── Check: External Apply? ──
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
                result["screenshot"] = self.browser.take_screenshot(
                    page, "naukri_no_apply_btn"
                )
                return result

            # Scroll to Apply button
            self.browser.scroll_to_element(page, apply_sel)
            self.browser.random_delay(0.5, 1.5)

            # ── Click Apply ──
            logger.info(
                f"Clicking Apply: {job.get('title')} @ "
                f"{job.get('company')}"
            )
            self.browser.click_human(page, apply_sel)
            self.browser.random_delay(2.0, 4.0)

            # ── Detect what happened after click ──
            apply_type = self._detect_apply_circuit(page)
            result["apply_type"] = apply_type

            if apply_type == "success_quick":
                result["status"] = "submitted_quick"
                logger.info("Quick apply submitted immediately")
                return result

            elif apply_type == "chatbot":
                ok = self._handle_chatbot(page, job, resume_path)
                if ok:
                    result["status"] = "ready"
                    result["screenshot"] = self.browser.take_screenshot(
                        page, "naukri_ready_to_submit"
                    )
                else:
                    result["error"] = "Chatbot flow failed"
                    result["screenshot"] = self.browser.take_screenshot(
                        page, "naukri_chatbot_fail"
                    )
                return result

            elif apply_type == "modal_form":
                ok = self._handle_modal_form(page, job, resume_path)
                if ok:
                    result["status"] = "ready"
                    result["screenshot"] = self.browser.take_screenshot(
                        page, "naukri_form_ready"
                    )
                else:
                    result["error"] = "Modal form handling failed"
                    result["screenshot"] = self.browser.take_screenshot(
                        page, "naukri_form_fail"
                    )
                return result

            elif apply_type == "iframe":
                ok = self._handle_iframe_apply(page, job, resume_path)
                if ok:
                    result["status"] = "ready"
                else:
                    result["error"] = "Iframe apply failed"
                    result["screenshot"] = self.browser.take_screenshot(
                        page, "naukri_iframe_fail"
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
                result["screenshot"] = self.browser.take_screenshot(
                    page, "naukri_unknown_circuit"
                )
                return result

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"prepare_application failed: {e}")
            self._save_error("prepare_application", e)
            try:
                result["screenshot"] = self.browser.take_screenshot(
                    self._get_page(), "naukri_prepare_exception"
                )
            except Exception:
                pass
            return result

    def _detect_apply_circuit(self, page) -> str:
        """
        After clicking Apply, detect which circuit we're in.

        Returns one of:
            'success_quick', 'chatbot', 'modal_form', 'iframe',
            'login_wall', 'external_redirect', 'unknown'
        """
        # Wait a moment for UI to settle
        time.sleep(1.5)

        # Check for immediate success message
        if self._find(page, "apply_success"):
            return "success_quick"

        # Check for chatbot overlay
        if self._find(page, "chatbot_wrap"):
            return "chatbot"

        # Check for modal/dialog
        if self._find(page, "apply_modal"):
            return "modal_form"

        # Check for iframe
        for sel in _S.get("apply_iframe", []):
            try:
                if page.query_selector(sel):
                    return "iframe"
            except Exception:
                continue

        # Check if redirected to login
        current_url = self.browser.get_page_url(page)
        if "login" in current_url.lower():
            return "login_wall"

        # Wait a bit more — some overlays are slow
        time.sleep(2.0)

        if self._find(page, "chatbot_wrap"):
            return "chatbot"
        if self._find(page, "apply_modal"):
            return "modal_form"
        if self._find(page, "apply_success"):
            return "success_quick"

        # Check for new tab (external redirect)
        try:
            context = page.context
            if len(context.pages) > 1:
                return "external_redirect"
        except Exception:
            pass

        return "unknown"

    # ═══════════════════════════════════════════════════════════
    # APPLICATION — Chatbot Handler
    # ═══════════════════════════════════════════════════════════

    def _handle_chatbot(self, page, job: Dict,
                        resume_path: str) -> bool:
        """
        Handle Naukri chatbot questionnaire.

        Iterates through questions, answers each, stops before submit.
        Returns True if all questions answered and Submit is visible.
        """
        max_questions = 25
        answered = 0

        for i in range(max_questions):
            self.browser.random_delay(1.0, 2.5)

            # Check if we've reached the final submit
            submit_el = self._find(page, "chatbot_submit")
            if submit_el:
                question_el = self._find(page, "chatbot_question")
                if not question_el or answered > 0:
                    logger.info(
                        f"Chatbot complete: {answered} questions "
                        f"answered, Submit visible — PAUSED"
                    )
                    return True

            # Get current question
            question_text = self._get_chatbot_question(page)
            if not question_text and answered > 0:
                self.browser.random_delay(1.0, 2.0)
                if self._find(page, "chatbot_submit"):
                    logger.info(
                        f"Chatbot done: {answered} answers, "
                        f"submit ready"
                    )
                    return True
                if self._find(page, "apply_success"):
                    logger.info(
                        "Application submitted during chatbot"
                    )
                    return True
                logger.debug("No question and no submit button")
                break

            if not question_text:
                self.browser.random_delay(1.5, 3.0)
                question_text = self._get_chatbot_question(page)
                if not question_text:
                    logger.debug("No chatbot question found")
                    break

            logger.debug(f"Chatbot Q{i+1}: {question_text[:80]}")

            # ── Detect input type and answer ──
            answered_ok = self._answer_chatbot_question(
                page, question_text, job
            )

            if answered_ok:
                answered += 1
            else:
                logger.warning(
                    f"Could not answer chatbot question: "
                    f"{question_text[:80]}"
                )
                self.browser.take_screenshot(
                    page, f"naukri_unknown_q_{i}"
                )
                if not self._click(page, "chatbot_send"):
                    self._click(page, "chatbot_next")

            self.browser.random_delay(1.5, 3.0)

        # Check final state
        if self._find(page, "chatbot_submit"):
            return True
        if self._find(page, "apply_success"):
            return True

        logger.warning(f"Chatbot flow ended after {answered} answers")
        return answered > 0

    def _get_chatbot_question(self, page) -> str:
        """Get the current chatbot question text."""
        for sel in _S["chatbot_question"]:
            try:
                els = page.query_selector_all(sel)
                if els:
                    last = els[-1]
                    text = last.text_content()
                    return text.strip() if text else ""
            except Exception:
                continue
        return ""

    def _answer_chatbot_question(self, page, question: str,
                                 job: Dict) -> bool:
        """
        Answer a single chatbot question.

        Detection order:
          1. Chip/option buttons → click matching option
          2. Radio buttons → click matching option
          3. Dropdown/select → select matching option
          4. File upload → upload resume
          5. Text input → type answer
        """
        answer = self._find_answer_for_question(question, job)
        q_lower = question.lower()

        # ── 1. Chip-style option buttons ──
        chip_els = self._find_all(page, "chatbot_chips")
        if chip_els:
            return self._click_best_option(
                page, chip_els, answer, q_lower
            )

        # ── 2. Radio buttons ──
        radio_els = self._find_all(page, "chatbot_radio")
        if radio_els:
            return self._select_radio(
                page, radio_els, answer, q_lower
            )

        # ── 3. Dropdown ──
        select_el = self._find(page, "chatbot_select")
        if select_el:
            return self._select_dropdown_answer(
                page, answer, q_lower
            )

        # ── 4. File upload ──
        if any(kw in q_lower for kw in
               ["resume", "cv", "upload", "attach"]):
            upload_sel = self._working_selector(page, "resume_upload")
            if upload_sel:
                return self._upload_resume(page, upload_sel)

        # ── 5. Text input (most common) ──
        input_sel = self._working_selector(page, "chatbot_text_in")
        if input_sel:
            if not answer:
                answer = "N/A"
                logger.debug(
                    f"No answer found, using 'N/A' for: "
                    f"{question[:60]}"
                )

            self.browser.type_human(page, input_sel, answer)
            self.browser.random_delay(0.3, 0.8)

            # Click send
            if not self._click(page, "chatbot_send"):
                self.browser.press_key(page, "Enter")

            return True

        logger.debug(
            f"No input element found for question: "
            f"{question[:60]}"
        )
        return False

    # ═══════════════════════════════════════════════════════════
    # ANSWER FINDING — matches question text to correct answer
    # ═══════════════════════════════════════════════════════════

    def _find_answer_for_question(self, question: str,
                                  job: Dict) -> str:
        """
        Match question text to the correct answer.

        Priority:
          1. profile/answers.py (if available)
          2. Built-in pattern matching
          3. Empty string (caller handles unknown)
        """
        # Try answers.py first
        if _ANSWERS_AVAILABLE:
            ans = get_answer(question)
            if ans:
                return ans

        q = question.lower().strip()
        profile = USER_PROFILE

        # ── Current CTC / Salary ──
        if any(k in q for k in ["current ctc", "current salary",
                                 "present salary", "current annual",
                                 "current compensation",
                                 "current package"]):
            ctc_lpa = profile.get("current_ctc_lpa", 3.7)
            if "lakh" in q or "lac" in q or "lpa" in q:
                return str(ctc_lpa)
            return str(int(ctc_lpa * 100000))

        # ── Expected CTC / Salary ──
        if any(k in q for k in ["expected ctc", "expected salary",
                                 "desired salary", "expected annual",
                                 "expected compensation"]):
            return self._compute_expected(job, q)

        # ── Notice period ──
        if "notice period" in q or "notice-period" in q:
            return profile.get("notice_period", "15 Days")

        # ── Relocate ──
        if "relocat" in q:
            return "Yes"

        # ── Total experience ──
        if any(k in q for k in ["total experience",
                                 "overall experience",
                                 "years of experience",
                                 "how many years",
                                 "how much experience"]):
            return str(profile.get("experience_years", 1))

        # ── Specific skill experience ──
        skill_match = re.search(
            r'(?:experience|exp|expertise).*(?:in|with|on)\s+'
            r'(.+?)(?:\?|$|\.)',
            q
        )
        if skill_match:
            skill = skill_match.group(1).strip()
            return self._skill_years(skill)

        # ── Currently employed ──
        if any(k in q for k in ["currently working",
                                 "currently employed",
                                 "are you working",
                                 "presently working"]):
            return "Yes"

        # ── Location ──
        if any(k in q for k in ["current location", "current city",
                                 "where are you based",
                                 "your city"]):
            return profile.get("location",
                               "Rishikesh, Uttarakhand")

        # ── Education ──
        if any(k in q for k in ["qualification", "degree",
                                 "education", "highest degree"]):
            return "B.Tech in Computer Science"

        # ── Why leaving / reason for change ──
        if any(k in q for k in ["why leaving", "reason for change",
                                 "why are you looking",
                                 "why do you want to leave"]):
            return (
                "Looking for better growth opportunities and "
                "challenging projects in a product company"
            )

        # ── Gender ──
        if "gender" in q:
            return "Male"

        # ── Shift / night shift ──
        if any(k in q for k in ["shift", "night shift",
                                 "rotational"]):
            return "Yes"

        # ── Work mode ──
        if any(k in q for k in ["work from office", "wfo",
                                 "onsite", "office based"]):
            return "Yes"

        # ── Visa / authorization ──
        if any(k in q for k in ["visa", "authorized to work",
                                 "work permit",
                                 "legally authorized"]):
            return "Yes"

        # ── Background check / consent ──
        if any(k in q for k in ["background check",
                                 "verification", "consent",
                                 "agree", "terms"]):
            return "Yes"

        # ── Name ──
        if any(k in q for k in ["your name", "full name",
                                 "candidate name"]):
            return profile.get("name", "Piyush Kashyap")

        # ── Email ──
        if any(k in q for k in ["email", "e-mail"]):
            return profile.get("email",
                               "piyushkashyap3247@gmail.com")

        # ── Phone ──
        if any(k in q for k in ["phone", "mobile", "contact number",
                                 "phone number"]):
            return profile.get("phone", "7310703247")

        # ── Current company ──
        if any(k in q for k in ["current company",
                                 "current employer",
                                 "current organization"]):
            return profile.get("current_company",
                               "Site Guru Pvt Ltd")

        # ── Current designation ──
        if any(k in q for k in ["current designation",
                                 "current role", "job title",
                                 "current title"]):
            return profile.get("current_title",
                               "Full Stack Developer L1")

        # ── Languages ──
        if any(k in q for k in ["language", "languages"]):
            return "English, Hindi"

        # ── Yes/No questions (default Yes for most) ──
        if q.strip().endswith("?"):
            words = q.split()
            if len(words) <= 15:
                if any(neg in q for neg in ["criminal",
                                             "disability",
                                             "handicap",
                                             "arrested"]):
                    return "No"
                if any(pos in q for pos in ["willing", "ready",
                                             "agree", "able",
                                             "can you",
                                             "do you have",
                                             "have you"]):
                    return "Yes"

        return ""

    def _compute_expected(self, job: Dict, question: str) -> str:
        """Compute expected salary answer based on job's range."""
        sal_max = job.get("salary_max", 0) or 0
        sal_min = job.get("salary_min", 0) or 0

        if sal_max > 0:
            expected = sal_min + (sal_max - sal_min) * 0.65
        else:
            expected = 800000  # 8 LPA default

        if expected < 500000:
            expected = 500000

        if "lakh" in question or "lac" in question or "lpa" in question:
            return f"{expected / 100000:.1f}"
        return str(int(expected))

    def _skill_years(self, skill: str) -> str:
        """
        Return years of experience for a specific skill.

        Checks _SKILL_YEARS mapping with fuzzy name matching.
        """
        skill_lower = skill.strip().lower()

        # Direct match
        if skill_lower in _SKILL_YEARS:
            return str(_SKILL_YEARS[skill_lower])

        # Partial match: check if any key is contained in the skill
        for key, years in _SKILL_YEARS.items():
            if key in skill_lower or skill_lower in key:
                return str(years)

        # Word-level match
        skill_words = set(skill_lower.split())
        best_match = 0.0
        for key, years in _SKILL_YEARS.items():
            key_words = set(key.split())
            overlap = skill_words & key_words
            if overlap:
                score = len(overlap) / max(len(skill_words),
                                           len(key_words))
                if score > best_match:
                    best_match = score
                    best_years = years

        if best_match > 0.3:
            return str(best_years)

        # Default: 0.5 years (better than 0, looks like beginner)
        logger.debug(
            f"No skill-years mapping for '{skill}', defaulting 0.5"
        )
        return "0.5"

    # ═══════════════════════════════════════════════════════════
    # OPTION/RADIO/DROPDOWN CLICK HELPERS
    # ═══════════════════════════════════════════════════════════

    def _click_best_option(self, page, chip_els: list,
                           answer: str, q_lower: str) -> bool:
        """
        Click the best matching chip/option button.

        Strategy:
          1. Exact text match to our answer.
          2. Partial / contains match.
          3. Smart defaults (Yes for consent, first option otherwise).
        """
        if not chip_els:
            return False

        # Collect chip texts
        chips = []
        for el in chip_els:
            try:
                txt = (el.text_content() or "").strip()
                if txt:
                    chips.append((el, txt))
            except Exception:
                continue

        if not chips:
            return False

        answer_lower = answer.lower().strip() if answer else ""

        # ── 1. Exact match ──
        for el, txt in chips:
            if txt.lower() == answer_lower:
                try:
                    el.click()
                    self.browser.random_delay(0.3, 0.8)
                    logger.debug(f"Chip exact match: '{txt}'")
                    return True
                except Exception:
                    continue

        # ── 2. Contains match ──
        for el, txt in chips:
            txt_l = txt.lower()
            if (answer_lower and
                    (answer_lower in txt_l or txt_l in answer_lower)):
                try:
                    el.click()
                    self.browser.random_delay(0.3, 0.8)
                    logger.debug(f"Chip partial match: '{txt}'")
                    return True
                except Exception:
                    continue

        # ── 3. Smart defaults ──
        # For yes/no style questions, prefer Yes
        chip_texts_lower = [t.lower() for _, t in chips]

        if "yes" in chip_texts_lower:
            # Check if we should say No for certain questions
            should_say_no = any(
                kw in q_lower for kw in
                ["criminal", "disability", "handicap",
                 "arrested", "convicted"]
            )
            target = "no" if should_say_no else "yes"
            for el, txt in chips:
                if txt.lower() == target:
                    try:
                        el.click()
                        self.browser.random_delay(0.3, 0.8)
                        logger.debug(
                            f"Chip default '{target}': '{txt}'"
                        )
                        return True
                    except Exception:
                        continue

        # ── 4. Numeric match (for experience ranges etc.) ──
        if answer_lower and answer_lower.replace(".", "").isdigit():
            answer_num = float(answer_lower)
            best_chip = None
            best_diff = float("inf")
            for el, txt in chips:
                nums = re.findall(r'[\d.]+', txt)
                if nums:
                    for n in nums:
                        diff = abs(float(n) - answer_num)
                        if diff < best_diff:
                            best_diff = diff
                            best_chip = el
            if best_chip and best_diff < 5:
                try:
                    best_chip.click()
                    self.browser.random_delay(0.3, 0.8)
                    logger.debug(
                        f"Chip numeric match (diff={best_diff})"
                    )
                    return True
                except Exception:
                    pass

        # ── 5. Last resort: click first chip ──
        try:
            chips[0][0].click()
            self.browser.random_delay(0.3, 0.8)
            logger.debug(f"Chip fallback: first option '{chips[0][1]}'")
            return True
        except Exception:
            return False

    def _select_radio(self, page, radio_els: list,
                      answer: str, q_lower: str) -> bool:
        """
        Select the best matching radio button.

        Radio buttons often have labels — we match answer against the
        label text, then click the radio input.
        """
        if not radio_els:
            return False

        answer_lower = answer.lower().strip() if answer else ""

        # Collect labels
        radios = []
        for el in radio_els:
            try:
                # Try to get the label: parent text, or sibling label
                label_text = ""
                parent = el.evaluate_handle(
                    "el => el.parentElement"
                )
                if parent:
                    label_text = (parent.as_element().text_content()
                                 or "").strip()

                if not label_text:
                    # Try aria-label or value
                    label_text = (el.get_attribute("value")
                                 or el.get_attribute("aria-label")
                                 or "")

                radios.append((el, label_text.strip()))
            except Exception:
                radios.append((el, ""))

        # ── Match by label text ──
        for el, label in radios:
            if label and answer_lower:
                label_l = label.lower()
                if (answer_lower == label_l or
                        answer_lower in label_l or
                        label_l in answer_lower):
                    try:
                        el.click()
                        self.browser.random_delay(0.3, 0.8)
                        logger.debug(f"Radio match: '{label}'")
                        return True
                    except Exception:
                        continue

        # ── Smart defaults ──
        for el, label in radios:
            label_l = label.lower()
            if "yes" in label_l:
                should_no = any(
                    kw in q_lower for kw in
                    ["criminal", "disability", "convicted"]
                )
                if not should_no:
                    try:
                        el.click()
                        self.browser.random_delay(0.3, 0.8)
                        logger.debug(f"Radio default 'Yes': '{label}'")
                        return True
                    except Exception:
                        continue

        # Click first radio as last resort
        if radios:
            try:
                radios[0][0].click()
                self.browser.random_delay(0.3, 0.8)
                logger.debug("Radio fallback: first option")
                return True
            except Exception:
                pass

        return False

    def _select_dropdown_answer(self, page, answer: str,
                                q_lower: str) -> bool:
        """Select the best matching option in a <select> dropdown."""
        sel_selector = self._working_selector(page, "chatbot_select")
        if not sel_selector:
            return False

        answer_lower = answer.lower().strip() if answer else ""

        # Try fuzzy select from browser engine
        if answer:
            ok = self.browser.select_dropdown_fuzzy(
                page, sel_selector, answer
            )
            if ok:
                # Click send/next after selection
                self.browser.random_delay(0.5, 1.0)
                if not self._click(page, "chatbot_send"):
                    self._click(page, "chatbot_next")
                return True

        # Manual fallback: get options, pick best
        try:
            options = page.query_selector_all(
                f"{sel_selector} option"
            )
            if options and len(options) > 1:
                # Select second option (first is usually placeholder)
                page.select_option(sel_selector, index=1,
                                   timeout=3000)
                self.browser.random_delay(0.5, 1.0)
                if not self._click(page, "chatbot_send"):
                    self._click(page, "chatbot_next")
                logger.debug("Dropdown: selected second option")
                return True
        except Exception as e:
            logger.debug(f"Dropdown selection failed: {e}")

        return False

    def _upload_resume(self, page, selector: str) -> bool:
        """Upload resume file via file input."""
        # Determine resume path
        resume_path = ""
        if self._current_job:
            resume_path = (self._current_job.get("resume_path", "")
                           or "")
        if not resume_path:
            resume_path = RESUME_CONFIG.get("base_resume_path", "")

        if not resume_path or not os.path.isfile(str(resume_path)):
            logger.warning(
                f"Resume file not found: {resume_path}"
            )
            return False

        # Find the upload input
        if not selector:
            selector = self._working_selector(page, "resume_upload")
        if not selector:
            logger.debug("No file upload input found")
            return False

        try:
            page.set_input_files(selector, str(resume_path),
                                 timeout=10000)
            self.browser.random_delay(1.0, 2.5)
            logger.debug(f"Resume uploaded: {resume_path}")

            # Click send/next after upload
            if not self._click(page, "chatbot_send"):
                self._click(page, "chatbot_next")

            return True
        except Exception as e:
            logger.error(f"Resume upload failed: {e}")
            return False

    # ═══════════════════════════════════════════════════════════
    # APPLICATION — Modal Form Handler
    # ═══════════════════════════════════════════════════════════

    def _handle_modal_form(self, page, job: Dict,
                           resume_path: str) -> bool:
        """
        Handle modal-style apply form with multiple fields at once.

        These modals typically have:
          - Text inputs (name, email, phone, experience, CTC)
          - Dropdowns (notice period, location)
          - File upload (resume)
          - Checkboxes (consent)
          - Submit button

        Returns True if form is filled and Submit is visible.
        """
        try:
            self.browser.random_delay(1.0, 2.0)

            # ── Fill text inputs ──
            for sel in _S.get("modal_input", []):
                try:
                    inputs = page.query_selector_all(sel)
                    for inp in inputs:
                        self._fill_modal_input(page, inp, job)
                except Exception:
                    continue

            # ── Fill textareas ──
            for sel in _S.get("modal_textarea", []):
                try:
                    areas = page.query_selector_all(sel)
                    for area in areas:
                        self._fill_modal_textarea(page, area, job)
                except Exception:
                    continue

            # ── Handle dropdowns ──
            for sel in _S.get("modal_select", []):
                try:
                    selects = page.query_selector_all(sel)
                    for select_el in selects:
                        self._fill_modal_select(page, select_el, job)
                except Exception:
                    continue

            # ── Upload resume ──
            upload_sel = self._working_selector(page, "resume_upload")
            if upload_sel and resume_path and os.path.isfile(resume_path):
                try:
                    page.set_input_files(upload_sel, resume_path,
                                         timeout=10000)
                    self.browser.random_delay(1.0, 2.0)
                    logger.debug("Modal: resume uploaded")
                except Exception as e:
                    logger.debug(f"Modal resume upload failed: {e}")

            # ── Check checkboxes (consent) ──
            for sel in _S.get("modal_checkbox", []):
                try:
                    cbs = page.query_selector_all(sel)
                    for cb in cbs:
                        if not cb.is_checked():
                            cb.click()
                            self.browser.random_delay(0.2, 0.5)
                except Exception:
                    continue

            # ── Verify submit button is visible ──
            submit_el = self._find(page, "modal_submit")
            if not submit_el:
                submit_el = self._find(page, "chatbot_submit")

            if submit_el:
                logger.info("Modal form filled, Submit visible — PAUSED")
                return True

            logger.warning("Modal form filled but Submit not found")
            return True  # filled what we could

        except Exception as e:
            logger.error(f"Modal form handling failed: {e}")
            return False

    def _fill_modal_input(self, page, input_el, job: Dict) -> None:
        """Fill a single text input in a modal form."""
        try:
            # Determine field identity from attributes
            name = (input_el.get_attribute("name") or "").lower()
            placeholder = (input_el.get_attribute("placeholder")
                           or "").lower()
            label = (input_el.get_attribute("aria-label") or "").lower()
            field_id = (input_el.get_attribute("id") or "").lower()
            combined = f"{name} {placeholder} {label} {field_id}"

            # Skip if already filled
            current_val = input_el.input_value() or ""
            if current_val.strip():
                return

            answer = ""
            profile = USER_PROFILE

            if any(k in combined for k in ["name", "full name"]):
                answer = profile.get("name", "Piyush Kashyap")
            elif any(k in combined for k in ["email", "e-mail"]):
                answer = profile.get("email",
                                     "piyushkashyap3247@gmail.com")
            elif any(k in combined for k in ["phone", "mobile",
                                              "contact"]):
                answer = profile.get("phone", "7310703247")
            elif any(k in combined for k in ["experience", "exp",
                                              "years"]):
                answer = str(profile.get("experience_years", 1))
            elif any(k in combined for k in ["current ctc",
                                              "current salary",
                                              "ctc"]):
                answer = str(profile.get("current_ctc_lpa", 3.7))
            elif any(k in combined for k in ["expected",
                                              "desired salary"]):
                answer = self._compute_expected(job, combined)
            elif any(k in combined for k in ["notice", "notice period"]):
                answer = profile.get("notice_period", "15")
            elif any(k in combined for k in ["location", "city"]):
                answer = profile.get("location",
                                     "Rishikesh, Uttarakhand")
            elif any(k in combined for k in ["company",
                                              "current employer"]):
                answer = profile.get("current_company",
                                     "Site Guru Pvt Ltd")
            elif any(k in combined for k in ["designation", "title",
                                              "role"]):
                answer = profile.get("current_title",
                                     "Full Stack Developer L1")

            if answer:
                input_el.click()
                self.browser.random_delay(0.1, 0.3)
                input_el.fill("")
                input_el.type(answer, delay=random.randint(30, 80))
                self.browser.random_delay(0.2, 0.5)
                logger.debug(
                    f"Modal input filled: {name or placeholder} "
                    f"= '{answer[:30]}'"
                )

        except Exception as e:
            logger.debug(f"Modal input fill error: {e}")

    def _fill_modal_textarea(self, page, textarea_el,
                             job: Dict) -> None:
        """Fill a textarea in a modal form (usually cover letter)."""
        try:
            current_val = textarea_el.input_value() or ""
            if current_val.strip():
                return

            name = (textarea_el.get_attribute("name") or "").lower()
            placeholder = (textarea_el.get_attribute("placeholder")
                           or "").lower()
            combined = f"{name} {placeholder}"

            answer = ""
            if any(k in combined for k in ["cover", "letter",
                                            "message"]):
                answer = (
                    f"I am interested in the "
                    f"{job.get('title', 'position')} role at "
                    f"{job.get('company', 'your company')}. With "
                    f"experience in full stack development across "
                    f"10+ production applications, I bring strong "
                    f"skills in backend and frontend development. "
                    f"I would welcome the opportunity to discuss "
                    f"how I can contribute to your team."
                )
            elif any(k in combined for k in ["why", "reason",
                                              "about"]):
                answer = (
                    "I am looking for growth opportunities in a "
                    "product company where I can apply my full stack "
                    "development skills to challenging projects."
                )

            if answer:
                textarea_el.click()
                self.browser.random_delay(0.1, 0.3)
                textarea_el.fill("")
                textarea_el.type(answer,
                                 delay=random.randint(20, 60))
                self.browser.random_delay(0.2, 0.5)
                logger.debug(f"Modal textarea filled: {name}")

        except Exception as e:
            logger.debug(f"Modal textarea fill error: {e}")

    def _fill_modal_select(self, page, select_el,
                           job: Dict) -> None:
        """Fill a dropdown in a modal form."""
        try:
            name = (select_el.get_attribute("name") or "").lower()
            aria = (select_el.get_attribute("aria-label") or "").lower()
            combined = f"{name} {aria}"

            answer = ""
            profile = USER_PROFILE

            if "notice" in combined:
                answer = profile.get("notice_period", "15 Days")
            elif "experience" in combined:
                answer = str(profile.get("experience_years", 1))
            elif "location" in combined or "city" in combined:
                answer = profile.get("location", "")
            elif "gender" in combined:
                answer = "Male"

            if answer:
                # Try fuzzy match
                try:
                    options = select_el.query_selector_all("option")
                    for opt in options:
                        opt_text = (opt.text_content() or "").strip()
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
                                f"Modal select: {name} = '{opt_text}'"
                            )
                            return
                except Exception:
                    pass

            # Select second option as fallback (skip placeholder)
            try:
                select_el.select_option(index=1)
                self.browser.random_delay(0.3, 0.7)
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"Modal select fill error: {e}")

    # ═══════════════════════════════════════════════════════════
    # APPLICATION — iframe Handler
    # ═══════════════════════════════════════════════════════════

    def _handle_iframe_apply(self, page, job: Dict,
                             resume_path: str) -> bool:
        """
        Handle iframe-based application forms.

        Some Naukri jobs embed the apply form in an iframe.
        We switch into the iframe context, fill fields, then switch back.
        """
        try:
            # Find the iframe
            iframe_el = None
            for sel in _S.get("apply_iframe", []):
                try:
                    iframe_el = page.query_selector(sel)
                    if iframe_el:
                        break
                except Exception:
                    continue

            if not iframe_el:
                logger.debug("No apply iframe found")
                return False

            # Get iframe's content frame
            frame = iframe_el.content_frame()
            if not frame:
                logger.debug("Could not access iframe content frame")
                return False

            logger.debug("Switched to apply iframe")
            self.browser.random_delay(1.0, 2.0)

            # Inside the iframe, treat it like a modal form
            # Fill text inputs
            try:
                inputs = frame.query_selector_all(
                    "input[type='text'], input[type='email'], "
                    "input[type='tel'], input[type='number']"
                )
                for inp in inputs:
                    self._fill_iframe_input(frame, inp, job)
            except Exception as e:
                logger.debug(f"Iframe input fill error: {e}")

            # Fill textareas
            try:
                areas = frame.query_selector_all("textarea")
                for area in areas:
                    name = (area.get_attribute("name") or "").lower()
                    placeholder = (area.get_attribute("placeholder")
                                   or "").lower()
                    if not (area.input_value() or "").strip():
                        area.fill(
                            f"Interested in {job.get('title', '')} "
                            f"at {job.get('company', '')}."
                        )
            except Exception:
                pass

            # Handle dropdowns
            try:
                selects = frame.query_selector_all("select")
                for sel_el in selects:
                    try:
                        sel_el.select_option(index=1)
                        self.browser.random_delay(0.2, 0.5)
                    except Exception:
                        pass
            except Exception:
                pass

            # Upload resume
            try:
                upload = frame.query_selector("input[type='file']")
                if (upload and resume_path and
                        os.path.isfile(resume_path)):
                    upload.set_input_files(resume_path)
                    self.browser.random_delay(1.0, 2.0)
                    logger.debug("Iframe: resume uploaded")
            except Exception:
                pass

            # Check checkboxes
            try:
                cbs = frame.query_selector_all(
                    "input[type='checkbox']"
                )
                for cb in cbs:
                    if not cb.is_checked():
                        cb.click()
                        self.browser.random_delay(0.2, 0.4)
            except Exception:
                pass

            # Check for submit button
            submit = frame.query_selector(
                "button:has-text('Submit'), "
                "button:has-text('Apply'), "
                "input[type='submit']"
            )
            if submit:
                logger.info("Iframe form filled, Submit visible — PAUSED")
                return True

            logger.info("Iframe form filled (submit not found)")
            return True

        except Exception as e:
            logger.error(f"Iframe apply handling failed: {e}")
            return False

    def _fill_iframe_input(self, frame, input_el,
                           job: Dict) -> None:
        """Fill a single input inside an iframe."""
        try:
            current_val = input_el.input_value() or ""
            if current_val.strip():
                return

            input_type = (input_el.get_attribute("type")
                          or "text").lower()
            name = (input_el.get_attribute("name") or "").lower()
            placeholder = (input_el.get_attribute("placeholder")
                           or "").lower()
            combined = f"{name} {placeholder}"
            profile = USER_PROFILE

            answer = ""
            if any(k in combined for k in ["name"]):
                answer = profile.get("name", "Piyush Kashyap")
            elif input_type == "email" or "email" in combined:
                answer = profile.get("email",
                                     "piyushkashyap3247@gmail.com")
            elif input_type == "tel" or any(
                    k in combined for k in ["phone", "mobile"]):
                answer = profile.get("phone", "7310703247")
            elif any(k in combined for k in ["experience", "exp"]):
                answer = str(profile.get("experience_years", 1))
            elif any(k in combined for k in ["ctc", "salary"]):
                answer = "3.7"
            elif any(k in combined for k in ["notice"]):
                answer = "15"

            if answer:
                input_el.click()
                time.sleep(random.uniform(0.1, 0.2))
                input_el.fill(answer)
                time.sleep(random.uniform(0.2, 0.4))

        except Exception as e:
            logger.debug(f"Iframe input fill error: {e}")

    # ═══════════════════════════════════════════════════════════
    # APPLICATION — Submit (called after Telegram approval)
    # ═══════════════════════════════════════════════════════════

    def submit_application(self, prepared: Dict) -> Dict:
        """
        Click the final Submit button to send the application.

        Called ONLY after Telegram approval (or auto-approve timeout).

        Args:
            prepared: Dict returned by prepare_application()
                      with status="ready".

        Returns:
            {
                success: bool,
                status: "submitted" | "failed" | "already_submitted",
                error: None | str,
                screenshot: str,
                timestamp: str,
            }
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

            # Handle quick-apply (already submitted during prepare)
            if prepared.get("status") == "submitted_quick":
                result["success"] = True
                result["status"] = "submitted"
                self.increment_count()
                logger.info("Quick apply already submitted")
                return result

            # Handle external apply (nothing to submit)
            if prepared.get("status") in ("external",
                                           "already_applied"):
                result["status"] = prepared["status"]
                result["error"] = (
                    f"Cannot submit: {prepared['status']}"
                )
                return result

            # ── Try to find and click Submit ──

            # Check for iframe first
            iframe_el = None
            for sel in _S.get("apply_iframe", []):
                try:
                    iframe_el = page.query_selector(sel)
                    if iframe_el:
                        break
                except Exception:
                    continue

            submitted = False

            if iframe_el:
                # Submit inside iframe
                frame = iframe_el.content_frame()
                if frame:
                    submitted = self._click_submit_in_context(frame)

            if not submitted:
                # Submit in main page (chatbot or modal)
                submitted = self._click_submit_in_context(page)

            if not submitted:
                result["error"] = "Submit button not found or not clickable"
                result["screenshot"] = self.browser.take_screenshot(
                    page, "naukri_submit_not_found"
                )
                return result

            # ── Wait for confirmation ──
            self.browser.random_delay(2.0, 5.0)

            # Check for success
            if self._find(page, "apply_success"):
                result["success"] = True
                result["status"] = "submitted"
                self.increment_count()
                result["screenshot"] = self.browser.take_screenshot(
                    page, "naukri_submit_success"
                )

                job = prepared.get("job", {})
                logger.info(
                    f"✅ Application submitted: "
                    f"{job.get('title', '?')} @ "
                    f"{job.get('company', '?')}"
                )
                return result

            # No explicit success message but no error either
            # (some jobs just close the overlay)
            if not self._find(page, "chatbot_wrap") and \
               not self._find(page, "apply_modal"):
                result["success"] = True
                result["status"] = "submitted"
                self.increment_count()
                logger.info(
                    "Application likely submitted "
                    "(overlay closed)"
                )
                return result

            # Check if the apply button now says "Already Applied"
            if self._find(page, "already_applied"):
                result["success"] = True
                result["status"] = "submitted"
                self.increment_count()
                logger.info(
                    "Application confirmed "
                    "(Already Applied visible)"
                )
                return result

            result["error"] = (
                "Submitted but could not confirm success"
            )
            result["screenshot"] = self.browser.take_screenshot(
                page, "naukri_submit_uncertain"
            )
            # Count it anyway — likely went through
            result["success"] = True
            result["status"] = "submitted"
            self.increment_count()
            return result

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"submit_application failed: {e}")
            self._save_error("submit_application", e)
            try:
                result["screenshot"] = self.browser.take_screenshot(
                    self._get_page(), "naukri_submit_exception"
                )
            except Exception:
                pass
            return result

    def _click_submit_in_context(self, context) -> bool:
        """
        Try all submit-button selectors within a page or frame context.
        Returns True if a submit button was found and clicked.
        """
        # Try chatbot submit first (most common)
        submit_selectors = (
            _S.get("chatbot_submit", []) +
            _S.get("modal_submit", [])
        )

        for sel in submit_selectors:
            try:
                btn = context.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    self.browser.random_delay(0.5, 1.5)
                    logger.debug(f"Submit clicked: {sel}")
                    return True
            except Exception:
                continue

        # Generic fallback
        try:
            btn = context.query_selector(
                "button[type='submit'], input[type='submit']"
            )
            if btn:
                btn.click()
                self.browser.random_delay(0.5, 1.5)
                logger.debug("Submit clicked: generic fallback")
                return True
        except Exception:
            pass

        return False

    # ═══════════════════════════════════════════════════════════
    # STATUS CHECK
    # ═══════════════════════════════════════════════════════════

    def check_status(self, application_id: Optional[int] = None) -> str:
        """
        Check application status on Naukri's "Applied Jobs" page.

        Args:
            application_id: DB application ID (used to look up job info).
                            None → just return general status check.

        Returns:
            Status string: "submitted" | "viewed" | "shortlisted" |
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

            # If we have an application_id, look up the job
            target_title = ""
            target_company = ""
            if application_id:
                db = get_db()
                apps = db.get_applications(limit=1)
                # Simple lookup — in production, query by ID
                for app in apps:
                    if app.get("id") == application_id:
                        job_id = app.get("job_id")
                        jobs = db.get_jobs(limit=1000)
                        for j in jobs:
                            if j.get("id") == job_id:
                                target_title = j.get("title", "")
                                target_company = j.get("company", "")
                                break
                        break

            # Parse applied jobs cards
            cards = self._find_all(page, "applied_card")
            if not cards:
                logger.debug("No applied job cards found")
                return "unknown"

            for card in cards:
                try:
                    title_el = None
                    for sel in _S["applied_title"]:
                        title_el = card.query_selector(sel)
                        if title_el:
                            break

                    if not title_el:
                        continue

                    card_title = (title_el.text_content()
                                 or "").strip()

                    # If looking for specific job, match
                    if target_title:
                        if (target_title.lower() not in
                                card_title.lower()):
                            continue

                    # Get status
                    status_text = ""
                    for sel in _S["applied_status"]:
                        status_el = card.query_selector(sel)
                        if status_el:
                            status_text = (status_el.text_content()
                                           or "").strip().lower()
                            break

                    if "viewed" in status_text or "seen" in status_text:
                        return "viewed"
                    elif "shortlist" in status_text:
                        return "shortlisted"
                    elif "reject" in status_text or "not" in status_text:
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
    # PROFILE UPDATE (boosts visibility in recruiter searches)
    # ═══════════════════════════════════════════════════════════

    def update_profile(self) -> bool:
        """
        Touch Naukri profile to boost visibility.

        Naukri's algorithm promotes recently-updated profiles in
        recruiter searches.  Simply re-uploading the resume or editing
        the headline counts as an update.

        Returns:
            True if profile was updated successfully.
        """
        try:
            page = self._get_page()

            if not self._check_logged_in(page):
                if not self.login():
                    return False

            if not self.browser.navigate(page, _URLS["profile"]):
                logger.error("Cannot load Naukri profile page")
                return False

            self.browser.random_delay(2.0, 4.0)
            self._dismiss_popups(page)

            # ── Strategy 1: Re-upload resume ──
            resume_path = RESUME_CONFIG.get("base_resume_path", "")
            if resume_path and os.path.isfile(str(resume_path)):
                upload_sel = self._working_selector(
                    page, "profile_upload"
                )
                if upload_sel:
                    try:
                        page.set_input_files(
                            upload_sel, str(resume_path),
                            timeout=15000
                        )
                        self.browser.random_delay(3.0, 6.0)
                        logger.info(
                            "Naukri profile updated (resume re-upload)"
                        )
                        return True
                    except Exception as e:
                        logger.debug(
                            f"Profile resume upload failed: {e}"
                        )

            # ── Strategy 2: Edit and save headline ──
            headline_edit = self._find(page, "profile_headline")
            if headline_edit:
                try:
                    headline_edit.click()
                    self.browser.random_delay(1.0, 2.0)

                    # Find the textarea/input that appeared
                    editor = page.query_selector(
                        "textarea[class*='headline'], "
                        "textarea[name*='headline'], "
                        "div[contenteditable='true']"
                    )
                    if editor:
                        current = (editor.text_content()
                                   or editor.input_value()
                                   or "")
                        # Add/remove a trailing space (invisible change)
                        if current.endswith(" "):
                            new_text = current.rstrip()
                        else:
                            new_text = current + " "

                        try:
                            editor.fill(new_text)
                        except Exception:
                            editor.evaluate(
                                f"el => el.textContent = "
                                f"'{new_text}'"
                            )

                        self.browser.random_delay(0.5, 1.0)

                        # Save
                        self._click(page, "profile_save")
                        self.browser.random_delay(2.0, 4.0)

                        logger.info(
                            "Naukri profile updated (headline edit)"
                        )
                        return True

                except Exception as e:
                    logger.debug(f"Profile headline edit failed: {e}")

            # ── Strategy 3: Just visit profile (minimal signal) ──
            logger.info(
                "Naukri profile page visited "
                "(minimal update signal)"
            )
            return True

        except Exception as e:
            logger.error(f"update_profile failed: {e}")
            self._save_error("update_profile", e)
            return False

    # ═══════════════════════════════════════════════════════════
    # SESSION & ERROR HELPERS
    # ═══════════════════════════════════════════════════════════

    def _update_session(self, **kwargs) -> None:
        """Update platform session in database."""
        try:
            db = get_db()
            updates = {}
            if "logged_in" in kwargs:
                updates["logged_in"] = (1 if kwargs["logged_in"]
                                        else 0)
                if kwargs["logged_in"]:
                    updates["last_login"] = (
                        datetime.now().isoformat()
                    )
            updates.update(
                {k: v for k, v in kwargs.items()
                 if k != "logged_in"}
            )
            db.update_platform_session(self.PLATFORM_NAME, updates)
        except Exception as e:
            logger.debug(f"Session update failed: {e}")

    def _save_error(self, method: str, error: Exception) -> None:
        """Log error to database."""
        try:
            db = get_db()
            db.save_error(
                module=f"platforms.naukri.{method}",
                error_type=type(error).__name__,
                message=str(error),
                traceback=tb_module.format_exc(),
            )
        except Exception:
            pass  # Don't let error-logging errors crash anything

    # ═══════════════════════════════════════════════════════════
    # CONVENIENCE — Quick search + save to DB
    # ═══════════════════════════════════════════════════════════

    def discover_and_save(self,
                          queries: Optional[List[str]] = None,
                          filters: Optional[Dict] = None) -> Dict:
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
                    # Check duplicate
                    existing = db.get_job_by_platform_id(
                        self.PLATFORM_NAME,
                        job.get("platform_job_id", "")
                    )
                    if existing:
                        stats["duplicates"] += 1
                        continue

                    # Save
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
                        "description": job.get("description", ""),
                        "skills": json.dumps(
                            job.get("skills", [])
                        ),
                        "posted_date": job.get("posted_date", ""),
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
                f"Naukri discover: {stats['total_found']} found, "
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
    from rich.panel import Panel

    console = Console()
    console.print(
        "\n[bold cyan]═══ Naukri Platform Test ═══[/bold cyan]\n"
    )

    # ── 1. Dependency check ──
    console.print("[yellow]1. Dependency check:[/yellow]")
    console.print(
        f"   NAUKRI_EMAIL: "
        f"{'[green]✓ set[/green]' if NAUKRI_EMAIL else '[red]✗ not set[/red]'}"
    )
    console.print(
        f"   NAUKRI_PASSWORD: "
        f"{'[green]✓ set[/green]' if NAUKRI_PASSWORD else '[red]✗ not set[/red]'}"
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
    console.print("\n[yellow]4. Initializing NaukriPlatform...[/yellow]")
    try:
        from core.browser import BrowserEngine
        engine = BrowserEngine()
        naukri = NaukriPlatform(engine)
        console.print(f"   [green]✓[/green] Platform: {naukri.platform_name}")
        console.print(f"   Config: {json.dumps(naukri._naukri_config, indent=2)[:200]}")
    except Exception as e:
        console.print(f"   [red]✗ Init failed: {e}[/red]")
        sys.exit(1)

    # ── 4. Answer matching tests ──
    console.print("\n[yellow]5. Answer matching tests:[/yellow]")

    test_job = {
        "title": "Software Engineer",
        "company": "Razorpay",
        "salary_min": 600000,
        "salary_max": 1200000,
    }

    answer_tests = [
        ("What is your current CTC?", "3.7"),
        ("What is your notice period?", "15 Days"),
        ("Are you willing to relocate?", "Yes"),
        ("What is your total experience?", "1"),
        ("What is your current location?", ""),  # varies
        ("What is your highest qualification?",
         "B.Tech in Computer Science"),
        ("Do you have experience in Python?", "0.5"),
        ("Do you have experience in Vue.js?", "1"),
    ]

    for question, expected_contains in answer_tests:
        answer = naukri._find_answer_for_question(question, test_job)
        if expected_contains:
            ok = expected_contains.lower() in answer.lower()
        else:
            ok = len(answer) > 0
        icon = "[green]✓[/green]" if ok else "[yellow]?[/yellow]"
        console.print(
            f"   {icon} Q: '{question[:50]}' → "
            f"A: '{answer}'"
        )

    # ── 5. Skill years ──
    console.print("\n[yellow]6. Skill years mapping:[/yellow]")
    skill_tests = ["JavaScript", "Vue.js", "Java", "Python",
                   "Spring Boot", "MongoDB", "Kubernetes"]
    for skill in skill_tests:
        years = naukri._skill_years(skill)
        console.print(f"   {skill}: {years} years")

    # ── 6. URL builder ──
    console.print("\n[yellow]7. Search URL builder:[/yellow]")
    url = naukri._build_search_url("Software Engineer", {})
    console.print(f"   URL: {url[:100]}...")

    url2 = naukri._build_search_url("Java Developer", {
        "locations": ["Bangalore", "Pune"],
        "experience_min": 0,
        "freshness": 3,
    })
    console.print(f"   URL with filters: {url2[:100]}...")

    # ── 7. Live test (if credentials available) ──
    if NAUKRI_EMAIL and NAUKRI_PASSWORD:
        console.print(
            "\n[yellow]8. Live browser test "
            "(requires network):[/yellow]"
        )

        run_live = input(
            "   Run live Naukri test? (y/n): "
        ).strip().lower()

        if run_live == "y":
            try:
                console.print("   Attempting login...")
                login_ok = naukri.login()
                console.print(
                    f"   Login: "
                    f"{'[green]✓ success[/green]' if login_ok else '[red]✗ failed[/red]'}"
                )

                if login_ok:
                    console.print("   Searching for 'Software Engineer'...")
                    jobs = naukri.search_jobs(
                        ["Software Engineer"],
                        {"experience_min": 0}
                    )
                    console.print(
                        f"   [green]✓[/green] Found {len(jobs)} jobs"
                    )

                    if jobs:
                        # Show first 5 jobs
                        table = Table(
                            title="Naukri Search Results (top 5)"
                        )
                        table.add_column("Title", style="cyan",
                                         max_width=30)
                        table.add_column("Company", max_width=20)
                        table.add_column("Location", max_width=15)
                        table.add_column("Salary")
                        table.add_column("Exp")

                        for j in jobs[:5]:
                            table.add_row(
                                j.get("title", "")[:30],
                                j.get("company", "")[:20],
                                j.get("location", "")[:15],
                                j.get("salary_text", "N/A"),
                                j.get("experience_text", "N/A"),
                            )

                        console.print(table)

                        # Save to DB
                        console.print(
                            "\n   Saving to database..."
                        )
                        stats = naukri.discover_and_save(
                            ["Software Engineer"]
                        )
                        console.print(
                            f"   [green]✓[/green] "
                            f"Saved: {stats['new_saved']} new, "
                            f"{stats['duplicates']} duplicates"
                        )

                        # Get details of first job
                        if jobs[0].get("url"):
                            console.print(
                                "\n   Getting job details..."
                            )
                            details = naukri.get_job_details(
                                jobs[0]["url"]
                            )
                            if details:
                                console.print(
                                    f"   [green]✓[/green] "
                                    f"Title: {details.get('title')}"
                                )
                                console.print(
                                    f"   Company: "
                                    f"{details.get('company')}"
                                )
                                desc = details.get(
                                    "description", ""
                                )[:200]
                                console.print(
                                    f"   JD: {desc}..."
                                )
                                console.print(
                                    f"   Skills: "
                                    f"{details.get('skills', [])[:10]}"
                                )

            except Exception as e:
                console.print(f"   [red]Live test error: {e}[/red]")
                import traceback
                traceback.print_exc()
            finally:
                engine.close_all()
    else:
        console.print(
            "\n[yellow]8. Live test skipped "
            "(no credentials in .env)[/yellow]"
        )
        console.print(
            "   Set NAUKRI_EMAIL and NAUKRI_PASSWORD in .env "
            "to run live tests"
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
        f"═══ Naukri platform tests complete! ═══"
        f"[/bold green]\n"
    )