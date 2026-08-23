#!/usr/bin/env python3
"""
main.py — Job Application AI Agent v1.0 — Entry Point

Phase 1 CLI: interactive menu, terminal dashboard, system diagnostics,
platform management, job viewing, and discovery triggering.
Gracefully handles modules not yet built (Phase 2/3/4).

Usage:
    python main.py                  # Interactive menu
    python main.py --status         # System status and exit
    python main.py --dashboard      # Terminal dashboard
    python main.py --discover       # Run one discovery cycle
    python main.py --jobs           # List jobs in database
    python main.py --start          # Start full agent loop (Phase 3)
"""

import os
import sys
import json
import signal
import argparse
import textwrap
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

# ═══════════════════════════════════════════════════════════════
#  PROJECT ROOT
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ═══════════════════════════════════════════════════════════════
#  RICH LIBRARY (required — part of tech stack)
# ═══════════════════════════════════════════════════════════════
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.tree import Tree
    from rich.prompt import Prompt, Confirm, IntPrompt
    from rich.columns import Columns
    from rich import box
    from rich.markup import escape
except ImportError:
    print("=" * 60)
    print("  Rich library is required for the CLI interface.")
    print("  Install it:  pip install rich")
    print("=" * 60)
    sys.exit(1)

console = Console()

# ═══════════════════════════════════════════════════════════════
#  SAFE MODULE IMPORTS — track what's available
# ═══════════════════════════════════════════════════════════════
AVAILABLE = {}  # module_name → True/False


def _safe_import(name: str, do_import):
    """Try importing a module, record availability."""
    try:
        mod = do_import()
        AVAILABLE[name] = True
        return mod
    except Exception as exc:
        AVAILABLE[name] = False
        return None


# ── Phase 1 core (should all be ✅) ──────────────────────────
_safe_import("config", lambda: __import__("config"))
_safe_import("core.logger", lambda: __import__("core.logger", fromlist=["get_logger"]))
_safe_import("core.db", lambda: __import__("core.db", fromlist=["get_db"]))
_safe_import("core.browser", lambda: __import__("core.browser", fromlist=["BrowserEngine"]))
_safe_import("platforms.base", lambda: __import__("platforms.base", fromlist=["PlatformBase"]))
_safe_import("profile.resume_data", lambda: __import__("profile.resume_data", fromlist=["get_base_resume"]))
_safe_import("profile.preferences", lambda: __import__("profile.preferences", fromlist=["get_preferences"]))
_safe_import("discovery.dedup", lambda: __import__("discovery.dedup", fromlist=["Deduplicator"]))
_safe_import("discovery.filters", lambda: __import__("discovery.filters", fromlist=["JobFilter"]))

# ── Phase 1 platforms ────────────────────────────────────────
_safe_import("platforms.naukri", lambda: __import__("platforms.naukri", fromlist=["NaukriPlatform"]))
_safe_import("platforms.indeed", lambda: __import__("platforms.indeed", fromlist=["IndeedPlatform"]))
_safe_import("platforms.foundit", lambda: __import__("platforms.foundit", fromlist=["FounditPlatform"]))
_safe_import("platforms.linkedin", lambda: __import__("platforms.linkedin", fromlist=["LinkedInPlatform"]))
_safe_import("platforms.manager", lambda: __import__("platforms.manager", fromlist=["PlatformManager"]))

# ── Phase 2 AI + resume ─────────────────────────────────────
_safe_import("ai.llm_client", lambda: __import__("ai.llm_client", fromlist=["LLMClient"]))
_safe_import("ai.job_matcher", lambda: __import__("ai.job_matcher", fromlist=["JobMatcher"]))
_safe_import("ai.resume_tailor", lambda: __import__("ai.resume_tailor", fromlist=["ResumeTailor"]))
_safe_import("ai.cover_letter", lambda: __import__("ai.cover_letter", fromlist=["CoverLetterGenerator"]))
_safe_import("resume.builder", lambda: __import__("resume.builder", fromlist=["ResumeBuilder"]))
_safe_import("resume.optimizer", lambda: __import__("resume.optimizer", fromlist=["ATSOptimizer"]))
_safe_import("profile.answers", lambda: __import__("profile.answers", fromlist=["get_answer"]))

# ── Phase 3 outreach + tracking ─────────────────────────────
_safe_import("outreach.email_sender", lambda: __import__("outreach.email_sender", fromlist=["EmailSender"]))
_safe_import("tracking.notifications", lambda: __import__("tracking.notifications", fromlist=["JobNotifier"]))
_safe_import("discovery.monitor", lambda: __import__("discovery.monitor", fromlist=["JobMonitor"]))

# ── Phase 4 web ──────────────────────────────────────────────
_safe_import("web.app", lambda: __import__("web.app", fromlist=["app"]))
_safe_import("tracking.reports", lambda: __import__("tracking.reports", fromlist=["ReportGenerator"]))

# ── Get logger ───────────────────────────────────────────────
logger = None
if AVAILABLE.get("core.logger"):
    from core.logger import get_logger
    logger = get_logger("main")


def log_info(msg, *args):
    if logger:
        logger.info(msg, *args)


def log_error(msg, *args):
    if logger:
        logger.error(msg, *args)


# ═══════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════

VERSION = "1.0.0-phase1"
AGENT_NAME = "Job Application AI Agent"

# Phase definitions for status display
PHASES = {
    "Phase 1 — Foundation": [
        "config", "core.logger", "core.db", "core.browser",
        "platforms.base", "profile.resume_data", "profile.preferences",
        "discovery.dedup", "discovery.filters",
        "platforms.naukri",
    ],
    "Phase 2 — AI + Resume": [
        "ai.llm_client", "ai.job_matcher", "ai.resume_tailor",
        "ai.cover_letter", "resume.builder", "resume.optimizer",
        "profile.answers",
    ],
    "Phase 3 — Full Pipeline": [
        "platforms.indeed", "platforms.foundit", "platforms.linkedin",
        "platforms.manager", "tracking.notifications",
        "outreach.email_sender", "discovery.monitor",
    ],
    "Phase 4 — Dashboard": [
        "tracking.reports", "web.app",
    ],
}

PLATFORM_NAMES = {
    "naukri": "Naukri",
    "indeed": "Indeed",
    "foundit": "Foundit",
    "linkedin": "LinkedIn",
}

# Graceful shutdown
_shutdown_requested = False


def _signal_handler(sig, frame):
    global _shutdown_requested
    if _shutdown_requested:
        console.print("\n[bold red]Force exit.[/]")
        sys.exit(1)
    _shutdown_requested = True
    console.print("\n[yellow]Shutting down gracefully… (Ctrl+C again to force)[/]")


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ═══════════════════════════════════════════════════════════════
#  UTILITY HELPERS
# ═══════════════════════════════════════════════════════════════

def clear_screen():
    """Clear terminal screen."""
    os.system("clear" if os.name != "nt" else "cls")


def pause(msg: str = "Press Enter to continue…"):
    """Wait for user to press Enter."""
    try:
        console.input(f"\n[dim]{msg}[/]")
    except (EOFError, KeyboardInterrupt):
        pass


def get_db_safe():
    """Get database instance or None."""
    if not AVAILABLE.get("core.db"):
        return None
    try:
        from core.db import get_db
        return get_db()
    except Exception as e:
        log_error("DB connect failed: %s", e)
        return None


def get_config_val(attr: str, default=None):
    """Safely get a config attribute."""
    if not AVAILABLE.get("config"):
        return default
    import config as cfg
    return getattr(cfg, attr, default)


def format_salary(amount):
    """Format salary in LPA."""
    if not amount:
        return "—"
    try:
        val = float(amount)
        if val >= 100000:
            # Absolute rupees → convert to LPA
            return f"₹{val / 100000:.1f} LPA"
        elif val > 0:
            # Already in LPA
            return f"{val:.1f} LPA"
        return "—"
    except (ValueError, TypeError):
        return str(amount)



def format_score(score):
    """Format match score with color."""
    if score is None:
        return "[dim]—[/]"
    s = float(score)
    if s >= 80:
        return f"[bold green]{s:.0f}%[/]"
    if s >= 60:
        return f"[green]{s:.0f}%[/]"
    if s >= 40:
        return f"[yellow]{s:.0f}%[/]"
    return f"[red]{s:.0f}%[/]"


def truncate(text: str, length: int = 40) -> str:
    """Truncate text with ellipsis."""
    if not text:
        return "—"
    text = str(text).replace("\n", " ").strip()
    if len(text) <= length:
        return text
    return text[: length - 1] + "…"


# ═══════════════════════════════════════════════════════════════
#  1. BANNER
# ═══════════════════════════════════════════════════════════════

def print_banner():
    """Print the application banner."""
    banner_text = Text()
    banner_text.append("Job Application AI Agent", style="bold cyan")
    banner_text.append(f"  v{VERSION}", style="dim")

    user_profile = get_config_val("USER_PROFILE", {})
    name = user_profile.get("name", "Unknown")
    location = user_profile.get("location", "Unknown")
    subtitle = f"{name} • {location} • {datetime.now().strftime('%a %d %b %Y, %I:%M %p')}"

    console.print(Panel(
        Text.from_markup(
            f"[bold cyan]🤖 {AGENT_NAME}[/]  [dim]v{VERSION}[/]\n"
            f"[dim]{subtitle}[/]"
        ),
        box=box.DOUBLE,
        border_style="cyan",
        padding=(0, 2),
    ))


# ═══════════════════════════════════════════════════════════════
#  2. SYSTEM STATUS
# ═══════════════════════════════════════════════════════════════

def show_module_status():
    """Show which modules are loaded/missing, grouped by phase."""
    console.rule("[bold]Module Status", style="blue")

    for phase_name, module_list in PHASES.items():
        loaded = sum(1 for m in module_list if AVAILABLE.get(m))
        total = len(module_list)
        pct = (loaded / total * 100) if total else 0

        if pct == 100:
            header_style = "green"
            icon = "✅"
        elif pct > 0:
            header_style = "yellow"
            icon = "🔨"
        else:
            header_style = "dim"
            icon = "⬜"

        tree = Tree(f"{icon} [{header_style}]{phase_name}[/] ({loaded}/{total})")
        for mod in module_list:
            if AVAILABLE.get(mod):
                tree.add(f"[green]✅ {mod}[/]")
            else:
                tree.add(f"[dim]⬜ {mod}[/]")
        console.print(tree)
        console.print()


def show_db_status():
    """Show database table counts."""
    db = get_db_safe()
    if not db:
        console.print("[red]  Database: ❌ Not available[/]")
        return

    console.rule("[bold]Database Status", style="blue")

    try:
        table_info = db.get_table_info()
        tbl = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold")
        tbl.add_column("Table", style="cyan", min_width=20)
        tbl.add_column("Rows", justify="right", style="white", min_width=10)

        for table_name, count in sorted(table_info.items()):
            style = "green" if count > 0 else "dim"
            tbl.add_row(table_name, f"[{style}]{count}[/]")

        console.print(tbl)
    except Exception as e:
        console.print(f"[red]  DB error: {e}[/]")


def show_platform_status():
    """Show platform login/session status."""
    console.rule("[bold]Platform Status", style="blue")

    db = get_db_safe()
    tbl = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold")
    tbl.add_column("Platform", style="cyan", min_width=12)
    tbl.add_column("Module", min_width=10)
    tbl.add_column("Logged In", min_width=10)
    tbl.add_column("Today Applied", justify="right", min_width=14)
    tbl.add_column("Daily Limit", justify="right", min_width=12)
    tbl.add_column("Status", min_width=10)

    platform_config = get_config_val("PLATFORM_CONFIG", {})

    for key, display_name in PLATFORM_NAMES.items():
        # Module available?
        mod_key = f"platforms.{key}"
        mod_ok = AVAILABLE.get(mod_key, False)
        mod_str = "[green]✅[/]" if mod_ok else "[dim]⬜[/]"

        # Config
        pconf = platform_config.get(key, {})
        enabled = pconf.get("enabled", False)
        max_daily = pconf.get("max_daily_applications", "—")

        # Session from DB
        logged_in = "—"
        today_count = "—"
        status = "[dim]disabled[/]"

        if db and enabled:
            try:
                session = db.get_platform_session(key)
                if session:
                    logged_in = "[green]Yes[/]" if session.get("logged_in") else "[red]No[/]"
                    today_count = str(session.get("daily_applied", 0))
                    raw_status = session.get("status", "active")
                    status_map = {
                        "active": "[green]active[/]",
                        "cooldown": "[yellow]cooldown[/]",
                        "banned": "[red]banned[/]",
                        "disabled": "[dim]disabled[/]",
                    }
                    status = status_map.get(raw_status, raw_status)
                else:
                    logged_in = "[dim]—[/]"
                    status = "[yellow]no session[/]"
            except Exception:
                pass
        elif not enabled:
            status = "[dim]disabled[/]"

        tbl.add_row(display_name, mod_str, logged_in, today_count, str(max_daily), status)

    console.print(tbl)


def show_credential_status():
    """Show which credentials are configured (without revealing values)."""
    console.rule("[bold]Credentials", style="blue")

    checks = [
        ("NAUKRI_EMAIL", "Naukri Email"),
        ("NAUKRI_PASSWORD", "Naukri Password"),
        ("INDEED_EMAIL", "Indeed Email"),
        ("LINKEDIN_EMAIL", "LinkedIn Email"),
        ("LINKEDIN_PASSWORD", "LinkedIn Password"),
        ("FOUNDIT_EMAIL", "Foundit Email"),
        ("SMTP_EMAIL", "SMTP Email"),
        ("SMTP_PASSWORD", "SMTP App Password"),
        ("GEMINI_API_KEY", "Gemini API Key"),
        ("GROQ_API_KEY", "Groq API Key"),
        ("TELEGRAM_BOT_TOKEN", "Telegram Bot Token"),
        ("TELEGRAM_CHAT_ID", "Telegram Chat ID"),
        ("HUNTER_API_KEY", "Hunter.io Key"),
    ]

    tbl = Table(box=box.SIMPLE, show_header=False)
    tbl.add_column("Credential", style="cyan", min_width=22)
    tbl.add_column("Status", min_width=14)

    for attr, label in checks:
        val = get_config_val(attr, None)
        # Also check in os.environ or .env loaded values
        if val is None or val == "" or val == "None":
            # Try from environment directly
            val = os.environ.get(attr, "")

        if val and str(val).strip() and str(val).strip().lower() not in ("none", ""):
            masked = str(val)[:3] + "•" * min(len(str(val)) - 3, 12)
            tbl.add_row(label, f"[green]✅ {masked}[/]")
        else:
            tbl.add_row(label, "[dim]⬜ not set[/]")

    console.print(tbl)


def show_full_status():
    """Combined status view (option 6)."""
    clear_screen()
    print_banner()
    show_module_status()
    show_db_status()
    show_platform_status()
    show_credential_status()

    # Quick stats
    db = get_db_safe()
    if db:
        try:
            stats = db.get_stats("today")
            console.rule("[bold]Today's Activity", style="blue")
            today_tbl = Table(box=box.SIMPLE, show_header=False)
            today_tbl.add_column("Metric", style="cyan", min_width=20)
            today_tbl.add_column("Value", justify="right", min_width=10)
            for key, val in stats.items():
                today_tbl.add_row(str(key).replace("_", " ").title(), str(val))
            console.print(today_tbl)
        except Exception:
            pass

    pause()


# ═══════════════════════════════════════════════════════════════
#  3. DASHBOARD (Rich terminal)
# ═══════════════════════════════════════════════════════════════

def show_dashboard():
    """Terminal dashboard with key metrics."""
    clear_screen()
    print_banner()

    db = get_db_safe()
    user_profile = get_config_val("USER_PROFILE", {})

    # ── Profile card ──────────────────────────────────────────
    name = user_profile.get("name", "—")
    email = user_profile.get("email", "—")
    current = user_profile.get("current_title", "—")
    target = ", ".join(user_profile.get("target_titles", [])[:3]) or "—"
    locs = ", ".join(user_profile.get("target_locations", [])[:4]) or "—"
    min_sal = user_profile.get("min_salary", "—")

    profile_text = (
        f"[bold]{name}[/] — {current}\n"
        f"[dim]Target:[/] {truncate(target, 60)}\n"
        f"[dim]Locations:[/] {locs}\n"
        f"[dim]Min Salary:[/] {format_salary(min_sal)}  |  "
        f"[dim]Notice:[/] {user_profile.get('notice_period', '—')}"
    )
    console.print(Panel(profile_text, title="👤 Profile", border_style="cyan",
                        box=box.ROUNDED))

    # ── Stats cards ───────────────────────────────────────────
    stats_today = {"discovered": 0, "matched": 0, "applied": 0, "responses": 0}
    stats_total = {"jobs": 0, "applications": 0, "contacts": 0, "emails": 0}

    if db:
        try:
            today_stats = db.get_stats("today")
            stats_today.update({
                k: v for k, v in today_stats.items()
                if k in stats_today
            })
        except Exception:
            pass

        try:
            info = db.get_table_info()
            stats_total["jobs"] = info.get("jobs", 0)
            stats_total["applications"] = info.get("applications", 0)
            stats_total["contacts"] = info.get("contacts", 0)
            stats_total["emails"] = info.get("emails", 0)
        except Exception:
            pass

    # Today
    today_card = (
        f"[bold green]{stats_today['discovered']}[/] discovered   "
        f"[bold yellow]{stats_today['matched']}[/] matched\n"
        f"[bold cyan]{stats_today['applied']}[/] applied      "
        f"[bold magenta]{stats_today['responses']}[/] responses"
    )
    console.print(Panel(today_card, title="📈 Today", border_style="green",
                        box=box.ROUNDED))

    # Totals
    total_card = (
        f"[bold]{stats_total['jobs']}[/] jobs   "
        f"[bold]{stats_total['applications']}[/] applications   "
        f"[bold]{stats_total['contacts']}[/] contacts   "
        f"[bold]{stats_total['emails']}[/] emails"
    )
    console.print(Panel(total_card, title="📊 Database Totals",
                        border_style="blue", box=box.ROUNDED))

    # ── Platform status mini ──────────────────────────────────
    platform_lines = []
    platform_config = get_config_val("PLATFORM_CONFIG", {})
    for key, name in PLATFORM_NAMES.items():
        mod_ok = AVAILABLE.get(f"platforms.{key}", False)
        pconf = platform_config.get(key, {})
        enabled = pconf.get("enabled", False)

        if not mod_ok:
            platform_lines.append(f"  [dim]⬜ {name}: module not built[/]")
        elif not enabled:
            platform_lines.append(f"  [dim]⬜ {name}: disabled[/]")
        else:
            # Check session
            session_status = "ready"
            if db:
                try:
                    sess = db.get_platform_session(key)
                    if sess and sess.get("logged_in"):
                        count = sess.get("daily_applied", 0)
                        limit = pconf.get("max_daily_applications", "?")
                        session_status = f"logged in ({count}/{limit} today)"
                        platform_lines.append(
                            f"  [green]✅ {name}: {session_status}[/]")
                        continue
                except Exception:
                    pass
            platform_lines.append(f"  [yellow]🔑 {name}: not logged in[/]")

    console.print(Panel(
        "\n".join(platform_lines) if platform_lines else "[dim]No platforms configured[/]",
        title="🌐 Platforms",
        border_style="yellow",
        box=box.ROUNDED,
    ))

    # ── Recent jobs ───────────────────────────────────────────
    if db:
        try:
            recent_jobs = db.get_jobs(limit=5)
            if recent_jobs:
                tbl = Table(
                    title="🆕 Recent Jobs",
                    box=box.SIMPLE_HEAVY,
                    show_header=True,
                    header_style="bold",
                    title_style="bold white",
                )
                tbl.add_column("#", style="dim", width=4)
                tbl.add_column("Platform", style="cyan", width=9)
                tbl.add_column("Company", width=18)
                tbl.add_column("Title", width=28)
                tbl.add_column("Location", width=14)
                tbl.add_column("Score", justify="center", width=7)
                tbl.add_column("Status", width=9)

                for j in recent_jobs:
                    tbl.add_row(
                        str(j.get("id", "")),
                        str(j.get("platform", "")),
                        truncate(j.get("company", ""), 17),
                        truncate(j.get("title", ""), 27),
                        truncate(j.get("location", ""), 13),
                        format_score(j.get("match_score")),
                        str(j.get("status", "")),
                    )
                console.print(tbl)
            else:
                console.print(Panel("[dim]No jobs discovered yet. Run Discovery "
                                    "to start finding jobs.[/]",
                                    border_style="dim"))
        except Exception as e:
            console.print(f"[dim]Could not load recent jobs: {e}[/]")

    # ── Phase status ──────────────────────────────────────────
    phase_parts = []
    for phase_name, mods in PHASES.items():
        loaded = sum(1 for m in mods if AVAILABLE.get(m))
        total = len(mods)
        if loaded == total:
            phase_parts.append(f"[green]✅ {phase_name} ({loaded}/{total})[/]")
        elif loaded > 0:
            phase_parts.append(f"[yellow]🔨 {phase_name} ({loaded}/{total})[/]")
        else:
            phase_parts.append(f"[dim]⬜ {phase_name} ({loaded}/{total})[/]")

    console.print(Panel(
        "\n".join(phase_parts),
        title="🔧 Build Progress",
        border_style="magenta",
        box=box.ROUNDED,
    ))

    pause()


# ═══════════════════════════════════════════════════════════════
#  4. JOB VIEWING
# ═══════════════════════════════════════════════════════════════

def show_jobs():
    """List jobs from the database with filtering."""
    clear_screen()
    console.rule("[bold cyan]Jobs Database[/]", style="cyan")

    db = get_db_safe()
    if not db:
        console.print("[red]Database not available.[/]")
        pause()
        return

    # Filter options
    console.print("\n[bold]Filter by:[/]")
    console.print("  1. All jobs")
    console.print("  2. By platform (naukri/indeed/foundit/linkedin)")
    console.print("  3. By status (new/matched/applied/skipped)")
    console.print("  4. High match (score ≥ 70)")
    console.print("  5. Back")

    choice = Prompt.ask("\nChoose", choices=["1", "2", "3", "4", "5"], default="1")

    kwargs = {"limit": 50}

    if choice == "5":
        return
    elif choice == "2":
        platform = Prompt.ask("Platform", choices=["naukri", "indeed", "foundit", "linkedin"],
                              default="naukri")
        kwargs["platform"] = platform
    elif choice == "3":
        status = Prompt.ask("Status",
                            choices=["new", "matched", "queued", "applying",
                                     "applied", "skipped", "expired", "duplicate"],
                            default="new")
        kwargs["status"] = status
    elif choice == "4":
        kwargs["min_score"] = 70.0

    try:
        jobs = db.get_jobs(**kwargs)
    except Exception as e:
        console.print(f"[red]Error loading jobs: {e}[/]")
        pause()
        return

    if not jobs:
        console.print("\n[dim]No jobs found matching your criteria.[/]")
        pause()
        return

    # Display table
    tbl = Table(
        title=f"Found {len(jobs)} jobs",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        row_styles=["", "dim"],
    )
    tbl.add_column("ID", style="bold", width=5, justify="right")
    tbl.add_column("Platform", width=9)
    tbl.add_column("Company", width=20)
    tbl.add_column("Title", width=30)
    tbl.add_column("Location", width=15)
    tbl.add_column("Salary", width=14)
    tbl.add_column("Score", justify="center", width=7)
    tbl.add_column("Status", width=9)
    tbl.add_column("Posted", width=10)

    for j in jobs:
        sal = ""
        sal_min = j.get("salary_min")
        sal_max = j.get("salary_max")
        if sal_min and sal_max:
            sal = f"{format_salary(sal_min)}-{format_salary(sal_max)}"
        elif sal_min:
            sal = format_salary(sal_min)

        posted = j.get("posted_date") or j.get("discovered_at") or ""
        if posted and len(posted) > 10:
            posted = posted[:10]

        tbl.add_row(
            str(j.get("id", "")),
            str(j.get("platform", "")),
            truncate(j.get("company", ""), 19),
            truncate(j.get("title", ""), 29),
            truncate(j.get("location", ""), 14),
            sal or "—",
            format_score(j.get("match_score")),
            str(j.get("status", "")),
            posted,
        )

    console.print(tbl)

    # View details?
    console.print()
    view_detail = Prompt.ask("Enter job ID for details (or 'b' for back)",
                             default="b")
    if view_detail.lower() != "b":
        try:
            show_job_detail(int(view_detail))
        except ValueError:
            console.print("[red]Invalid ID.[/]")
            pause()


def show_job_detail(job_id: int):
    """Show full details for a single job."""
    db = get_db_safe()
    if not db:
        console.print("[red]Database not available.[/]")
        pause()
        return

    try:
        # get_jobs with specific filters to find by ID
        jobs = db.get_jobs(limit=10000)
        job = None
        for j in jobs:
            if j.get("id") == job_id:
                job = j
                break
    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        pause()
        return

    if not job:
        console.print(f"[red]Job #{job_id} not found.[/]")
        pause()
        return

    clear_screen()
    console.rule(f"[bold cyan]Job #{job_id} Details[/]", style="cyan")

    # Build detail panel
    details = []
    fields = [
        ("Platform", "platform"),
        ("Platform ID", "platform_job_id"),
        ("Title", "title"),
        ("Company", "company"),
        ("Location", "location"),
        ("Job Type", "job_type"),
        ("Work Mode", "work_mode"),
        ("Experience", None),
        ("Salary", None),
        ("Match Score", None),
        ("Status", "status"),
        ("URL", "url"),
        ("Posted", "posted_date"),
        ("Discovered", "discovered_at"),
    ]

    for label, key in fields:
        if key:
            val = job.get(key, "—") or "—"
        elif label == "Experience":
            exp_min = job.get("experience_min")
            exp_max = job.get("experience_max")
            if exp_min is not None and exp_max is not None:
                val = f"{exp_min}-{exp_max} years"
            elif exp_min:
                val = f"{exp_min}+ years"
            else:
                val = "—"
        elif label == "Salary":
            sal_min = job.get("salary_min")
            sal_max = job.get("salary_max")
            currency = job.get("currency", "INR")
            if sal_min and sal_max:
                val = f"{format_salary(sal_min)} - {format_salary(sal_max)} {currency}"
            elif sal_min:
                val = f"{format_salary(sal_min)}+ {currency}"
            else:
                val = "—"
        elif label == "Match Score":
            score = job.get("match_score")
            val = f"{score:.0f}%" if score else "Not scored"
        else:
            val = "—"

        details.append(f"[cyan]{label}:[/] {escape(str(val))}")

    console.print(Panel("\n".join(details), title="📋 Job Info",
                        border_style="cyan", box=box.ROUNDED))

    # Skills
    skills = job.get("skills")
    if skills:
        if isinstance(skills, str):
            try:
                skills = json.loads(skills)
            except json.JSONDecodeError:
                skills = [s.strip() for s in skills.split(",") if s.strip()]
        if isinstance(skills, list) and skills:
            skill_text = ", ".join(f"[green]{escape(s)}[/]" for s in skills)
            console.print(Panel(skill_text, title="🛠 Skills",
                                border_style="green", box=box.ROUNDED))

    # Match details
    match_details = job.get("match_details")
    if match_details:
        if isinstance(match_details, str):
            try:
                match_details = json.loads(match_details)
            except json.JSONDecodeError:
                match_details = None
        if isinstance(match_details, dict):
            md_lines = []
            for k, v in match_details.items():
                md_lines.append(f"[cyan]{k}:[/] {v}")
            console.print(Panel("\n".join(md_lines), title="🎯 Match Details",
                                border_style="yellow", box=box.ROUNDED))

    # Description
    desc = job.get("description", "")
    if desc:
        # Truncate for display
        if len(desc) > 2000:
            desc = desc[:2000] + "\n\n[dim]… (truncated, full JD in database)[/]"
        console.print(Panel(escape(desc), title="📝 Job Description",
                            border_style="blue", box=box.ROUNDED))

    # Notes
    notes = job.get("notes")
    if notes:
        console.print(Panel(escape(str(notes)), title="📌 Notes",
                            border_style="dim", box=box.ROUNDED))

    pause()


# ═══════════════════════════════════════════════════════════════
#  5. PLATFORM MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def manage_platforms():
    """Platform management sub-menu."""
    while True:
        clear_screen()
        console.rule("[bold cyan]Platform Management[/]", style="cyan")

        console.print("\n  1. View platform status")
        console.print("  2. Login to platform")
        console.print("  3. Test browser launch")
        console.print("  4. Check cookies / sessions")
        console.print("  5. Reset daily counters")
        console.print("  0. Back to main menu")

        choice = Prompt.ask("\nChoose", choices=["0", "1", "2", "3", "4", "5"],
                            default="0")

        if choice == "0":
            return
        elif choice == "1":
            show_platform_status()
            pause()
        elif choice == "2":
            platform_login()
        elif choice == "3":
            test_browser()
        elif choice == "4":
            check_sessions()
        elif choice == "5":
            reset_daily_counts()


def platform_login():
    """Attempt to login to a platform."""
    available_platforms = []
    for key, name in PLATFORM_NAMES.items():
        mod_key = f"platforms.{key}"
        if AVAILABLE.get(mod_key):
            available_platforms.append(key)

    if not available_platforms:
        console.print("[yellow]No platform modules are built yet.[/]")
        console.print("[dim]Build platforms/naukri.py first (Phase 1).[/]")
        pause()
        return

    console.print("\n[bold]Available platforms:[/]")
    for i, key in enumerate(available_platforms, 1):
        console.print(f"  {i}. {PLATFORM_NAMES.get(key, key)}")
    console.print(f"  0. Cancel")

    choice = Prompt.ask("Choose", default="0")
    if choice == "0":
        return

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(available_platforms):
            platform_key = available_platforms[idx]
            console.print(f"\n[cyan]Attempting login to {PLATFORM_NAMES[platform_key]}…[/]")

            # Import and instantiate the platform
            try:
                if platform_key == "naukri":
                    from platforms.naukri import NaukriPlatform
                    if not AVAILABLE.get("core.browser"):
                        console.print("[red]Browser engine not available.[/]")
                        pause()
                        return
                    from core.browser import BrowserEngine
                    browser = BrowserEngine()
                    platform = NaukriPlatform(browser)
                    success = platform.login()
                    if success:
                        console.print(f"[bold green]✅ Logged in to {PLATFORM_NAMES[platform_key]}![/]")
                    else:
                        console.print(f"[red]❌ Login failed for {PLATFORM_NAMES[platform_key]}.[/]")
                    browser.close_all()

                elif platform_key == "indeed":
                    from platforms.indeed import IndeedPlatform
                    from core.browser import BrowserEngine
                    browser = BrowserEngine()
                    platform = IndeedPlatform(browser)
                    success = platform.login()
                    if success:
                        console.print(f"[bold green]✅ Session active for {PLATFORM_NAMES[platform_key]}![/]")
                    else:
                        console.print(f"[yellow]Cookie login needed — check Telegram for instructions.[/]")
                    browser.close_all()

                elif platform_key == "linkedin":
                    from platforms.linkedin import LinkedInPlatform
                    from core.browser import BrowserEngine
                    browser = BrowserEngine()
                    platform = LinkedInPlatform(browser)
                    success = platform.login()
                    if success:
                        console.print(f"[bold green]✅ Logged in to {PLATFORM_NAMES[platform_key]} (search only)![/]")
                    else:
                        console.print(f"[red]❌ Login failed.[/]")
                    browser.close_all()

                elif platform_key == "foundit":
                    from platforms.foundit import FounditPlatform
                    from core.browser import BrowserEngine
                    browser = BrowserEngine()
                    platform = FounditPlatform(browser)
                    success = platform.login()
                    if success:
                        console.print(f"[bold green]✅ Logged in to {PLATFORM_NAMES[platform_key]}![/]")
                    else:
                        console.print(f"[red]❌ Login failed.[/]")
                    browser.close_all()
                else:
                    console.print(f"[yellow]Platform '{platform_key}' login not implemented in main.py.[/]")

            except Exception as e:
                console.print(f"[red]Login error: {e}[/]")
                log_error("Platform login error (%s): %s", platform_key, e)
        else:
            console.print("[red]Invalid choice.[/]")
    except ValueError:
        console.print("[red]Invalid input.[/]")

    pause()


def test_browser():
    """Test browser engine launch."""
    if not AVAILABLE.get("core.browser"):
        console.print("[red]Browser engine (core/browser.py) not available.[/]")
        pause()
        return

    console.print("\n[cyan]Launching browser test…[/]")
    try:
        from core.browser import BrowserEngine
        browser = BrowserEngine()

        headless = Confirm.ask("Run headless?", default=True)
        console.print("[dim]Starting browser…[/]")
        page = browser.launch("test", headless=headless)

        console.print("[green]✅ Browser launched successfully![/]")
        console.print("[dim]Navigating to example.com…[/]")
        page.goto("https://example.com", timeout=15000)
        title = page.title()
        console.print(f"[green]Page title: {title}[/]")

        # Screenshot
        ss_dir = get_config_val("CACHE_DIR", os.path.join(PROJECT_ROOT, "cache"))
        os.makedirs(ss_dir, exist_ok=True)
        ss_path = os.path.join(ss_dir, "browser_test.png")
        page.screenshot(path=ss_path)
        console.print(f"[green]Screenshot saved: {ss_path}[/]")

        browser.close_all()
        console.print("[bold green]✅ Browser test passed![/]")

    except Exception as e:
        console.print(f"[red]❌ Browser test failed: {e}[/]")
        log_error("Browser test error: %s", e)

    pause()


def check_sessions():
    """Check saved browser sessions/cookies."""
    profiles_dir = get_config_val(
        "BROWSER_PROFILES_DIR",
        os.path.join(PROJECT_ROOT, "browser_profiles")
    )

    console.print(f"\n[cyan]Browser profiles dir:[/] {profiles_dir}")

    if not os.path.isdir(profiles_dir):
        console.print("[dim]No browser profiles directory found.[/]")
        pause()
        return

    for name in sorted(os.listdir(profiles_dir)):
        path = os.path.join(profiles_dir, name)
        if os.path.isdir(path):
            # Check for cookies file
            cookie_file = os.path.join(path, "cookies.json")
            has_cookies = os.path.isfile(cookie_file)
            if has_cookies:
                size = os.path.getsize(cookie_file)
                mtime = datetime.fromtimestamp(
                    os.path.getmtime(cookie_file)
                ).strftime("%Y-%m-%d %H:%M")
                console.print(
                    f"  [green]✅ {name}[/] — cookies: {size} bytes, last updated: {mtime}"
                )
            else:
                console.print(f"  [dim]⬜ {name}[/] — no cookies saved")

    pause()


def reset_daily_counts():
    """Reset daily application counters."""
    db = get_db_safe()
    if not db:
        console.print("[red]Database not available.[/]")
        pause()
        return

    if Confirm.ask("Reset all daily application counters?", default=False):
        try:
            db.reset_daily_counts()
            console.print("[green]✅ Daily counts reset.[/]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/]")

    pause()


# ═══════════════════════════════════════════════════════════════
#  6. DISCOVERY / SEARCH
# ═══════════════════════════════════════════════════════════════

def run_discovery():
    """Run one discovery cycle (search → dedup → filter → save)."""
    if not AVAILABLE.get("platforms.naukri"):
        console.print("[yellow]No platform modules available yet.[/]")
        console.print("[dim]Build platforms/naukri.py to enable discovery.[/]")
        pause()
        return

    if AVAILABLE.get("discovery.monitor"):
        console.print("[cyan]Running discovery cycle via JobMonitor…[/]")
        try:
            from discovery.monitor import JobMonitor
            monitor = JobMonitor()
            monitor._init_components()          # ← ADD THIS
            result = monitor.discover_cycle()
            console.print(f"\n[bold green]Discovery complete![/]")
            for k, v in result.items():
                console.print(f"  {k}: {v}")
        except Exception as e:
            console.print(f"[red]Discovery error: {e}[/]")
            log_error("Discovery error: %s", e)
    else:
        manual_search()

    pause()


def manual_search(cli_platform: str = None, cli_query: str = None):
    """Manual search on a specific platform."""
    clear_screen()
    console.rule("[bold cyan]Manual Job Search[/]", style="cyan")

    # Find available platform modules
    available = []
    for key, name in PLATFORM_NAMES.items():
        if AVAILABLE.get(f"platforms.{key}"):
            available.append((key, name))

    if not available:
        console.print("[yellow]No platform modules built yet.[/]")
        console.print("[dim]Build platforms/naukri.py first.[/]")
        pause()
        return

    # If CLI args provided, skip interactive prompts
    if cli_platform and cli_query:
        platform_key = cli_platform
        platform_name = PLATFORM_NAMES.get(platform_key, platform_key)
        if not AVAILABLE.get(f"platforms.{platform_key}"):
            console.print(f"[red]Platform module '{platform_key}' not available.[/]")
            pause()
            return
        queries = [cli_query]
    else:
        console.print("\n[bold]Available platforms:[/]")
        for i, (key, name) in enumerate(available, 1):
            note = " [dim](search only)[/]" if key == "linkedin" else ""
            console.print(f"  {i}. {name}{note}")
        console.print("  0. Cancel")

        choice = Prompt.ask("Choose platform", default="0")
        if choice == "0":
            return

        try:
            idx = int(choice) - 1
            if not (0 <= idx < len(available)):
                console.print("[red]Invalid choice.[/]")
                pause()
                return
        except ValueError:
            console.print("[red]Invalid input.[/]")
            pause()
            return

        platform_key, platform_name = available[idx]

        # Get search query
        platform_config = get_config_val("PLATFORM_CONFIG", {})
        pconf = platform_config.get(platform_key, {})
        default_queries = pconf.get("search_queries", ["Full Stack Developer"])

        console.print(f"\n[dim]Configured queries: {', '.join(default_queries)}[/]")
        query = Prompt.ask("Search query (Enter for configured queries)",
                           default="")
        queries = [query] if query else default_queries

    console.print(f"\n[cyan]Searching {platform_name} for: {', '.join(queries)}[/]")
    console.print("[dim]This may take a minute…[/]\n")

    try:
        from core.browser import BrowserEngine
        browser = BrowserEngine()

        # Instantiate platform
        if platform_key == "naukri":
            from platforms.naukri import NaukriPlatform
            platform = NaukriPlatform(browser)
        elif platform_key == "indeed":
            from platforms.indeed import IndeedPlatform
            platform = IndeedPlatform(browser)
        elif platform_key == "foundit":
            from platforms.foundit import FounditPlatform
            platform = FounditPlatform(browser)
        elif platform_key == "linkedin":
            from platforms.linkedin import LinkedInPlatform
            platform = LinkedInPlatform(browser)
        else:
            console.print(f"[red]Platform '{platform_key}' not supported yet.[/]")
            pause()
            return

        # Login first
        console.print("[dim]Logging in…[/]")
        login_ok = platform.login()
        if not login_ok:
            console.print("[red]❌ Login failed. Cannot search.[/]")
            browser.close_all()
            pause()
            return
        console.print("[green]✅ Logged in[/]\n")

        # Search
        all_jobs = []
        for q in queries:
            if _shutdown_requested:
                break
            console.print(f"[cyan]Searching: '{q}'…[/]")
            try:
                results = platform.search_jobs(queries=[q], filters={})
                console.print(f"  Found {len(results)} results")
                all_jobs.extend(results)
            except Exception as e:
                console.print(f"  [red]Search error: {e}[/]")

        browser.close_all()

        if not all_jobs:
            console.print("\n[yellow]No jobs found.[/]")
            pause()
            return

        # Dedup
        dedup = None
        if AVAILABLE.get("discovery.dedup"):
            from discovery.dedup import Deduplicator
            dedup = Deduplicator()
            unique_jobs = dedup.merge_duplicates(all_jobs)
            dupes = len(all_jobs) - len(unique_jobs)
            if dupes > 0:
                console.print(f"[dim]Removed {dupes} duplicates[/]")
            all_jobs = unique_jobs

        # Filter (if available)
        if AVAILABLE.get("discovery.filters") and AVAILABLE.get("profile.preferences"):
            from discovery.filters import JobFilter
            from profile.preferences import get_preferences
            jf = JobFilter()
            prefs = get_preferences()
            filtered = jf.apply_filters(all_jobs, prefs)
            skipped = len(all_jobs) - len(filtered)
            if skipped > 0:
                console.print(f"[dim]Filtered out {skipped} jobs[/]")
            all_jobs = filtered

        # Save to DB
        db = get_db_safe()
        saved_count = 0
        if db:
            for job in all_jobs:
                try:
                    db.save_job(job)
                    saved_count += 1
                    if dedup:
                        try:
                            dedup.mark_seen(job)
                        except Exception:
                            pass
                except Exception:
                    pass

        console.print(f"\n[bold green]✅ Search complete![/]")
        console.print(f"  Total found: {len(all_jobs)}")
        console.print(f"  Saved to DB: {saved_count}")

        # Show results
        if all_jobs:
            tbl = Table(title="Search Results", box=box.SIMPLE_HEAVY,
                        show_header=True, header_style="bold")
            tbl.add_column("#", width=3)
            tbl.add_column("Company", width=20)
            tbl.add_column("Title", width=30)
            tbl.add_column("Location", width=15)
            tbl.add_column("Salary", width=15)

            for i, j in enumerate(all_jobs[:20], 1):
                sal = ""
                if j.get("salary_text"):
                    sal = truncate(str(j["salary_text"]), 14)
                tbl.add_row(
                    str(i),
                    truncate(j.get("company", ""), 19),
                    truncate(j.get("title", ""), 29),
                    truncate(j.get("location", ""), 14),
                    sal or "—",
                )
            console.print(tbl)
            if len(all_jobs) > 20:
                console.print(f"[dim]  … and {len(all_jobs) - 20} more[/]")

    except Exception as e:
        console.print(f"[red]Search error: {e}[/]")
        log_error("Manual search error: %s", e)
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/]")

    pause()

# ═══════════════════════════════════════════════════════════════
#  7. PROFILE VIEWER
# ═══════════════════════════════════════════════════════════════

def show_profile():
    """Display resume data and preferences."""
    clear_screen()
    console.rule("[bold cyan]Profile & Preferences[/]", style="cyan")

    # Resume data
    if AVAILABLE.get("profile.resume_data"):
        from profile.resume_data import get_base_resume
        try:
            resume = get_base_resume()
            console.print(Panel(
                f"[bold]{resume.name}[/]\n"
                f"Email: {resume.email}  |  Phone: {resume.phone}\n"
                f"Location: {resume.location}\n"
                f"LinkedIn: {resume.linkedin}\n"
                f"GitHub: {resume.github}\n\n"
                f"[bold]Summary:[/]\n{truncate(resume.summary, 200)}",
                title="📄 Resume Data",
                border_style="cyan",
                box=box.ROUNDED,
            ))

            # Skills
            if resume.skills:
                if isinstance(resume.skills, dict):
                    skill_lines = []
                    for cat, items in resume.skills.items():
                        if isinstance(items, list):
                            skill_lines.append(
                                f"[cyan]{cat}:[/] {', '.join(items)}"
                            )
                        else:
                            skill_lines.append(f"[cyan]{cat}:[/] {items}")
                    console.print(Panel(
                        "\n".join(skill_lines),
                        title="🛠 Skills",
                        border_style="green",
                        box=box.ROUNDED,
                    ))

            # Experience
            if resume.experience:
                exp_lines = []
                for exp in resume.experience:
                    if isinstance(exp, dict):
                        exp_lines.append(
                            f"[bold]{exp.get('title', '')}[/] @ {exp.get('company', '')}"
                            f"\n  [dim]{exp.get('period', '')}[/]"
                        )
                    else:
                        exp_lines.append(str(exp))
                console.print(Panel(
                    "\n".join(exp_lines),
                    title="💼 Experience",
                    border_style="yellow",
                    box=box.ROUNDED,
                ))

        except Exception as e:
            console.print(f"[red]Error loading resume: {e}[/]")
    else:
        console.print("[dim]profile/resume_data.py not available.[/]")

    # Preferences
    if AVAILABLE.get("profile.preferences"):
        from profile.preferences import get_preferences
        try:
            prefs = get_preferences()
            pref_lines = []
            for field in [
                "target_titles", "target_locations", "min_salary",
                "max_salary", "preferred_work_mode", "notice_period",
                "willing_to_relocate", "blacklist_companies",
            ]:
                val = getattr(prefs, field, None)
                if val is not None:
                    if isinstance(val, list):
                        val = ", ".join(str(v) for v in val[:5])
                        if not val:
                            val = "—"
                    pref_lines.append(
                        f"[cyan]{field.replace('_', ' ').title()}:[/] {val}"
                    )
            console.print(Panel(
                "\n".join(pref_lines),
                title="🎯 Job Preferences",
                border_style="magenta",
                box=box.ROUNDED,
            ))
        except Exception as e:
            console.print(f"[red]Error loading preferences: {e}[/]")
    else:
        console.print("[dim]profile/preferences.py not available.[/]")

    pause()


# ═══════════════════════════════════════════════════════════════
#  8. DATABASE TOOLS (embedded in Status menu)
# ═══════════════════════════════════════════════════════════════

def database_tools():
    """Database inspection and maintenance."""
    while True:
        clear_screen()
        console.rule("[bold cyan]Database Tools[/]", style="cyan")

        console.print("\n  1. Table info (row counts)")
        console.print("  2. View recent jobs")
        console.print("  3. View applications")
        console.print("  4. View errors log")
        console.print("  5. Pipeline / funnel stats")
        console.print("  6. Export jobs to JSON")
        console.print("  7. Clear all data (⚠️ destructive)")
        console.print("  0. Back")

        choice = Prompt.ask("\nChoose", choices=["0", "1", "2", "3", "4", "5", "6", "7"],
                            default="0")

        if choice == "0":
            return
        elif choice == "1":
            show_db_status()
            pause()
        elif choice == "2":
            show_jobs()
        elif choice == "3":
            show_applications()
        elif choice == "4":
            show_errors()
        elif choice == "5":
            show_pipeline()
        elif choice == "6":
            export_jobs_json()
        elif choice == "7":
            clear_database()


def show_applications():
    """Show applications from DB."""
    db = get_db_safe()
    if not db:
        console.print("[red]Database not available.[/]")
        pause()
        return

    try:
        apps = db.get_applications(limit=30)
    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        pause()
        return

    if not apps:
        console.print("[dim]No applications found.[/]")
        pause()
        return

    tbl = Table(
        title=f"Applications ({len(apps)})",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold",
    )
    tbl.add_column("ID", width=4)
    tbl.add_column("Job ID", width=6)
    tbl.add_column("Platform", width=10)
    tbl.add_column("Method", width=12)
    tbl.add_column("Status", width=12)
    tbl.add_column("Applied At", width=12)
    tbl.add_column("Follow-ups", width=10, justify="center")

    for a in apps:
        applied = a.get("applied_at") or ""
        if applied and len(applied) > 10:
            applied = applied[:10]

        status = a.get("status", "")
        status_styles = {
            "submitted": "[cyan]submitted[/]",
            "viewed": "[blue]viewed[/]",
            "shortlisted": "[green]shortlisted[/]",
            "interview": "[bold green]interview[/]",
            "rejected": "[red]rejected[/]",
            "offer": "[bold yellow]OFFER[/]",
            "ghosted": "[dim]ghosted[/]",
        }

        tbl.add_row(
            str(a.get("id", "")),
            str(a.get("job_id", "")),
            str(a.get("platform", "")),
            str(a.get("method", "")),
            status_styles.get(status, status),
            applied,
            str(a.get("follow_up_count", 0)),
        )

    console.print(tbl)
    pause()


def show_errors():
    """Show recent errors from DB."""
    db = get_db_safe()
    if not db:
        console.print("[red]Database not available.[/]")
        pause()
        return

    try:
        # Get errors — db.get_jobs won't work, need direct query
        conn = db.conn if hasattr(db, 'conn') else None
        if not conn:
            console.print("[dim]Cannot access errors table directly.[/]")
            pause()
            return

        cursor = conn.execute(
            "SELECT id, timestamp, module, error_type, message "
            "FROM errors ORDER BY id DESC LIMIT 20"
        )
        errors = cursor.fetchall()
    except Exception as e:
        console.print(f"[dim]Error reading errors table: {e}[/]")
        pause()
        return

    if not errors:
        console.print("[green]No errors logged. 🎉[/]")
        pause()
        return

    tbl = Table(
        title="Recent Errors",
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold red",
    )
    tbl.add_column("ID", width=4)
    tbl.add_column("Time", width=19)
    tbl.add_column("Module", width=18)
    tbl.add_column("Type", width=18)
    tbl.add_column("Message", width=40)

    for e in errors:
        tbl.add_row(
            str(e[0]),
            str(e[1] or "")[:19],
            str(e[2] or ""),
            str(e[3] or ""),
            truncate(str(e[4] or ""), 39),
        )

    console.print(tbl)
    pause()


def show_pipeline():
    """Show application funnel / pipeline stats."""
    db = get_db_safe()
    if not db:
        console.print("[red]Database not available.[/]")
        pause()
        return

    try:
        pipeline = db.get_pipeline()
    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        pause()
        return

    console.rule("[bold]Application Pipeline[/]", style="cyan")

    stages = [
        ("🔍 Discovered", "new", "cyan"),
        ("🎯 Matched", "matched", "green"),
        ("📋 Queued", "queued", "yellow"),
        ("📤 Applied", "applied", "blue"),
        ("👁 Viewed", "viewed", "magenta"),
        ("⭐ Shortlisted", "shortlisted", "green"),
        ("🎤 Interview", "interview", "bold green"),
        ("🎁 Offer", "offer", "bold yellow"),
        ("❌ Rejected", "rejected", "red"),
        ("👻 Ghosted", "ghosted", "dim"),
    ]

    max_val = max(pipeline.values()) if pipeline else 1

    for label, key, color in stages:
        count = pipeline.get(key, 0)
        bar_len = int((count / max(max_val, 1)) * 30) if count > 0 else 0
        bar = "█" * bar_len
        console.print(f"  {label:20s} [{color}]{bar} {count}[/]")

    pause()


def export_jobs_json():
    """Export all jobs to a JSON file."""
    db = get_db_safe()
    if not db:
        console.print("[red]Database not available.[/]")
        pause()
        return

    try:
        jobs = db.get_jobs(limit=50000)
        output_dir = get_config_val("CACHE_DIR", os.path.join(PROJECT_ROOT, "cache"))
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(
            output_dir,
            f"jobs_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2, default=str, ensure_ascii=False)
        console.print(f"[green]✅ Exported {len(jobs)} jobs to:[/] {filepath}")
    except Exception as e:
        console.print(f"[red]Export error: {e}[/]")

    pause()


def clear_database():
    """Clear all data from the database."""
    console.print("\n[bold red]⚠️  WARNING: This will delete ALL data![/]")
    console.print("[red]Jobs, applications, contacts, emails, errors — everything.[/]")

    if not Confirm.ask("\nAre you absolutely sure?", default=False):
        console.print("[dim]Cancelled.[/]")
        pause()
        return

    confirm_text = Prompt.ask("Type 'DELETE' to confirm")
    if confirm_text != "DELETE":
        console.print("[dim]Cancelled.[/]")
        pause()
        return

    db = get_db_safe()
    if not db:
        console.print("[red]Database not available.[/]")
        pause()
        return

    try:
        conn = db.conn if hasattr(db, "conn") else None
        if conn:
            tables = ["emails", "applications", "contacts", "jobs",
                       "platform_sessions", "errors"]
            for table in tables:
                try:
                    conn.execute(f"DELETE FROM {table}")
                except Exception:
                    pass
            conn.commit()
            console.print("[green]✅ All data cleared.[/]")
        else:
            console.print("[red]Cannot access database connection.[/]")
    except Exception as e:
        console.print(f"[red]Error clearing database: {e}[/]")

    pause()


# ═══════════════════════════════════════════════════════════════
#  9. PHASE 2/3/4 PLACEHOLDERS
# ═══════════════════════════════════════════════════════════════

def start_agent():
    """Start the full autonomous agent loop (Phase 3)."""
    if not AVAILABLE.get("discovery.monitor"):
        console.print(Panel(
            "[yellow]Full agent loop requires Phase 3 modules:[/]\n\n"
            "  • discovery/monitor.py  (main loop)\n"
            "  • tracking/notifications.py  (Telegram)\n"
            "  • platforms/manager.py  (orchestrator)\n"
            "  • ai/job_matcher.py  (scoring)\n"
            "  • ai/resume_tailor.py  (resume)\n\n"
            "[dim]Build Phase 2 and Phase 3 modules first.[/]",
            title="🚀 Start Agent — Not Ready",
            border_style="yellow",
            box=box.ROUNDED,
        ))
        pause()
        return

    console.print("[cyan]Starting full agent loop…[/]")
    try:
        from discovery.monitor import JobMonitor
        monitor = JobMonitor()
        console.print("[green]Agent started. Press Ctrl+C to stop.[/]")
        monitor.start()
    except KeyboardInterrupt:
        console.print("\n[yellow]Agent stopped.[/]")
    except Exception as e:
        console.print(f"[red]Agent error: {e}[/]")
        log_error("Agent error: %s", e)

    pause()


def apply_queue():
    """Process pending application queue."""
    if not AVAILABLE.get("discovery.monitor"):
        console.print("[yellow]Requires discovery/monitor.py[/]")
        pause()
        return

    console.print("[cyan]Processing application queue…[/]")
    try:
        from discovery.monitor import JobMonitor
        monitor = JobMonitor()
        monitor._init_components()
        result = monitor.apply_cycle()
        console.print(f"\n[bold green]Apply cycle complete![/]")
        for k, v in result.items():
            console.print(f"  {k}: {v}")
    except Exception as e:
        console.print(f"[red]Apply error: {e}[/]")
        log_error("Apply queue error: %s", e)

    pause()

def resume_tools():
    """Resume tailoring and ATS tools (Phase 2)."""
    if not AVAILABLE.get("resume.builder"):
        console.print(Panel(
            "[yellow]Resume tools require Phase 2 modules:[/]\n\n"
            "  • ai/llm_client.py\n"
            "  • ai/resume_tailor.py\n"
            "  • resume/builder.py\n"
            "  • resume/optimizer.py\n\n"
            "[dim]Build Phase 2 modules first.[/]",
            title="📄 Resume Tools — Not Ready",
            border_style="yellow",
            box=box.ROUNDED,
        ))
        pause()
        return

    # Phase 2 sub-menu
    clear_screen()
    console.rule("[bold cyan]Resume Tools[/]", style="cyan")
    console.print("\n  1. Preview base resume")
    console.print("  2. Tailor resume for a job")
    console.print("  3. ATS score check")
    console.print("  4. Generate cover letter")
    console.print("  0. Back")

    choice = Prompt.ask("Choose", choices=["0", "1", "2", "3", "4"], default="0")

    if choice == "0":
        return
    # Phase 2 implementations would go here
    console.print("[dim]Coming in Phase 2 implementation.[/]")
    pause()


def email_tools():
    """Email outreach tools (Phase 3)."""
    if not AVAILABLE.get("outreach.email_sender"):
        console.print("[yellow]Email tools require Phase 3 modules "
                      "(outreach/email_sender.py, etc.).[/]")
        pause()
        return

    console.print("[dim]Not yet implemented — coming in Phase 3.[/]")
    pause()


def web_dashboard():
    """Launch web dashboard (Phase 4)."""
    if not AVAILABLE.get("web.app"):
        console.print(Panel(
            "[yellow]Web dashboard requires Phase 4 modules:[/]\n\n"
            "  • web/app.py (Flask)\n"
            "  • web/templates/\n"
            "  • tracking/reports.py\n\n"
            "[dim]Build Phase 4 modules first.[/]",
            title="🌍 Web Dashboard — Not Ready",
            border_style="yellow",
            box=box.ROUNDED,
        ))
        pause()
        return

    console.print("[cyan]Launching web dashboard at http://localhost:5000 …[/]")
    try:
        from web.app import app
        app.run(host="0.0.0.0", port=5000, debug=False)
    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        pause()


# ═══════════════════════════════════════════════════════════════
#  MAIN MENU
# ═══════════════════════════════════════════════════════════════

MENU_ITEMS = [
    ("1", "🚀  Start Agent",       "Full autonomous loop",          start_agent),
    ("2", "🔍  Discovery Only",    "Find jobs, don't apply",        run_discovery),
    ("3", "📤  Apply Queue",       "Process pending applications",  apply_queue),
    ("4", "📊  Dashboard",         "Terminal overview",             show_dashboard),
    ("5", "🔎  Manual Search",     "Search a specific platform",    manual_search),
    ("6", "📋  System Status",     "Modules, platforms, DB",        show_full_status),
    ("7", "🌐  Manage Platforms",  "Login, test, sessions",         manage_platforms),
    ("8", "📄  Resume Tools",      "Tailor, preview, ATS",          resume_tools),
    ("9", "📧  Email Tools",       "Queue, send, follow-ups",       email_tools),
    ("10", "🌍  Web Dashboard",    "Launch browser dashboard",      web_dashboard),
    ("11", "👤  View Profile",     "Resume data & preferences",     show_profile),
    ("12", "🗄️   Database Tools",   "View, export, clean DB",        database_tools),
    ("0", "❌  Exit",              "",                              None),
]


def show_menu():
    """Display the main menu."""
    console.print()
    tbl = Table(
        box=box.SIMPLE,
        show_header=False,
        show_edge=False,
        pad_edge=False,
        padding=(0, 2),
    )
    tbl.add_column("Key", style="bold cyan", width=4, justify="right")
    tbl.add_column("Action", width=24)
    tbl.add_column("Description", style="dim", width=35)

    for key, label, desc, _ in MENU_ITEMS:
        tbl.add_row(key, label, desc)

    console.print(tbl)


def menu_loop():
    """Main interactive menu loop."""
    valid_choices = [item[0] for item in MENU_ITEMS]

    while not _shutdown_requested:
        clear_screen()
        print_banner()
        show_menu()

        try:
            choice = Prompt.ask(
                "\n[bold cyan]Choose[/]",
                choices=valid_choices,
                default="4",
                show_choices=False,
            )
        except (EOFError, KeyboardInterrupt):
            break

        if choice == "0":
            console.print("\n[cyan]Goodbye! Happy job hunting! 🚀[/]\n")
            break

        # Find and execute the action
        for key, label, desc, action in MENU_ITEMS:
            if key == choice and action:
                try:
                    action()
                except KeyboardInterrupt:
                    console.print("\n[yellow]Action interrupted.[/]")
                    pause()
                except Exception as e:
                    console.print(f"\n[red]Error: {e}[/]")
                    log_error("Menu action error (%s): %s", label, e)
                    import traceback
                    console.print(f"[dim]{traceback.format_exc()}[/]")
                    pause()
                break


# ═══════════════════════════════════════════════════════════════
#  CLI ARGUMENT PARSER
# ═══════════════════════════════════════════════════════════════

def parse_args():
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description=f"{AGENT_NAME} v{VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python main.py                # Interactive menu
              python main.py --dashboard    # Terminal dashboard
              python main.py --status       # System status
              python main.py --jobs         # List jobs in DB
              python main.py --discover     # Run one discovery cycle
              python main.py --start        # Start full agent
        """),
    )

    parser.add_argument(
        "--start", action="store_true",
        help="Start full autonomous agent loop",
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="Run one discovery cycle (search → dedup → save)",
    )
    parser.add_argument(
        "--dashboard", action="store_true",
        help="Show terminal dashboard",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show system status and exit",
    )
    parser.add_argument(
        "--jobs", action="store_true",
        help="List jobs in database",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="Show application history",
    )
    parser.add_argument(
        "--profile", action="store_true",
        help="Show profile and preferences",
    )
    parser.add_argument(
        "--search", type=str, metavar="QUERY",
        help="Manual search with given query",
    )
    parser.add_argument(
        "--platform", type=str,
        choices=["naukri", "indeed", "foundit", "linkedin"],
        help="Platform for --search (default: naukri)",
        default="naukri",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"{AGENT_NAME} v{VERSION}",
    )

    return parser.parse_args()


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    """Entry point: handle CLI args or launch interactive menu."""
    args = parse_args()
    log_info("Agent started (v%s)", VERSION)

    # ── Ensure required directories exist ─────────────────────
    for dir_attr in ["CACHE_DIR", "LOGS_DIR", "BROWSER_PROFILES_DIR", "RESUME_OUTPUT_DIR"]:
        dir_path = get_config_val(dir_attr)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

    # ── Handle CLI args (non-interactive mode) ────────────────
    if args.start:
        start_agent()
        return

    if args.discover:
        print_banner()
        run_discovery()
        return

    if args.dashboard:
        show_dashboard()
        return

    if args.status:
        show_full_status()
        return

    if args.jobs:
        print_banner()
        show_jobs()
        return

    if args.history:
        print_banner()
        show_applications()
        return

    if args.profile:
        show_profile()
        return

    if args.search:
        print_banner()
        manual_search(cli_platform=args.platform, cli_query=args.search)
        return


    # ── No args → interactive menu ────────────────────────────
    menu_loop()

    log_info("Agent stopped")


# ═══════════════════════════════════════════════════════════════
#  TEST BLOCK
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    main()