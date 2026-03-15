#!/usr/bin/env python3
"""
platforms/linkedin.py — LinkedIn Platform Integration

╔══════════════════════════════════════════════════════════════════════╗
║  ⚠  SEARCH AND SCRAPE ONLY — NO AUTO-APPLY                        ║
║                                                                      ║
║  LinkedIn aggressively bans automation.  This module ONLY:           ║
║    • Logs in (email + password, cookies persisted)                   ║
║    • Searches jobs via the /jobs/search/ endpoint                    ║
║    • Extracts job details (JD, skills, salary, company)              ║
║    • Returns structured data for the matcher / pipeline              ║
║                                                                      ║
║  prepare_application() and submit_application() raise               ║
║  NotImplementedError.  Apply via other platforms or email outreach.  ║
╚══════════════════════════════════════════════════════════════════════╝

Auth:
  • email + password auto-login
  • Cookies saved → reused on subsequent runs
  • 2FA / verification pin → Telegram asks user for code
  • Challenge page (phone/email verify) → Telegram alert + wait

Anti-ban layers:
  • Very slow, human-like interaction (longer delays than other platforms)
  • Session limit: 20 min active, 15-30 min break
  • Max 100 search-result pages / day
  • Randomised scroll, hover, idle pauses
  • Persistent browser profile with saved cookies
  • No clicking Apply — ever
"""

from __future__ import annotations

import os
import re
import json
import time
import random
import asyncio
import traceback as tb_module
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, quote_plus

from platforms.base import PlatformBase
from core.logger import get_logger
from core.db import get_db
from config import (
    BASE_DIR,
    BROWSER_PROFILES_DIR,
    CACHE_DIR,
    PLATFORM_CONFIG,
    STEALTH_CONFIG,
    USER_PROFILE,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://www.linkedin.com"
_LOGIN_URL = f"{_BASE_URL}/login"
_FEED_URL = f"{_BASE_URL}/feed/"
_JOBS_URL = f"{_BASE_URL}/jobs/search/"

# Experience level filter values (LinkedIn f_E param)
_EXPERIENCE_MAP: Dict[str, str] = {
    "internship": "1",
    "entry_level": "2",
    "associate": "3",
    "mid_senior": "4",
    "director": "5",
    "executive": "6",
}

# Date posted filter (LinkedIn f_TPR param)
_DATE_POSTED_MAP: Dict[str, str] = {
    "past_24h": "r86400",
    "past_week": "r604800",
    "past_month": "r2592000",
    "any_time": "",
}

# Job type filter (LinkedIn f_JT param)
_JOB_TYPE_MAP: Dict[str, str] = {
    "full_time": "F",
    "part_time": "P",
    "contract": "C",
    "temporary": "T",
    "internship": "I",
    "volunteer": "V",
    "other": "O",
}

# Work mode (LinkedIn f_WT param)
_WORK_MODE_MAP: Dict[str, str] = {
    "onsite": "1",
    "remote": "2",
    "hybrid": "3",
}

# Location GeoId mapping for major Indian cities
_GEO_IDS: Dict[str, str] = {
    "bangalore": "105214831",
    "bengaluru": "105214831",
    "hyderabad": "105556991",
    "pune": "114806696",
    "mumbai": "115884833",
    "delhi": "116894696",
    "delhi ncr": "116894696",
    "new delhi": "116894696",
    "noida": "104869687",
    "gurgaon": "106376068",
    "gurugram": "106376068",
    "chennai": "106340085",
    "kolkata": "106372875",
    "india": "102713980",
    "remote": "",
}

# ── Selectors — multiple per element for resilience ──────────────────

_SEL: Dict[str, List[str]] = {
    # ── Login page ─────────────────────────────────────────────────────
    "username_input": [
        "#username",
        'input[name="session_key"]',
        'input[autocomplete="username"]',
        'input[type="text"]#username',
    ],
    "password_input": [
        "#password",
        'input[name="session_password"]',
        'input[autocomplete="current-password"]',
        'input[type="password"]',
    ],
    "login_button": [
        'button[type="submit"]',
        'button:has-text("Sign in")',
        'button[data-litms-control-urn*="login-submit"]',
        ".login__form_action_container button",
    ],
    # ── Logged-in indicators ───────────────────────────────────────────
    "nav_me": [
        "#global-nav-icon",
        ".global-nav__me",
        'img[alt*="Photo of"]',
        ".feed-identity-module",
        ".global-nav__primary-items",
        'a[href*="/feed/"]',
        ".scaffold-layout",
    ],
    "sign_in_link": [
        'a:has-text("Sign in")',
        'a[href*="/login"]',
        ".nav__button-secondary",
    ],
    # ── 2FA / Verification ─────────────────────────────────────────────
    "2fa_input": [
        "#input__phone_verification_pin",
        "#input__email_verification_pin",
        'input[name="pin"]',
        'input[id*="verification"]',
        'input[name="challengeData"]',
    ],
    "2fa_submit": [
        "#two-step-submit-button",
        'button:has-text("Submit")',
        'button:has-text("Verify")',
        'button[type="submit"]',
    ],
    "challenge_page": [
        "#challenge",
        ".challenge",
        "#app__container .checkpoint",
        'h1:has-text("Let\'s do a quick security check")',
        'h1:has-text("Security verification")',
        'p:has-text("verify")',
    ],
    # ── Job search results ─────────────────────────────────────────────
    "jobs_list_container": [
        ".jobs-search__results-list",
        ".scaffold-layout__list",
        "ul.jobs-search-results__list",
        ".jobs-search-results-list",
        "div.jobs-search-results",
    ],
    "job_cards": [
        ".jobs-search-results__list-item",
        "li.jobs-search-results__list-item",
        ".job-card-container",
        ".jobs-search__results-list > li",
        "div.job-card-list",
        "li.ember-view.occludable-update",
        '[data-occludable-job-id]',
    ],
    "card_title": [
        ".job-card-list__title",
        ".job-card-container__link",
        "a.job-card-list__title",
        "a.job-card-container__link",
        ".artdeco-entity-lockup__title a",
        "a.disabled.ember-view.job-card-container__link",
        "strong",
    ],
    "card_company": [
        ".job-card-container__primary-description",
        ".job-card-container__company-name",
        ".artdeco-entity-lockup__subtitle",
        ".job-card-list__company-name",
        "a.job-card-container__company-name",
        ".job-card-container__primary-description",
    ],
    "card_location": [
        ".job-card-container__metadata-item",
        ".artdeco-entity-lockup__caption",
        ".job-card-container__metadata-wrapper li",
        ".job-card-list__metadata-item",
    ],
    "card_date": [
        "time",
        "time[datetime]",
        ".job-card-container__listed-time",
        ".job-card-list__footer-wrapper time",
    ],
    "card_link": [
        "a.job-card-list__title",
        "a.job-card-container__link",
        "a.disabled.ember-view",
        "a[href*='/jobs/view/']",
    ],
    # ── Job detail panel / page ────────────────────────────────────────
    "detail_title": [
        ".job-details-jobs-unified-top-card__job-title h1",
        ".jobs-unified-top-card__job-title",
        "h1.t-24",
        ".top-card-layout__title",
        "h2.top-card-layout__title",
        "h1.topcard__title",
        "h1",
    ],
    "detail_company": [
        ".job-details-jobs-unified-top-card__company-name a",
        ".job-details-jobs-unified-top-card__company-name",
        ".jobs-unified-top-card__company-name a",
        ".jobs-unified-top-card__company-name",
        "a.topcard__org-name-link",
        ".topcard__flavor--black-link",
        "a.top-card-layout__company-url",
        "span.topcard__flavor",
    ],
    "detail_location": [
        ".job-details-jobs-unified-top-card__bullet",
        ".jobs-unified-top-card__bullet",
        ".topcard__flavor--bullet",
        "span.topcard__flavor:not(.topcard__flavor--black-link)",
        ".top-card-layout__second-subline span",
    ],
    "detail_work_mode": [
        ".job-details-jobs-unified-top-card__workplace-type",
        ".jobs-unified-top-card__workplace-type",
        "span.ui-label--accent-3",
        'span:has-text("Remote")',
        'span:has-text("Hybrid")',
        'span:has-text("On-site")',
    ],
    "detail_salary": [
        ".salary.compensation__salary",
        ".job-details-jobs-unified-top-card__job-insight span",
        ".compensation__salary",
        'li:has-text("₹")',
        'li:has-text("LPA")',
        'li:has-text("per annum")',
        'span:has-text("₹")',
    ],
    "detail_description": [
        ".jobs-description__content",
        ".jobs-description-content__text",
        ".jobs-box__html-content",
        "#job-details",
        ".description__text",
        ".show-more-less-html__markup",
        "article.jobs-description__container",
        ".jobs-description",
    ],
    "detail_criteria": [
        ".job-details-jobs-unified-top-card__job-insight",
        ".jobs-unified-top-card__job-insight",
        ".description__job-criteria-list",
        "li.job-criteria__item",
        ".jobs-box__group",
    ],
    "detail_posted": [
        ".jobs-unified-top-card__posted-date",
        ".posted-time-ago__text",
        "span.topcard__flavor--metadata",
        'span:has-text("ago")',
        'span:has-text("Posted")',
    ],
    "detail_applicants": [
        ".jobs-unified-top-card__applicant-count",
        'span:has-text("applicants")',
        'span:has-text("applicant")',
        ".num-applicants__caption",
    ],
    "show_more_btn": [
        'button:has-text("Show more")',
        'button:has-text("See more")',
        "button.jobs-description__footer-button",
        ".show-more-less-html__button",
        'button[aria-label="Show more"]',
    ],
    # ── Pagination ─────────────────────────────────────────────────────
    "pagination": [
        ".artdeco-pagination",
        ".jobs-search-pagination",
        "ul.artdeco-pagination__pages",
    ],
    "next_page_btn": [
        'button[aria-label="Page {n}"]',  # template — replaced at runtime
        "li.artdeco-pagination__indicator--number button",
    ],
    # ── Popups / overlays ──────────────────────────────────────────────
    "close_popup": [
        'button[aria-label="Dismiss"]',
        'button[aria-label="Close"]',
        'button:has-text("Dismiss")',
        'button:has-text("Not now")',
        'button:has-text("Skip")',
        "button.msg-overlay-bubble-header__control--close",
        "button.artdeco-toast-item__dismiss",
        'button[data-test-modal-close-btn]',
        ".artdeco-modal__dismiss",
    ],
    "messaging_overlay_close": [
        'button[data-control-name="overlay.close_conversation_window"]',
        "button.msg-overlay-bubble-header__control--close",
        'header button[aria-label="Close your conversations"]',
    ],
    "cookie_accept": [
        'button:has-text("Accept")',
        'button:has-text("Accept & Close")',
        'button[action-type="ACCEPT"]',
        'button:has-text("Accept cookies")',
    ],
}

# Skills patterns for extraction
_SKILL_PATTERNS: List[str] = [
    r"\bJava\b(?!\s*Script)",
    r"\bPython\b",
    r"\bJavaScript\b",
    r"\bTypeScript\b",
    r"\bReact(?:\.js|JS)?\b",
    r"\bAngular(?:\.js|JS)?\b",
    r"\bVue(?:\.js|JS)?\b",
    r"\bNode(?:\.js|JS)?\b",
    r"\bExpress(?:\.js|JS)?\b",
    r"\bNuxt(?:\.js|JS)?\b",
    r"\bNext(?:\.js|JS)?\b",
    r"\bSpring\s*Boot\b",
    r"\bSpring\b",
    r"\bDjango\b",
    r"\bFlask\b",
    r"\bFastAPI\b",
    r"\bMySQL\b",
    r"\bPostgreSQL\b",
    r"\bMongoDB\b",
    r"\bRedis\b",
    r"\bDocker\b",
    r"\bKubernetes\b",
    r"\bAWS\b",
    r"\bAzure\b",
    r"\bGCP\b",
    r"\bGit\b",
    r"\bLinux\b",
    r"\bREST\s*API[s]?\b",
    r"\bGraphQL\b",
    r"\bKafka\b",
    r"\bRabbitMQ\b",
    r"\bElasticsearch\b",
    r"\bHTML5?\b",
    r"\bCSS3?\b",
    r"\bTailwind(?:\s*CSS)?\b",
    r"\bBootstrap\b",
    r"\bSQL\b",
    r"\bNoSQL\b",
    r"\bC\+\+\b",
    r"\bC#\b",
    r"\bGo(?:lang)?\b",
    r"\bRust\b",
    r"\bScala\b",
    r"\bKotlin\b",
    r"\bSwift\b",
    r"\bMicroservices?\b",
    r"\bCI\s*/\s*CD\b",
    r"\bJenkins\b",
    r"\bTerraform\b",
    r"\bAnsible\b",
    r"\bAgile\b",
    r"\bScrum\b",
    r"\bJIRA\b",
    r"\bConfluence\b",
    r"\bMachine\s*Learning\b",
    r"\bDeep\s*Learning\b",
    r"\bNLP\b",
    r"\bTensorFlow\b",
    r"\bPyTorch\b",
    r"\bWebSocket[s]?\b",
    r"\bJWT\b",
    r"\bOAuth\b",
    r"\bHibernate\b",
    r"\bJPA\b",
    r"\bMaven\b",
    r"\bGradle\b",
    r"\bSalesforce\b",
    r"\bApex\b",
    r"\bLWC\b",
    r"\bRazorpay\b",
    r"\bStripe\b",
    r"\bVuetify\b",
    r"\bSASS\b",
    r"\bLESS\b",
    r"\bWebpack\b",
    r"\bVite\b",
]


# ═══════════════════════════════════════════════════════════════════════════
# LinkedInPlatform
# ═══════════════════════════════════════════════════════════════════════════


class LinkedInPlatform(PlatformBase):
    """
    LinkedIn job-platform adapter — **SEARCH & SCRAPE ONLY**.

    This class will never click "Apply" or "Easy Apply".
    Jobs discovered here are applied-to via Naukri, Indeed, company
    career pages, or direct email outreach.

    Anti-ban strategy
    ─────────────────
    • Extra-long random delays (5-20 s between actions)
    • Session limited to ~20 minutes, then 15-30 min break
    • Max 100 pages / day across all queries
    • Human-like scrolling, idle pauses, hover behaviour
    • Persistent browser profile + saved cookies
    • No apply clicks — the single biggest ban trigger
    """

    PLATFORM = "linkedin"

    # ─── init ──────────────────────────────────────────────────────────

    def __init__(self, browser_engine, notifier=None):
        """
        Parameters
        ----------
        browser_engine : BrowserEngine
            Shared browser engine (from ``core/browser.py``).
        notifier : JobNotifier | None
            Telegram notifier (from ``tracking/notifications.py``).
        """
        super().__init__(browser_engine, notifier)
        self.platform_name: str = self.PLATFORM
        self.logger = get_logger("LinkedInPlatform")
        self.db = get_db()

        # Platform config
        cfg = PLATFORM_CONFIG.get("linkedin", {})
        self.max_daily: int = cfg.get("max_daily_applications", 0)  # always 0
        self.search_queries: List[str] = cfg.get(
            "search_queries",
            [
                "software developer",
                "full stack developer",
                "backend developer",
                "java developer",
                "SDE-1",
                "node.js developer",
                "python developer",
            ],
        )
        self.max_pages: int = cfg.get("max_pages_per_query", 3)

        # LinkedIn-specific safety
        self._max_pages_per_day: int = cfg.get("max_pages_per_day", 100)
        self._session_max_min: int = cfg.get("session_max_minutes", 20)
        self._session_break_min: Tuple[int, int] = tuple(
            cfg.get("session_break_minutes", [15, 30])
        )
        self._pages_today: int = 0
        self._session_start: Optional[datetime] = None

        # Runtime
        self.page = None
        self._delay_range: Tuple[float, float] = (5.0, 15.0)  # extra slow

        self.logger.info(
            "LinkedInPlatform initialised  (SEARCH ONLY, max %d pages/day, "
            "session %d min)",
            self._max_pages_per_day,
            self._session_max_min,
        )

    # ═══════════════════════════════════════════════════════════════════
    #  INTERNAL HELPERS
    # ═══════════════════════════════════════════════════════════════════

    async def _el(self, page, key: str, timeout: int = 5000):
        """Return first visible element matching any selector in ``_SEL[key]``."""
        for sel in _SEL.get(key, []):
            try:
                el = await page.wait_for_selector(sel, timeout=timeout, state="visible")
                if el:
                    return el
            except Exception:
                continue
        return None

    async def _els(self, page, key: str, timeout: int = 5000) -> List:
        """Return all elements for the first working selector."""
        for sel in _SEL.get(key, []):
            try:
                await page.wait_for_selector(sel, timeout=timeout, state="visible")
                els = await page.query_selector_all(sel)
                if els:
                    return els
            except Exception:
                continue
        return []

    @staticmethod
    async def _txt(el) -> str:
        if el is None:
            return ""
        try:
            t = await el.inner_text()
            return (t or "").strip()
        except Exception:
            try:
                t = await el.text_content()
                return (t or "").strip()
            except Exception:
                return ""

    @staticmethod
    async def _attr(el, name: str) -> str:
        if el is None:
            return ""
        try:
            v = await el.get_attribute(name)
            return (v or "").strip()
        except Exception:
            return ""

    async def _css_for(self, el) -> str:
        try:
            eid = await el.get_attribute("id")
            if eid:
                return f"#{eid}"
            dtid = await el.get_attribute("data-testid")
            if dtid:
                return f'[data-testid="{dtid}"]'
            tag = await el.evaluate("e=>e.tagName.toLowerCase()")
            name = await el.get_attribute("name")
            if name:
                return f'{tag}[name="{name}"]'
            cls = await el.get_attribute("class")
            if cls:
                first_cls = cls.split()[0]
                return f"{tag}.{first_cls}"
        except Exception:
            pass
        return "button:visible"

    # ── delays & scroll ────────────────────────────────────────────────

    async def _rand(self, lo: float = 3.0, hi: float = 8.0):
        """Async random sleep — LinkedIn gets EXTRA slow delays."""
        try:
            await self.browser_engine.random_delay(lo, hi)
        except Exception:
            await asyncio.sleep(random.uniform(lo, hi))

    async def _linkedin_delay(self):
        """Standard inter-action delay for LinkedIn (longer than other platforms)."""
        lo, hi = self._delay_range
        await self._rand(lo, hi)

    async def _human_scroll(self, page, rounds: int = 4):
        """Scroll page like a human reading job listings."""
        for _ in range(rounds):
            dy = random.randint(150, 500)
            await page.evaluate(f"window.scrollBy(0,{dy})")
            await self._rand(1.0, 3.0)
            # Occasionally pause (reading)
            if random.random() < 0.3:
                await self._rand(2.0, 5.0)

    async def _human_scroll_to_bottom(self, page):
        """Slowly scroll to the bottom (loads lazy content)."""
        for _ in range(random.randint(4, 8)):
            dy = random.randint(300, 700)
            await page.evaluate(f"window.scrollBy(0,{dy})")
            await self._rand(0.8, 2.5)
        # Scroll back up a bit (looks human)
        if random.random() < 0.4:
            await page.evaluate(f"window.scrollBy(0,-{random.randint(100, 300)})")
            await self._rand(0.5, 1.5)

    async def _screenshot(self, page, label: str) -> str:
        try:
            return await self.browser_engine.take_screenshot(page, label)
        except Exception:
            return ""

    # ── popup dismissal ────────────────────────────────────────────────

    async def _dismiss_popups(self, page):
        """Dismiss messaging overlays, cookie banners, modals."""
        # Messaging overlay — common annoyance
        for sel in _SEL.get("messaging_overlay_close", []):
            try:
                el = await page.wait_for_selector(sel, timeout=1000, state="visible")
                if el:
                    await el.click()
                    await self._rand(0.3, 1.0)
            except Exception:
                continue

        # Other popups
        targets = (
            _SEL.get("close_popup", [])
            + _SEL.get("cookie_accept", [])
        )
        for sel in targets:
            try:
                el = await page.wait_for_selector(sel, timeout=800, state="visible")
                if el:
                    await el.click()
                    await self._rand(0.3, 0.8)
            except Exception:
                continue

    # ── session management ─────────────────────────────────────────────

    def _check_session_limits(self) -> bool:
        """Return True if we can continue, False if session break needed."""
        if self._pages_today >= self._max_pages_per_day:
            self.logger.warning(
                "Daily page limit reached (%d/%d)",
                self._pages_today, self._max_pages_per_day,
            )
            return False

        if self._session_start:
            elapsed = (datetime.now() - self._session_start).total_seconds() / 60
            if elapsed >= self._session_max_min:
                self.logger.info(
                    "Session limit reached (%.0f min) — need break", elapsed
                )
                return False
        return True

    async def _session_break(self):
        """Take a session break to avoid detection."""
        lo, hi = self._session_break_min
        wait_min = random.uniform(lo, hi)
        self.logger.info("Taking %.1f min session break …", wait_min)
        await asyncio.sleep(wait_min * 60)
        self._session_start = datetime.now()
        self.logger.info("Session break complete — resuming")

    def _increment_page_count(self):
        self._pages_today += 1

    # ═══════════════════════════════════════════════════════════════════
    #  LOGIN
    # ═══════════════════════════════════════════════════════════════════

    async def login(self) -> bool:
        """
        Log in to LinkedIn.

        1. Launch browser with ``linkedin`` profile.
        2. Load saved cookies → navigate to feed.
        3. If logged in → done.
        4. Otherwise → auto-fill email + password → click Sign in.
        5. Handle 2FA / verification challenge via Telegram.
        6. Save cookies on success.
        """
        self.logger.info("═══ LinkedIn Login ═══")
        try:
            self.page = await self.browser_engine.launch("linkedin", headless=False)
            if not self.page:
                self.logger.error("Browser launch failed")
                return False

            # Load cookies and try feed
            await self.browser_engine.load_cookies("linkedin")
            await self.page.goto(_FEED_URL, wait_until="domcontentloaded", timeout=30_000)
            await self._rand(3, 5)
            await self._dismiss_popups(self.page)

            if await self._is_logged_in(self.page):
                self.logger.info("✅ LinkedIn: logged in via saved cookies")
                await self.browser_engine.save_cookies("linkedin")
                self._update_session(logged_in=True)
                self._session_start = datetime.now()
                return True

            # ── Auto-login with credentials ────────────────────────────
            self.logger.info("Not logged in — attempting credential login")
            await self.page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
            await self._rand(2, 4)
            await self._dismiss_popups(self.page)

            # Email / username
            email = os.getenv("LINKEDIN_EMAIL", "")
            password = os.getenv("LINKEDIN_PASSWORD", "")

            if not email or not password:
                self.logger.error("LINKEDIN_EMAIL / LINKEDIN_PASSWORD not set in .env")
                self._tg("⚠️ LinkedIn credentials not configured in .env")
                self._update_session(logged_in=False, error="Credentials missing")
                return False

            username_el = await self._el(self.page, "username_input", timeout=5000)
            if not username_el:
                self.logger.error("Username input not found on login page")
                await self._screenshot(self.page, "linkedin_no_username")
                self._update_session(logged_in=False, error="Login page changed")
                return False

            css_u = await self._css_for(username_el)
            await self.browser_engine.type_human(self.page, css_u, email)
            await self._rand(1.0, 2.5)

            password_el = await self._el(self.page, "password_input", timeout=3000)
            if not password_el:
                self.logger.error("Password input not found")
                self._update_session(logged_in=False, error="Password input missing")
                return False

            css_p = await self._css_for(password_el)
            await self.browser_engine.type_human(self.page, css_p, password)
            await self._rand(1.0, 2.0)

            # Click Sign in
            login_btn = await self._el(self.page, "login_button", timeout=3000)
            if login_btn:
                css_l = await self._css_for(login_btn)
                await self.browser_engine.click_human(self.page, css_l)
            else:
                # Fallback: press Enter
                await self.page.keyboard.press("Enter")

            await self._rand(4, 7)

            # ── Handle post-login scenarios ────────────────────────────
            # Scenario A: 2FA / verification pin
            if await self._handle_2fa():
                pass  # handled

            # Scenario B: Challenge page (email/phone verify)
            if await self._handle_challenge():
                pass  # handled

            # Scenario C: CAPTCHA
            cap = await self.detect_captcha(self.page)
            if cap:
                self.logger.warning("CAPTCHA on login: %s", cap)
                solved = await self.handle_captcha(self.page, self.notifier)
                if not solved:
                    self._update_session(logged_in=False, error="CAPTCHA unsolved",
                                         cooldown_h=2)
                    return False
                await self._rand(3, 5)

            # Final check
            await self._dismiss_popups(self.page)
            if await self._is_logged_in(self.page):
                self.logger.info("✅ LinkedIn: login successful")
                await self.browser_engine.save_cookies("linkedin")
                self._update_session(logged_in=True)
                self._session_start = datetime.now()
                self._tg("✅ LinkedIn login successful!")
                return True

            # Try navigating to feed explicitly
            await self.page.goto(_FEED_URL, wait_until="domcontentloaded", timeout=15_000)
            await self._rand(3, 5)
            if await self._is_logged_in(self.page):
                self.logger.info("✅ LinkedIn: login successful (after redirect)")
                await self.browser_engine.save_cookies("linkedin")
                self._update_session(logged_in=True)
                self._session_start = datetime.now()
                return True

            # Failed
            self.logger.error("❌ LinkedIn login failed")
            await self._screenshot(self.page, "linkedin_login_failed")
            self._update_session(logged_in=False, error="Login failed after all attempts",
                                 cooldown_h=1)
            return False

        except Exception as exc:
            self.logger.error("Login exception: %s", exc)
            self.db.save_error("linkedin.login", type(exc).__name__,
                               str(exc), tb_module.format_exc())
            self._update_session(logged_in=False, error=str(exc))
            return False

    # ── login sub-routines ─────────────────────────────────────────────

    async def _is_logged_in(self, page) -> bool:
        """Heuristic check for logged-in state."""
        # Positive indicators
        for sel in _SEL.get("nav_me", []):
            try:
                if await page.wait_for_selector(sel, timeout=2000):
                    return True
            except Exception:
                continue

        # URL-based check
        url = page.url
        if "/feed" in url and "/login" not in url:
            return True
        if "/mynetwork" in url or "/messaging" in url or "/jobs" in url:
            if "/login" not in url:
                return True

        # Negative indicators
        for sel in _SEL.get("sign_in_link", []):
            try:
                el = await page.wait_for_selector(sel, timeout=1500, state="visible")
                if el:
                    return False
            except Exception:
                continue

        if "/login" in url or "/authwall" in url or "/signup" in url:
            return False

        return False

    async def _handle_2fa(self) -> bool:
        """Handle 2FA / verification pin page."""
        pin_input = await self._el(self.page, "2fa_input", timeout=3000)
        if not pin_input:
            return False

        self.logger.info("2FA / verification page detected")
        self._tg("🔐 *LinkedIn 2FA Required*\nPlease provide the verification code.")

        # Ask via Telegram
        code = None
        if self.notifier:
            try:
                code = self.notifier.send_otp_request("linkedin")
            except Exception as exc:
                self.logger.error("Telegram OTP request failed: %s", exc)

        if not code:
            # Wait for user to enter manually in browser
            self.logger.info("Waiting 3 min for user to complete 2FA in browser …")
            for _ in range(36):
                await asyncio.sleep(5)
                if await self._is_logged_in(self.page):
                    return True
                # Check if still on 2FA page
                pin_still = await self._el(self.page, "2fa_input", timeout=1000)
                if not pin_still:
                    return True
            return False

        # Enter code
        css_pin = await self._css_for(pin_input)
        await self.browser_engine.type_human(self.page, css_pin, code)
        await self._rand(1.0, 2.0)

        submit_btn = await self._el(self.page, "2fa_submit", timeout=3000)
        if submit_btn:
            css_s = await self._css_for(submit_btn)
            await self.browser_engine.click_human(self.page, css_s)
        else:
            await self.page.keyboard.press("Enter")

        await self._rand(4, 7)
        return await self._is_logged_in(self.page)

    async def _handle_challenge(self) -> bool:
        """Handle LinkedIn security challenge (email/phone verification)."""
        challenge = await self._el(self.page, "challenge_page", timeout=2000)
        if not challenge:
            # Also check URL
            if "/checkpoint/" not in self.page.url and "/challenge/" not in self.page.url:
                return False

        self.logger.info("Security challenge detected")
        self._tg(
            "🛡 *LinkedIn Security Challenge*\n\n"
            "Please complete the verification in the browser window.\n"
            "⏱ Waiting up to 5 minutes."
        )

        for _ in range(60):
            await asyncio.sleep(5)
            if await self._is_logged_in(self.page):
                return True
            url = self.page.url
            if "/feed" in url or "/jobs" in url:
                return True
            if "/checkpoint/" not in url and "/challenge/" not in url and "/login" not in url:
                return True

        self.logger.error("Security challenge not resolved in time")
        return False

    # ── session DB helpers ─────────────────────────────────────────────

    def _update_session(self, *, logged_in: bool, error: str = None,
                        cooldown_h: float = 0):
        updates: Dict[str, Any] = {
            "logged_in": int(logged_in),
            "last_login": datetime.now().isoformat() if logged_in else None,
            "status": "active" if logged_in else ("cooldown" if cooldown_h else "active"),
            "last_error": error,
        }
        if cooldown_h:
            updates["cooldown_until"] = (
                datetime.now() + timedelta(hours=cooldown_h)
            ).isoformat()
        self.db.update_platform_session("linkedin", updates)

    def _tg(self, msg: str):
        if self.notifier:
            try:
                self.notifier.send_platform_issue("linkedin", msg)
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════════════
    #  SEARCH JOBS
    # ═══════════════════════════════════════════════════════════════════

    async def search_jobs(
        self,
        queries: List[str] | None = None,
        filters: Dict | None = None,
    ) -> List[Dict]:
        """
        Search LinkedIn jobs across queries × locations with pagination.

        Parameters
        ----------
        queries : list[str] | None
            Search keywords.  Falls back to config.
        filters : dict | None
            Optional keys:
            • ``locations`` (list[str]) — city names mapped to GeoIDs
            • ``date_posted`` (str) — "past_24h", "past_week", "past_month"
            • ``job_type`` (str) — "full_time", "contract", etc.
            • ``experience`` (str) — "entry_level", "associate", etc.
            • ``work_mode`` (str) — "remote", "onsite", "hybrid"
            • ``salary`` (str) — not supported by LinkedIn free search

        Returns
        -------
        list[dict]
            Unique jobs with: ``platform_job_id, url, title, company,
            location, salary_text, experience_text, description,
            posted_date, skills, work_mode, job_type, discovered_at``.
        """
        self.logger.info("═══ LinkedIn Search (SCRAPE ONLY) ═══")
        if not self.page:
            self.logger.error("Browser not ready — call login() first")
            return []

        if not await self._is_logged_in(self.page):
            self.logger.error("Not logged in — skipping search")
            return []

        queries = queries or self.search_queries
        filters = filters or {}
        locations: List[str] = filters.get(
            "locations",
            USER_PROFILE.get(
                "target_locations",
                ["Bangalore", "Hyderabad", "Pune", "Remote", "Delhi NCR", "Mumbai"],
            ),
        )
        date_posted: str = filters.get("date_posted", "past_week")
        job_type: str = filters.get("job_type", "full_time")
        experience: str = filters.get("experience", "entry_level")
        work_mode: str = filters.get("work_mode", "")

        all_jobs: List[Dict] = []

        for q in queries:
            if not self._check_session_limits():
                self.logger.info("Session / daily limit — taking break")
                await self._session_break()

            for loc in locations:
                if not self._check_session_limits():
                    await self._session_break()

                try:
                    self.logger.info("→ '%s' in '%s'", q, loc)
                    batch = await self._run_search(
                        q, loc, date_posted, job_type, experience, work_mode
                    )
                    self.logger.info("  found %d jobs", len(batch))
                    all_jobs.extend(batch)

                    # Extra long delay between query-location combos
                    await self._linkedin_delay()

                except Exception as exc:
                    self.logger.error("Search error '%s'/'%s': %s", q, loc, exc)
                    self.db.save_error("linkedin.search", type(exc).__name__,
                                       str(exc), tb_module.format_exc())
                    await self._rand(10, 20)  # extra cooldown on error

        # Deduplicate
        seen: set = set()
        unique: List[Dict] = []
        for j in all_jobs:
            jid = j.get("platform_job_id", "")
            if jid and jid not in seen:
                seen.add(jid)
                unique.append(j)

        self.logger.info("═══ LinkedIn Search Complete: %d unique jobs ═══", len(unique))
        return unique

    # ── single search runner ───────────────────────────────────────────

    async def _run_search(
        self,
        query: str,
        location: str,
        date_posted: str,
        job_type: str,
        experience: str,
        work_mode: str,
    ) -> List[Dict]:
        """Execute a single (query, location) search with pagination."""
        jobs: List[Dict] = []

        for page_num in range(self.max_pages):
            if not self._check_session_limits():
                break

            url = self._build_search_url(
                query, location, date_posted, job_type, experience, work_mode, page_num
            )
            self.logger.debug("  page %d → %s", page_num + 1, url)

            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await self._rand(3, 6)
                await self._dismiss_popups(self.page)

                # Check for login redirect
                if "/login" in self.page.url or "/authwall" in self.page.url:
                    self.logger.warning("Redirected to login — session expired?")
                    self._tg("⚠️ LinkedIn session expired during search")
                    break

                # CAPTCHA check
                cap = await self.detect_captcha(self.page)
                if cap:
                    self.logger.warning("CAPTCHA on search page")
                    solved = await self.handle_captcha(self.page, self.notifier)
                    if not solved:
                        self.logger.error("CAPTCHA unsolved — stopping search")
                        self._update_session(logged_in=True, error="CAPTCHA on search",
                                             cooldown_h=1)
                        break

                # Scroll to load lazy content
                await self._human_scroll_to_bottom(self.page)
                self._increment_page_count()

                # No results?
                no_res_sels = [
                    'h1:has-text("No matching jobs")',
                    'div:has-text("No results found")',
                    '.jobs-search-no-results-banner',
                ]
                no_res = False
                for sel in no_res_sels:
                    try:
                        el = await self.page.wait_for_selector(sel, timeout=1500)
                        if el:
                            no_res = True
                            break
                    except Exception:
                        continue
                if no_res:
                    self.logger.info("  no results for '%s' in '%s'", query, location)
                    break

                # Parse cards
                batch = await self._parse_job_cards(self.page, query, location)
                if not batch:
                    self.logger.debug("  0 cards — stopping pagination")
                    break
                jobs.extend(batch)

                # Check if more pages exist
                if page_num < self.max_pages - 1:
                    has_next = await self._has_next_page(self.page, page_num + 2)
                    if not has_next:
                        break

                # Delay between pages
                await self._linkedin_delay()

            except Exception as exc:
                self.logger.error("  page %d error: %s", page_num + 1, exc)
                break

        return jobs

    def _build_search_url(
        self,
        query: str,
        location: str,
        date_posted: str,
        job_type: str,
        experience: str,
        work_mode: str,
        page_num: int,
    ) -> str:
        """Build LinkedIn jobs search URL with filters."""
        params: Dict[str, str] = {
            "keywords": query,
            "location": location,
            "refresh": "true",
            "sortBy": "DD",  # Date Descending (most recent first)
        }

        # GeoID
        loc_lower = location.lower().strip()
        geo_id = _GEO_IDS.get(loc_lower, "")
        if geo_id:
            params["geoId"] = geo_id

        # Date posted
        tpr = _DATE_POSTED_MAP.get(date_posted, "")
        if tpr:
            params["f_TPR"] = tpr

        # Job type
        jt = _JOB_TYPE_MAP.get(job_type, "")
        if jt:
            params["f_JT"] = jt

        # Experience
        exp = _EXPERIENCE_MAP.get(experience, "")
        if exp:
            params["f_E"] = exp

        # Work mode
        wm = _WORK_MODE_MAP.get(work_mode, "")
        if wm:
            params["f_WT"] = wm

        # Pagination (LinkedIn uses start=25 * page_num)
        if page_num > 0:
            params["start"] = str(page_num * 25)

        return f"{_JOBS_URL}?{urlencode(params)}"

    async def _has_next_page(self, page, next_page_num: int) -> bool:
        """Check if the next page exists in pagination."""
        try:
            pagination = await self._el(page, "pagination", timeout=2000)
            if not pagination:
                return False
            # Look for page N button
            btn = await page.query_selector(
                f'button[aria-label="Page {next_page_num}"]'
            )
            if btn:
                return True
            # Alternative: any button with next page number
            btns = await page.query_selector_all(
                "li.artdeco-pagination__indicator--number button"
            )
            for b in btns:
                txt = await self._txt(b)
                if txt.strip() == str(next_page_num):
                    return True
        except Exception:
            pass
        return False

    # ── card parsing ───────────────────────────────────────────────────

    async def _parse_job_cards(self, page, query: str, location: str) -> List[Dict]:
        """Parse all job cards on the current search-results page."""
        cards = await self._els(page, "job_cards", timeout=8000)
        if not cards:
            # LinkedIn sometimes uses different containers — try broader
            try:
                cards = await page.query_selector_all(
                    "li[data-occludable-job-id], "
                    "div.job-card-container, "
                    "div.jobs-search-results__list-item"
                )
            except Exception:
                pass
        if not cards:
            self.logger.debug("  no job cards found")
            return []

        results: List[Dict] = []
        for card in cards:
            try:
                data = await self._extract_card(card, query, location)
                if data and data.get("platform_job_id"):
                    results.append(data)
            except Exception as exc:
                self.logger.debug("  card parse error: %s", exc)

            # Small delay between card processing (anti-pattern)
            if random.random() < 0.2:
                await self._rand(0.5, 1.5)

        return results

    async def _extract_card(self, card, query: str, location: str) -> Optional[Dict]:
        """Extract structured data from a single job card."""
        now_iso = datetime.now().isoformat()

        # ── Job ID ─────────────────────────────────────────────────────
        jid = ""
        # data-occludable-job-id is the most reliable
        jid = await self._attr(card, "data-occludable-job-id")
        if not jid:
            jid = await self._attr(card, "data-job-id")
        if not jid:
            jid = await self._attr(card, "data-entity-urn")
            if jid and ":" in jid:
                jid = jid.split(":")[-1]
        if not jid:
            # Try extracting from link href
            link_el = await card.query_selector("a[href*='/jobs/view/']")
            if link_el:
                href = await self._attr(link_el, "href")
                m = re.search(r"/jobs/view/(\d+)", href)
                if m:
                    jid = m.group(1)
        if not jid:
            return None

        # ── Title ──────────────────────────────────────────────────────
        title = ""
        for sel in _SEL["card_title"]:
            el = await card.query_selector(sel)
            if el:
                title = await self._txt(el)
                if title:
                    # Clean up: remove trailing noise
                    title = re.sub(r"\s+", " ", title).strip()
                    break
        if not title:
            return None

        # ── URL ────────────────────────────────────────────────────────
        url = f"{_BASE_URL}/jobs/view/{jid}/"
        for sel in _SEL["card_link"]:
            el = await card.query_selector(sel)
            if el:
                href = await self._attr(el, "href")
                if href:
                    if href.startswith("/"):
                        href = f"{_BASE_URL}{href}"
                    # Clean tracking params
                    href = re.sub(r"\?.*$", "", href)
                    url = href
                    break

        # ── Company ────────────────────────────────────────────────────
        company = ""
        for sel in _SEL["card_company"]:
            el = await card.query_selector(sel)
            if el:
                company = await self._txt(el)
                if company:
                    company = company.strip().split("\n")[0].strip()
                    break

        # ── Location ───────────────────────────────────────────────────
        loc = ""
        for sel in _SEL["card_location"]:
            el = await card.query_selector(sel)
            if el:
                loc = await self._txt(el)
                if loc:
                    loc = loc.strip().split("\n")[0].strip()
                    break

        # ── Posted date ────────────────────────────────────────────────
        posted = ""
        for sel in _SEL["card_date"]:
            el = await card.query_selector(sel)
            if el:
                posted = await self._txt(el)
                if not posted:
                    posted = await self._attr(el, "datetime")
                if posted:
                    break

        # ── Work mode ──────────────────────────────────────────────────
        card_text = await self._txt(card)
        card_lower = card_text.lower()
        if "remote" in card_lower:
            work_mode = "remote"
        elif "hybrid" in card_lower:
            work_mode = "hybrid"
        elif "on-site" in card_lower or "onsite" in card_lower:
            work_mode = "onsite"
        else:
            work_mode = "onsite"

        # ── Salary (rare in card, but sometimes present) ───────────────
        salary = ""
        salary_match = re.search(
            r"₹[\d,.\s]+(?:[-–to]+\s*₹?[\d,.\s]+)?(?:\s*(?:LPA|per\s*annum|a\s*year))?",
            card_text, re.IGNORECASE,
        )
        if salary_match:
            salary = salary_match.group(0).strip()

        # ── Experience hint ────────────────────────────────────────────
        exp_text = ""
        exp_match = re.search(r"(\d+)\s*[-–to]+\s*(\d+)\s*(?:years?|yrs?)", card_lower)
        if not exp_match:
            exp_match = re.search(r"(\d+)\+?\s*(?:years?|yrs?)", card_lower)
        if exp_match:
            exp_text = exp_match.group(0)

        # ── Skills from card snippet ───────────────────────────────────
        skills_found: List[str] = []
        for pat in _SKILL_PATTERNS:
            m = re.search(pat, card_text, re.IGNORECASE)
            if m:
                skills_found.append(m.group(0))

        return {
            "platform": "linkedin",
            "platform_job_id": jid,
            "url": url,
            "title": title,
            "company": company,
            "location": loc,
            "salary_text": salary,
            "experience_text": exp_text,
            "description": "",  # filled by get_job_details
            "posted_date": posted,
            "skills": list(set(skills_found)),
            "work_mode": work_mode,
            "job_type": "full-time",
            "discovered_at": now_iso,
            "search_query": query,
            "search_location": location,
        }

    # ═══════════════════════════════════════════════════════════════════
    #  GET JOB DETAILS
    # ═══════════════════════════════════════════════════════════════════

    async def get_job_details(self, job_url: str) -> Dict:
        """
        Navigate to a LinkedIn job page and extract full details.

        Parameters
        ----------
        job_url : str
            URL like ``https://www.linkedin.com/jobs/view/123456/``

        Returns
        -------
        dict
            ``title, company, location, salary_text, experience_text,
            description, skills, work_mode, job_type, posted_date,
            applicant_count, seniority_level, employment_type, industries,
            job_functions``.
        """
        self.logger.info("Fetching LinkedIn details: %s", job_url)
        if not self.page:
            self.logger.error("Browser not ready")
            return {}

        if not self._check_session_limits():
            self.logger.info("Session limit — taking break before detail fetch")
            await self._session_break()

        try:
            await self.page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
            await self._rand(3, 6)
            await self._dismiss_popups(self.page)

            # Check login redirect
            if "/login" in self.page.url or "/authwall" in self.page.url:
                self.logger.warning("Redirected to login while fetching details")
                return {}

            # CAPTCHA
            cap = await self.detect_captcha(self.page)
            if cap:
                solved = await self.handle_captcha(self.page, self.notifier)
                if not solved:
                    return {}

            # Scroll to load content
            await self._human_scroll(self.page, rounds=random.randint(2, 4))
            self._increment_page_count()

            # Click "Show more" on description if present
            show_more = await self._el(self.page, "show_more_btn", timeout=3000)
            if show_more:
                try:
                    css_sm = await self._css_for(show_more)
                    await self.browser_engine.click_human(self.page, css_sm)
                    await self._rand(1.0, 2.5)
                except Exception:
                    pass

            result: Dict[str, Any] = {"url": job_url}

            # ── Title ──────────────────────────────────────────────────
            el = await self._el(self.page, "detail_title", timeout=5000)
            result["title"] = await self._txt(el)

            # ── Company ────────────────────────────────────────────────
            el = await self._el(self.page, "detail_company", timeout=3000)
            company = await self._txt(el)
            # Clean: sometimes includes "· followers"
            company = re.split(r"\s*[·•]\s*", company)[0].strip()
            result["company"] = company

            # ── Location ───────────────────────────────────────────────
            el = await self._el(self.page, "detail_location", timeout=3000)
            loc = await self._txt(el)
            loc = loc.strip().split("\n")[0].strip()
            result["location"] = loc

            # ── Work mode ──────────────────────────────────────────────
            wm_el = await self._el(self.page, "detail_work_mode", timeout=2000)
            wm_text = await self._txt(wm_el)
            wm_lower = wm_text.lower()
            if "remote" in wm_lower:
                result["work_mode"] = "remote"
            elif "hybrid" in wm_lower:
                result["work_mode"] = "hybrid"
            elif "on-site" in wm_lower or "onsite" in wm_lower:
                result["work_mode"] = "onsite"
            else:
                # Fallback from location
                if "remote" in loc.lower():
                    result["work_mode"] = "remote"
                else:
                    result["work_mode"] = "onsite"

            # ── Salary ─────────────────────────────────────────────────
            salary = ""
            for sel in _SEL["detail_salary"]:
                try:
                    el = await self.page.wait_for_selector(sel, timeout=2000)
                    if el:
                        s = await self._txt(el)
                        if s and ("₹" in s or "lpa" in s.lower() or "inr" in s.lower()
                                  or "per" in s.lower() or "$" in s):
                            salary = s.strip()
                            break
                except Exception:
                    continue
            result["salary_text"] = salary

            # ── Posted date ────────────────────────────────────────────
            posted = ""
            for sel in _SEL["detail_posted"]:
                try:
                    el = await self.page.wait_for_selector(sel, timeout=2000)
                    if el:
                        posted = await self._txt(el)
                        if posted:
                            break
                except Exception:
                    continue
            result["posted_date"] = posted

            # ── Applicant count ────────────────────────────────────────
            app_count = ""
            for sel in _SEL["detail_applicants"]:
                try:
                    el = await self.page.wait_for_selector(sel, timeout=2000)
                    if el:
                        app_count = await self._txt(el)
                        if app_count:
                            break
                except Exception:
                    continue
            result["applicant_count"] = app_count

            # ── Full description ───────────────────────────────────────
            description = ""
            desc_el = await self._el(self.page, "detail_description", timeout=6000)
            if desc_el:
                description = await self._txt(desc_el)
            result["description"] = description

            # ── Job criteria (seniority, type, function, industries) ───
            criteria = await self._extract_criteria(self.page)
            result.update(criteria)

            # ── Skills from description ────────────────────────────────
            all_text = f"{result.get('title', '')} {description}"
            skills: List[str] = []
            for pat in _SKILL_PATTERNS:
                m = re.search(pat, all_text, re.IGNORECASE)
                if m:
                    skills.append(m.group(0))
            result["skills"] = list(set(skills))

            # ── Experience from description ────────────────────────────
            exp_text = ""
            exp_m = re.search(
                r"(\d+)\s*[-–to]+\s*(\d+)\s*(?:years?|yrs?)", description, re.IGNORECASE
            )
            if not exp_m:
                exp_m = re.search(r"(\d+)\+?\s*(?:years?|yrs?)", description, re.IGNORECASE)
            if exp_m:
                exp_text = exp_m.group(0)
            result["experience_text"] = exp_text

            result["job_type"] = criteria.get("employment_type", "full-time")

            self.logger.info(
                "  ✅ '%s' @ '%s' — %s — %d skills — %s",
                result.get("title", "?"),
                result.get("company", "?"),
                result.get("work_mode", "?"),
                len(result.get("skills", [])),
                result.get("applicant_count", "? applicants"),
            )

            # Human-like delay after reading a job
            await self._linkedin_delay()

            return result

        except Exception as exc:
            self.logger.error("get_job_details error: %s", exc)
            self.db.save_error("linkedin.get_job_details", type(exc).__name__,
                               str(exc), tb_module.format_exc())
            return {}

    async def _extract_criteria(self, page) -> Dict[str, str]:
        """Extract seniority level, employment type, job function, industries."""
        result: Dict[str, str] = {
            "seniority_level": "",
            "employment_type": "",
            "job_functions": "",
            "industries": "",
        }

        try:
            criteria_els = await self._els(page, "detail_criteria", timeout=3000)
            for el in criteria_els:
                text = await self._txt(el)
                text_lower = text.lower()

                if "seniority" in text_lower:
                    # Extract value after header
                    lines = text.strip().split("\n")
                    if len(lines) >= 2:
                        result["seniority_level"] = lines[-1].strip()
                elif "employment type" in text_lower or "job type" in text_lower:
                    lines = text.strip().split("\n")
                    if len(lines) >= 2:
                        result["employment_type"] = lines[-1].strip()
                elif "function" in text_lower:
                    lines = text.strip().split("\n")
                    if len(lines) >= 2:
                        result["job_functions"] = lines[-1].strip()
                elif "industr" in text_lower:
                    lines = text.strip().split("\n")
                    if len(lines) >= 2:
                        result["industries"] = lines[-1].strip()
        except Exception:
            pass

        # Alternative: try structured job-criteria list
        try:
            items = await page.query_selector_all("li.description__job-criteria-item")
            for item in items:
                header_el = await item.query_selector(
                    ".description__job-criteria-subheader"
                )
                value_el = await item.query_selector(
                    ".description__job-criteria-text"
                )
                if header_el and value_el:
                    header = (await self._txt(header_el)).lower()
                    value = (await self._txt(value_el)).strip()
                    if "seniority" in header:
                        result["seniority_level"] = value
                    elif "employment" in header or "type" in header:
                        result["employment_type"] = value
                    elif "function" in header:
                        result["job_functions"] = value
                    elif "industr" in header:
                        result["industries"] = value
        except Exception:
            pass

        return result

    # ═══════════════════════════════════════════════════════════════════
    #  APPLY — NOT SUPPORTED (raises NotImplementedError)
    # ═══════════════════════════════════════════════════════════════════

    async def prepare_application(
        self,
        job: Dict,
        resume_path: str,
        cover_letter: Optional[str] = None,
    ) -> Dict:
        """
        ❌ **NOT SUPPORTED** — LinkedIn bans auto-apply.

        Jobs discovered on LinkedIn should be applied-to via:
        • Naukri / Indeed / Foundit (if listed there too)
        • Company career page
        • Direct email to HR

        Raises
        ------
        NotImplementedError
            Always.
        """
        raise NotImplementedError(
            "LinkedIn auto-apply is NOT supported — guaranteed ban risk.  "
            "Use Naukri / Indeed / email outreach instead."
        )

    async def submit_application(self, prepared: Dict) -> Dict:
        """
        ❌ **NOT SUPPORTED** — see ``prepare_application``.

        Raises
        ------
        NotImplementedError
            Always.
        """
        raise NotImplementedError(
            "LinkedIn auto-apply is NOT supported — guaranteed ban risk."
        )

    # ═══════════════════════════════════════════════════════════════════
    #  CHECK STATUS — limited (no apply = nothing to check)
    # ═══════════════════════════════════════════════════════════════════

    async def check_status(self, application_id: int = None) -> str:
        """
        LinkedIn doesn't track status since we don't apply here.
        Returns "not_applicable" always.
        """
        return "not_applicable"

    # ═══════════════════════════════════════════════════════════════════
    #  CAPTCHA / CHALLENGE DETECTION
    # ═══════════════════════════════════════════════════════════════════

    async def detect_captcha(self, page) -> Optional[str]:
        """Detect LinkedIn CAPTCHA / security challenge."""
        try:
            url = page.url.lower()
            html = ""
            try:
                html = (await page.content()).lower()
            except Exception:
                pass

            # URL-based
            if "/checkpoint/" in url or "/challenge/" in url:
                return "challenge"

            # reCAPTCHA
            recaptcha_sels = [
                'iframe[src*="captcha"]',
                'iframe[src*="recaptcha"]',
                ".g-recaptcha",
                "#recaptcha",
            ]
            for sel in recaptcha_sels:
                try:
                    el = await page.wait_for_selector(sel, timeout=1000)
                    if el:
                        return "recaptcha"
                except Exception:
                    continue

            # LinkedIn's own "security check"
            security_markers = [
                "let's do a quick security check",
                "security verification",
                "verify you're not a robot",
                "verify it's you",
                "unusual activity",
                "we've restricted your account",
                "account restricted",
            ]
            for marker in security_markers:
                if marker in html:
                    return "security_check"

            # Arkose Labs (FunCaptcha) — LinkedIn sometimes uses this
            arkose_sels = [
                'iframe[src*="arkoselabs"]',
                'iframe[src*="funcaptcha"]',
                "#captcha-internal",
            ]
            for sel in arkose_sels:
                try:
                    el = await page.wait_for_selector(sel, timeout=1000)
                    if el:
                        return "funcaptcha"
                except Exception:
                    continue

        except Exception:
            pass
        return None

    async def handle_captcha(self, page, notifier=None) -> bool:
        """
        Handle LinkedIn CAPTCHA / security challenge.

        Strategy:
        - Screenshot + Telegram alert
        - Wait for user to solve in browser (up to 5 min)
        - Auto-continue if challenge disappears
        """
        cap_type = await self.detect_captcha(page)
        if not cap_type:
            return True

        self.logger.warning("Handling LinkedIn challenge: %s", cap_type)
        notifier = notifier or self.notifier

        # Screenshot for debug / Telegram
        ss = await self._screenshot(page, f"linkedin_{cap_type}")

        # Alert user
        if notifier:
            try:
                msg = (
                    f"🛡 *LinkedIn Security Challenge*\n"
                    f"Type: `{cap_type}`\n\n"
                    f"Please solve in the open browser window.\n"
                    f"⏱ Waiting up to 5 minutes."
                )
                notifier.send_platform_issue("linkedin", msg)
            except Exception:
                pass

        # Wait for resolution
        for _ in range(60):  # 5 min = 60 × 5s
            await asyncio.sleep(5)
            new_cap = await self.detect_captcha(page)
            if not new_cap:
                self.logger.info("  ✅ Challenge resolved")
                return True
            # Check if we're back on a normal page
            url = page.url
            if "/feed" in url or "/jobs" in url:
                return True

        self.logger.error("  ❌ Challenge not resolved in 5 minutes")
        self._update_session(logged_in=True, error=f"Challenge ({cap_type}) unresolved",
                             cooldown_h=2)
        return False

    async def detect_otp_page(self, page) -> bool:
        """Check if current page is 2FA / OTP."""
        pin_input = await self._el(page, "2fa_input", timeout=1500)
        return pin_input is not None

    async def handle_otp(self, page, notifier=None) -> bool:
        """Delegate to _handle_2fa (same flow)."""
        return await self._handle_2fa()

    # ═══════════════════════════════════════════════════════════════════
    #  EXTRA: SCRAPE COMPANY INFO (optional enrichment)
    # ═══════════════════════════════════════════════════════════════════

    async def get_company_info(self, company_url: str) -> Dict:
        """
        Optionally scrape basic company info (size, industry, website).

        Parameters
        ----------
        company_url : str
            LinkedIn company page URL.

        Returns
        -------
        dict
            ``{name, industry, size, website, description, followers}``.
        """
        self.logger.info("Fetching company info: %s", company_url)
        if not self.page:
            return {}

        if not self._check_session_limits():
            return {}

        try:
            # Navigate to company /about page
            about_url = company_url.rstrip("/") + "/about/"
            await self.page.goto(about_url, wait_until="domcontentloaded", timeout=30_000)
            await self._rand(3, 6)
            await self._dismiss_popups(self.page)
            await self._human_scroll(self.page, rounds=2)
            self._increment_page_count()

            if "/login" in self.page.url or "/authwall" in self.page.url:
                return {}

            result: Dict[str, str] = {"url": company_url}

            # Company name
            name_el = await self.page.query_selector("h1")
            if name_el:
                result["name"] = await self._txt(name_el)

            # About section — parse key-value pairs
            try:
                # LinkedIn uses dt/dd or definition list pattern
                dds = await self.page.query_selector_all(
                    "dl.overflow-hidden dt, dl.overflow-hidden dd"
                )
                key = ""
                for dd in dds:
                    tag = await dd.evaluate("e=>e.tagName.toLowerCase()")
                    txt = await self._txt(dd)
                    if tag == "dt":
                        key = txt.lower()
                    elif tag == "dd" and key:
                        if "industry" in key:
                            result["industry"] = txt
                        elif "company size" in key or "employees" in key:
                            result["size"] = txt
                        elif "website" in key:
                            result["website"] = txt
                        elif "headquarters" in key:
                            result["headquarters"] = txt
                        elif "founded" in key:
                            result["founded"] = txt
                        elif "type" in key:
                            result["company_type"] = txt
                        key = ""
            except Exception:
                pass

            # Description
            try:
                desc_el = await self.page.query_selector(
                    "section.org-about-module__margin-bottom p"
                )
                if not desc_el:
                    desc_el = await self.page.query_selector(
                        ".org-top-card-summary__info-text"
                    )
                if desc_el:
                    result["description"] = await self._txt(desc_el)
            except Exception:
                pass

            # Followers
            try:
                follow_sels = [
                    ".org-top-card-summary__follower-count",
                    'span:has-text("followers")',
                ]
                for sel in follow_sels:
                    el = await self.page.query_selector(sel)
                    if el:
                        result["followers"] = await self._txt(el)
                        break
            except Exception:
                pass

            self.logger.info("  ✅ Company: %s — %s — %s",
                             result.get("name", "?"),
                             result.get("industry", "?"),
                             result.get("size", "?"))

            await self._linkedin_delay()
            return result

        except Exception as exc:
            self.logger.error("get_company_info error: %s", exc)
            return {}

    # ═══════════════════════════════════════════════════════════════════
    #  CLEANUP
    # ═══════════════════════════════════════════════════════════════════

    async def close(self):
        """Save cookies and close browser."""
        try:
            if self.page:
                await self.browser_engine.save_cookies("linkedin")
                self.logger.info("Cookies saved for linkedin")
        except Exception as exc:
            self.logger.warning("Error saving cookies: %s", exc)

        try:
            await self.browser_engine.close("linkedin")
        except Exception:
            pass

        self.page = None
        self.logger.info(
            "LinkedInPlatform closed  (pages today: %d)", self._pages_today
        )


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN — standalone test
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Smoke test:
        python platforms/linkedin.py

    Tests login → search → fetch details for first result.
    Requires .env with LINKEDIN_EMAIL, LINKEDIN_PASSWORD.
    """
    import sys

    async def _test():
        from core.browser import BrowserEngine

        notifier = None
        try:
            from tracking.notifications import JobNotifier
            notifier = JobNotifier()
        except Exception:
            print("[WARN] Notifier not available — running without Telegram")

        engine = BrowserEngine()
        li = LinkedInPlatform(engine, notifier)

        print("\n══════════════════════════════════════════")
        print("  LinkedIn Platform — Smoke Test")
        print("  ⚠  SEARCH & SCRAPE ONLY — NO APPLY")
        print("══════════════════════════════════════════\n")

        # ── Login ──────────────────────────────────────────────────────
        print("[1/4] Logging in …")
        ok = await li.login()
        if not ok:
            print("❌ Login failed. Exiting.")
            await li.close()
            return

        # ── Search ─────────────────────────────────────────────────────
        print("\n[2/4] Searching (this will be slow — anti-ban) …")
        jobs = await li.search_jobs(
            queries=["full stack developer"],
            filters={
                "locations": ["Bangalore"],
                "date_posted": "past_week",
                "experience": "entry_level",
            },
        )
        print(f"  → Found {len(jobs)} unique jobs")

        if jobs:
            print("\n  Top 5:")
            for j in jobs[:5]:
                print(f"     • {j['title']} @ {j['company']} — {j['location']}")
                print(f"       ID: {j['platform_job_id']}  |  {j.get('posted_date', '?')}")

            # ── Details ────────────────────────────────────────────────
            print(f"\n[3/4] Fetching details for first job …")
            details = await li.get_job_details(jobs[0]["url"])
            if details:
                print(f"  Title      : {details.get('title')}")
                print(f"  Company    : {details.get('company')}")
                print(f"  Location   : {details.get('location')}")
                print(f"  Work Mode  : {details.get('work_mode')}")
                print(f"  Salary     : {details.get('salary_text') or 'Not listed'}")
                print(f"  Posted     : {details.get('posted_date')}")
                print(f"  Applicants : {details.get('applicant_count')}")
                print(f"  Seniority  : {details.get('seniority_level')}")
                print(f"  Type       : {details.get('employment_type')}")
                print(f"  Skills     : {', '.join(details.get('skills', []))}")
                desc = details.get("description", "")
                print(f"  JD         : {desc[:300]}…" if len(desc) > 300 else f"  JD: {desc}")
            else:
                print("  ⚠ Could not fetch details")
        else:
            print("  No jobs found — try different queries / locations")

        # ── Verify apply is blocked ────────────────────────────────────
        print("\n[4/4] Verifying apply is blocked …")
        try:
            await li.prepare_application({"title": "test"}, "/tmp/test.pdf")
            print("  ❌ ERROR: apply should have raised NotImplementedError!")
        except NotImplementedError as e:
            print(f"  ✅ Apply correctly blocked: {e}")

        # ── Cleanup ────────────────────────────────────────────────────
        print("\nCleaning up …")
        await li.close()
        print("\n✅ Test complete.\n")

    try:
        asyncio.run(_test())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)