"""
config.py — Single Source of Truth for Job Application AI Agent
All other files import settings from here. Never hardcode credentials or paths elsewhere.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ═══════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH)

CACHE_DIR = BASE_DIR / "cache"
LOGS_DIR = BASE_DIR / "logs"
DB_PATH = BASE_DIR / "data" / "job_agent.db"
BROWSER_PROFILES_DIR = BASE_DIR / "browser_profiles"
RESUME_OUTPUT_DIR = BASE_DIR / "resume" / "output"
RESUME_TEMPLATES_DIR = BASE_DIR / "profile" / "templates"

# Auto-create directories
for d in [CACHE_DIR, LOGS_DIR, DB_PATH.parent, BROWSER_PROFILES_DIR, RESUME_OUTPUT_DIR, RESUME_TEMPLATES_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════
# CREDENTIALS (from .env)
# ═══════════════════════════════════════════════════════════
NAUKRI_EMAIL = os.getenv("NAUKRI_EMAIL", "")
NAUKRI_PASSWORD = os.getenv("NAUKRI_PASSWORD", "")

INDEED_EMAIL = os.getenv("INDEED_EMAIL", "")
# Indeed: cookie-based, no password

LINKEDIN_EMAIL = os.getenv("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD", "")

FOUNDIT_EMAIL = os.getenv("FOUNDIT_EMAIL", "")
# Foundit: cookie-based OTP, no password stored

SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")  # Optional, 50 free/month

# ═══════════════════════════════════════════════════════════
# USER PROFILE
# ═══════════════════════════════════════════════════════════
USER_PROFILE = {
    "name": "Piyush Kashyap",
    "email": "piyushkashyap3247@gmail.com",
    "phone": "+91 73107 03247",
    "location": "Rishikesh, Uttarakhand, India",
    "experience_years": 1.0,
    "current_title": "Full Stack Developer L1",
    "current_company": "Site Guru Pvt Ltd",
    "current_ctc": 3.7,  # LPA
    "notice_period": "Immediate / 15 days",
    "target_titles": [
        "Software Engineer",
        "Backend Developer",
        "Full Stack Developer",
        "SDE-1",
        "SDE 1",
        "Java Developer",
        "Spring Boot Developer",
        "Node.js Developer",
        "Python Developer",
        "Software Developer",
        "Junior Software Engineer",
        "Associate Software Engineer",
        "Web Developer",
        "MERN Stack Developer",
        "Application Developer",
    ],
    "target_locations": [
        "Bangalore",
        "Bengaluru",
        "Hyderabad",
        "Pune",
        "Remote",
        "Delhi NCR",
        "Delhi",
        "Noida",
        "Gurugram",
        "Gurgaon",
        "Mumbai",
        "Work From Home",
        "Hybrid",
    ],
    "min_salary": 6.0,  # LPA — do not target roles below the requested floor
    "preferred_salary_min": 6.0,  # LPA
    "preferred_salary_max": 10.0,  # LPA
    "dream_salary_max": 16.0,  # LPA
    "skills": [
        "JavaScript", "ES6+", "Java", "Python", "SQL",
        "Vue.js", "Nuxt.js", "React.js", "React",
        "Vuetify", "Tailwind CSS", "HTML", "CSS",
        "Node.js", "Express.js", "Spring Boot",
        "REST APIs", "WebSockets", "Microservices",
        "MySQL", "MongoDB", "PostgreSQL", "Redis",
        "Razorpay", "META WhatsApp API",
        "Git", "GitHub", "Docker", "Kafka", "JWT",
        "Linux", "Agile",
    ],
    "linkedin_url": "https://linkedin.com/in/piyush-kashyap731",
    "github_url": "https://github.com/Piyush731",
    "dob": "",  # Fill if needed for forms
    "gender": "Male",
    "willing_to_relocate": True,
    "preferred_work_mode": ["Remote", "Hybrid", "On-site"],
}

# ═══════════════════════════════════════════════════════════
# PLATFORM CONFIG
# ═══════════════════════════════════════════════════════════
PLATFORM_CONFIG = {
    "naukri": {
        "enabled": True,
        "base_url": "https://www.naukri.com",
        "max_daily_applications": 25,
        "search_interval_minutes": 30,
        "search_queries": [
            "Java Developer",
            "Spring Boot Developer",
            "Full Stack Developer",
            "Backend Developer",
            "SDE 1",
            "Software Engineer",
            "Node.js Developer",
            "MERN Stack Developer",
            "Python Developer",
            "React Developer",
        ],
        "experience_range": (0, 2),  # years
        "rate_limit_seconds": (180, 360),  # 3-6 min between applies
        "cooldown_hours": 24,
        "max_pages_per_query": 5,
    },
    "indeed": {
        "enabled": False,
        "base_url": "https://in.indeed.com",
        "max_daily_applications": 20,
        "search_interval_minutes": 45,
        "search_queries": [
            "Java Developer",
            "Full Stack Developer",
            "Backend Developer",
            "Software Engineer",
            "SDE 1",
            "Node.js Developer",
        ],
        "experience_range": (0, 2),
        "rate_limit_seconds": (180, 420),  # 3-7 min
        "cooldown_hours": 24,
        "max_pages_per_query": 3,
    },
    "foundit": {
        "enabled": False,
        "base_url": "https://www.foundit.in",
        "max_daily_applications": 20,
        "search_interval_minutes": 45,
        "search_queries": [
            "Java Developer",
            "Full Stack Developer",
            "Backend Developer",
            "Software Engineer",
            "Node.js Developer",
        ],
        "experience_range": (0, 2),
        "rate_limit_seconds": (180, 420),
        "cooldown_hours": 24,
        "max_pages_per_query": 3,
    },
    "linkedin": {
        "enabled": True,
        "base_url": "https://www.linkedin.com",
        "max_daily_applications": 0,  # SEARCH ONLY — no apply
        "search_interval_minutes": 60,
        "search_queries": [
            "Java Developer",
            "Full Stack Developer",
            "Backend Developer",
            "SDE 1",
            "Software Engineer",
        ],
        "experience_range": (0, 2),
        "rate_limit_seconds": (300, 600),  # 5-10 min between searches
        "cooldown_hours": 48,
        "max_pages_per_query": 3,
    },
}

# ═══════════════════════════════════════════════════════════
# AI CONFIG
# ═══════════════════════════════════════════════════════════
AI_CONFIG = {
    # Primary — Gemini free tier
    "provider": "gemini",
    "model": "gemini-3.6-flash",
    "api_key_env": "GEMINI_API_KEY",
    # Backup — Groq free tier
    "backup_provider": "groq",
    "backup_model": "openai/gpt-oss-20b",
    "backup_api_key_env": "GROQ_API_KEY",
    # Fallback — local Ollama
    "local_provider": "ollama",
    "local_model": "llama3.2:3b",
    "local_url": "http://localhost:11434",
    # Generation settings
    "temperature": 0.3,
    "max_tokens": 1000,
    # Rate limits (Gemini free tier)
    "rpm_limit": 14,  # Stay under 15 RPM
    "daily_limit": 1400,  # Stay under 1500 RPD
    "tokens_per_day_limit": 900000,  # Stay under 1M
}

# ═══════════════════════════════════════════════════════════
# RESUME CONFIG
# ═══════════════════════════════════════════════════════════
RESUME_CONFIG = {
    "base_resume_path": str(RESUME_TEMPLATES_DIR / "resume_base.docx"),
    "output_dir": str(RESUME_OUTPUT_DIR),
    "output_format": "docx",  # docx or pdf
    "tailor_mode": "light",  # generic / light / full
    "include_cover_letter": True,
    "ats_optimize": True,
    "max_resume_pages": 2,
    "font_name": "Calibri",
    "font_size": 11,
    "header_font_size": 14,
}

# ═══════════════════════════════════════════════════════════
# EMAIL CONFIG
# ═══════════════════════════════════════════════════════════
EMAIL_CONFIG = {
    "enabled": True,
    "daily_limit": 30,
    "follow_up_schedule": [3, 7, 14],  # days after application
    "send_hours": (9, 18),  # IST, business hours only
    "timezone": "Asia/Kolkata",
    "min_match_score_for_email": 80,  # Only email HR for 80+ matches
    "rate_limit_seconds": 120,  # 2 min between emails
    "max_follow_ups": 3,  # Don't spam
}

# ═══════════════════════════════════════════════════════════
# MATCH CONFIG
# ═══════════════════════════════════════════════════════════
MATCH_CONFIG = {
    "min_score_to_apply": 30,
    "auto_apply_score": 30,  # Skip approval for high matches
    "email_hr_score": 80,  # Email HR directly for 80+ matches
    "weights": {
        "title": 0.25,
        "skills": 0.30,
        "experience": 0.15,
        "location": 0.15,
        "salary": 0.10,
        "company_quality": 0.05,
    },
    "blacklist_companies": [
        # Companies to never apply to
    ],
    "blacklist_titles": [
        "intern",
        "internship",
        "trainee",
        "data entry",
        "bpo",
        "telecaller",
        "sales executive",
        "marketing executive",
        "content writer",
        "seo executive",
        "graphic designer",
        "ui/ux",
        "devops",
        "cloud engineer",
        "data scientist",
        "ml engineer",
        "ai engineer",
        "ios developer",
        "android developer",
        "flutter developer",
        "react native developer",
        "wordpress developer",
        "php developer",
        "lead",
        "manager",
        "architect",
        "principal",
        "staff engineer",
        "director",
        "vp",
        "head of",
        "chief",
        "senior software engineer",
        "senior developer",
        "sr.",
        "sr ",
    ],
    "whitelist_companies": [
        # Dream companies — always apply
        "Razorpay",
        "Zerodha",
        "Flipkart",
        "PhonePe",
        "Swiggy",
        "Zomato",
        "Freshworks",
        "Zoho",
        "Atlassian",
        "Google",
        "Microsoft",
        "Amazon",
        "Paytm",
        "CRED",
        "Dream11",
        "Meesho",
        "Groww",
        "Slice",
        "Jupiter",
        "Postman",
    ],
}

# ═══════════════════════════════════════════════════════════
# STEALTH / ANTI-BAN CONFIG
# ═══════════════════════════════════════════════════════════
STEALTH_CONFIG = {
    "random_delay_range": (3, 12),  # seconds between actions
    "page_scroll_delay": (1, 3),
    "typing_delay": (0.05, 0.15),  # per character
    "session_max_minutes": 45,
    "session_break_minutes": (10, 30),
    "headless": os.getenv("JOB_AGENT_HEADLESS", "true").strip().lower() in {"1", "true", "yes", "on"},  # Server-safe by default
    "human_mouse_movement": True,
    "active_hours": (8, 23),  # Only operate 8AM-11PM IST
    "skip_probability": 0.05,  # Skip 5% of valid jobs (looks human)
    "viewport_sizes": [
        (1366, 768),
        (1440, 900),
        (1536, 864),
        (1920, 1080),
    ],
    "user_agents": [
        # Kept empty — playwright-stealth handles this
        # Add custom ones here if needed
    ],
}

# ═══════════════════════════════════════════════════════════
# TELEGRAM CONFIG
# ═══════════════════════════════════════════════════════════
TELEGRAM_CONFIG = {
    "enabled": True,
    "send_matches": True,
    "send_applications": True,
    "send_responses": True,
    "send_daily_report": True,
    "send_errors": True,
    "approve_before_apply": True,  # Require explicit approval before submission
    "approve_timeout_minutes": 30,  # Expiry safely skips; it never auto-submits
    "captcha_timeout_minutes": 5,
    "otp_timeout_minutes": 2,
    "answer_review_timeout_minutes": 5,
}

# ═══════════════════════════════════════════════════════════
# SCHEDULE CONFIG
# ═══════════════════════════════════════════════════════════
SCHEDULE_CONFIG = {
    "discovery_interval_minutes": 30,
    "apply_batch_size": 10,
    "follow_up_check_hours": 12,
    "daily_report_hour": 20,  # 8PM IST
    "profile_update_hours": 48,  # Update Naukri profile every 48h
    "session_refresh_hours": 6,  # Refresh cookies every 6h
}

# ═══════════════════════════════════════════════════════════
# LOG CONFIG
# ═══════════════════════════════════════════════════════════
LOG_CONFIG = {
    "level": "INFO",
    "max_bytes": 10 * 1024 * 1024,  # 10 MB
    "backup_count": 5,
    "log_file": str(LOGS_DIR / "agent.log"),
    "console_colors": True,
}


# ═══════════════════════════════════════════════════════════
# VALIDATION — Run on import to catch issues early
# ═══════════════════════════════════════════════════════════
def validate_config():
    """Validate critical configuration. Returns list of warnings."""
    warnings = []
    errors = []

    # Critical checks
    if not GEMINI_API_KEY and not GROQ_API_KEY:
        errors.append("No AI API key set! Need at least GEMINI_API_KEY or GROQ_API_KEY in .env")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        warnings.append("Telegram not configured — no notifications/approvals")

    # Platform checks
    if PLATFORM_CONFIG["naukri"]["enabled"] and (not NAUKRI_EMAIL or not NAUKRI_PASSWORD):
        warnings.append("Naukri enabled but credentials missing in .env")

    if PLATFORM_CONFIG["indeed"]["enabled"] and not INDEED_EMAIL:
        warnings.append("Indeed enabled but INDEED_EMAIL missing in .env")

    if PLATFORM_CONFIG["linkedin"]["enabled"] and (not LINKEDIN_EMAIL or not LINKEDIN_PASSWORD):
        warnings.append("LinkedIn enabled but credentials missing in .env")

    if PLATFORM_CONFIG["foundit"]["enabled"] and not FOUNDIT_EMAIL:
        warnings.append("Foundit enabled but FOUNDIT_EMAIL missing in .env")

    # Email checks
    if EMAIL_CONFIG["enabled"] and (not SMTP_EMAIL or not SMTP_PASSWORD):
        warnings.append("Email enabled but SMTP credentials missing in .env")

    # Weight validation
    total_weight = sum(MATCH_CONFIG["weights"].values())
    if abs(total_weight - 1.0) > 0.01:
        warnings.append(f"Match weights sum to {total_weight}, should be 1.0")

    # Path checks
    if not ENV_PATH.exists():
        errors.append(f".env file not found at {ENV_PATH}")

    return {"errors": errors, "warnings": warnings}


# ═══════════════════════════════════════════════════════════
# TEST BLOCK
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("  Job Agent — Config Validation")
    print("=" * 60)

    result = validate_config()

    print(f"\n📂 Base Dir:        {BASE_DIR}")
    print(f"📂 Cache Dir:       {CACHE_DIR}")
    print(f"📂 Logs Dir:        {LOGS_DIR}")
    print(f"📂 DB Path:         {DB_PATH}")
    print(f"📂 Browser Profiles:{BROWSER_PROFILES_DIR}")
    print(f"📂 Resume Output:   {RESUME_OUTPUT_DIR}")

    print(f"\n👤 User: {USER_PROFILE['name']}")
    print(f"📧 Email: {USER_PROFILE['email']}")
    print(f"💼 Current: {USER_PROFILE['current_title']} @ {USER_PROFILE['current_company']}")
    print(f"💰 Target: {USER_PROFILE['min_salary']}-{USER_PROFILE['preferred_salary_max']} LPA")

    print(f"\n🔑 Credentials Status:")
    print(f"   Naukri:   {'✅' if NAUKRI_EMAIL and NAUKRI_PASSWORD else '❌'} ({NAUKRI_EMAIL or 'not set'})")
    print(f"   Indeed:   {'✅' if INDEED_EMAIL else '❌'} ({INDEED_EMAIL or 'not set'})")
    print(f"   LinkedIn: {'✅' if LINKEDIN_EMAIL and LINKEDIN_PASSWORD else '❌'} ({LINKEDIN_EMAIL or 'not set'})")
    print(f"   Foundit:  {'✅' if FOUNDIT_EMAIL else '❌'} ({FOUNDIT_EMAIL or 'not set'})")
    print(f"   SMTP:     {'✅' if SMTP_EMAIL and SMTP_PASSWORD else '❌'} ({SMTP_EMAIL or 'not set'})")
    print(f"   Gemini:   {'✅' if GEMINI_API_KEY else '❌'} ({'set' if GEMINI_API_KEY else 'not set'})")
    print(f"   Groq:     {'✅' if GROQ_API_KEY else '❌'} ({'set' if GROQ_API_KEY else 'not set'})")
    print(f"   Telegram: {'✅' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else '❌'}")
    print(f"   Hunter:   {'✅' if HUNTER_API_KEY else '⚪ (optional)'}")

    print(f"\n🔧 Platform Status:")
    for name, cfg in PLATFORM_CONFIG.items():
        status = "✅ Enabled" if cfg["enabled"] else "❌ Disabled"
        limit = cfg["max_daily_applications"]
        apply_text = f"max {limit}/day" if limit > 0 else "SEARCH ONLY"
        print(f"   {name.capitalize():12s} {status} — {apply_text}")

    print(f"\n🤖 AI Config:")
    print(f"   Primary:  {AI_CONFIG['provider']} ({AI_CONFIG['model']})")
    print(f"   Backup:   {AI_CONFIG['backup_provider']} ({AI_CONFIG['backup_model']})")
    print(f"   Fallback: {AI_CONFIG['local_provider']} ({AI_CONFIG['local_model']})")
    print(f"   RPM Limit: {AI_CONFIG['rpm_limit']} | Daily Limit: {AI_CONFIG['daily_limit']}")

    print(f"\n📊 Match Config:")
    print(f"   Min to apply: {MATCH_CONFIG['min_score_to_apply']}")
    print(f"   Auto-apply:   {MATCH_CONFIG['auto_apply_score']}+")
    print(f"   Email HR:     {MATCH_CONFIG['email_hr_score']}+")
    print(f"   Weights:      {MATCH_CONFIG['weights']}")
    print(f"   Blacklist Co: {len(MATCH_CONFIG['blacklist_companies'])} companies")
    print(f"   Blacklist Ti: {len(MATCH_CONFIG['blacklist_titles'])} titles")
    print(f"   Whitelist Co: {len(MATCH_CONFIG['whitelist_companies'])} companies")

    if result["errors"]:
        print(f"\n❌ ERRORS ({len(result['errors'])}):")
        for e in result["errors"]:
            print(f"   🔴 {e}")

    if result["warnings"]:
        print(f"\n⚠️  WARNINGS ({len(result['warnings'])}):")
        for w in result["warnings"]:
            print(f"   🟡 {w}")

    if not result["errors"] and not result["warnings"]:
        print(f"\n✅ All checks passed! Config is valid.")
    elif not result["errors"]:
        print(f"\n✅ No critical errors. Warnings above are non-blocking.")
    else:
        print(f"\n❌ Fix errors above before running the agent.")

    print()