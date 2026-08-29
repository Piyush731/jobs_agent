#!/usr/bin/env python3
"""
platforms/linkedin.py — LinkedIn Platform Integration (SYNCHRONOUS)

╔══════════════════════════════════════════════════════════════════╗
║  ⚠  SEARCH AND SCRAPE ONLY — NO AUTO-APPLY                    ║
║  prepare_application() and submit_application() raise          ║
║  NotImplementedError.                                           ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import os
import re
import json
import time
import random
import traceback as tb_module
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from platforms.base import PlatformBase
from core.logger import get_logger
from core.db import get_db
from config import (
    PLATFORM_CONFIG,
    STEALTH_CONFIG,
    USER_PROFILE,
)

logger = get_logger("platforms.linkedin")

_BASE_URL = "https://www.linkedin.com"
_LOGIN_URL = f"{_BASE_URL}/login"
_FEED_URL = f"{_BASE_URL}/feed/"
_JOBS_URL = f"{_BASE_URL}/jobs/search/"

_EXPERIENCE_MAP = {
    "internship": "1", "entry_level": "2", "associate": "3",
    "mid_senior": "4", "director": "5", "executive": "6",
}
_DATE_POSTED_MAP = {
    "past_24h": "r86400", "past_week": "r604800",
    "past_month": "r2592000", "any_time": "",
}
_JOB_TYPE_MAP = {
    "full_time": "F", "part_time": "P", "contract": "C",
    "temporary": "T", "internship": "I",
}
_WORK_MODE_MAP = {"onsite": "1", "remote": "2", "hybrid": "3"}
_GEO_IDS = {
    "bangalore": "105214831", "bengaluru": "105214831",
    "hyderabad": "105556991", "pune": "114806696",
    "mumbai": "115884833", "delhi": "116894696",
    "delhi ncr": "116894696", "noida": "104869687",
    "gurgaon": "106376068", "gurugram": "106376068",
    "chennai": "106340085", "kolkata": "106372875",
    "india": "102713980", "remote": "",
}

_SEL: Dict[str, List[str]] = {
    "username_input": [
        "#username", 'input[name="session_key"]',
        'input[autocomplete="username"]',
    ],
    "password_input": [
        "#password", 'input[name="session_password"]',
        'input[type="password"]',
    ],
    "login_button": [
        'button[type="submit"]', 'button:has-text("Sign in")',
    ],
    "nav_me": [
        "#global-nav-icon", ".global-nav__me",
        'img[alt*="Photo of"]', ".feed-identity-module",
        ".global-nav__primary-items", 'a[href*="/feed/"]',
        ".scaffold-layout",
    ],
    "sign_in_link": [
        'a:has-text("Sign in")', 'a[href*="/login"]',
    ],
    "2fa_input": [
        "#input__phone_verification_pin",
        "#input__email_verification_pin",
        'input[name="pin"]', 'input[id*="verification"]',
    ],
    "2fa_submit": [
        "#two-step-submit-button", 'button:has-text("Submit")',
        'button:has-text("Verify")', 'button[type="submit"]',
    ],
    "challenge_page": [
        "#challenge", ".challenge",
        '#app__container .checkpoint',
        'h1:has-text("security check")',
    ],
    "job_cards": [
        ".jobs-search-results__list-item",
        "li.jobs-search-results__list-item",
        ".job-card-container",
        ".jobs-search__results-list > li",
        "li.ember-view.occludable-update",
        '[data-occludable-job-id]',
    ],
    "card_title": [
        ".job-card-list__title", ".job-card-container__link",
        "a.job-card-list__title", "a.job-card-container__link",
        ".artdeco-entity-lockup__title a", "strong",
    ],
    "card_company": [
        ".job-card-container__primary-description",
        ".job-card-container__company-name",
        ".artdeco-entity-lockup__subtitle",
    ],
    "card_location": [
        ".job-card-container__metadata-item",
        ".artdeco-entity-lockup__caption",
    ],
    "card_date": [
        "time", "time[datetime]",
        ".job-card-container__listed-time",
    ],
    "card_link": [
        "a.job-card-list__title", "a.job-card-container__link",
        "a[href*='/jobs/view/']",
    ],
    "detail_title": [
        ".job-details-jobs-unified-top-card__job-title h1",
        ".jobs-unified-top-card__job-title",
        "h1.t-24", ".top-card-layout__title", "h1",
    ],
    "detail_company": [
        ".job-details-jobs-unified-top-card__company-name a",
        ".jobs-unified-top-card__company-name a",
        "a.topcard__org-name-link", "span.topcard__flavor",
    ],
    "detail_location": [
        ".job-details-jobs-unified-top-card__bullet",
        ".jobs-unified-top-card__bullet",
        ".topcard__flavor--bullet",
    ],
    "detail_work_mode": [
        ".job-details-jobs-unified-top-card__workplace-type",
        'span:has-text("Remote")', 'span:has-text("Hybrid")',
        'span:has-text("On-site")',
    ],
    "detail_salary": [
        ".salary.compensation__salary",
        ".compensation__salary",
        'li:has-text("₹")', 'li:has-text("LPA")',
    ],
    "detail_description": [
        ".jobs-description__content",
        ".jobs-description-content__text",
        ".jobs-box__html-content", "#job-details",
        ".show-more-less-html__markup",
        ".jobs-description",
    ],
    "detail_criteria": [
        ".job-details-jobs-unified-top-card__job-insight",
        "li.job-criteria__item",
    ],
    "detail_posted": [
        ".jobs-unified-top-card__posted-date",
        'span:has-text("ago")', 'span:has-text("Posted")',
    ],
    "detail_applicants": [
        ".jobs-unified-top-card__applicant-count",
        'span:has-text("applicants")',
    ],
    "show_more_btn": [
        'button:has-text("Show more")',
        "button.jobs-description__footer-button",
        ".show-more-less-html__button",
    ],
    "pagination": [
        ".artdeco-pagination",
        "ul.artdeco-pagination__pages",
    ],
    "close_popup": [
        'button[aria-label="Dismiss"]', 'button[aria-label="Close"]',
        'button:has-text("Dismiss")', 'button:has-text("Not now")',
        "button.msg-overlay-bubble-header__control--close",
        ".artdeco-modal__dismiss",
    ],
    "messaging_overlay_close": [
        "button.msg-overlay-bubble-header__control--close",
        'header button[aria-label="Close your conversations"]',
    ],
    "cookie_accept": [
        'button:has-text("Accept")', 'button[action-type="ACCEPT"]',
    ],
}

_SKILL_PATTERNS: List[str] = [
    r"\bJava\b(?!\s*Script)", r"\bPython\b", r"\bJavaScript\b",
    r"\bTypeScript\b", r"\bReact(?:\.js|JS)?\b",
    r"\bVue(?:\.js|JS)?\b", r"\bNode(?:\.js|JS)?\b",
    r"\bExpress(?:\.js|JS)?\b", r"\bSpring\s*Boot\b",
    r"\bDjango\b", r"\bFlask\b", r"\bFastAPI\b",
    r"\bMySQL\b", r"\bPostgreSQL\b", r"\bMongoDB\b", r"\bRedis\b",
    r"\bDocker\b", r"\bKubernetes\b", r"\bAWS\b", r"\bAzure\b",
    r"\bGit\b", r"\bLinux\b", r"\bREST\s*API[s]?\b",
    r"\bGraphQL\b", r"\bKafka\b", r"\bHTML5?\b", r"\bCSS3?\b",
    r"\bTailwind(?:\s*CSS)?\b", r"\bSQL\b", r"\bMicroservices?\b",
    r"\bCI\s*/\s*CD\b", r"\bAgile\b", r"\bScrum\b",
    r"\bWebSocket[s]?\b", r"\bJWT\b",
]


# ═══════════════════════════════════════════════════════════════
# HELPERS (module-level, sync)
# ═══════════════════════════════════════════════════════════════

def _find(page, key: str, timeout: int = 5000):
    for sel in _SEL.get(key, []):
        try:
            el = page.wait_for_selector(sel, timeout=timeout, state="visible")
            if el:
                return el
        except Exception:
            continue
    return None


def _find_all(page, key: str, timeout: int = 5000) -> list:
    for sel in _SEL.get(key, []):
        try:
            page.wait_for_selector(sel, timeout=timeout, state="visible")
            els = page.query_selector_all(sel)
            if els:
                return els
        except Exception:
            continue
    return []


def _txt(el) -> str:
    if el is None:
        return ""
    try:
        t = el.inner_text()
        return (t or "").strip()
    except Exception:
        try:
            t = el.text_content()
            return (t or "").strip()
        except Exception:
            return ""


def _attr(el, name: str) -> str:
    if el is None:
        return ""
    try:
        v = el.get_attribute(name)
        return (v or "").strip()
    except Exception:
        return ""


def _css_for(el) -> str:
    try:
        eid = el.get_attribute("id")
        if eid:
            return f"#{eid}"
        tag = el.evaluate("e=>e.tagName.toLowerCase()")
        name = el.get_attribute("name")
        if name:
            return f'{tag}[name="{name}"]'
        cls = el.get_attribute("class")
        if cls:
            return f"{tag}.{cls.split()[0]}"
    except Exception:
        pass
    return "button:visible"


def _extract_skills(text: str) -> List[str]:
    found = []
    for pat in _SKILL_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            found.append(m.group(0))
    return list(set(found))


# ═══════════════════════════════════════════════════════════════
# LinkedInPlatform (SYNCHRONOUS)
# ═══════════════════════════════════════════════════════════════

class LinkedInPlatform(PlatformBase):
    """
    LinkedIn — SEARCH & SCRAPE ONLY. No auto-apply.
    """

    PLATFORM_NAME = "linkedin"

    def __init__(self, browser_engine, notifier=None):
        super().__init__(browser_engine)
        if notifier:
            self.set_notifier(notifier)
        self.platform_name = self.PLATFORM_NAME

        if not hasattr(self, 'browser'):
            self.browser = browser_engine

        self.db = get_db()

        cfg = PLATFORM_CONFIG.get("linkedin", {})
        self.search_queries = cfg.get("search_queries", [
            "software developer", "full stack developer",
            "backend developer", "java developer",
            "SDE-1", "node.js developer",
        ])
        self.max_pages = cfg.get("max_pages_per_query", 3)
        self._max_pages_per_day = cfg.get("max_pages_per_day", 100)
        self._session_max_min = cfg.get("session_max_minutes", 20)
        self._session_break_range = tuple(
            cfg.get("session_break_minutes", [15, 30]))
        self._pages_today = 0
        self._session_start: Optional[datetime] = None
        self._page = None
        self._delay_range = (5.0, 15.0)  # extra slow for LinkedIn

        logger.info("LinkedInPlatform initialised (SEARCH ONLY, "
                     "max %d pages/day)", self._max_pages_per_day)

    # ═══════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ═══════════════════════════════════════════════════════════

    def _get_page(self):
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    _ = self._page.url
                    return self._page
            except Exception:
                pass
        self._page = self.browser.launch(self.PLATFORM_NAME,
                                         headless=False)
        return self._page

    def _dismiss_popups(self, page) -> None:
        targets = (
            _SEL.get("messaging_overlay_close", [])
            + _SEL.get("close_popup", [])
            + _SEL.get("cookie_accept", [])
        )
        for sel in targets:
            try:
                if self.browser.element_visible(page, sel):
                    page.click(sel, timeout=2000)
                    time.sleep(random.uniform(0.3, 1.0))
            except Exception:
                continue

    def _linkedin_delay(self) -> None:
        lo, hi = self._delay_range
        self.browser.random_delay(lo, hi)

    def _human_scroll(self, page, rounds: int = 4) -> None:
        for _ in range(rounds):
            self.browser.scroll_page(page, "down",
                                     random.randint(150, 500))
            self.browser.random_delay(1.0, 3.0)
            if random.random() < 0.3:
                self.browser.random_delay(2.0, 5.0)

    def _check_session_limits(self) -> bool:
        if self._pages_today >= self._max_pages_per_day:
            logger.warning("Daily page limit reached (%d/%d)",
                           self._pages_today, self._max_pages_per_day)
            return False
        if self._session_start:
            elapsed = (datetime.now() - self._session_start
                       ).total_seconds() / 60
            if elapsed >= self._session_max_min:
                logger.info("Session limit reached (%.0f min)", elapsed)
                return False
        return True

    def _session_break(self) -> None:
        lo, hi = self._session_break_range
        wait_min = random.uniform(lo, hi)
        logger.info("Taking %.1f min session break", wait_min)
        time.sleep(wait_min * 60)
        self._session_start = datetime.now()

    def _save_error(self, method: str, error: Exception) -> None:
        try:
            self.db.save_error(
                module=f"platforms.linkedin.{method}",
                error_type=type(error).__name__,
                message=str(error),
                traceback=tb_module.format_exc(),
            )
        except Exception:
            pass

    def _update_session(self, *, logged_in: bool,
                        error: str = None,
                        cooldown_h: float = 0) -> None:
        updates = {
            "logged_in": int(logged_in),
            "last_login": datetime.now().isoformat() if logged_in else None,
            "status": "active" if logged_in else (
                "cooldown" if cooldown_h else "active"),
            "last_error": error,
        }
        if cooldown_h:
            updates["cooldown_until"] = (
                datetime.now() + timedelta(hours=cooldown_h)
            ).isoformat()
        try:
            self.db.update_platform_session("linkedin", updates)
        except Exception:
            pass

    def _tg(self, msg: str) -> None:
        """Send Telegram alert using base class notifier."""
        notifier = getattr(self, '_notifier', None)
        if notifier:
            try:
                notifier.send_platform_issue("linkedin", msg)
            except Exception:
                pass

    def _check_logged_in(self, page) -> bool:
        """
        Check if currently logged in to LinkedIn.
        Named _check_logged_in to avoid collision with base class
        is_logged_in which may be a bool property.
        """
        for sel in _SEL.get("nav_me", []):
            try:
                if page.query_selector(sel):
                    return True
            except Exception:
                continue

        url = self.browser.get_page_url(page)
        if "/feed" in url and "/login" not in url:
            return True

        for sel in _SEL.get("sign_in_link", []):
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    return False
            except Exception:
                continue

        if "/login" in url or "/authwall" in url:
            return False
        return False

    # ═══════════════════════════════════════════════════════════
    # LOGIN
    # ═══════════════════════════════════════════════════════════

    def login(self) -> bool:
        logger.info("═══ LinkedIn Login ═══")
        try:
            page = self._get_page()

            self.browser.load_cookies("linkedin")
            self.browser.navigate(page, _FEED_URL)
            self.browser.random_delay(3, 5)
            self._dismiss_popups(page)

            if self._check_logged_in(page):
                logger.info("✅ LinkedIn: logged in via cookies")
                self.browser.save_cookies("linkedin")
                self._update_session(logged_in=True)
                self._session_start = datetime.now()
                return True

            # Credential login
            email = os.getenv("LINKEDIN_EMAIL", "")
            password = os.getenv("LINKEDIN_PASSWORD", "")
            if not email or not password:
                logger.error("LINKEDIN_EMAIL/PASSWORD not in .env")
                self._update_session(logged_in=False,
                                     error="Credentials missing")
                return False

            self.browser.navigate(page, _LOGIN_URL)
            self.browser.random_delay(2, 4)
            self._dismiss_popups(page)

            # Email
            u_el = _find(page, "username_input", timeout=5000)
            if not u_el:
                logger.error("Username input not found")
                self._update_session(logged_in=False,
                                     error="Login page changed")
                return False
            self.browser.type_human(page, _css_for(u_el), email)
            self.browser.random_delay(1.0, 2.5)

            # Password
            p_el = _find(page, "password_input", timeout=3000)
            if not p_el:
                logger.error("Password input not found")
                return False
            self.browser.type_human(page, _css_for(p_el), password)
            self.browser.random_delay(1.0, 2.0)

            # Submit
            btn = _find(page, "login_button", timeout=3000)
            if btn:
                self.browser.click_human(page, _css_for(btn))
            else:
                self.browser.press_key(page, "Enter")
            self.browser.random_delay(4, 7)

            # Handle 2FA
            self._handle_2fa(page)

            # Handle challenge
            self._handle_challenge(page)

            # CAPTCHA
            cap = self.detect_captcha(page)
            if cap:
                self.handle_captcha(page, getattr(self, '_notifier', None))
                self.browser.random_delay(3, 5)

            self._dismiss_popups(page)
            if self._check_logged_in(page):
                logger.info("✅ LinkedIn: login successful")
                self.browser.save_cookies("linkedin")
                self._update_session(logged_in=True)
                self._session_start = datetime.now()
                return True

            # Try feed
            self.browser.navigate(page, _FEED_URL)
            self.browser.random_delay(3, 5)
            if self._check_logged_in(page):
                logger.info("✅ LinkedIn: login successful (redirect)")
                self.browser.save_cookies("linkedin")
                self._update_session(logged_in=True)
                self._session_start = datetime.now()
                return True

            logger.error("❌ LinkedIn login failed")
            self.browser.take_screenshot(page, "linkedin_login_failed")
            self._update_session(logged_in=False,
                                 error="Login failed", cooldown_h=1)
            return False

        except Exception as exc:
            logger.error("Login exception: %s", exc)
            self._save_error("login", exc)
            return False

    def _handle_2fa(self, page) -> bool:
        pin_el = _find(page, "2fa_input", timeout=3000)
        if not pin_el:
            return False

        logger.info("2FA detected")
        self._tg("🔐 *LinkedIn 2FA Required*\n"
                  "Provide the verification code.")

        code = None
        notifier = getattr(self, '_notifier', None)
        if notifier:
            try:
                code = notifier.send_otp_request("linkedin")
            except Exception:
                pass

        if not code:
            logger.info("Waiting 3 min for manual 2FA in browser")
            for _ in range(36):
                time.sleep(5)
                if self._check_logged_in(page):
                    return True
                if not _find(page, "2fa_input", timeout=1000):
                    return True
            return False

        self.browser.type_human(page, _css_for(pin_el), code)
        self.browser.random_delay(1.0, 2.0)

        submit = _find(page, "2fa_submit", timeout=3000)
        if submit:
            self.browser.click_human(page, _css_for(submit))
        else:
            self.browser.press_key(page, "Enter")
        self.browser.random_delay(4, 7)
        return self._check_logged_in(page)

    def _handle_challenge(self, page) -> bool:
        challenge = _find(page, "challenge_page", timeout=2000)
        url = self.browser.get_page_url(page)
        if not challenge and "/checkpoint/" not in url:
            return False

        logger.info("Security challenge detected")
        self._tg("🛡 *LinkedIn Security Challenge*\n"
                  "Complete verification in browser.\n⏱ 5 min")

        for _ in range(60):
            time.sleep(5)
            if self._check_logged_in(page):
                return True
            cur_url = self.browser.get_page_url(page)
            if "/feed" in cur_url or "/jobs" in cur_url:
                return True
        return False

    # ═══════════════════════════════════════════════════════════
    # SEARCH JOBS
    # ═══════════════════════════════════════════════════════════

    def search_jobs(self, queries: Optional[List[str]] = None,
                    filters: Optional[Dict] = None) -> List[Dict]:
        logger.info("═══ LinkedIn Search (SCRAPE ONLY) ═══")
        page = self._get_page()

        if not self._check_logged_in(page):
            logger.warning("Not logged in, attempting login")
            if not self.login():
                return []

        queries = queries or self.search_queries
        filters = filters or {}
        locations = filters.get("locations",
                                USER_PROFILE.get("target_locations", [
                                    "Bangalore", "Hyderabad", "Pune",
                                    "Remote", "Delhi NCR", "Mumbai"]))
        date_posted = filters.get("date_posted", "past_week")
        job_type = filters.get("job_type", "full_time")
        experience = filters.get("experience", "entry_level")
        work_mode = filters.get("work_mode", "")

        all_jobs: List[Dict] = []

        for q in queries:
            if not self._check_session_limits():
                self._session_break()

            for loc in locations:
                if not self._check_session_limits():
                    self._session_break()

                try:
                    logger.info("→ '%s' in '%s'", q, loc)
                    batch = self._run_search(
                        page, q, loc, date_posted,
                        job_type, experience, work_mode)
                    logger.info("  found %d jobs", len(batch))
                    all_jobs.extend(batch)
                    self._linkedin_delay()
                except Exception as exc:
                    logger.error("Search error '%s'/'%s': %s",
                                 q, loc, exc)
                    self._save_error("search_jobs", exc)
                    self.browser.random_delay(10, 20)

        # Deduplicate
        seen: set = set()
        unique: List[Dict] = []
        for j in all_jobs:
            jid = j.get("platform_job_id", "")
            if jid and jid not in seen:
                seen.add(jid)
                unique.append(j)

        logger.info("═══ LinkedIn Search Complete: %d unique ═══",
                     len(unique))
        return unique

    def _run_search(self, page, query, location, date_posted,
                    job_type, experience, work_mode) -> List[Dict]:
        jobs = []
        for page_num in range(self.max_pages):
            if not self._check_session_limits():
                break

            url = self._build_search_url(
                query, location, date_posted,
                job_type, experience, work_mode, page_num)

            if not self.browser.navigate(page, url):
                break
            self.browser.random_delay(3, 6)
            self._dismiss_popups(page)

            cur_url = self.browser.get_page_url(page)
            if "/login" in cur_url or "/authwall" in cur_url:
                logger.warning("Session expired during search")
                break

            cap = self.detect_captcha(page)
            if cap:
                if not self.handle_captcha(page, getattr(self, '_notifier', None)):
                    break

            # Scroll to load lazy content
            self._human_scroll(page, rounds=random.randint(4, 8))
            self._pages_today += 1

            batch = self._parse_job_cards(page, query, location)
            if not batch:
                break
            jobs.extend(batch)

            if page_num < self.max_pages - 1:
                if not self._has_next_page(page, page_num + 2):
                    break
            self._linkedin_delay()

        return jobs

    def _build_search_url(self, query, location, date_posted,
                          job_type, experience, work_mode,
                          page_num) -> str:
        params = {
            "keywords": query,
            "location": location,
            "refresh": "true",
            "sortBy": "DD",
        }
        geo_id = _GEO_IDS.get(location.lower().strip(), "")
        if geo_id:
            params["geoId"] = geo_id

        tpr = _DATE_POSTED_MAP.get(date_posted, "")
        if tpr:
            params["f_TPR"] = tpr
        jt = _JOB_TYPE_MAP.get(job_type, "")
        if jt:
            params["f_JT"] = jt
        exp = _EXPERIENCE_MAP.get(experience, "")
        if exp:
            params["f_E"] = exp
        wm = _WORK_MODE_MAP.get(work_mode, "")
        if wm:
            params["f_WT"] = wm
        if page_num > 0:
            params["start"] = str(page_num * 25)

        return f"{_JOBS_URL}?{urlencode(params)}"

    def _has_next_page(self, page, next_num: int) -> bool:
        try:
            btn = page.query_selector(
                f'button[aria-label="Page {next_num}"]')
            if btn:
                return True
            btns = page.query_selector_all(
                "li.artdeco-pagination__indicator--number button")
            for b in btns:
                if _txt(b).strip() == str(next_num):
                    return True
        except Exception:
            pass
        return False

    def _parse_job_cards(self, page, query, location) -> List[Dict]:
        cards = _find_all(page, "job_cards", timeout=8000)
        if not cards:
            try:
                cards = page.query_selector_all(
                    "li[data-occludable-job-id], "
                    "div.job-card-container")
            except Exception:
                pass
        if not cards:
            return []

        results = []
        for card in cards:
            try:
                data = self._extract_card(card, query, location)
                if data and data.get("platform_job_id"):
                    results.append(data)
            except Exception:
                pass
            if random.random() < 0.2:
                self.browser.random_delay(0.5, 1.5)
        return results

    def _extract_card(self, card, query, location) -> Optional[Dict]:
        now_iso = datetime.now().isoformat()

        # Job ID
        jid = _attr(card, "data-occludable-job-id")
        if not jid:
            jid = _attr(card, "data-job-id")
        if not jid:
            urn = _attr(card, "data-entity-urn")
            if urn and ":" in urn:
                jid = urn.split(":")[-1]
        if not jid:
            link = card.query_selector("a[href*='/jobs/view/']")
            if link:
                href = _attr(link, "href")
                m = re.search(r"/jobs/view/(\d+)", href)
                if m:
                    jid = m.group(1)
        if not jid:
            return None

        # Title
        title = ""
        for sel in _SEL["card_title"]:
            el = card.query_selector(sel)
            if el:
                title = _txt(el)
                if title:
                    title = re.sub(r"\s+", " ", title).strip()
                    break
        if not title:
            return None

        # URL
        url = f"{_BASE_URL}/jobs/view/{jid}/"
        for sel in _SEL["card_link"]:
            el = card.query_selector(sel)
            if el:
                href = _attr(el, "href")
                if href:
                    if href.startswith("/"):
                        href = f"{_BASE_URL}{href}"
                    url = re.sub(r"\?.*$", "", href)
                    break

        # Company
        company = ""
        for sel in _SEL["card_company"]:
            el = card.query_selector(sel)
            if el:
                company = _txt(el).split("\n")[0].strip()
                if company:
                    break

        # Location
        loc = ""
        for sel in _SEL["card_location"]:
            el = card.query_selector(sel)
            if el:
                loc = _txt(el).split("\n")[0].strip()
                if loc:
                    break

        # Posted date
        posted = ""
        for sel in _SEL["card_date"]:
            el = card.query_selector(sel)
            if el:
                posted = _txt(el) or _attr(el, "datetime")
                if posted:
                    break

        # Work mode / skills from card text
        card_text = _txt(card)
        card_lower = card_text.lower()

        work_mode = "onsite"
        if "remote" in card_lower:
            work_mode = "remote"
        elif "hybrid" in card_lower:
            work_mode = "hybrid"

        skills = _extract_skills(card_text)
        closed_markers = (
            "no longer accepting applications",
            "applications are closed",
            "job is no longer available",
            "this job has expired",
            "position has been filled",
        )
        status = "expired" if any(marker in card_lower for marker in closed_markers) else "new"

        return {
            "platform": "linkedin",
            "platform_job_id": jid,
            "url": url,
            "title": title,
            "company": company,
            "location": loc,
            "salary_text": "",
            "experience_text": "",
            "description": "",
            "posted_date": posted,
            "skills": skills,
            "work_mode": work_mode,
            "job_type": "full-time",
            "status": status,
            "discovered_at": now_iso,
        }

    # ═══════════════════════════════════════════════════════════
    # GET JOB DETAILS
    # ═══════════════════════════════════════════════════════════

    def get_job_details(self, job_url: str) -> Dict:
        logger.info("Fetching LinkedIn details: %s", job_url)
        page = self._get_page()

        if not self._check_session_limits():
            self._session_break()

        try:
            if not self.browser.navigate(page, job_url):
                return {}
            self.browser.random_delay(3, 6)
            self._dismiss_popups(page)

            cur_url = self.browser.get_page_url(page)
            if "/login" in cur_url or "/authwall" in cur_url:
                return {}

            cap = self.detect_captcha(page)
            if cap and not self.handle_captcha(page, getattr(self, '_notifier', None)):
                return {}

            self._human_scroll(page, rounds=random.randint(2, 4))
            self._pages_today += 1

            # Show more
            sm = _find(page, "show_more_btn", timeout=3000)
            if sm:
                try:
                    self.browser.click_human(page, _css_for(sm))
                    self.browser.random_delay(1.0, 2.5)
                except Exception:
                    pass

            result: Dict[str, Any] = {"url": job_url}

            el = _find(page, "detail_title", timeout=5000)
            result["title"] = _txt(el)

            el = _find(page, "detail_company", timeout=3000)
            company = _txt(el)
            result["company"] = re.split(r"\s*[·•]\s*", company)[0].strip()

            el = _find(page, "detail_location", timeout=3000)
            result["location"] = _txt(el).split("\n")[0].strip()

            # Work mode
            wm_el = _find(page, "detail_work_mode", timeout=2000)
            wm_text = _txt(wm_el).lower()
            if "remote" in wm_text:
                result["work_mode"] = "remote"
            elif "hybrid" in wm_text:
                result["work_mode"] = "hybrid"
            else:
                result["work_mode"] = "onsite"

            # Salary
            salary = ""
            for sel in _SEL["detail_salary"]:
                try:
                    el2 = page.query_selector(sel)
                    if el2:
                        s = _txt(el2)
                        if s and ("₹" in s or "lpa" in s.lower()):
                            salary = s
                            break
                except Exception:
                    continue
            result["salary_text"] = salary

            # Posted
            posted = ""
            for sel in _SEL["detail_posted"]:
                try:
                    el2 = page.query_selector(sel)
                    if el2:
                        posted = _txt(el2)
                        if posted:
                            break
                except Exception:
                    continue
            result["posted_date"] = posted

            # Applicants
            app_count = ""
            for sel in _SEL["detail_applicants"]:
                try:
                    el2 = page.query_selector(sel)
                    if el2:
                        app_count = _txt(el2)
                        if app_count:
                            break
                except Exception:
                    continue
            result["applicant_count"] = app_count

            # Description
            desc_el = _find(page, "detail_description", timeout=6000)
            description = _txt(desc_el)
            result["description"] = description

            # Skills
            result["skills"] = _extract_skills(
                f"{result.get('title','')} {description}")

            # Experience
            exp_m = re.search(
                r"(\d+)\s*[-–to]+\s*(\d+)\s*(?:years?|yrs?)",
                description, re.I)
            if not exp_m:
                exp_m = re.search(r"(\d+)\+?\s*(?:years?|yrs?)",
                                  description, re.I)
            result["experience_text"] = exp_m.group(0) if exp_m else ""
            result["job_type"] = "full-time"

            logger.info("  ✅ '%s' @ '%s' — %d skills",
                         result.get("title", "?"),
                         result.get("company", "?"),
                         len(result.get("skills", [])))
            self._linkedin_delay()
            return result

        except Exception as exc:
            logger.error("get_job_details error: %s", exc)
            self._save_error("get_job_details", exc)
            return {}

    # ═══════════════════════════════════════════════════════════
    # APPLY — NOT SUPPORTED
    # ═══════════════════════════════════════════════════════════

    def prepare_application(self, job, resume_path,
                            cover_letter=None) -> Dict:
        raise NotImplementedError(
            "LinkedIn auto-apply NOT supported — ban risk.")

    def submit_application(self, prepared) -> Dict:
        raise NotImplementedError(
            "LinkedIn auto-apply NOT supported — ban risk.")

    def check_status(self, application_id=None) -> str:
        return "not_applicable"


# ═══════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  LinkedIn Platform — Test")
    print("  ⚠  SEARCH & SCRAPE ONLY — NO APPLY")
    print("=" * 60)

    from core.browser import BrowserEngine
    engine = BrowserEngine()
    li = LinkedInPlatform(engine)

    # 1. Basic checks
    print("\n[1] Basic checks:")
    assert hasattr(li, 'browser'), "self.browser missing!"
    assert li.browser is engine
    print("  ✓ self.browser OK")

    for m in ['can_apply', 'increment_count']:
        assert hasattr(li, m), f"{m} missing!"
        print(f"  ✓ {m}")

    # 2. Apply blocked
    print("\n[2] Apply blocked:")
    try:
        li.prepare_application({}, "/tmp/x.pdf")
        print("  ✗ Should have raised!")
    except NotImplementedError:
        print("  ✓ prepare_application raises NotImplementedError")

    try:
        li.submit_application({})
        print("  ✗ Should have raised!")
    except NotImplementedError:
        print("  ✓ submit_application raises NotImplementedError")

    # 3. URL builder
    print("\n[3] URL builder:")
    url = li._build_search_url(
        "Java Developer", "Bangalore", "past_week",
        "full_time", "entry_level", "", 0)
    assert "keywords=Java" in url
    assert "geoId=105214831" in url
    print(f"  ✓ {url[:80]}...")

    # 4. Status
    print("\n[4] check_status:")
    assert li.check_status() == "not_applicable"
    print("  ✓ Returns 'not_applicable'")

    # 5. Credentials
    print("\n[5] Credentials:")
    has_email = bool(os.getenv("LINKEDIN_EMAIL", ""))
    has_pwd = bool(os.getenv("LINKEDIN_PASSWORD", ""))
    print(f"  LINKEDIN_EMAIL: {'✓' if has_email else '✗ not set'}")
    print(f"  LINKEDIN_PASSWORD: {'✓' if has_pwd else '✗ not set'}")

    print(f"\n✅ LinkedIn platform tests complete!\n")