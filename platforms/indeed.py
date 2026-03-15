#!/usr/bin/env python3
"""
platforms/indeed.py — Indeed Platform Integration (SYNCHRONOUS)

Auth: Cookie-based ONLY.  No password stored.
  • First run → visible browser + Telegram alert → user logs in via Google /
    magic-link → cookies saved.
  • Subsequent runs → saved cookies are loaded automatically.
  • Re-auth typically needed every 2-4 weeks.

Capabilities:
  • Search jobs across queries × locations with pagination
  • Extract full job details (JD, skills, salary, experience)
  • Semi-auto Indeed Apply: fill form → pause → Telegram approve → submit
  • Resume upload (file input OR file-chooser dialog OR "resume on file")
  • Multi-step form traversal with smart field detection and answering
  • CAPTCHA detection + Telegram-assisted solving
  • OTP handling via Telegram
  • Rate limiting: 20 apps/day, 3+ min gap, cooldown on issues
  • Full anti-ban: human-like delays, scrolling, typing, mouse movement
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
    BASE_DIR,
    BROWSER_PROFILES_DIR,
    CACHE_DIR,
    PLATFORM_CONFIG,
    STEALTH_CONFIG,
    USER_PROFILE,
)

# Optional — gracefully degrade if profile/answers isn't built yet
try:
    from profile.answers import get_answer, get_salary_answer, get_standard
except ImportError:
    get_answer = get_salary_answer = get_standard = None

logger = get_logger("platforms.indeed")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://in.indeed.com"
_SEARCH_URL = f"{_BASE_URL}/jobs"

_SEL: Dict[str, List[str]] = {
    # ── Search page ────────────────────────────────────────────────────
    "search_input": [
        "input#text-input-what",
        'input[name="q"]',
        "#what",
        'input[aria-label*="what"]',
        'input[placeholder*="job title"]',
    ],
    "location_input": [
        "input#text-input-where",
        'input[name="l"]',
        "#where",
        'input[aria-label*="where"]',
        'input[placeholder*="location"]',
    ],
    "search_button": [
        'button[type="submit"]',
        "button.yosegi-InlineWhatWhere-primaryButton",
        'button:has-text("Find jobs")',
        'button:has-text("Search")',
    ],
    # ── Job cards ──────────────────────────────────────────────────────
    "job_cards": [
        ".job_seen_beacon",
        ".jobsearch-ResultsList > li",
        '[data-testid="slider_item"]',
        ".resultContent",
        "div.cardOutline",
        "li.css-5lfssm",
        "td.resultContent",
    ],
    "job_title": [
        "h2.jobTitle a",
        "h2.jobTitle span",
        '[data-testid="jobTitle"]',
        ".jobTitle > a",
        ".jcs-JobTitle",
        "a[data-jk]",
    ],
    "company_name": [
        '[data-testid="company-name"]',
        ".companyName",
        "span.css-92r8pb",
        ".company_location .companyName",
        'span[data-testid="company-name"]',
    ],
    "location": [
        '[data-testid="text-location"]',
        ".companyLocation",
        "div.css-1p0sjhy",
        ".company_location .companyLocation",
    ],
    "salary": [
        ".salary-snippet-container",
        '[data-testid="attribute_snippet_testid"]',
        ".salaryText",
        ".metadata.salary-snippet-container",
        "div.css-1cvvo1b",
    ],
    "date_posted": [
        ".date",
        "span.css-qvloho",
        '[data-testid="myJobsStateDate"]',
        ".result-footer .date",
    ],
    "job_snippet": [
        ".job-snippet",
        ".underShelfFooter",
        'div[class*="job-snippet"]',
        "table.jobCardShelfContainer",
    ],
    # ── Job detail page ────────────────────────────────────────────────
    "job_description": [
        "#jobDescriptionText",
        ".jobsearch-jobDescriptionText",
        '[data-testid="jobDescriptionText"]',
        "#jobDescription",
        ".jobsearch-JobComponent-description",
    ],
    "detail_title": [
        "h1.jobsearch-JobInfoHeader-title",
        'h1[data-testid="jobsearch-JobInfoHeader-title"]',
        "h2.jobsearch-JobInfoHeader-title",
        "h1.icl-u-xs-mb--xs",
        ".jobsearch-JobInfoHeader-title",
        "h1",
    ],
    "detail_company": [
        '[data-testid="inlineHeader-companyName"] a',
        '[data-testid="inlineHeader-companyName"]',
        ".jobsearch-InlineCompanyRating a",
        ".jobsearch-InlineCompanyRating",
        'div[data-company-name="true"] a',
        ".css-1saizt3 a",
    ],
    "detail_location": [
        '[data-testid="inlineHeader-companyLocation"]',
        ".jobsearch-JobInfoHeader-subtitle > div:last-child",
        ".css-6z8o9s",
        'div[data-testid="job-location"]',
    ],
    "detail_salary": [
        "#salaryInfoAndJobType",
        '[data-testid="attribute_snippet_testid"]',
        ".jobsearch-JobMetadataHeader-item",
        ".salary-snippet-container",
        "span.css-19j1a75",
    ],
    "detail_date": [
        ".jobsearch-HiringInsights-entry--bullet",
        'span:has-text("Posted")',
        'span:has-text("days ago")',
        'span:has-text("Just posted")',
        'span:has-text("Today")',
    ],
    # ── Apply button ───────────────────────────────────────────────────
    "apply_button": [
        "button#indeedApplyButton",
        "button.indeed-apply-button",
        'button:has-text("Apply now")',
        'button:has-text("Apply on Indeed")',
        'a:has-text("Apply now")',
        "#applyButtonLinkContainer button",
        'button[data-testid="indeedApplyButton"]',
    ],
    "external_apply": [
        'button:has-text("Apply on company site")',
        'a:has-text("Apply on company site")',
    ],
    # ── Apply form ─────────────────────────────────────────────────────
    "resume_upload": [
        'input[type="file"]',
        'input[accept*="pdf"]',
        'input[accept*="doc"]',
        "#resume-upload",
        'input[name="resume"]',
    ],
    "continue_button": [
        'button:has-text("Continue")',
        'button:has-text("Next")',
        'button[data-testid="continue-button"]',
        "button.ia-continueButton",
        "#ia-continueButton",
    ],
    "submit_button": [
        'button:has-text("Submit your application")',
        'button:has-text("Submit application")',
        'button:has-text("Submit")',
        'button[data-testid="submit-button"]',
        "button.ia-submitButton",
    ],
    "review_button": [
        'button:has-text("Review")',
        'button:has-text("Review your application")',
    ],
    # ── Pagination ─────────────────────────────────────────────────────
    "next_page": [
        'a[data-testid="pagination-page-next"]',
        'a[aria-label="Next Page"]',
        'a:has-text("Next")',
        "nav a:last-child",
    ],
    # ── Auth / Misc ────────────────────────────────────────────────────
    "sign_in_prompt": [
        'a:has-text("Sign in")',
        'button:has-text("Sign in")',
        '[data-testid="sign-in"]',
    ],
    "email_input": [
        "input#ifl-InputFormField-3",
        'input[type="email"]',
        'input[name="__email"]',
        "#email-input",
    ],
    "captcha_frame": [
        'iframe[src*="captcha"]',
        'iframe[src*="recaptcha"]',
        "#recaptcha",
        ".g-recaptcha",
    ],
    "close_popup": [
        'button[aria-label="Close"]',
        "button.icl-CloseButton",
        '[data-testid="close-button"]',
        "button.popover-x-button-close",
    ],
    "cookie_accept": [
        "button#onetrust-accept-btn-handler",
        'button:has-text("Accept")',
        'button:has-text("Accept all")',
        'button:has-text("I accept")',
    ],
}

# Skills regex patterns for extraction from JD text
_SKILL_PATTERNS: List[str] = [
    r"\bJava\b", r"\bPython\b", r"\bJavaScript\b", r"\bTypeScript\b",
    r"\bReact(?:\.js|JS)?\b", r"\bAngular(?:\.js|JS)?\b",
    r"\bVue(?:\.js|JS)?\b", r"\bNode(?:\.js|JS)?\b",
    r"\bExpress(?:\.js|JS)?\b", r"\bNuxt(?:\.js|JS)?\b",
    r"\bNext(?:\.js|JS)?\b", r"\bSpring\s*Boot\b", r"\bSpring\b",
    r"\bDjango\b", r"\bFlask\b", r"\bFastAPI\b",
    r"\bMySQL\b", r"\bPostgreSQL\b", r"\bMongoDB\b", r"\bRedis\b",
    r"\bDocker\b", r"\bKubernetes\b", r"\bAWS\b", r"\bAzure\b",
    r"\bGCP\b", r"\bGit\b", r"\bLinux\b",
    r"\bREST\s*API[s]?\b", r"\bGraphQL\b", r"\bKafka\b",
    r"\bRabbitMQ\b", r"\bElasticsearch\b",
    r"\bHTML\b", r"\bCSS\b", r"\bTailwind\b", r"\bBootstrap\b",
    r"\bSQL\b", r"\bNoSQL\b", r"\bC\+\+\b", r"\bC#\b",
    r"\bGo(?:lang)?\b", r"\bRust\b", r"\bScala\b", r"\bKotlin\b",
    r"\bSwift\b", r"\bMicroservices?\b", r"\bCI/CD\b",
    r"\bJenkins\b", r"\bTerraform\b", r"\bAnsible\b",
    r"\bAgile\b", r"\bScrum\b", r"\bJIRA\b", r"\bConfluence\b",
    r"\bMachine\s*Learning\b", r"\bDeep\s*Learning\b", r"\bNLP\b",
    r"\bTensorFlow\b", r"\bPyTorch\b",
    r"\bWebSocket[s]?\b", r"\bJWT\b", r"\bOAuth\b",
    r"\bHibernate\b", r"\bJPA\b", r"\bMaven\b", r"\bGradle\b",
    r"\bRazorpay\b", r"\bStripe\b",
]


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _find(page, key: str, timeout: int = 5000):
    """Return first visible element matching any selector in _SEL[key]."""
    for sel in _SEL.get(key, []):
        try:
            el = page.wait_for_selector(sel, timeout=timeout, state="visible")
            if el:
                return el
        except Exception:
            continue
    return None


def _find_all(page, key: str, timeout: int = 5000) -> list:
    """Return all elements for first working selector in _SEL[key]."""
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
    """Safely extract trimmed text from an element."""
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
    """Safely get an attribute value."""
    if el is None:
        return ""
    try:
        v = el.get_attribute(name)
        return (v or "").strip()
    except Exception:
        return ""


def _css_for(el) -> str:
    """Best-effort CSS selector for an element."""
    try:
        eid = el.get_attribute("id")
        if eid:
            return f"#{eid}"
        dtid = el.get_attribute("data-testid")
        if dtid:
            return f'[data-testid="{dtid}"]'
        tag = el.evaluate("e=>e.tagName.toLowerCase()")
        name = el.get_attribute("name")
        if name:
            return f'{tag}[name="{name}"]'
        txt = _txt(el)
        if txt and len(txt) < 40:
            safe = txt.replace('"', '\\"')[:35]
            return f'{tag}:has-text("{safe}")'
        cls = el.get_attribute("class")
        if cls:
            return f"{tag}.{cls.split()[0]}"
    except Exception:
        pass
    return "button:visible"


def _extract_skills(text: str) -> List[str]:
    """Extract skill names from text using regex patterns."""
    found = []
    for pat in _SKILL_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            found.append(m.group(0))
    return list(set(found))


# ═══════════════════════════════════════════════════════════════════════════
# IndeedPlatform
# ═══════════════════════════════════════════════════════════════════════════

class IndeedPlatform(PlatformBase):
    """
    Indeed job-platform adapter (SYNCHRONOUS — matches browser.py).

    Authentication: Cookie-based only — no password stored.
    Limits: 20 applications/day, ≥3 min gap, auto-cooldown on issues.
    """

    PLATFORM_NAME = "indeed"

    def __init__(self, browser_engine, notifier=None):
        super().__init__(browser_engine)
        self.notifier = notifier
        self.platform_name = self.PLATFORM_NAME

        # Ensure self.browser exists
        if not hasattr(self, 'browser'):
            self.browser = browser_engine

        self.db = get_db()

        # Platform config
        cfg = PLATFORM_CONFIG.get("indeed", {})
        self.max_daily: int = cfg.get("max_daily_applications", 20)
        _rl = cfg.get("rate_limit_seconds", {"min": 180, "max": 360})
        if isinstance(_rl, dict):
            self._gap_min = _rl.get("min", 180)
            self._gap_max = _rl.get("max", 360)
        elif isinstance(_rl, (int, float)):
            self._gap_min = int(_rl)
            self._gap_max = int(_rl) + 120
        else:
            self._gap_min, self._gap_max = 180, 360

        self.search_queries: List[str] = cfg.get("search_queries", [
            "software developer", "full stack developer",
            "backend developer", "java developer",
            "node.js developer", "SDE", "python developer",
        ])
        self.max_pages: int = cfg.get("max_pages_per_query", 3)

        # Runtime state
        self._page = None
        self._last_apply_ts: Optional[datetime] = None
        self._prepared: Dict[str, Dict] = {}

        logger.info("IndeedPlatform initialised (max %d/day, gap %d-%ds)",
                     self.max_daily, self._gap_min, self._gap_max)

    # ═══════════════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ═══════════════════════════════════════════════════════════════════

    def _get_page(self):
        """Get or launch the Indeed browser page."""
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    _ = self._page.url
                    return self._page
            except Exception:
                pass
        self._page = self.browser.launch(self.PLATFORM_NAME)
        return self._page

    def _dismiss_popups(self, page) -> None:
        """Click away cookie banners, overlays, dialogs."""
        targets = (
            _SEL.get("close_popup", [])
            + _SEL.get("cookie_accept", [])
            + [
                'button:has-text("No thanks")',
                'button:has-text("Not now")',
                'button:has-text("Dismiss")',
                'button:has-text("Skip")',
                '[aria-label="Close"]',
            ]
        )
        for sel in targets:
            try:
                if self.browser.element_visible(page, sel):
                    page.click(sel, timeout=2000)
                    time.sleep(random.uniform(0.3, 1.0))
            except Exception:
                continue

    def _human_scroll(self, page, rounds: int = 3) -> None:
        """Scroll page like a human reading."""
        for _ in range(rounds):
            self.browser.scroll_page(page, "down",
                                     random.randint(200, 550))
            self.browser.random_delay(0.6, 2.0)

    def _close_extra_tabs(self, page) -> None:
        """Keep only the first tab open."""
        try:
            context = page.context
            for p in context.pages[1:]:
                p.close()
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════════
    # LOGIN (cookie-based)
    # ═══════════════════════════════════════════════════════════════════

    def login(self) -> bool:
        """
        Authenticate to Indeed via saved cookies.

        If cookies are expired/missing, opens visible browser and sends
        Telegram alert for manual Google/magic-link login.
        """
        logger.info("═══ Indeed Login ═══")
        try:
            page = self._get_page()

            # Load cookies + navigate
            self.browser.load_cookies("indeed")
            self.browser.navigate(page, _BASE_URL)
            self.browser.random_delay(2, 4)
            self._dismiss_popups(page)

            # Already logged in?
            if self._is_logged_in(page):
                logger.info("✅ Indeed: logged in via saved cookies")
                self.browser.save_cookies("indeed")
                self._update_session(logged_in=True)
                return True

            # Manual login flow
            logger.info("Not logged in — requesting manual auth")
            self._telegram_login_alert()
            self.browser.navigate(page, f"{_BASE_URL}/account/login")
            self.browser.random_delay(2, 3)

            # Pre-fill email
            email = USER_PROFILE.get("email", "")
            if email:
                email_el = _find(page, "email_input", timeout=4000)
                if email_el:
                    css = _css_for(email_el)
                    self.browser.type_human(page, css, email)

            # Wait for manual login
            ok = self._wait_for_login(page, timeout_s=300)
            if ok:
                logger.info("✅ Indeed: manual login succeeded")
                self.browser.save_cookies("indeed")
                self._update_session(logged_in=True)
                self._tg("✅ Indeed login successful!")
                return True

            logger.error("❌ Indeed login timed out")
            self._update_session(logged_in=False, error="Manual login timed out",
                                 cooldown_h=1)
            return False

        except Exception as exc:
            logger.error("Login exception: %s", exc)
            self._save_error("login", exc)
            self._update_session(logged_in=False, error=str(exc))
            return False

    def _is_logged_in(self, page) -> bool:
        """Heuristic check for logged-in state."""
        pos_sels = [
            "#AccountMenu",
            'button[data-gnav-element-name="AccountMenu"]',
            '[data-testid="account-menu"]',
            'a[href*="/account"]',
            "#gnav-header-account",
            'a[href*="/myjobs"]',
            "#profileMenuButton",
        ]
        for sel in pos_sels:
            try:
                if page.query_selector(sel):
                    return True
            except Exception:
                continue

        # Negative: sign-in link visible = NOT logged in
        sign = _find(page, "sign_in_prompt", timeout=1500)
        if sign:
            return False

        url = self.browser.get_page_url(page)
        if "/account/login" in url or "/registration" in url:
            return False
        return False

    def _wait_for_login(self, page, timeout_s: int = 300) -> bool:
        """Poll every 5s until login detected or timeout."""
        t0 = time.monotonic()
        while (time.monotonic() - t0) < timeout_s:
            try:
                if self._is_logged_in(page):
                    return True
                url = self.browser.get_page_url(page)
                if any(p in url for p in ["/jobs", "/myjobs", "/?from=", "/myaccount"]):
                    self.browser.random_delay(2, 3)
                    if self._is_logged_in(page):
                        return True
                    self.browser.navigate(page, _BASE_URL)
                    self.browser.random_delay(2, 3)
                    return self._is_logged_in(page)
            except Exception:
                pass
            time.sleep(5)
        return False

    def _telegram_login_alert(self) -> None:
        if not self.notifier:
            return
        try:
            self.notifier.send_platform_issue(
                "indeed",
                "🔐 *Indeed Login Required*\n\n"
                "A browser window is open.\n"
                "Log in via Google or email magic-link.\n"
                "⏱ Timeout: 5 minutes",
            )
        except Exception:
            pass

    def _update_session(self, *, logged_in: bool, error: str = None,
                        cooldown_h: float = 0) -> None:
        updates: Dict[str, Any] = {
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
            self.db.update_platform_session("indeed", updates)
        except Exception as e:
            logger.debug("Session update failed: %s", e)

    def _tg(self, msg: str) -> None:
        if self.notifier:
            try:
                self.notifier.send_platform_issue("indeed", msg)
            except Exception:
                pass

    def _save_error(self, method: str, error: Exception) -> None:
        try:
            self.db.save_error(
                module=f"platforms.indeed.{method}",
                error_type=type(error).__name__,
                message=str(error),
                traceback=tb_module.format_exc(),
            )
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════════
    # SEARCH JOBS
    # ═══════════════════════════════════════════════════════════════════

    def search_jobs(self, queries: Optional[List[str]] = None,
                    filters: Optional[Dict] = None) -> List[Dict]:
        """
        Search Indeed across queries × locations with pagination.

        Returns list of unique job dicts.
        """
        logger.info("═══ Indeed Search ═══")
        page = self._get_page()

        if not self._is_logged_in(page):
            logger.warning("Not logged in, attempting login first")
            if not self.login():
                logger.error("Cannot search: login failed")
                return []

        queries = queries or self.search_queries
        filters = filters or {}
        locations: List[str] = filters.get(
            "locations",
            USER_PROFILE.get("target_locations", [
                "Bangalore", "Hyderabad", "Pune",
                "Remote", "Delhi NCR", "Mumbai",
            ]),
        )
        fromage: int = filters.get("fromage", 3)
        job_type: str = filters.get("job_type", "fulltime")
        sort_by: str = filters.get("sort", "date")

        all_jobs: List[Dict] = []

        for q in queries:
            for loc in locations:
                try:
                    logger.info("→ '%s' in '%s'", q, loc)
                    batch = self._run_query(page, q, loc, fromage,
                                            job_type, sort_by)
                    logger.info("  found %d jobs", len(batch))
                    all_jobs.extend(batch)
                    self.browser.random_delay(
                        *STEALTH_CONFIG.get("random_delay_range", (3, 12)))
                except Exception as exc:
                    logger.error("Search error '%s'/'%s': %s", q, loc, exc)
                    self._save_error("search_jobs", exc)

        # Deduplicate
        seen: set = set()
        unique: List[Dict] = []
        for j in all_jobs:
            jid = j.get("platform_job_id", "")
            if jid and jid not in seen:
                seen.add(jid)
                unique.append(j)

        logger.info("═══ Indeed Search Complete: %d unique jobs ═══",
                     len(unique))
        return unique

    def _run_query(self, page, query: str, location: str,
                   fromage: int, job_type: str, sort_by: str) -> List[Dict]:
        """Execute a single (query, location) search with pagination."""
        jobs: List[Dict] = []

        for page_num in range(self.max_pages):
            params: Dict[str, Any] = {
                "q": query, "l": location,
                "fromage": fromage, "sort": sort_by,
            }
            if job_type:
                params["jt"] = job_type
            if page_num > 0:
                params["start"] = page_num * 10

            url = f"{_SEARCH_URL}?{urlencode(params)}"
            logger.debug("  page %d → %s", page_num + 1, url)

            try:
                if not self.browser.navigate(page, url):
                    break
                self.browser.random_delay(2, 4)
                self._dismiss_popups(page)
                self._human_scroll(page, rounds=random.randint(2, 4))

                # CAPTCHA check
                cap = self.detect_captcha(page)
                if cap:
                    logger.warning("CAPTCHA on search page")
                    if not self.handle_captcha(page, self.notifier):
                        break

                # No results?
                no_res = False
                for sel in ['.jobsearch-NoResult-messageContainer',
                            'p:has-text("did not match any jobs")',
                            'p:has-text("No matching jobs found")']:
                    try:
                        if page.query_selector(sel):
                            no_res = True
                            break
                    except Exception:
                        continue
                if no_res:
                    logger.info("  no results for '%s' in '%s'",
                                query, location)
                    break

                batch = self._parse_job_cards(page, query, location)
                if not batch:
                    break
                jobs.extend(batch)

                # Next page?
                if page_num < self.max_pages - 1:
                    nxt = _find(page, "next_page", timeout=2000)
                    if not nxt:
                        break
                    self.browser.random_delay(1.5, 3.0)

            except Exception as exc:
                logger.error("  page %d error: %s", page_num + 1, exc)
                break

        return jobs

    def _parse_job_cards(self, page, query: str,
                         location: str) -> List[Dict]:
        """Parse all job cards on current search results page."""
        cards = _find_all(page, "job_cards", timeout=8000)
        if not cards:
            return []

        results: List[Dict] = []
        for card in cards:
            try:
                data = self._extract_card(card, query, location)
                if data and data.get("platform_job_id"):
                    results.append(data)
            except Exception as exc:
                logger.debug("  card parse error: %s", exc)
        return results

    def _extract_card(self, card, query: str,
                      location: str) -> Optional[Dict]:
        """Extract structured data from a single job card."""
        now_iso = datetime.now().isoformat()

        # Job ID
        jid = ""
        for a in ("data-jk", "id", "data-id"):
            jid = _attr(card, a)
            if jid:
                break
        if not jid:
            link_el = card.query_selector("a[data-jk]")
            if link_el:
                jid = _attr(link_el, "data-jk")
            if not jid:
                link_el = card.query_selector("a[href*='jk=']")
                if link_el:
                    href = _attr(link_el, "href")
                    m = re.search(r"jk=([a-f0-9]+)", href)
                    if m:
                        jid = m.group(1)
        if not jid:
            return None

        # Title
        title = ""
        for sel in _SEL["job_title"]:
            el = card.query_selector(sel)
            if el:
                title = _txt(el)
                if title:
                    break
        if not title:
            return None

        # URL
        url = f"{_BASE_URL}/viewjob?jk={jid}"
        for sel in _SEL["job_title"]:
            el = card.query_selector(sel)
            if el:
                href = _attr(el, "href")
                if href:
                    if href.startswith("/"):
                        href = f"{_BASE_URL}{href}"
                    url = href
                    break

        # Company
        company = ""
        for sel in _SEL["company_name"]:
            el = card.query_selector(sel)
            if el:
                company = _txt(el)
                if company:
                    break

        # Location
        loc = ""
        for sel in _SEL["location"]:
            el = card.query_selector(sel)
            if el:
                loc = _txt(el)
                if loc:
                    break

        # Salary
        salary = ""
        for sel in _SEL["salary"]:
            el = card.query_selector(sel)
            if el:
                salary = _txt(el)
                if salary:
                    break

        # Posted date
        posted = ""
        for sel in _SEL["date_posted"]:
            el = card.query_selector(sel)
            if el:
                posted = _txt(el)
                if posted:
                    break

        # Snippet
        snippet = ""
        for sel in _SEL["job_snippet"]:
            el = card.query_selector(sel)
            if el:
                snippet = _txt(el)
                if snippet:
                    break

        # Work mode
        combined = f"{title} {loc} {snippet}".lower()
        if "remote" in combined:
            work_mode = "remote"
        elif "hybrid" in combined:
            work_mode = "hybrid"
        else:
            work_mode = "onsite"

        # Experience
        exp_text = ""
        exp_match = re.search(
            r"(\d+)\s*[-–to]+\s*(\d+)\s*(?:years?|yrs?)", combined)
        if not exp_match:
            exp_match = re.search(r"(\d+)\+?\s*(?:years?|yrs?)", combined)
        if exp_match:
            exp_text = exp_match.group(0)

        # Skills
        skills_found = _extract_skills(f"{title} {snippet}")

        return {
            "platform": "indeed",
            "platform_job_id": jid,
            "url": url,
            "title": title,
            "company": company,
            "location": loc,
            "salary_text": salary,
            "experience_text": exp_text,
            "description": snippet,
            "posted_date": posted,
            "skills": skills_found,
            "work_mode": work_mode,
            "job_type": "full-time",
            "discovered_at": now_iso,
        }

    # ═══════════════════════════════════════════════════════════════════
    # GET JOB DETAILS
    # ═══════════════════════════════════════════════════════════════════

    def get_job_details(self, job_url: str) -> Dict:
        """Navigate to job page and extract full details."""
        logger.info("Fetching details: %s", job_url)
        page = self._get_page()

        try:
            if not self.browser.navigate(page, job_url):
                return {}
            self.browser.random_delay(2, 4)
            self._dismiss_popups(page)
            self._human_scroll(page, rounds=random.randint(2, 3))

            result: Dict[str, Any] = {"url": job_url}

            el = _find(page, "detail_title", timeout=5000)
            result["title"] = _txt(el)

            el = _find(page, "detail_company", timeout=3000)
            result["company"] = _txt(el)

            el = _find(page, "detail_location", timeout=3000)
            result["location"] = _txt(el)

            el = _find(page, "detail_salary", timeout=3000)
            result["salary_text"] = _txt(el)

            # Posted date
            posted = ""
            for sel in _SEL["detail_date"]:
                try:
                    el2 = page.query_selector(sel)
                    if el2:
                        posted = _txt(el2)
                        if posted:
                            break
                except Exception:
                    continue
            result["posted_date"] = posted

            # Full JD
            desc_el = _find(page, "job_description", timeout=6000)
            description = _txt(desc_el)
            result["description"] = description

            # Skills
            result["skills"] = _extract_skills(description)

            # Experience
            exp_text = ""
            exp_m = re.search(
                r"(\d+)\s*[-–to]+\s*(\d+)\s*(?:years?|yrs?)",
                description, re.IGNORECASE)
            if not exp_m:
                exp_m = re.search(
                    r"(\d+)\+?\s*(?:years?|yrs?)",
                    description, re.IGNORECASE)
            if exp_m:
                exp_text = exp_m.group(0)
            result["experience_text"] = exp_text

            # Work mode
            combined_lower = (
                f"{result.get('title','')} "
                f"{result.get('location','')} "
                f"{description}"
            ).lower()
            if "remote" in combined_lower:
                result["work_mode"] = "remote"
            elif "hybrid" in combined_lower:
                result["work_mode"] = "hybrid"
            else:
                result["work_mode"] = "onsite"

            result["job_type"] = "full-time"

            # Indeed Apply available?
            apply_btn = _find(page, "apply_button", timeout=3000)
            external_btn = _find(page, "external_apply", timeout=1500)
            result["has_indeed_apply"] = (
                apply_btn is not None and external_btn is None)

            logger.info("  ✅ '%s' @ '%s' — %d skills, apply=%s",
                         result.get("title", "?"),
                         result.get("company", "?"),
                         len(result.get("skills", [])),
                         result.get("has_indeed_apply"))
            return result

        except Exception as exc:
            logger.error("get_job_details error: %s", exc)
            self._save_error("get_job_details", exc)
            return {}

    # ═══════════════════════════════════════════════════════════════════
    # PREPARE APPLICATION (fill form — DO NOT SUBMIT)
    # ═══════════════════════════════════════════════════════════════════

    def prepare_application(self, job: Dict, resume_path: str,
                            cover_letter: Optional[str] = None) -> Dict:
        """
        Navigate to job, click Apply, fill multi-step form, pause
        before final submission.
        """
        jid = str(job.get("id", job.get("platform_job_id", "unknown")))
        logger.info("═══ Prepare Application: %s @ %s ═══",
                     job.get("title", "?"), job.get("company", "?"))

        result = {
            "status": "failed",
            "job": job,
            "platform": self.PLATFORM_NAME,
            "apply_type": "indeed_apply",
            "resume_path": resume_path,
            "error": None,
            "timestamp": datetime.now().isoformat(),
        }

        page = self._get_page()

        if not self._can_apply_now():
            result["error"] = "Rate limit / daily cap"
            return result

        if not os.path.isfile(resume_path):
            result["error"] = f"Resume not found: {resume_path}"
            return result

        url = job.get("url", "")
        if not url:
            result["error"] = "No job URL"
            return result

        try:
            if not self.browser.navigate(page, url):
                result["error"] = "Cannot load job page"
                return result
            self.browser.random_delay(2, 4)
            self._dismiss_popups(page)
            self._human_scroll(page, rounds=random.randint(1, 3))

            # CAPTCHA check
            cap = self.detect_captcha(page)
            if cap:
                if not self.handle_captcha(page, self.notifier):
                    result["error"] = "CAPTCHA unsolved"
                    return result

            # Click Apply
            apply_btn = _find(page, "apply_button", timeout=5000)
            if not apply_btn:
                ext = _find(page, "external_apply", timeout=2000)
                if ext:
                    result["status"] = "external"
                    result["apply_type"] = "external"
                    result["error"] = "External apply — not Indeed Apply"
                    return result
                result["error"] = "Apply button not found"
                return result

            css = _css_for(apply_btn)
            self.browser.click_human(page, css)
            self.browser.random_delay(3, 5)

            # Indeed Apply may open in new tab
            apply_page = page
            try:
                pages = page.context.pages
                if len(pages) > 1:
                    apply_page = pages[-1]
                    apply_page.wait_for_load_state(
                        "domcontentloaded", timeout=15000)
                    self.browser.random_delay(2, 3)
            except Exception:
                pass

            # Walk through multi-step form
            form_result = self._walk_apply_form(
                apply_page, resume_path, cover_letter, job)

            if form_result.get("ready_to_submit"):
                self._prepared[jid] = {
                    "apply_page": apply_page,
                    "job": job,
                    "resume_path": resume_path,
                    "timestamp": datetime.now().isoformat(),
                }
                result["status"] = "ready"
                result["screenshot"] = self.browser.take_screenshot(
                    apply_page, f"indeed_ready_{jid}")
                logger.info("  ✅ Application PREPARED — awaiting approval")
                return result
            else:
                result["error"] = form_result.get("error", "Form walk failed")
                self.browser.take_screenshot(
                    apply_page, f"indeed_prepare_fail_{jid}")
                self._close_extra_tabs(page)
                return result

        except Exception as exc:
            logger.error("prepare_application error: %s", exc)
            self._save_error("prepare_application", exc)
            self._close_extra_tabs(page)
            result["error"] = str(exc)
            return result

    def _walk_apply_form(self, page, resume_path: str,
                         cover_letter: Optional[str],
                         job: Dict) -> Dict:
        """Walk through Indeed's multi-step apply form."""
        MAX_STEPS = 12
        steps = 0

        for step in range(MAX_STEPS):
            self.browser.random_delay(1.5, 3.0)
            self._dismiss_popups(page)

            page_html = self.browser.get_page_html(page).lower()

            # Submit button = done
            submit_btn = _find(page, "submit_button", timeout=2000)
            if submit_btn:
                logger.info("  step %d: SUBMIT button found — ready",
                            step + 1)
                return {"ready_to_submit": True, "steps": step + 1}

            # Review button
            review_btn = _find(page, "review_button", timeout=1500)
            if review_btn:
                logger.info("  step %d: REVIEW button — clicking",
                            step + 1)
                css = _css_for(review_btn)
                self.browser.click_human(page, css)
                self.browser.random_delay(2, 4)
                submit_btn2 = _find(page, "submit_button", timeout=5000)
                if submit_btn2:
                    return {"ready_to_submit": True, "steps": step + 2}
                continue

            # Success page?
            if self._is_success_page(page_html, self.browser.get_page_url(page)):
                return {"ready_to_submit": False,
                        "error": "Already submitted (success page)"}

            # CAPTCHA
            cap = self.detect_captcha(page)
            if cap:
                if not self.handle_captcha(page, self.notifier):
                    return {"ready_to_submit": False,
                            "error": "CAPTCHA unsolved"}
                continue

            # Resume upload
            uploaded = self._upload_resume(page, resume_path)
            if uploaded:
                logger.info("  step %d: resume uploaded", step + 1)

            # Fill fields
            fields_handled = self._handle_form_fields(
                page, job, cover_letter)
            if fields_handled:
                logger.info("  step %d: %d fields handled",
                            step + 1, fields_handled)
                steps = step + 1

            # Continue / Next
            cont_btn = _find(page, "continue_button", timeout=3000)
            if cont_btn:
                logger.info("  step %d: clicking Continue", step + 1)
                css = _css_for(cont_btn)
                self.browser.click_human(page, css)
                self.browser.random_delay(2, 4)
                continue

            # Fallback buttons
            clicked = False
            for sel in ['button:has-text("Apply")',
                        'button:has-text("Save")',
                        'button:has-text("Confirm")',
                        'button[type="submit"]']:
                try:
                    if page.query_selector(sel):
                        self.browser.click_human(page, sel)
                        self.browser.random_delay(2, 4)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                logger.warning("  step %d: stuck — no button found",
                               step + 1)
                return {"ready_to_submit": False, "steps": steps,
                        "error": f"Stuck at step {step + 1}"}

        return {"ready_to_submit": False,
                "error": "Max steps exceeded", "steps": steps}

    @staticmethod
    def _is_success_page(html_lower: str, url: str) -> bool:
        markers = [
            "application has been submitted",
            "your application was sent",
            "you have applied",
            "application submitted",
            "successfully applied",
            "thank you for applying",
        ]
        for m in markers:
            if m in html_lower:
                return True
        if "/post-apply" in url or "applied=true" in url:
            return True
        return False

    def _upload_resume(self, page, resume_path: str) -> bool:
        """Upload resume via file input or file-chooser dialog."""
        abs_path = os.path.abspath(resume_path)
        if not os.path.isfile(abs_path):
            return False

        # Strategy 1: direct input[type=file]
        for sel in _SEL["resume_upload"]:
            try:
                inp = page.query_selector(sel)
                if inp:
                    inp.set_input_files(abs_path)
                    logger.info("    resume uploaded via input[type=file]")
                    self.browser.random_delay(1.5, 3.0)
                    return True
            except Exception:
                continue

        # Strategy 2: click upload button → file chooser
        upload_btn_sels = [
            'button:has-text("Upload resume")',
            'button:has-text("Upload")',
            'label:has-text("Upload resume")',
            '[data-testid="upload-resume"]',
        ]
        for sel in upload_btn_sels:
            try:
                btn = page.query_selector(sel)
                if btn:
                    with page.expect_file_chooser(timeout=5000) as fc_info:
                        btn.click()
                    file_chooser = fc_info.value
                    file_chooser.set_files(abs_path)
                    logger.info("    resume uploaded via file chooser")
                    self.browser.random_delay(1.5, 3.0)
                    return True
            except Exception:
                continue

        # Strategy 3: resume on file
        for sel in ['button:has-text("Use last uploaded")',
                    'div:has-text("Resume on Indeed")',
                    ':has-text("resume on file")']:
            try:
                if page.query_selector(sel):
                    logger.info("    Indeed resume on file — skipping")
                    return True
            except Exception:
                continue

        return False

    # ── form field handling ────────────────────────────────────────────

    def _handle_form_fields(self, page, job: Dict,
                            cover_letter: Optional[str] = None) -> int:
        """Detect and fill all form fields on current step."""
        handled = 0

        # Text inputs
        for sel in ['input[type="text"]:visible',
                    'input[type="email"]:visible',
                    'input[type="tel"]:visible',
                    'input[type="number"]:visible']:
            try:
                inputs = page.query_selector_all(sel)
                for inp in inputs:
                    if self._handle_single_input(page, inp, job):
                        handled += 1
            except Exception:
                continue

        # Textareas
        try:
            textareas = page.query_selector_all("textarea:visible")
            for ta in textareas:
                if self._handle_textarea(page, ta, job, cover_letter):
                    handled += 1
        except Exception:
            pass

        # Selects
        try:
            selects = page.query_selector_all("select:visible")
            for sel_el in selects:
                if self._handle_select(page, sel_el, job):
                    handled += 1
        except Exception:
            pass

        # Radio groups
        try:
            groups = page.query_selector_all(
                'fieldset:visible, div[role="radiogroup"]:visible')
            for rg in groups:
                if self._handle_radio_group(page, rg, job):
                    handled += 1
        except Exception:
            pass

        # Checkboxes
        try:
            cbs = page.query_selector_all('input[type="checkbox"]:visible')
            for cb in cbs:
                if self._handle_checkbox(page, cb):
                    handled += 1
        except Exception:
            pass

        return handled

    def _handle_single_input(self, page, inp, job: Dict) -> bool:
        try:
            val = inp.input_value() or ""
            if val.strip():
                return False

            name = (_attr(inp, "name")).lower()
            ph = (_attr(inp, "placeholder")).lower()
            label_text = self._get_label_for(page, inp)
            label_lower = label_text.lower()
            inp_type = (_attr(inp, "type")).lower() or "text"
            aria = (_attr(inp, "aria-label")).lower()
            combined = f"{name} {ph} {label_lower} {aria}"

            answer = self._resolve_field_answer(combined, inp_type, job)
            if not answer:
                return False

            css = _css_for(inp)
            self.browser.type_human(page, css, answer)
            self.browser.random_delay(0.5, 1.2)
            return True
        except Exception:
            return False

    def _handle_textarea(self, page, ta, job: Dict,
                         cover_letter: Optional[str]) -> bool:
        try:
            val = ta.input_value() or ""
            if val.strip() and len(val.strip()) > 5:
                return False

            label_text = self._get_label_for(page, ta)
            label_lower = label_text.lower()
            name = (_attr(ta, "name")).lower()
            ph = (_attr(ta, "placeholder")).lower()
            combined = f"{name} {ph} {label_lower}"

            answer = ""
            if any(kw in combined for kw in [
                "cover letter", "cover_letter", "coverletter",
                "why are you interested", "why apply",
                "message to hiring", "additional information",
            ]):
                answer = cover_letter or self._default_cover(job)
            elif get_answer:
                answer = get_answer(label_text) or get_answer(combined)

            if not answer:
                answer = self._fallback_text_answer(combined, job)
            if not answer:
                return False

            css = _css_for(ta)
            ta.click()
            self.browser.random_delay(0.3, 0.8)
            self.browser.type_human(page, css, answer[:2000])
            return True
        except Exception:
            return False

    def _handle_select(self, page, sel_el, job: Dict) -> bool:
        try:
            label_text = self._get_label_for(page, sel_el)
            label_lower = label_text.lower()
            name = (_attr(sel_el, "name")).lower()
            combined = f"{name} {label_lower}"

            options = sel_el.query_selector_all("option")
            opt_values: List[Tuple[str, str]] = []
            for opt in options:
                ov = _attr(opt, "value")
                ot = _txt(opt)
                if ov or ot:
                    opt_values.append((ov, ot))

            if not opt_values:
                return False

            desired = self._resolve_dropdown_answer(
                combined, opt_values, job)
            if desired:
                sel_el.select_option(value=desired)
                self.browser.random_delay(0.3, 0.8)
                return True
            return False
        except Exception:
            return False

    def _handle_radio_group(self, page, group, job: Dict) -> bool:
        try:
            label_text = _txt(group)
            label_lower = label_text.lower()

            radios = group.query_selector_all('input[type="radio"]')
            if not radios:
                return False

            for r in radios:
                if r.get_attribute("checked") is not None:
                    return False

            radio_opts: List[Tuple[Any, str]] = []
            for r in radios:
                r_label = self._get_label_for(page, r)
                if not r_label:
                    parent = r.evaluate_handle("e => e.parentElement")
                    if parent:
                        r_label = _txt(parent.as_element())
                radio_opts.append((r, r_label.lower()))

            pick = self._resolve_radio_answer(
                label_lower, radio_opts, job)
            if pick is not None:
                css = _css_for(pick)
                self.browser.click_human(page, css)
                self.browser.random_delay(0.3, 0.8)
                return True
            return False
        except Exception:
            return False

    def _handle_checkbox(self, page, cb) -> bool:
        try:
            if cb.is_checked():
                return False

            label = self._get_label_for(page, cb)
            label_lower = label.lower()

            auto_check = [
                "agree", "consent", "acknowledge", "terms",
                "privacy", "confirm", "certify", "authorize",
                "i have read", "accept", "understand",
            ]
            for kw in auto_check:
                if kw in label_lower:
                    css = _css_for(cb)
                    self.browser.click_human(page, css)
                    self.browser.random_delay(0.2, 0.5)
                    return True
            return False
        except Exception:
            return False

    def _get_label_for(self, page, el) -> str:
        """Find the label associated with a form element."""
        try:
            eid = _attr(el, "id")
            if eid:
                lbl = page.query_selector(f'label[for="{eid}"]')
                if lbl:
                    return _txt(lbl)

            aria = _attr(el, "aria-label")
            if aria:
                return aria

            lblby = _attr(el, "aria-labelledby")
            if lblby:
                ref = page.query_selector(f"#{lblby}")
                if ref:
                    return _txt(ref)

            # Ancestor label
            try:
                ancestor = el.evaluate_handle(
                    "e => e.closest('label') || "
                    "e.parentElement?.querySelector('label')")
                if ancestor:
                    t = _txt(ancestor.as_element())
                    if t:
                        return t
            except Exception:
                pass

            ph = _attr(el, "placeholder")
            if ph:
                return ph

            # Preceding sibling text
            try:
                prev_text = el.evaluate(
                    """e => {
                        let p = e.previousElementSibling;
                        if (p) return p.textContent || '';
                        let par = e.parentElement;
                        if (par) {
                            let label = par.querySelector('label, span, p, div');
                            if (label && label !== e) return label.textContent || '';
                        }
                        return '';
                    }""")
                if prev_text and isinstance(prev_text, str):
                    return prev_text.strip()
            except Exception:
                pass

        except Exception:
            pass
        return ""

    # ── answer resolution ──────────────────────────────────────────────

    def _resolve_field_answer(self, combined: str,
                              inp_type: str, job: Dict) -> str:
        c = combined
        if any(k in c for k in ["first name", "firstname",
                                 "first_name", "given name"]):
            return USER_PROFILE.get("name", "Piyush Kashyap").split()[0]
        if any(k in c for k in ["last name", "lastname",
                                 "last_name", "surname"]):
            parts = USER_PROFILE.get("name", "Piyush Kashyap").split()
            return parts[-1] if len(parts) > 1 else parts[0]
        if any(k in c for k in ["full name", "fullname",
                                 "your name", "name"]) and "company" not in c:
            return USER_PROFILE.get("name", "Piyush Kashyap")
        if any(k in c for k in ["email", "e-mail"]) and "company" not in c:
            return USER_PROFILE.get("email", "piyushkashyap3247@gmail.com")
        if any(k in c for k in ["phone", "mobile", "contact number"]):
            return USER_PROFILE.get("phone", "+91 73107 03247")
        if any(k in c for k in ["city", "location",
                                 "address"]) and "company" not in c:
            return USER_PROFILE.get("location", "Rishikesh, Uttarakhand")
        if any(k in c for k in ["linkedin"]):
            return USER_PROFILE.get("linkedin_url",
                                    "https://linkedin.com/in/piyush-kashyap731")
        if any(k in c for k in ["github", "portfolio", "website"]):
            return USER_PROFILE.get("github_url",
                                    "https://github.com/Piyush731")
        if any(k in c for k in ["current ctc", "current salary"]):
            return "3.7 LPA"
        if any(k in c for k in ["expected ctc", "expected salary",
                                 "desired salary",
                                 "salary expectation"]):
            if get_salary_answer:
                return get_salary_answer(job)
            return "6-10 LPA (negotiable)"
        if any(k in c for k in ["notice period", "notice_period"]):
            return "15 days"
        if any(k in c for k in ["experience", "total experience",
                                 "years of experience"]) \
                and "relevant" not in c:
            return str(USER_PROFILE.get("experience_years", 1))
        if any(k in c for k in ["relevant experience"]):
            return str(USER_PROFILE.get("experience_years", 1))
        if any(k in c for k in ["current company",
                                 "current employer"]):
            return "Site Guru Pvt Ltd"
        if any(k in c for k in ["current title", "current role",
                                 "current designation"]) \
                and "desired" not in c:
            return USER_PROFILE.get("current_title",
                                    "Full Stack Developer L1")
        if any(k in c for k in ["pincode", "zip code", "postal code"]):
            return "249201"
        if any(k in c for k in ["date of birth", "dob"]):
            return "2003-07-31"

        if get_answer:
            ans = get_answer(combined)
            if ans:
                return str(ans)

        return ""

    def _resolve_dropdown_answer(self, combined: str,
                                 opts: List[Tuple[str, str]],
                                 job: Dict) -> Optional[str]:
        c = combined
        preference_map = {
            "experience": ["0-1", "1-2", "0", "1", "fresher", "entry"],
            "notice": ["immediate", "15", "less than", "0-15"],
            "relocat": ["yes", "true", "willing"],
            "work auth": ["yes", "authorized", "citizen"],
            "sponsor": ["no", "do not"],
            "gender": ["prefer not", "male"],
            "job type": ["full-time", "full time", "permanent"],
            "shift": ["day", "general", "any"],
            "education": ["bachelor", "b.tech", "btech", "engineering"],
            "salary": ["negotiable"],
        }
        for keyword, prefs in preference_map.items():
            if keyword in c:
                for pref in prefs:
                    for val, text in opts:
                        if pref in text.lower() or pref in val.lower():
                            return val
                break

        for val, text in opts:
            tl = text.lower().strip()
            if tl and tl not in ("select", "choose", "--",
                                  "select one", "please select",
                                  "", "choose one"):
                return val
        return None

    def _resolve_radio_answer(self, label_lower: str,
                              opts: List[Tuple[Any, str]],
                              job: Dict) -> Any:
        positive_map = {
            "authorized": ["yes"], "legally": ["yes"],
            "sponsorship": ["no"], "relocat": ["yes"],
            "willing to travel": ["yes"],
            "background check": ["yes"], "drug test": ["yes"],
            "18 years": ["yes"], "shift": ["yes"],
            "currently employed": ["yes"],
            "gender": ["prefer not", "male"],
            "veteran": ["no", "prefer not"],
            "disability": ["no", "prefer not"],
            "race": ["prefer not"],
            "ethnicity": ["prefer not"],
            "hear about": ["job board", "indeed", "online"],
        }
        for keyword, prefs in positive_map.items():
            if keyword in label_lower:
                for pref in prefs:
                    for el, text in opts:
                        if pref in text:
                            return el
                break

        for el, text in opts:
            if text.strip().lower() == "yes":
                return el
        return opts[0][0] if opts else None

    def _fallback_text_answer(self, combined: str, job: Dict) -> str:
        c = combined
        if any(kw in c for kw in ["why", "interest", "motivation"]):
            company = job.get("company", "your company")
            title = job.get("title", "this role")
            return (
                f"I am excited about the {title} role at {company}. "
                f"With hands-on experience building 10+ production "
                f"applications as a sole developer, I bring strong "
                f"full-stack skills and ownership mentality."
            )
        if any(kw in c for kw in ["strength", "skill",
                                   "qualification"]):
            return (
                "Full-stack development (Vue.js, Node.js, Java "
                "Spring Boot), end-to-end project ownership, "
                "10+ production apps, REST APIs, WebSockets."
            )
        return ""

    def _default_cover(self, job: Dict) -> str:
        company = job.get("company", "your company")
        title = job.get("title", "the open position")
        return (
            f"Dear Hiring Team,\n\n"
            f"I am writing to express my strong interest in the "
            f"{title} role at {company}. As a Full Stack Developer "
            f"with experience building 10+ production applications "
            f"independently, I am confident I can contribute "
            f"meaningfully to your team.\n\n"
            f"Key highlights:\n"
            f"• Sole developer on multi-tenant ERP (57+ DB tables)\n"
            f"• WebSocket integrations, third-party APIs "
            f"(META, Razorpay, RTO)\n"
            f"• Published app on Google Play Store\n"
            f"• JavaScript, Java, Python, Vue.js, Node.js, "
            f"Spring Boot\n\n"
            f"Available within 15 days.\n\n"
            f"Best regards,\nPiyush Kashyap\n"
            f"+91 73107 03247 | piyushkashyap3247@gmail.com"
        )

    # ═══════════════════════════════════════════════════════════════════
    # SUBMIT APPLICATION
    # ═══════════════════════════════════════════════════════════════════

    def submit_application(self, prepared: Dict) -> Dict:
        """Click final Submit button after Telegram approval."""
        jid = str(prepared.get("job", {}).get(
            "id", prepared.get("job", {}).get(
                "platform_job_id", "unknown")))
        logger.info("═══ Submitting application %s ═══", jid)

        result = {
            "success": False,
            "status": "failed",
            "error": None,
            "timestamp": datetime.now().isoformat(),
        }

        state = self._prepared.pop(jid, None)
        if not state:
            result["error"] = "No prepared state — call prepare first"
            return result

        apply_page = state.get("apply_page")
        if not apply_page:
            result["error"] = "Apply page lost"
            return result

        try:
            submit_btn = _find(apply_page, "submit_button", timeout=8000)
            if not submit_btn:
                self.browser.take_screenshot(
                    apply_page, f"indeed_no_submit_{jid}")
                result["error"] = "Submit button not found"
                return result

            css = _css_for(submit_btn)
            self.browser.click_human(apply_page, css)
            self.browser.random_delay(3, 6)

            # Verify
            html = self.browser.get_page_html(apply_page).lower()
            url = self.browser.get_page_url(apply_page)

            if self._is_success_page(html, url):
                self._last_apply_ts = datetime.now()
                self.increment_count()
                result["success"] = True
                result["status"] = "submitted"
                logger.info("  ✅ Application SUBMITTED for %s", jid)
                self._close_extra_tabs(apply_page)
                return result

            # Wait and retry check
            self.browser.random_delay(2, 4)
            html2 = self.browser.get_page_html(apply_page).lower()
            url2 = self.browser.get_page_url(apply_page)
            if self._is_success_page(html2, url2):
                self._last_apply_ts = datetime.now()
                self.increment_count()
                result["success"] = True
                result["status"] = "submitted"
                self._close_extra_tabs(apply_page)
                return result

            # Optimistic count
            self._last_apply_ts = datetime.now()
            self.increment_count()
            result["success"] = True
            result["status"] = "submitted"
            result["note"] = "Submit clicked, confirmation unclear"
            self._close_extra_tabs(apply_page)
            return result

        except Exception as exc:
            logger.error("submit_application error: %s", exc)
            self._save_error("submit_application", exc)
            result["error"] = str(exc)
            self._close_extra_tabs(self._get_page())
            return result

    # ═══════════════════════════════════════════════════════════════════
    # CHECK STATUS
    # ═══════════════════════════════════════════════════════════════════

    def check_status(self, application_id: Optional[int] = None) -> str:
        """Check application status on Indeed's My Jobs page."""
        logger.info("Checking Indeed application status")
        page = self._get_page()

        try:
            self.browser.navigate(page, f"{_BASE_URL}/myjobs?advn=1")
            self.browser.random_delay(2, 4)
            self._dismiss_popups(page)

            html = self.browser.get_page_html(page).lower()

            status_indicators = {
                "viewed": ["employer viewed", "viewed by employer"],
                "interview": ["interview", "scheduled"],
                "rejected": ["not selected", "position filled"],
            }
            for status, keywords in status_indicators.items():
                for kw in keywords:
                    if kw in html:
                        return status
            return "submitted"

        except Exception as exc:
            logger.error("check_status error: %s", exc)
            return "unknown"

    # ═══════════════════════════════════════════════════════════════════
    # RATE LIMITING
    # ═══════════════════════════════════════════════════════════════════

    def _can_apply_now(self) -> bool:
        if not self.can_apply():
            return False
        if self.is_in_cooldown():
            return False
        if self._last_apply_ts:
            elapsed = (datetime.now() - self._last_apply_ts).total_seconds()
            if elapsed < self._gap_min:
                logger.info("Rate limit: wait %.0fs more",
                            self._gap_min - elapsed)
                return False
        return True


# ═══════════════════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Indeed Platform — Smoke Test")
    print("=" * 60)

    # ── 1. Import check ──
    print("\n[1] Import check:")
    print(f"  ✓ IndeedPlatform loaded")
    print(f"  answers.py: {'✓' if get_answer else '⚠ not available'}")

    # ── 2. Parsers ──
    print("\n[2] Skill extraction:")
    test_jd = ("We need a Java developer with Spring Boot, "
               "Docker, Kubernetes, and REST API experience.")
    skills = _extract_skills(test_jd)
    print(f"  JD: '{test_jd}'")
    print(f"  Skills: {skills}")
    assert "Java" in skills, "Java not found!"
    assert "Docker" in skills, "Docker not found!"
    print("  ✓ Skill extraction OK")

    # ── 3. Answer resolution ──
    print("\n[3] Answer resolution:")
    from core.browser import BrowserEngine
    engine = BrowserEngine()
    indeed = IndeedPlatform(engine)

    test_job = {"title": "SDE-1", "company": "Flipkart",
                "salary_min": 600000, "salary_max": 1200000}

    tests = [
        ("email address", "email", "piyushkashyap3247"),
        ("phone number", "tel", "73107"),
        ("current ctc", "text", "3.7"),
        ("notice period", "text", "15"),
        ("total experience", "text", "1"),
    ]
    for label, inp_type, expected_contains in tests:
        ans = indeed._resolve_field_answer(label, inp_type, test_job)
        ok = expected_contains.lower() in ans.lower()
        print(f"  {'✓' if ok else '✗'} '{label}' → '{ans}'")

    # ── 4. Cover letter ──
    print("\n[4] Default cover letter:")
    cover = indeed._default_cover(test_job)
    print(f"  Length: {len(cover)} chars")
    assert "Flipkart" in cover
    assert "SDE-1" in cover
    print("  ✓ Cover letter OK")

    # ── 5. Browser integration ──
    print("\n[5] Browser integration:")
    assert hasattr(indeed, 'browser'), "self.browser missing!"
    assert indeed.browser is engine, "self.browser != engine!"
    print("  ✓ self.browser OK")

    for m in ['can_apply', 'increment_count', 'detect_captcha',
              'handle_captcha', 'detect_otp_page', 'handle_otp']:
        assert hasattr(indeed, m), f"{m} missing!"
        print(f"  ✓ {m}")

    print(f"\n✅ Indeed platform tests complete!\n")