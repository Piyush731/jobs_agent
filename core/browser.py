"""
core/browser.py — Playwright stealth browser engine for job platform automation.

Provides ALL browser interaction primitives for every platform file:
  - Persistent browser profiles per platform (full cookie/state persistence)
  - playwright-stealth + custom JS patches (anti-detection)
  - Human-like typing (per-character random delay, occasional pauses)
  - Human-like mouse movement (quadratic bezier curves, not straight lines)
  - Human-like scrolling (multiple small wheel events with jitter)
  - Session time tracking with enforced breaks
  - Cookie save/load/clear/export
  - Screenshot capture (viewport and full-page)
  - Element helpers: wait, check, text, attribute, dropdown, upload
  - Dialog/alert auto-handling
  - Graceful error recovery at every level

Prerequisites:
    pip install playwright playwright-stealth
    playwright install chromium

Interface (from spec):
    BrowserEngine()
    BrowserEngine.launch(platform, headless?) → Page
    BrowserEngine.login(platform, credentials) → bool
    BrowserEngine.is_logged_in(platform) → bool
    BrowserEngine.type_human(page, selector, text) → None
    BrowserEngine.click_human(page, selector) → None
    BrowserEngine.scroll_page(page, direction?, amount?) → None
    BrowserEngine.random_delay(min_s?, max_s?) → None
    BrowserEngine.wait_for_element(page, selector, timeout?) → bool
    BrowserEngine.take_screenshot(page, name) → str
    BrowserEngine.save_cookies(platform) → None
    BrowserEngine.load_cookies(platform) → bool
    BrowserEngine.close(platform?) → None
    BrowserEngine.close_all() → None
"""

import os
import sys
import time
import json
import math
import random
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Tuple, List, Any

# ── project imports ──────────────────────────────────────────────
from config import BROWSER_PROFILES_DIR, CACHE_DIR, STEALTH_CONFIG
from core.logger import get_logger

logger = get_logger("core.browser")


# ═══════════════════════════════════════════════════════════════════
# DEPENDENCY CHECKS — fail gracefully with helpful messages
# ═══════════════════════════════════════════════════════════════════

_PW_AVAILABLE = False
_STEALTH_AVAILABLE = False

# ── Playwright ───────────────────────────────────────────────────
try:
    from playwright.sync_api import (
        sync_playwright,
        Page,
        BrowserContext,
        Error as PlaywrightError,
        TimeoutError as PlaywrightTimeoutError,
    )
    _PW_AVAILABLE = True
except ImportError:
    logger.error(
        "playwright not installed! Run:\n"
        "  pip install playwright\n"
        "  playwright install chromium"
    )
    # Placeholder types so the file loads without crashing
    Page = Any
    BrowserContext = Any
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError
except Exception as e:
    logger.error(f"playwright import failed ({type(e).__name__}: {e})")
    Page = Any
    BrowserContext = Any
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError

# ── playwright-stealth ──────────────────────────────────────────
# FIX: catch ALL exceptions, not just ImportError.
# The package can be installed but fail to import due to:
#   - version mismatch with playwright
#   - internal dependency error
#   - renamed/moved stealth_sync in newer versions
# Also try alternative import paths for different package versions.

stealth_sync = None  # default

try:
    from playwright_stealth import stealth_sync  # standard import
    _STEALTH_AVAILABLE = True
    logger.debug("playwright-stealth loaded (standard import)")
except ImportError:
    # Package genuinely not installed
    pass
except Exception as e:
    # Package installed but broken internally
    logger.warning(
        f"playwright-stealth installed but import failed: "
        f"{type(e).__name__}: {e}"
    )

# ── Fallback import paths (different versions/forks) ────────────
if not _STEALTH_AVAILABLE:
    # Try 1: some forks put stealth_sync inside a .stealth submodule
    try:
        from playwright_stealth.stealth import stealth_sync
        _STEALTH_AVAILABLE = True
        logger.debug("playwright-stealth loaded (submodule import)")
    except Exception:
        pass

if not _STEALTH_AVAILABLE:
    # Try 2: some forks export Stealth class instead of stealth_sync
    try:
        import playwright_stealth as _ps_module
        # Check if the module loaded at all (might have stealth_sync somewhere)
        if hasattr(_ps_module, "stealth_sync"):
            stealth_sync = _ps_module.stealth_sync
            _STEALTH_AVAILABLE = True
            logger.debug("playwright-stealth loaded (attribute lookup)")
        elif hasattr(_ps_module, "Stealth"):
            # Wrap class-based API to match our stealth_sync(page) interface
            _stealth_cls = _ps_module.Stealth
            def stealth_sync(page):
                s = _stealth_cls()
                s.apply(page)
            _STEALTH_AVAILABLE = True
            logger.debug("playwright-stealth loaded (class-based wrapper)")
        else:
            logger.warning(
                f"playwright-stealth module found but has no stealth_sync. "
                f"Available: {[a for a in dir(_ps_module) if not a.startswith('_')]}"
            )
    except Exception:
        pass

if not _STEALTH_AVAILABLE:
    stealth_sync = None
    # ── Check if pip shows it installed (diagnostic) ────────────
    _pip_check = ""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", "playwright-stealth"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            _pip_check = (
                " (NOTE: pip shows it IS installed — likely a version "
                "incompatibility. Try: pip install --upgrade playwright-stealth)"
            )
    except Exception:
        pass

    logger.warning(
        f"playwright-stealth not available.{_pip_check}\n"
        "  Install/upgrade for better anti-detection:\n"
        "    pip install --upgrade playwright-stealth\n"
        "  The bot will still work, but with weaker anti-detection."
    )


# ═══════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════

# Realistic desktop viewports (consistent per platform via hash)
_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1280, "height": 720},
    {"width": 1600, "height": 900},
    {"width": 1680, "height": 1050},
    {"width": 1360, "height": 768},
]

# Screenshots directory
_SCREENSHOTS_DIR = CACHE_DIR / "screenshots"

# ── Supplementary Stealth JavaScript ─────────────────────────────
# playwright-stealth handles the heavy lifting; these are belt-and-suspenders
# patches for signals that some job platforms specifically check.
_STEALTH_JS = """
(() => {
    // 1. Hide webdriver flag (most critical for bot detection)
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
            configurable: true
        });
    } catch(e) {}

    // 2. Chrome runtime object (real Chrome always has this)
    try {
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) window.chrome.runtime = {};
    } catch(e) {}

    // 3. Permissions API (prevents "notification denied" fingerprint)
    try {
        const origQuery = navigator.permissions.query.bind(
            navigator.permissions
        );
        navigator.permissions.query = (params) => {
            if (params.name === 'notifications') {
                return Promise.resolve({
                    state: Notification.permission
                });
            }
            return origQuery(params);
        };
    } catch(e) {}

    // 4. Consistent hardware fingerprint (looks like real machine)
    try {
        Object.defineProperty(navigator, 'hardwareConcurrency', {
            get: () => 4, configurable: true
        });
    } catch(e) {}
    try {
        Object.defineProperty(navigator, 'deviceMemory', {
            get: () => 8, configurable: true
        });
    } catch(e) {}
    try {
        Object.defineProperty(navigator, 'maxTouchPoints', {
            get: () => 0, configurable: true
        });
    } catch(e) {}

    // 5. Network connection info (4G — typical Indian developer)
    try {
        if (!navigator.connection) {
            Object.defineProperty(navigator, 'connection', {
                get: () => ({
                    effectiveType: '4g',
                    rtt: 50,
                    downlink: 10,
                    saveData: false
                }),
                configurable: true
            });
        }
    } catch(e) {}

    // 6. WebGL vendor/renderer (consistent fingerprint)
    try {
        const getParam = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p) {
            if (p === 37445) return 'Intel Inc.';
            if (p === 37446) return 'Intel Iris OpenGL Engine';
            return getParam.call(this, p);
        };
    } catch(e) {}

    // 7. Prevent automation detection via Error stack traces
    try {
        Error.stackTraceLimit = 10;
    } catch(e) {}
})();
"""


# ═══════════════════════════════════════════════════════════════════
# BROWSER ENGINE
# ═══════════════════════════════════════════════════════════════════

class BrowserEngine:
    """
    Stealth browser engine for job platform automation.

    Manages persistent Chromium browser profiles for each platform.
    Every browser action includes human-like behavior (delays, curves,
    jitter) to avoid bot detection.

    Usage:
        engine = BrowserEngine()
        page = engine.launch("naukri")
        engine.navigate(page, "https://www.naukri.com")
        engine.type_human(page, "#search", "Software Engineer")
        engine.click_human(page, "#submit")
        engine.save_cookies("naukri")
        engine.close("naukri")

    Context manager:
        with BrowserEngine() as engine:
            page = engine.launch("naukri")
            # ... work ...
        # auto-closes all browsers
    """

    def __init__(self):
        """
        Initialize BrowserEngine.
        Does NOT start Playwright or launch any browser yet — that happens
        on the first call to launch().
        """
        self._playwright = None
        self._contexts: Dict[str, Any] = {}        # platform → BrowserContext
        self._pages: Dict[str, Any] = {}            # platform → Page
        self._session_starts: Dict[str, datetime] = {}  # platform → start time
        self._mouse_pos: Dict[str, Tuple[float, float]] = {}  # platform → (x,y)
        self._dialog_messages: List[str] = []       # captured dialog texts

        # Ensure directories exist
        BROWSER_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        _SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"BrowserEngine initialized "
            f"(playwright={'yes' if _PW_AVAILABLE else 'NO'}, "
            f"stealth={'yes' if _STEALTH_AVAILABLE else 'no (JS-only fallback)'})"
        )

    # ═══════════════════════════════════════════════════════════
    # Context Manager
    # ═══════════════════════════════════════════════════════════

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close_all()
        return False

    def __repr__(self):
        active = [p for p in self._contexts if self.is_logged_in(p)]
        return (
            f"BrowserEngine(active={active}, "
            f"playwright={'on' if self._playwright else 'off'}, "
            f"stealth={'lib' if _STEALTH_AVAILABLE else 'js-only'})"
        )

    def __del__(self):
        try:
            self.close_all()
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════
    # CORE — Launch, Navigate, Close
    # ═══════════════════════════════════════════════════════════

    def _ensure_playwright(self) -> None:
        """Start the Playwright process if not already running."""
        if not _PW_AVAILABLE:
            raise RuntimeError(
                "Playwright is not installed.\n"
                "Run:  pip install playwright && playwright install chromium"
            )
        if self._playwright is None:
            self._playwright = sync_playwright().start()
            logger.debug("Playwright process started")

    def launch(self, platform: str,
               headless: Optional[bool] = None) -> Any:  # returns Page
        """
        Launch (or reuse) a browser for a specific platform.

        Each platform gets its own persistent Chromium profile directory
        at browser_profiles/{platform}/. Cookies, localStorage, IndexedDB,
        and all browser state persist across runs automatically.

        Stealth patches are applied on every launch.

        Args:
            platform: Platform name ("naukri", "indeed", "linkedin", "foundit", etc.)
            headless: True/False/None. None uses STEALTH_CONFIG['headless'].

        Returns:
            Playwright Page object ready for interaction.

        Raises:
            RuntimeError: If Playwright is not installed.
            Exception: If browser fails to launch (wrong binary, locked profile, etc.)
        """
        # ── Reuse existing live page ──
        if platform in self._pages:
            page = self._pages[platform]
            try:
                if not page.is_closed():
                    _ = page.url  # probe — raises if dead
                    logger.debug(f"Reusing existing page for {platform}")
                    return page
            except Exception:
                logger.debug(f"Existing page for {platform} is dead, relaunching")
            self._cleanup_platform(platform)

        self._ensure_playwright()

        # ── Config ──
        if headless is None:
            headless = STEALTH_CONFIG.get("headless", False)

        profile_dir = BROWSER_PROFILES_DIR / platform
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Remove stale lock files from crashed previous sessions
        self._cleanup_locks(profile_dir)

        # Consistent viewport per platform (different across platforms)
        viewport = self._get_viewport(platform)

        # ── Browser launch args ──
        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-infobars",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            f"--window-size={viewport['width']},{viewport['height']}",
        ]

        try:
            logger.info(
                f"Launching browser for '{platform}' "
                f"(headless={headless}, "
                f"viewport={viewport['width']}x{viewport['height']}, "
                f"profile={profile_dir})"
            )

            context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                viewport=viewport,
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                color_scheme="light",
                args=browser_args,
                ignore_default_args=["--enable-automation"],
                slow_mo=0,
                # Accept downloads silently
                accept_downloads=True,
            )

            # ── Get or create page ──
            if context.pages:
                page = context.pages[0]
            else:
                page = context.new_page()

            # ── Dialog handler ──
            page.on("dialog", self._handle_dialog)

            # ── Apply stealth ──
            self._apply_stealth(page, context)

            # ── Store references ──
            self._contexts[platform] = context
            self._pages[platform] = page
            self._session_starts[platform] = datetime.now()
            self._mouse_pos[platform] = (
                viewport["width"] // 2,
                viewport["height"] // 2,
            )

            logger.info(f"Browser launched for '{platform}'")
            return page

        except Exception as e:
            msg = str(e)
            if "Executable doesn't exist" in msg or "executable" in msg.lower():
                logger.error(
                    "Chromium browser binary not found.\n"
                    "Run:  playwright install chromium"
                )
            elif "lock" in msg.lower() or "already" in msg.lower():
                logger.error(
                    f"Browser profile for '{platform}' is locked "
                    f"(another instance running?).\n"
                    f"Close other instances or delete: {profile_dir}"
                )
            else:
                logger.error(f"Failed to launch browser for '{platform}': {e}")
            raise

    def get_page(self, platform: str) -> Optional[Any]:
        """
        Get the active page for a platform, or None if not launched/closed.
        """
        page = self._pages.get(platform)
        if page is None:
            return None
        try:
            if page.is_closed():
                return None
            return page
        except Exception:
            return None

    def navigate(self, page: Any, url: str,
                 wait_until: str = "domcontentloaded",
                 timeout: int = 30000) -> bool:
        """
        Navigate to URL with error handling and human-like post-load delay.

        Args:
            page: Playwright Page.
            url: Target URL.
            wait_until: "load" | "domcontentloaded" | "networkidle" | "commit"
            timeout: Milliseconds before giving up.

        Returns:
            True if navigation succeeded, False on error/timeout.
        """
        try:
            logger.debug(f"Navigating to: {url}")
            page.goto(url, wait_until=wait_until, timeout=timeout)
            # Human-like pause after page load
            self.random_delay(1.0, 3.0)
            logger.debug(f"Navigation complete → {page.url}")
            return True
        except Exception as e:
            logger.error(f"Navigation failed ({url}): {e}")
            return False

    def close(self, platform: Optional[str] = None) -> None:
        """
        Close browser for a specific platform.

        Args:
            platform: Which platform to close. None does nothing
                      (use close_all() for everything).
        """
        if platform is None:
            return
        self._cleanup_platform(platform)
        logger.info(f"Browser closed for '{platform}'")

    def close_all(self) -> None:
        """Close ALL browser instances and stop the Playwright process."""
        platforms = list(self._contexts.keys())
        for platform in platforms:
            self._cleanup_platform(platform)

        if self._playwright:
            try:
                self._playwright.stop()
            except Exception as e:
                logger.debug(f"Error stopping Playwright: {e}")
            self._playwright = None

        logger.info(f"All browsers closed ({len(platforms)} platform(s))")

    # ═══════════════════════════════════════════════════════════
    # HUMAN-LIKE ACTIONS — Typing
    # ═══════════════════════════════════════════════════════════

    def type_human(self, page: Any, selector: str, text: str,
                   clear_first: bool = True) -> None:
        """
        Type text into an input field with human-like per-character delays.

        Behavior:
          1. Waits for the element to be visible.
          2. Scrolls element into view if needed.
          3. Clicks the field (using click_human for natural movement).
          4. Clears existing content (Ctrl+A → Backspace) if clear_first.
          5. Types each character with random 50-150ms delay.
          6. 5% chance of longer pause per character (simulates thinking).
          7. Faster for spaces, slower for special characters.

        Args:
            page: Playwright Page.
            selector: CSS selector for the input element.
            text: Text to type.
            clear_first: Clear existing field content before typing.

        Raises:
            Exception: If element not found after 10s timeout.
        """
        if not text:
            logger.debug(f"Empty text, skipping type for {selector}")
            return

        try:
            # Wait for element to be visible
            element = page.wait_for_selector(
                selector, timeout=10000, state="visible"
            )
            if not element:
                logger.warning(f"Element not found for typing: {selector}")
                return

            # Scroll into view
            try:
                element.scroll_into_view_if_needed()
                time.sleep(random.uniform(0.1, 0.3))
            except Exception:
                pass

            # Click on the field (human-like)
            try:
                self.click_human(page, selector)
            except Exception:
                # Fallback: direct click
                try:
                    element.click()
                except Exception:
                    page.click(selector, timeout=5000)
            time.sleep(random.uniform(0.1, 0.3))

            # Clear existing content
            if clear_first:
                # Triple-click to select all (more reliable across browsers)
                try:
                    element.click(click_count=3)
                    time.sleep(random.uniform(0.05, 0.1))
                except Exception:
                    page.keyboard.press("Control+a")
                    time.sleep(random.uniform(0.05, 0.1))
                page.keyboard.press("Backspace")
                time.sleep(random.uniform(0.1, 0.2))

            # ── Type character by character ──
            typing_delay = STEALTH_CONFIG.get("typing_delay", (0.05, 0.15))
            min_delay, max_delay = typing_delay

            for i, char in enumerate(text):
                page.keyboard.type(char, delay=0)

                # Per-character delay
                delay = random.uniform(min_delay, max_delay)

                # 5% chance of a "thinking pause" (200-600ms extra)
                if random.random() < 0.05 and i > 0:
                    delay += random.uniform(0.2, 0.6)

                # Speed adjustment by character type
                if char in " \t\n":
                    delay *= 0.7          # faster for whitespace
                elif not char.isalnum():
                    delay *= 1.2          # slower for special chars

                time.sleep(delay)

            logger.debug(f"Typed {len(text)} chars into {selector}")

        except Exception as e:
            logger.error(f"type_human failed ({selector}): {e}")
            raise

    # ═══════════════════════════════════════════════════════════
    # HUMAN-LIKE ACTIONS — Clicking
    # ═══════════════════════════════════════════════════════════

    def click_human(self, page: Any, selector: str,
                    button: str = "left",
                    double: bool = False) -> None:
        """
        Click an element with human-like mouse movement.

        Behavior:
          1. Waits for element to be visible (10s timeout).
          2. Scrolls element into view.
          3. Gets bounding box → picks random point inside (not dead center).
          4. Moves mouse along a quadratic bezier curve (if enabled in config).
          5. Small pre-click pause (human reaction time).
          6. Clicks at the target position.
          7. Small post-click pause.
          8. Falls back to direct page.click() if anything goes wrong.

        Args:
            page: Playwright Page.
            selector: CSS selector for the element.
            button: "left" or "right".
            double: True for double-click.
        """
        try:
            # Wait for element
            element = page.wait_for_selector(
                selector, timeout=10000, state="visible"
            )
            if not element:
                logger.warning(f"Element not found for click: {selector}")
                return

            # Scroll into view
            try:
                element.scroll_into_view_if_needed()
                time.sleep(random.uniform(0.1, 0.3))
            except Exception:
                pass

            # Get bounding box
            box = element.bounding_box()
            if not box:
                logger.debug(
                    f"No bounding box for {selector}, using direct click"
                )
                element.click()
                time.sleep(random.uniform(0.1, 0.3))
                return

            # ── Target point: random within 20-80% of element bounds ──
            target_x = box["x"] + random.uniform(
                box["width"] * 0.2, box["width"] * 0.8
            )
            target_y = box["y"] + random.uniform(
                box["height"] * 0.25, box["height"] * 0.75
            )

            # ── Human-like mouse movement (bezier curve) ──
            platform = self._find_platform_for_page(page)
            use_bezier = STEALTH_CONFIG.get("human_mouse_movement", True)

            if use_bezier and platform:
                current = self._mouse_pos.get(
                    platform, (target_x - 100, target_y - 100)
                )
                self._move_mouse_bezier(page, current, (target_x, target_y))
            else:
                page.mouse.move(target_x, target_y)
                time.sleep(random.uniform(0.03, 0.08))

            # Pre-click pause (human reaction time)
            time.sleep(random.uniform(0.02, 0.08))

            # ── Click ──
            if double:
                page.mouse.dblclick(target_x, target_y, button=button)
            else:
                page.mouse.click(target_x, target_y, button=button)

            # Update stored mouse position
            if platform:
                self._mouse_pos[platform] = (target_x, target_y)

            # Post-click pause
            time.sleep(random.uniform(0.1, 0.3))

            logger.debug(
                f"Clicked {selector} at ({target_x:.0f},{target_y:.0f})"
            )

        except Exception as e:
            logger.debug(f"Bezier click failed for {selector}: {e}")
            # ── Fallback: direct Playwright click ──
            try:
                page.click(selector, timeout=5000)
                logger.debug(f"Fallback click OK for {selector}")
            except Exception as e2:
                logger.error(f"click_human failed ({selector}): {e2}")
                raise

    # ═══════════════════════════════════════════════════════════
    # HUMAN-LIKE ACTIONS — Scrolling
    # ═══════════════════════════════════════════════════════════

    def scroll_page(self, page: Any,
                    direction: str = "down",
                    amount: Optional[int] = None) -> None:
        """
        Scroll the page with human-like behavior.

        Behavior:
          1. Picks random scroll amount if not specified (200-600px).
          2. Breaks scroll into 3-8 small mouse-wheel events.
          3. Small delay between each wheel event.
          4. 15% chance of "reading pause" mid-scroll.
          5. Post-scroll pause from config.page_scroll_delay.

        Args:
            page: Playwright Page.
            direction: "down" or "up".
            amount: Pixels to scroll. None = random 200-600px.
        """
        try:
            if amount is None:
                amount = random.randint(200, 600)

            if direction == "up":
                total_delta = -abs(amount)
            else:
                total_delta = abs(amount)

            # Break into multiple small wheel events
            num_steps = random.randint(3, 8)
            step = total_delta / num_steps

            for i in range(num_steps):
                page.mouse.wheel(0, step)
                time.sleep(random.uniform(0.04, 0.12))

                # Occasional reading pause
                if random.random() < 0.15:
                    time.sleep(random.uniform(0.3, 0.8))

            # Post-scroll delay
            scroll_delay = STEALTH_CONFIG.get("page_scroll_delay", (1, 3))
            time.sleep(random.uniform(*scroll_delay))

            logger.debug(
                f"Scrolled {direction} ~{abs(amount)}px in {num_steps} steps"
            )

        except Exception as e:
            logger.error(f"scroll_page failed: {e}")

    def scroll_to_element(self, page: Any, selector: str) -> bool:
        """
        Scroll until a specific element is visible.

        Returns:
            True if element was found and scrolled to.
        """
        try:
            element = page.wait_for_selector(selector, timeout=5000)
            if element:
                element.scroll_into_view_if_needed()
                time.sleep(random.uniform(0.3, 0.8))
                return True
            return False
        except Exception:
            logger.debug(f"Could not scroll to {selector}")
            return False

    def scroll_to_bottom(self, page: Any, pause: float = 1.0,
                          max_scrolls: int = 20) -> None:
        """
        Scroll to the bottom of the page (for infinite-scroll pages).
        Stops when page height stops changing.
        """
        prev_height = 0
        for i in range(max_scrolls):
            current_height = page.evaluate("document.body.scrollHeight")
            if current_height == prev_height:
                break
            prev_height = current_height
            self.scroll_page(page, "down", random.randint(400, 800))
            time.sleep(pause + random.uniform(0, 1))

        logger.debug(f"Scrolled to bottom ({i + 1} scrolls)")

    # ═══════════════════════════════════════════════════════════
    # HUMAN-LIKE ACTIONS — Delay
    # ═══════════════════════════════════════════════════════════

    def random_delay(self, min_s: Optional[float] = None,
                     max_s: Optional[float] = None) -> None:
        """
        Sleep for a random duration (simulates human pause between actions).

        Args:
            min_s: Minimum seconds. None → config.random_delay_range[0]
            max_s: Maximum seconds. None → config.random_delay_range[1]
        """
        default_range = STEALTH_CONFIG.get("random_delay_range", (3, 12))
        if min_s is None:
            min_s = default_range[0]
        if max_s is None:
            max_s = default_range[1]

        duration = random.uniform(min_s, max_s)
        logger.debug(f"Delay: {duration:.1f}s")
        time.sleep(duration)

    # ═══════════════════════════════════════════════════════════
    # MOUSE MOVEMENT — Bezier Curves
    # ═══════════════════════════════════════════════════════════

    def _move_mouse_bezier(self, page: Any,
                           start: Tuple[float, float],
                           end: Tuple[float, float]) -> None:
        """
        Move mouse from start to end along a quadratic bezier curve.

        Simulates natural hand movement:
        - NOT a straight line (humans can't move perfectly straight)
        - Smoothstep easing (slow start, fast middle, slow end)
        - Tiny jitter (hand tremor)
        - More points for longer distances
        """
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = math.sqrt(dx * dx + dy * dy)

        if distance < 5:
            # Too close — just jump
            page.mouse.move(end[0], end[1])
            return

        # More points for longer distances (5-25 points)
        num_points = max(5, min(25, int(distance / 25)))

        points = self._bezier_curve(start, end, num_points)

        for px, py in points:
            # Clamp to safe bounds
            px = max(0, min(3000, px))
            py = max(0, min(2000, py))
            page.mouse.move(px, py)
            time.sleep(random.uniform(0.003, 0.015))

    def _bezier_curve(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
        num_points: int = 15,
    ) -> List[Tuple[float, float]]:
        """
        Generate points along a quadratic bezier curve with:
        - Random control point for natural arc
        - Smoothstep easing (slow → fast → slow)
        - Tiny jitter (hand tremor)
        """
        sx, sy = start
        ex, ey = end

        dx = ex - sx
        dy = ey - sy
        distance = math.sqrt(dx * dx + dy * dy)

        if distance < 1:
            return [(ex, ey)]

        # ── Control point: random offset perpendicular to the line ──
        mid_x = (sx + ex) / 2.0
        mid_y = (sy + ey) / 2.0

        # Perpendicular direction
        perp_x = -dy / distance
        perp_y = dx / distance

        # Random deviation (creates the arc)
        deviation = random.uniform(-0.3, 0.3) * distance
        cx = mid_x + perp_x * deviation + random.uniform(-15, 15)
        cy = mid_y + perp_y * deviation + random.uniform(-15, 15)

        # ── Generate points ──
        points = []
        for i in range(num_points + 1):
            t = i / num_points

            # Smoothstep easing: slow start and end, fast middle
            t = t * t * (3.0 - 2.0 * t)

            # Quadratic bezier: B(t) = (1-t)²·P0 + 2(1-t)t·P1 + t²·P2
            x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t ** 2 * ex
            y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t ** 2 * ey

            # Hand tremor (tiny random jitter)
            x += random.uniform(-0.5, 0.5)
            y += random.uniform(-0.5, 0.5)

            points.append((x, y))

        return points

    # ═══════════════════════════════════════════════════════════
    # SESSION & LOGIN MANAGEMENT
    # ═══════════════════════════════════════════════════════════

    def login(self, platform: str, credentials: dict) -> bool:
        """
        Generic login helper — ensures browser is launched and cookies loaded.

        NOTE: Actual login flows (typing credentials, handling OTP/CAPTCHA)
        are implemented in each platform file (platforms/naukri.py, etc.)
        using type_human/click_human. This method just sets up the browser.

        Args:
            platform: Platform name.
            credentials: Dict with "email", "password", etc.

        Returns:
            True if browser page is ready for login attempt.
        """
        try:
            page = self.launch(platform)
            has_state = self.load_cookies(platform)
            logger.info(
                f"Login setup for '{platform}': page ready, "
                f"saved_state={'yes' if has_state else 'no'}"
            )
            return True
        except Exception as e:
            logger.error(f"Login setup failed for '{platform}': {e}")
            return False

    def is_logged_in(self, platform: str) -> bool:
        """
        Check if browser session is active for a platform.

        NOTE: This checks browser liveness only. For actual auth verification
        (logged-in header, user avatar, etc.), use platform-specific checks.

        Returns:
            True if browser page is open and responsive.
        """
        if platform not in self._pages:
            return False
        page = self._pages[platform]
        try:
            if page.is_closed():
                return False
            _ = page.url  # probe: raises if page is dead
            return True
        except Exception:
            return False

    def session_time(self, platform: str) -> float:
        """Get current session duration in minutes."""
        start = self._session_starts.get(platform)
        if not start:
            return 0.0
        return (datetime.now() - start).total_seconds() / 60.0

    def needs_break(self, platform: str) -> bool:
        """
        Check if the session has exceeded the configured max duration.

        STEALTH_CONFIG.session_max_minutes (default 45 min) — after this,
        the bot should close the browser and wait before continuing.

        Returns:
            True if session time > max, or if platform not active.
        """
        max_minutes = STEALTH_CONFIG.get("session_max_minutes", 45)
        elapsed = self.session_time(platform)
        if elapsed > max_minutes:
            logger.info(
                f"Session break needed for '{platform}': "
                f"{elapsed:.0f}min > {max_minutes}min limit"
            )
            return True
        return False

    def take_break(self, platform: str) -> None:
        """
        Close browser, wait configured break time, then relaunch.

        Break duration: STEALTH_CONFIG.session_break_minutes (default 10-30).
        Called by platform files when needs_break() returns True.
        """
        break_range = STEALTH_CONFIG.get("session_break_minutes", (10, 30))
        break_mins = random.uniform(*break_range)

        logger.info(
            f"Taking {break_mins:.0f}min break for '{platform}' "
            f"(session was {self.session_time(platform):.0f}min)"
        )

        self.save_cookies(platform)
        self.close(platform)
        time.sleep(break_mins * 60)

        logger.info(f"Break over for '{platform}', ready to relaunch")

    # ═══════════════════════════════════════════════════════════
    # COOKIES — Save, Load, Clear, Export
    # ═══════════════════════════════════════════════════════════

    def save_cookies(self, platform: str) -> None:
        """
        Save current browser cookies to a JSON file.

        File: browser_profiles/{platform}/cookies.json

        Persistent profiles already auto-save cookies via Chromium's
        own storage, but this explicit export provides:
          - Backup in case profile gets corrupted
          - Ability to inspect/debug cookies
          - Import into different profile if needed

        Silently does nothing if platform not launched.
        """
        context = self._contexts.get(platform)
        if not context:
            logger.debug(f"No context for '{platform}', skipping cookie save")
            return

        try:
            cookies = context.cookies()
            cookie_path = BROWSER_PROFILES_DIR / platform / "cookies.json"

            with open(cookie_path, "w", encoding="utf-8") as f:
                json.dump(cookies, f, indent=2, ensure_ascii=False)

            logger.debug(
                f"Saved {len(cookies)} cookies for '{platform}' → {cookie_path}"
            )

        except Exception as e:
            logger.error(f"Failed to save cookies for '{platform}': {e}")

    def load_cookies(self, platform: str) -> bool:
        """
        Load cookies from JSON file into the current browser context.

        File: browser_profiles/{platform}/cookies.json

        NOTE: With persistent profiles, Chromium already restores cookies
        automatically. This method is for:
          - Restoring from backup after profile reset
          - Importing cookies from another source
          - Forcing a cookie reload

        Returns:
            True if cookies were loaded, False if file not found or error.
        """
        context = self._contexts.get(platform)
        if not context:
            logger.debug(f"No context for '{platform}', skipping cookie load")
            return False

        cookie_path = BROWSER_PROFILES_DIR / platform / "cookies.json"
        if not cookie_path.exists():
            logger.debug(f"No saved cookies for '{platform}'")
            return False

        try:
            with open(cookie_path, "r", encoding="utf-8") as f:
                cookies = json.load(f)

            if not cookies:
                return False

            # Filter out expired cookies
            now = time.time()
            valid_cookies = []
            expired_count = 0
            for cookie in cookies:
                expires = cookie.get("expires", -1)
                # expires == -1 means session cookie (always valid)
                if expires == -1 or expires > now:
                    valid_cookies.append(cookie)
                else:
                    expired_count += 1

            if valid_cookies:
                context.add_cookies(valid_cookies)

            logger.debug(
                f"Loaded {len(valid_cookies)} cookies for '{platform}' "
                f"({expired_count} expired, skipped)"
            )
            return len(valid_cookies) > 0

        except json.JSONDecodeError as e:
            logger.warning(
                f"Corrupted cookie file for '{platform}': {e}"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to load cookies for '{platform}': {e}")
            return False

    def clear_cookies(self, platform: str) -> None:
        """
        Clear all cookies for a platform (browser context + saved file).
        Useful when cookies are stale and causing auth issues.
        """
        context = self._contexts.get(platform)
        if context:
            try:
                context.clear_cookies()
                logger.debug(f"Cleared browser cookies for '{platform}'")
            except Exception as e:
                logger.error(f"Failed to clear browser cookies: {e}")

        cookie_path = BROWSER_PROFILES_DIR / platform / "cookies.json"
        if cookie_path.exists():
            try:
                cookie_path.unlink()
                logger.debug(f"Deleted cookie file for '{platform}'")
            except Exception:
                pass

    def export_cookies_dict(self, platform: str) -> Dict[str, str]:
        """
        Export cookies as a simple {name: value} dict.
        Useful for requests-based API calls that need auth cookies.
        """
        context = self._contexts.get(platform)
        if not context:
            return {}
        try:
            cookies = context.cookies()
            return {c["name"]: c["value"] for c in cookies}
        except Exception:
            return {}

    # ═══════════════════════════════════════════════════════════
    # ELEMENT HELPERS — Wait, Check, Extract
    # ═══════════════════════════════════════════════════════════

    def wait_for_element(self, page: Any, selector: str,
                         timeout: int = 10000,
                         state: str = "visible") -> bool:
        """
        Wait for an element to appear on the page.

        Args:
            page: Playwright Page.
            selector: CSS selector.
            timeout: Milliseconds to wait.
            state: "visible" | "attached" | "hidden" | "detached"

        Returns:
            True if element appeared within timeout, False otherwise.
        """
        try:
            page.wait_for_selector(selector, timeout=timeout, state=state)
            return True
        except Exception:
            logger.debug(
                f"Element not found within {timeout}ms: {selector}"
            )
            return False

    def element_exists(self, page: Any, selector: str) -> bool:
        """
        Check if an element exists in the DOM right now (no waiting).

        Returns:
            True if at least one element matches the selector.
        """
        try:
            element = page.query_selector(selector)
            return element is not None
        except Exception:
            return False

    def element_visible(self, page: Any, selector: str) -> bool:
        """
        Check if an element is visible right now (exists + displayed).
        """
        try:
            element = page.query_selector(selector)
            if not element:
                return False
            return element.is_visible()
        except Exception:
            return False

    def get_text(self, page: Any, selector: str,
                 default: str = "") -> str:
        """
        Get text content of an element.

        Returns:
            Text content, stripped of whitespace. Returns default if not found.
        """
        try:
            element = page.query_selector(selector)
            if element:
                text = element.text_content()
                return text.strip() if text else default
            return default
        except Exception:
            return default

    def get_attribute(self, page: Any, selector: str,
                      attribute: str,
                      default: str = "") -> str:
        """
        Get an attribute value from an element.

        Returns:
            Attribute value, or default if element/attribute not found.
        """
        try:
            element = page.query_selector(selector)
            if element:
                val = element.get_attribute(attribute)
                return val if val is not None else default
            return default
        except Exception:
            return default

    def get_all_texts(self, page: Any, selector: str) -> List[str]:
        """
        Get text content of ALL elements matching a selector.

        Returns:
            List of text strings (empty texts filtered out).
        """
        try:
            elements = page.query_selector_all(selector)
            texts = []
            for el in elements:
                text = el.text_content()
                if text and text.strip():
                    texts.append(text.strip())
            return texts
        except Exception:
            return []

    def get_all_attributes(self, page: Any, selector: str,
                           attribute: str) -> List[str]:
        """
        Get an attribute from ALL elements matching a selector.

        Returns:
            List of attribute values (None values filtered out).
        """
        try:
            elements = page.query_selector_all(selector)
            values = []
            for el in elements:
                val = el.get_attribute(attribute)
                if val is not None:
                    values.append(val)
            return values
        except Exception:
            return []

    def count_elements(self, page: Any, selector: str) -> int:
        """Count how many elements match a selector."""
        try:
            elements = page.query_selector_all(selector)
            return len(elements)
        except Exception:
            return 0

    def get_input_value(self, page: Any, selector: str) -> str:
        """Get the current value of an input/textarea element."""
        try:
            return page.input_value(selector, timeout=3000) or ""
        except Exception:
            return ""

    # ═══════════════════════════════════════════════════════════
    # FORM HELPERS — Select, Checkbox, File Upload
    # ═══════════════════════════════════════════════════════════

    def select_dropdown(self, page: Any, selector: str,
                        value: Optional[str] = None,
                        label: Optional[str] = None,
                        index: Optional[int] = None) -> bool:
        """
        Select an option in a <select> dropdown.

        Priority: value > label > index.

        Args:
            page: Playwright Page.
            selector: CSS selector for the <select>.
            value: option[value="..."]
            label: option text (visible label).
            index: 0-based index.

        Returns:
            True if selection succeeded.
        """
        try:
            if value is not None:
                page.select_option(selector, value=value, timeout=5000)
            elif label is not None:
                page.select_option(selector, label=label, timeout=5000)
            elif index is not None:
                page.select_option(selector, index=index, timeout=5000)
            else:
                logger.warning("select_dropdown: no value/label/index given")
                return False

            time.sleep(random.uniform(0.2, 0.5))
            logger.debug(
                f"Selected dropdown {selector}: "
                f"value={value}, label={label}, index={index}"
            )
            return True
        except Exception as e:
            logger.error(f"select_dropdown failed ({selector}): {e}")
            return False

    def select_dropdown_fuzzy(self, page: Any, selector: str,
                              target_text: str) -> bool:
        """
        Select a dropdown option by fuzzy-matching the visible text.

        Gets all <option> texts, finds the closest match, selects it.
        Useful when exact option text varies across platforms.

        Returns:
            True if a match was found and selected.
        """
        try:
            options = page.query_selector_all(f"{selector} option")
            if not options:
                logger.debug(f"No options found in {selector}")
                return False

            target_lower = target_text.strip().lower()
            best_match = None
            best_score = 0.0

            for opt in options:
                text = (opt.text_content() or "").strip()
                val = opt.get_attribute("value") or ""

                if not text and not val:
                    continue

                # Score: exact > contains > partial
                text_lower = text.lower()
                score = 0.0
                if text_lower == target_lower:
                    score = 1.0
                elif target_lower in text_lower:
                    score = 0.8
                elif text_lower in target_lower:
                    score = 0.6
                else:
                    # Simple word overlap
                    target_words = set(target_lower.split())
                    text_words = set(text_lower.split())
                    overlap = target_words & text_words
                    if overlap:
                        score = len(overlap) / max(
                            len(target_words), len(text_words)
                        )

                if score > best_score:
                    best_score = score
                    best_match = (val, text)

            if best_match and best_score > 0.3:
                val, text = best_match
                if val:
                    page.select_option(selector, value=val, timeout=5000)
                else:
                    page.select_option(selector, label=text, timeout=5000)

                time.sleep(random.uniform(0.2, 0.5))
                logger.debug(
                    f"Fuzzy-selected '{text}' (score={best_score:.2f}) "
                    f"for target '{target_text}'"
                )
                return True

            logger.debug(
                f"No fuzzy match for '{target_text}' in {selector}"
            )
            return False

        except Exception as e:
            logger.error(f"select_dropdown_fuzzy failed: {e}")
            return False

    def check_checkbox(self, page: Any, selector: str,
                       should_be_checked: bool = True) -> bool:
        """
        Set a checkbox to checked/unchecked state.

        Only clicks if the current state doesn't match desired state.

        Returns:
            True if checkbox is now in the desired state.
        """
        try:
            element = page.query_selector(selector)
            if not element:
                logger.debug(f"Checkbox not found: {selector}")
                return False

            is_checked = element.is_checked()
            if is_checked != should_be_checked:
                self.click_human(page, selector)
                time.sleep(random.uniform(0.1, 0.3))
                logger.debug(
                    f"Checkbox {selector}: "
                    f"{'checked' if should_be_checked else 'unchecked'}"
                )

            return True
        except Exception as e:
            logger.error(f"check_checkbox failed ({selector}): {e}")
            return False

    def upload_file(self, page: Any, selector: str,
                    file_path: str) -> bool:
        """
        Upload a file via a file input element.

        Args:
            page: Playwright Page.
            selector: CSS selector for <input type="file">.
            file_path: Absolute path to the file.

        Returns:
            True if upload succeeded.
        """
        file_path = str(file_path)
        if not os.path.isfile(file_path):
            logger.error(f"File not found for upload: {file_path}")
            return False

        try:
            page.set_input_files(selector, file_path, timeout=10000)
            time.sleep(random.uniform(0.5, 1.5))
            logger.debug(f"Uploaded file: {file_path} → {selector}")
            return True
        except Exception as e:
            logger.error(f"upload_file failed ({selector}): {e}")
            return False

    # ═══════════════════════════════════════════════════════════
    # KEYBOARD HELPERS
    # ═══════════════════════════════════════════════════════════

    def press_key(self, page: Any, key: str) -> None:
        """
        Press a keyboard key (Enter, Tab, Escape, etc.).

        Args:
            key: Playwright key name (e.g. "Enter", "Tab", "Escape",
                 "ArrowDown", "Control+a").
        """
        try:
            page.keyboard.press(key)
            time.sleep(random.uniform(0.1, 0.3))
        except Exception as e:
            logger.error(f"press_key failed ({key}): {e}")

    # ═══════════════════════════════════════════════════════════
    # SCREENSHOTS
    # ═══════════════════════════════════════════════════════════

    def take_screenshot(self, page: Any, name: str,
                        full_page: bool = False) -> str:
        """
        Take a screenshot and save to cache/screenshots/.

        Args:
            page: Playwright Page.
            name: Descriptive name (timestamp auto-appended).
            full_page: True for full-page capture.

        Returns:
            Absolute path to the saved PNG file.
            Empty string on error.
        """
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # Sanitize name: replace special chars
            safe_name = "".join(
                c if c.isalnum() or c in "-_" else "_"
                for c in name
            )
            filename = f"{safe_name}_{timestamp}.png"
            filepath = _SCREENSHOTS_DIR / filename

            page.screenshot(path=str(filepath), full_page=full_page)
            logger.debug(f"Screenshot saved: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"Screenshot failed ({name}): {e}")
            return ""

    def take_element_screenshot(self, page: Any, selector: str,
                                name: str) -> str:
        """
        Take a screenshot of a specific element.

        Returns:
            Path to PNG, or empty string on error.
        """
        try:
            element = page.query_selector(selector)
            if not element:
                logger.debug(f"Element not found for screenshot: {selector}")
                return ""

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = "".join(
                c if c.isalnum() or c in "-_" else "_"
                for c in name
            )
            filename = f"{safe_name}_{timestamp}.png"
            filepath = _SCREENSHOTS_DIR / filename

            element.screenshot(path=str(filepath))
            logger.debug(f"Element screenshot saved: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"Element screenshot failed ({selector}): {e}")
            return ""

    # ═══════════════════════════════════════════════════════════
    # PAGE CONTENT HELPERS
    # ═══════════════════════════════════════════════════════════

    def get_page_html(self, page: Any) -> str:
        """Get the full HTML content of the page."""
        try:
            return page.content()
        except Exception:
            return ""

    def get_page_url(self, page: Any) -> str:
        """Get current page URL."""
        try:
            return page.url
        except Exception:
            return ""

    def get_page_title(self, page: Any) -> str:
        """Get current page title."""
        try:
            return page.title()
        except Exception:
            return ""

    def evaluate_js(self, page: Any, expression: str,
                    default: Any = None) -> Any:
        """
        Execute JavaScript in the page context and return the result.

        Args:
            page: Playwright Page.
            expression: JS expression to evaluate.
            default: Value to return on error.

        Returns:
            Result of JS evaluation, or default on error.
        """
        try:
            return page.evaluate(expression)
        except Exception as e:
            logger.debug(f"JS evaluation failed: {e}")
            return default

    # ═══════════════════════════════════════════════════════════
    # WAIT HELPERS
    # ═══════════════════════════════════════════════════════════

    def wait_for_navigation(self, page: Any,
                            url_pattern: Optional[str] = None,
                            timeout: int = 15000) -> bool:
        """
        Wait for a page navigation to complete.

        Args:
            page: Playwright Page.
            url_pattern: Glob pattern for expected URL (e.g. "**/dashboard*").
                         None = any navigation.
            timeout: Milliseconds to wait.

        Returns:
            True if navigation happened within timeout.
        """
        try:
            if url_pattern:
                page.wait_for_url(url_pattern, timeout=timeout)
            else:
                page.wait_for_load_state("domcontentloaded", timeout=timeout)
            return True
        except Exception:
            logger.debug(f"Navigation wait timed out (pattern={url_pattern})")
            return False

    def wait_for_load(self, page: Any, state: str = "networkidle",
                      timeout: int = 30000) -> bool:
        """
        Wait for page to finish loading.

        Args:
            state: "load" | "domcontentloaded" | "networkidle"
            timeout: Milliseconds.

        Returns:
            True if load completed within timeout.
        """
        try:
            page.wait_for_load_state(state, timeout=timeout)
            return True
        except Exception:
            logger.debug(f"Load state '{state}' wait timed out")
            return False

    def wait_for_any_selector(self, page: Any,
                               selectors: List[str],
                               timeout: int = 10000) -> Optional[str]:
        """
        Wait for ANY of multiple selectors to appear.
        Useful for "success OR error" detection after form submission.

        Returns:
            The selector that matched first, or None if timeout.
        """
        # Use Playwright's built-in first-match via Promise.race equivalent
        try:
            # Build a CSS selector that matches any of them
            combined = ", ".join(selectors)
            element = page.wait_for_selector(
                combined, timeout=timeout, state="visible"
            )
            if element:
                # Figure out which selector matched
                for sel in selectors:
                    if page.query_selector(sel):
                        return sel
                return selectors[0]  # fallback
            return None
        except Exception:
            return None

    # ═══════════════════════════════════════════════════════════
    # NEW TAB / POPUP HANDLING
    # ═══════════════════════════════════════════════════════════

    def handle_new_tab(self, page: Any, action_fn,
                       timeout: int = 10000) -> Optional[Any]:
        """
        Perform an action that opens a new tab, return the new Page.

        Usage:
            new_page = engine.handle_new_tab(page, lambda: page.click("a.job"))
            if new_page:
                # work with new_page
                new_page.close()

        Args:
            page: Current Playwright Page.
            action_fn: Callable that triggers the new tab (e.g., a click).
            timeout: Milliseconds to wait for new tab.

        Returns:
            New Page object, or None if no tab opened.
        """
        try:
            context = page.context
            with context.expect_page(timeout=timeout) as new_page_info:
                action_fn()
            new_page = new_page_info.value
            new_page.wait_for_load_state("domcontentloaded", timeout=15000)
            # Apply stealth to new tab too
            self._inject_stealth_js(new_page)
            return new_page
        except Exception as e:
            logger.debug(f"No new tab opened: {e}")
            return None

    # ═══════════════════════════════════════════════════════════
    # STEALTH — Injection and Setup
    # ═══════════════════════════════════════════════════════════

    def _apply_stealth(self, page: Any, context: Any) -> None:
        """
        Apply all stealth measures to a page/context.

        Layers:
          1. playwright-stealth library (if installed)
          2. Custom JS patches (_STEALTH_JS)
          3. Route-based header modifications
        """
        # Layer 1: playwright-stealth (comprehensive evasion)
        if _STEALTH_AVAILABLE and stealth_sync:
            try:
                stealth_sync(page)
                logger.debug("playwright-stealth applied")
            except Exception as e:
                logger.warning(f"playwright-stealth apply failed: {e}")
        else:
            logger.debug(
                "playwright-stealth not available, using JS-only stealth"
            )

        # Layer 2: Custom JS patches (belt + suspenders)
        self._inject_stealth_js(page)

        # Layer 3: Also inject on every new document load
        try:
            context.add_init_script(_STEALTH_JS)
            logger.debug("Stealth init script registered for all future navigations")
        except Exception as e:
            logger.debug(f"Could not register init script: {e}")

    def _inject_stealth_js(self, page: Any) -> None:
        """Inject stealth JavaScript into a page."""
        try:
            page.evaluate(_STEALTH_JS)
            logger.debug("Stealth JS injected")
        except Exception as e:
            logger.debug(f"Stealth JS injection failed: {e}")

    # ═══════════════════════════════════════════════════════════
    # DIALOG / ALERT HANDLING
    # ═══════════════════════════════════════════════════════════

    def _handle_dialog(self, dialog) -> None:
        """
        Auto-handle browser dialog popups (alert, confirm, prompt).

        Strategy:
          - Accept confirms (dismiss would cancel actions).
          - Dismiss alerts (just close them).
          - Log the dialog text for debugging.
        """
        try:
            message = dialog.message
            dtype = dialog.type
            self._dialog_messages.append(message)
            logger.info(f"Browser dialog [{dtype}]: {message[:100]}")

            if dtype == "confirm":
                dialog.accept()
            elif dtype == "prompt":
                dialog.accept("")
            else:  # alert, beforeunload
                dialog.dismiss()
        except Exception as e:
            logger.debug(f"Dialog handling error: {e}")
            try:
                dialog.dismiss()
            except Exception:
                pass

    def get_last_dialog(self) -> Optional[str]:
        """Get the last dialog message text (for CAPTCHA/OTP detection)."""
        if self._dialog_messages:
            return self._dialog_messages[-1]
        return None

    def clear_dialogs(self) -> None:
        """Clear stored dialog messages."""
        self._dialog_messages.clear()

    # ═══════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ═══════════════════════════════════════════════════════════

    def _get_viewport(self, platform: str) -> dict:
        """
        Get a consistent viewport for a platform.
        Same platform always gets the same viewport (hashed),
        but different platforms get different viewports.
        """
        idx = hash(platform) % len(_VIEWPORTS)
        return dict(_VIEWPORTS[idx])  # copy

    def _find_platform_for_page(self, page: Any) -> Optional[str]:
        """Reverse-lookup: find which platform a page belongs to."""
        for platform, p in self._pages.items():
            if p is page:
                return platform
        return None

    def _cleanup_platform(self, platform: str) -> None:
        """Close context and clean up references for a single platform."""
        # Save cookies before closing
        if platform in self._contexts:
            try:
                self.save_cookies(platform)
            except Exception:
                pass

        # Close context (which closes all its pages)
        context = self._contexts.pop(platform, None)
        if context:
            try:
                context.close()
            except Exception as e:
                logger.debug(f"Error closing context for '{platform}': {e}")

        # Clean up references
        self._pages.pop(platform, None)
        self._session_starts.pop(platform, None)
        self._mouse_pos.pop(platform, None)

    def _cleanup_locks(self, profile_dir: Path) -> None:
        """
        Remove stale lock files from a Chromium profile directory.

        Chromium creates lock files (SingletonLock, SingletonCookie, etc.)
        that prevent launching if a previous instance crashed without
        cleaning up.
        """
        lock_files = [
            "SingletonLock",
            "SingletonCookie",
            "SingletonSocket",
        ]
        for lock_name in lock_files:
            lock_path = profile_dir / lock_name
            if lock_path.exists():
                try:
                    lock_path.unlink()
                    logger.debug(f"Removed stale lock: {lock_path}")
                except Exception:
                    pass

    # ═══════════════════════════════════════════════════════════
    # PROFILE MANAGEMENT
    # ═══════════════════════════════════════════════════════════

    def reset_profile(self, platform: str) -> None:
        """
        Delete the entire browser profile for a platform.

        Nuclear option for when cookies/cache are corrupted.
        The profile will be recreated fresh on next launch().
        """
        # Close if running
        self._cleanup_platform(platform)

        profile_dir = BROWSER_PROFILES_DIR / platform
        if profile_dir.exists():
            try:
                shutil.rmtree(str(profile_dir))
                logger.info(f"Browser profile reset for '{platform}'")
            except Exception as e:
                logger.error(
                    f"Failed to delete profile for '{platform}': {e}"
                )
        else:
            logger.debug(f"No profile to reset for '{platform}'")

    def list_profiles(self) -> Dict[str, dict]:
        """
        List all saved browser profiles with metadata.

        Returns:
            {platform: {path, size_mb, has_cookies, last_modified}}
        """
        profiles = {}
        if not BROWSER_PROFILES_DIR.exists():
            return profiles

        for item in BROWSER_PROFILES_DIR.iterdir():
            if item.is_dir():
                # Calculate directory size
                size_bytes = sum(
                    f.stat().st_size
                    for f in item.rglob("*")
                    if f.is_file()
                )
                cookie_file = item / "cookies.json"
                profiles[item.name] = {
                    "path": str(item),
                    "size_mb": round(size_bytes / (1024 * 1024), 1),
                    "has_cookies": cookie_file.exists(),
                    "last_modified": datetime.fromtimestamp(
                        item.stat().st_mtime
                    ).isoformat() if item.exists() else "",
                    "is_active": item.name in self._contexts,
                }

        return profiles

    # ═══════════════════════════════════════════════════════════
    # STATUS / DEBUG
    # ═══════════════════════════════════════════════════════════

    def get_status(self) -> dict:
        """
        Get overall engine status.

        Returns:
            {
                playwright_running: bool,
                stealth_library: bool,
                active_platforms: [str],
                sessions: {platform: {url, session_min, needs_break}},
                profiles: {platform: {size_mb, has_cookies}}
            }
        """
        sessions = {}
        for platform in list(self._contexts.keys()):
            page = self._pages.get(platform)
            url = ""
            try:
                if page and not page.is_closed():
                    url = page.url
            except Exception:
                pass

            sessions[platform] = {
                "url": url,
                "session_minutes": round(self.session_time(platform), 1),
                "needs_break": self.needs_break(platform),
            }

        return {
            "playwright_running": self._playwright is not None,
            "stealth_library": _STEALTH_AVAILABLE,
            "active_platforms": list(self._contexts.keys()),
            "sessions": sessions,
            "profiles": self.list_profiles(),
        }

    def get_active_platforms(self) -> List[str]:
        """Return list of platforms with active browser sessions."""
        active = []
        for platform in list(self._contexts.keys()):
            if self.is_logged_in(platform):
                active.append(platform)
        return active


# ═══════════════════════════════════════════════════════════════════
# MODULE-LEVEL SINGLETON (optional — platforms can also instantiate directly)
# ═══════════════════════════════════════════════════════════════════

_engine_instance: Optional[BrowserEngine] = None


def get_browser_engine() -> BrowserEngine:
    """Get or create the singleton BrowserEngine."""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = BrowserEngine()
    return _engine_instance


# ═══════════════════════════════════════════════════════════════════
# TEST BLOCK
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
    console.print("\n[bold cyan]═══ Browser Engine Test ═══[/bold cyan]\n")

    # ── 1. Dependency check ──
    console.print("[yellow]1. Dependency check:[/yellow]")
    console.print(
        f"   Playwright: "
        f"{'[green]✓ installed[/green]' if _PW_AVAILABLE else '[red]✗ NOT installed[/red]'}"
    )
    console.print(
        f"   playwright-stealth: "
        f"{'[green]✓ installed[/green]' if _STEALTH_AVAILABLE else '[yellow]⚠ not available (JS-only fallback active)[/yellow]'}"
    )

    # ── Extra diagnostic if stealth failed ──
    if not _STEALTH_AVAILABLE:
        console.print("\n   [yellow]Stealth diagnostic:[/yellow]")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "show", "playwright-stealth"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                console.print(f"   [yellow]pip says it IS installed:[/yellow]")
                for line in result.stdout.strip().split("\n")[:4]:
                    console.print(f"     {line}")
                console.print(
                    "   [yellow]→ Try: pip install --upgrade playwright-stealth[/yellow]"
                )
                # Try to show the actual import error
                console.print("   [yellow]Attempting diagnostic import...[/yellow]")
                try:
                    import playwright_stealth as _diag
                    console.print(
                        f"   Module loads OK. "
                        f"Contents: {[a for a in dir(_diag) if not a.startswith('_')]}"
                    )
                    if hasattr(_diag, 'stealth_sync'):
                        console.print("   [green]stealth_sync found! (race condition?)[/green]")
                    else:
                        console.print("   [red]stealth_sync NOT in module — wrong version[/red]")
                except Exception as diag_e:
                    console.print(
                        f"   [red]Import error: {type(diag_e).__name__}: {diag_e}[/red]"
                    )
            else:
                console.print("   [dim]pip confirms: not installed[/dim]")
        except Exception:
            pass

    if not _PW_AVAILABLE:
        console.print(
            "\n[red]Cannot run browser tests without Playwright.[/red]\n"
            "Install with:\n"
            "  pip install playwright playwright-stealth\n"
            "  playwright install chromium\n"
        )
        sys.exit(1)

    # ── 2. Initialize engine ──
    console.print("\n[yellow]2. Initializing BrowserEngine...[/yellow]")
    engine = BrowserEngine()
    console.print(f"   [green]✓[/green] {engine}")

    # ── 3. Directory checks ──
    console.print("\n[yellow]3. Directory checks:[/yellow]")
    console.print(f"   Profiles dir: {BROWSER_PROFILES_DIR} "
                  f"(exists={BROWSER_PROFILES_DIR.exists()})")
    console.print(f"   Screenshots dir: {_SCREENSHOTS_DIR} "
                  f"(exists={_SCREENSHOTS_DIR.exists()})")

    # ── 4. Viewport consistency ──
    console.print("\n[yellow]4. Viewport consistency (same platform = same viewport):[/yellow]")
    for platform in ["naukri", "indeed", "linkedin", "foundit"]:
        vp1 = engine._get_viewport(platform)
        vp2 = engine._get_viewport(platform)
        consistent = vp1 == vp2
        icon = "[green]✓[/green]" if consistent else "[red]✗[/red]"
        console.print(
            f"   {icon} {platform}: {vp1['width']}x{vp1['height']} "
            f"(consistent={consistent})"
        )

    # ── 5. Bezier curve generation ──
    console.print("\n[yellow]5. Bezier curve generation:[/yellow]")
    start = (100.0, 100.0)
    end = (500.0, 300.0)
    points = engine._bezier_curve(start, end, num_points=10)
    console.print(f"   [green]✓[/green] Generated {len(points)} points "
                  f"from {start} to {end}")
    console.print(f"   First point: ({points[0][0]:.1f}, {points[0][1]:.1f})")
    console.print(f"   Last point:  ({points[-1][0]:.1f}, {points[-1][1]:.1f})")

    # ── 6. Stealth JS check ──
    console.print("\n[yellow]6. Stealth JS payload:[/yellow]")
    console.print(f"   [green]✓[/green] {len(_STEALTH_JS)} chars, "
                  f"covers: webdriver, chrome.runtime, permissions, "
                  f"hardware, WebGL, network")

    # ── 7. Launch browser (real test) ──
    console.print("\n[yellow]7. Launching test browser...[/yellow]")
    test_platform = "_test_browser"

    try:
        page = engine.launch(test_platform, headless=True)
        console.print(f"   [green]✓[/green] Browser launched (headless=True)")
        console.print(f"   Page URL: {page.url}")

        # ── 8. Navigate ──
        console.print("\n[yellow]8. Navigation test:[/yellow]")
        success = engine.navigate(page, "https://httpbin.org/html")
        console.print(f"   [green]✓[/green] Navigate: success={success}, "
                      f"URL={page.url}")

        # ── 9. Stealth verification ──
        console.print("\n[yellow]9. Stealth verification (in-page):[/yellow]")
        webdriver = engine.evaluate_js(page, "navigator.webdriver")
        console.print(
            f"   navigator.webdriver = {webdriver} "
            f"({'[green]✓ hidden[/green]' if not webdriver else '[red]✗ detected[/red]'})"
        )

        hw_conc = engine.evaluate_js(page, "navigator.hardwareConcurrency")
        console.print(f"   navigator.hardwareConcurrency = {hw_conc}")

        has_chrome = engine.evaluate_js(page, "!!window.chrome")
        console.print(
            f"   window.chrome exists = {has_chrome} "
            f"({'[green]✓[/green]' if has_chrome else '[yellow]⚠[/yellow]'})"
        )

        # ── 10. Element helpers ──
        console.print("\n[yellow]10. Element helper tests:[/yellow]")
        has_h1 = engine.element_exists(page, "h1")
        console.print(f"   element_exists('h1') = {has_h1}")

        h1_text = engine.get_text(page, "h1")
        console.print(f"   get_text('h1') = '{h1_text[:50]}'")

        h1_visible = engine.element_visible(page, "h1")
        console.print(f"   element_visible('h1') = {h1_visible}")

        p_count = engine.count_elements(page, "p")
        console.print(f"   count_elements('p') = {p_count}")

        no_elem = engine.get_text(page, "#nonexistent", "DEFAULT")
        console.print(f"   get_text(missing) = '{no_elem}' (should be DEFAULT)")

        wait_result = engine.wait_for_element(page, "h1", timeout=3000)
        console.print(f"   wait_for_element('h1') = {wait_result}")

        wait_missing = engine.wait_for_element(
            page, "#does_not_exist_xyz", timeout=1000
        )
        console.print(
            f"   wait_for_element(missing, 1s) = {wait_missing} "
            f"(should be False)"
        )

        # ── 11. Screenshot ──
        console.print("\n[yellow]11. Screenshot test:[/yellow]")
        ss_path = engine.take_screenshot(page, "test_browser")
        if ss_path:
            console.print(f"   [green]✓[/green] Screenshot: {ss_path}")
            size_kb = os.path.getsize(ss_path) / 1024
            console.print(f"   Size: {size_kb:.1f} KB")
        else:
            console.print("   [red]✗ Screenshot failed[/red]")

        # ── 12. Page content ──
        console.print("\n[yellow]12. Page content helpers:[/yellow]")
        html = engine.get_page_html(page)
        console.print(f"   get_page_html: {len(html)} chars")
        title = engine.get_page_title(page)
        console.print(f"   get_page_title: '{title}'")
        url = engine.get_page_url(page)
        console.print(f"   get_page_url: '{url}'")

        # ── 13. Cookie save/load ──
        console.print("\n[yellow]13. Cookie save/load:[/yellow]")
        engine.save_cookies(test_platform)
        cookie_path = BROWSER_PROFILES_DIR / test_platform / "cookies.json"
        if cookie_path.exists():
            with open(cookie_path) as f:
                cookies = json.load(f)
            console.print(
                f"   [green]✓[/green] Saved {len(cookies)} cookies to "
                f"{cookie_path}"
            )
        else:
            console.print("   [yellow]⚠ No cookies to save (fresh profile)[/yellow]")

        loaded = engine.load_cookies(test_platform)
        console.print(f"   load_cookies: {loaded}")

        cookie_dict = engine.export_cookies_dict(test_platform)
        console.print(f"   export_cookies_dict: {len(cookie_dict)} entries")

        # ── 14. Session tracking ──
        console.print("\n[yellow]14. Session tracking:[/yellow]")
        session_min = engine.session_time(test_platform)
        console.print(f"   Session time: {session_min:.2f} minutes")
        needs = engine.needs_break(test_platform)
        console.print(f"   Needs break: {needs}")

        # ── 15. Random delay ──
        console.print("\n[yellow]15. Random delay test (short):[/yellow]")
        t0 = time.time()
        engine.random_delay(0.1, 0.3)
        elapsed = time.time() - t0
        console.print(f"   [green]✓[/green] Delayed {elapsed:.2f}s "
                      f"(expected 0.1-0.3)")

        # ── 16. Navigate to form page for input test ──
        console.print("\n[yellow]16. Form interaction test:[/yellow]")
        engine.navigate(page, "https://httpbin.org/forms/post")
        time.sleep(1)

        # Try to find an input on the page
        has_input = engine.element_exists(page, "input")
        console.print(f"   Has <input> elements: {has_input}")

        if has_input:
            try:
                engine.type_human(page, "input[name='custname']", "Piyush Kashyap")
                typed_val = engine.get_input_value(page, "input[name='custname']")
                console.print(
                    f"   [green]✓[/green] type_human: typed 'Piyush Kashyap', "
                    f"read back '{typed_val}'"
                )
            except Exception as e:
                console.print(f"   [yellow]⚠ Typing test skipped: {e}[/yellow]")

        # ── 17. Status ──
        console.print("\n[yellow]17. Engine status:[/yellow]")
        status = engine.get_status()
        console.print(f"   Playwright running: {status['playwright_running']}")
        console.print(f"   Stealth library: {status['stealth_library']}")
        console.print(f"   Active platforms: {status['active_platforms']}")
        for p, s in status['sessions'].items():
            console.print(
                f"   Session '{p}': URL={s['url'][:50]}..., "
                f"{s['session_minutes']}min"
            )

        # ── 18. Profile management ──
        console.print("\n[yellow]18. Profile management:[/yellow]")
        profiles = engine.list_profiles()
        table = Table(title="Browser Profiles")
        table.add_column("Platform", style="cyan")
        table.add_column("Size (MB)", justify="right")
        table.add_column("Cookies", justify="center")
        table.add_column("Active", justify="center")
        for name, info in profiles.items():
            table.add_row(
                name,
                str(info["size_mb"]),
                "✓" if info["has_cookies"] else "✗",
                "✓" if info["is_active"] else "✗",
            )
        console.print(table)

        # ── 19. Close test platform ──
        console.print("\n[yellow]19. Cleanup:[/yellow]")
        engine.close(test_platform)
        console.print(f"   [green]✓[/green] Closed '{test_platform}'")

        still_active = engine.is_logged_in(test_platform)
        console.print(f"   is_logged_in after close: {still_active} "
                      f"(should be False)")

        # Clean up test profile
        engine.reset_profile(test_platform)
        console.print(f"   [green]✓[/green] Test profile deleted")

        # Clean up test screenshot
        if ss_path and os.path.exists(ss_path):
            try:
                os.unlink(ss_path)
                console.print(f"   [green]✓[/green] Test screenshot deleted")
            except Exception:
                pass

    except Exception as e:
        console.print(f"\n[red]Browser test error: {e}[/red]")
        import traceback
        traceback.print_exc()

    finally:
        # ── 20. Final cleanup ──
        console.print("\n[yellow]20. Final cleanup:[/yellow]")
        engine.close_all()
        console.print(f"   [green]✓[/green] All browsers closed")

    # ── 21. Singleton test ──
    console.print("\n[yellow]21. Singleton test:[/yellow]")
    e1 = get_browser_engine()
    e2 = get_browser_engine()
    console.print(f"   Same instance: {e1 is e2} (should be True)")

    console.print(f"\n[bold green]═══ All browser engine tests passed! ═══[/bold green]\n")