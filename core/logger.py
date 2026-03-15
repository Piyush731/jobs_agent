"""
core/logger.py — Rotating file + colored console logger
Usage: from core.logger import get_logger
       logger = get_logger(__name__)
       logger.info("Hello")
"""

import sys
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Import config
try:
    from config import LOG_CONFIG, LOGS_DIR
except ImportError:
    # Fallback if run standalone
    LOG_CONFIG = {
        "level": "INFO",
        "max_bytes": 10 * 1024 * 1024,
        "backup_count": 5,
        "log_file": "logs/agent.log",
        "console_colors": True,
    }
    LOGS_DIR = Path("logs")
    LOGS_DIR.mkdir(exist_ok=True)


# ═══════════════════════════════════════════════════════════
# ANSI Color Codes
# ═══════════════════════════════════════════════════════════
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    BG_RED = "\033[41m"
    BG_YELLOW = "\033[43m"

    LEVEL_COLORS = {
        "DEBUG": CYAN,
        "INFO": GREEN,
        "WARNING": YELLOW,
        "ERROR": RED,
        "CRITICAL": f"{BOLD}{BG_RED}{WHITE}",
    }

    MODULE_COLORS = {
        "config": MAGENTA,
        "core.db": BLUE,
        "core.browser": CYAN,
        "platforms": YELLOW,
        "ai": GREEN,
        "resume": MAGENTA,
        "outreach": BLUE,
        "tracking": CYAN,
        "discovery": YELLOW,
        "web": GREEN,
        "main": WHITE,
    }


# ═══════════════════════════════════════════════════════════
# Colored Console Formatter
# ═══════════════════════════════════════════════════════════
class ColoredFormatter(logging.Formatter):
    """Console formatter with ANSI colors."""

    def __init__(self, use_colors=True):
        super().__init__()
        self.use_colors = use_colors

    def format(self, record):
        # Timestamp
        timestamp = self.formatTime(record, "%H:%M:%S")

        # Level
        level = record.levelname
        if self.use_colors:
            level_color = Colors.LEVEL_COLORS.get(level, Colors.WHITE)
            level_str = f"{level_color}{level:8s}{Colors.RESET}"
        else:
            level_str = f"{level:8s}"

        # Module name (shortened)
        name = record.name
        if len(name) > 25:
            parts = name.split(".")
            if len(parts) > 2:
                name = f"{parts[0]}.{parts[-1]}"
            if len(name) > 25:
                name = name[:22] + "..."

        if self.use_colors:
            # Find module color
            mod_color = Colors.WHITE
            for prefix, color in Colors.MODULE_COLORS.items():
                if record.name.startswith(prefix):
                    mod_color = color
                    break
            name_str = f"{mod_color}{name:25s}{Colors.RESET}"
        else:
            name_str = f"{name:25s}"

        # Message
        message = record.getMessage()

        # Format
        line = f"{Colors.DIM}{timestamp}{Colors.RESET} │ {level_str} │ {name_str} │ {message}"

        # Exception info
        if record.exc_info and record.exc_info[0]:
            exc_text = self.formatException(record.exc_info)
            if self.use_colors:
                line += f"\n{Colors.RED}{exc_text}{Colors.RESET}"
            else:
                line += f"\n{exc_text}"

        return line


# ═══════════════════════════════════════════════════════════
# File Formatter (no colors, full detail)
# ═══════════════════════════════════════════════════════════
class FileFormatter(logging.Formatter):
    """Plain text formatter for log files."""

    def __init__(self):
        super().__init__(
            fmt="%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


# ═══════════════════════════════════════════════════════════
# Logger Cache (singleton per name)
# ═══════════════════════════════════════════════════════════
_loggers = {}
_handlers_initialized = False
_root_handler_console = None
_root_handler_file = None


def _init_root_handlers():
    """Initialize root handlers once."""
    global _handlers_initialized, _root_handler_console, _root_handler_file

    if _handlers_initialized:
        return

    root_logger = logging.getLogger("job_agent")
    root_logger.setLevel(getattr(logging, LOG_CONFIG["level"], logging.INFO))

    # Prevent duplicate handlers
    if root_logger.handlers:
        _handlers_initialized = True
        return

    # Console handler
    _root_handler_console = logging.StreamHandler(sys.stdout)
    _root_handler_console.setLevel(logging.DEBUG)
    _root_handler_console.setFormatter(
        ColoredFormatter(use_colors=LOG_CONFIG.get("console_colors", True))
    )
    root_logger.addHandler(_root_handler_console)

    # File handler
    log_file = Path(LOG_CONFIG["log_file"])
    log_file.parent.mkdir(parents=True, exist_ok=True)

    _root_handler_file = RotatingFileHandler(
        filename=str(log_file),
        maxBytes=LOG_CONFIG["max_bytes"],
        backupCount=LOG_CONFIG["backup_count"],
        encoding="utf-8",
    )
    _root_handler_file.setLevel(logging.DEBUG)
    _root_handler_file.setFormatter(FileFormatter())
    root_logger.addHandler(_root_handler_file)

    # Don't propagate to root
    root_logger.propagate = False

    _handlers_initialized = True


# ═══════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════
def get_logger(name: str) -> logging.Logger:
    """
    Get a named logger under 'job_agent' namespace.

    Usage:
        from core.logger import get_logger
        logger = get_logger(__name__)
        logger.info("Starting discovery cycle")

    Args:
        name: Module name (typically __name__)

    Returns:
        logging.Logger with colored console + rotating file output
    """
    if name in _loggers:
        return _loggers[name]

    _init_root_handlers()

    # Create child logger under job_agent namespace
    if name.startswith("job_agent."):
        logger_name = name
    else:
        # Strip leading module path, keep meaningful part
        logger_name = f"job_agent.{name}"

    logger = logging.getLogger(logger_name)
    # Child inherits handlers from parent, don't add more
    logger.propagate = True

    _loggers[name] = logger
    return logger


def set_level(level: str):
    """
    Change log level at runtime.

    Args:
        level: "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root_logger = logging.getLogger("job_agent")
    root_logger.setLevel(numeric_level)

    logger = get_logger("core.logger")
    logger.info(f"Log level changed to {level.upper()}")


# ═══════════════════════════════════════════════════════════
# TEST BLOCK
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("  Logger Test")
    print("=" * 60)
    print()

    # Test different module names
    log1 = get_logger("config")
    log2 = get_logger("core.db")
    log3 = get_logger("core.browser")
    log4 = get_logger("platforms.naukri")
    log5 = get_logger("ai.job_matcher")
    log6 = get_logger("discovery.monitor")
    log7 = get_logger("main")

    log1.info("Config loaded successfully")
    log2.info("Database initialized — 6 tables created")
    log3.info("Browser launched for naukri (headless=False)")
    log4.warning("Naukri rate limit approaching: 23/25 today")
    log5.info("Job scored: SDE-1 @ Razorpay — 87/100")
    log6.info("Discovery cycle complete: 47 new, 12 matched")
    log7.info("Agent started — Phase 1 active")

    # Test levels
    print()
    test_log = get_logger("test")
    test_log.debug("This is a DEBUG message")
    test_log.info("This is an INFO message")
    test_log.warning("This is a WARNING message")
    test_log.error("This is an ERROR message")
    test_log.critical("This is a CRITICAL message")

    # Test exception
    print()
    try:
        x = 1 / 0
    except Exception:
        test_log.exception("Caught an exception")

    # Test level change
    print()
    set_level("WARNING")
    test_log.info("This should NOT appear (level=WARNING)")
    test_log.warning("This SHOULD appear (level=WARNING)")
    set_level("INFO")
    test_log.info("Back to INFO level — this should appear")

    print()
    log_file = LOG_CONFIG["log_file"]
    print(f"✅ Check log file: {log_file}")
    print("✅ Logger test complete!")