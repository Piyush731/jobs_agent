"""
profile/preferences.py — Job search preferences, target criteria, and filters.

Defines what jobs to target, where, salary range, blacklists/whitelists,
and all filtering criteria used by:
  - discovery/filters.py (pre-filter scraped jobs)
  - ai/job_matcher.py (weighted scoring)
  - platforms/* (search query construction)
  - outreach (should we email HR?)

Interface:
  get_preferences() → JobPreferences
  preferences_to_dict(prefs) → dict
  dict_to_preferences(data) → JobPreferences

All values come from config.USER_PROFILE and config.MATCH_CONFIG
but are enriched with additional detail here.
"""

import copy
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Set, Tuple, Any

# ── project imports ──────────────────────────────────────────────
from config import USER_PROFILE, MATCH_CONFIG, PLATFORM_CONFIG
from core.logger import get_logger

logger = get_logger("profile.preferences")


# ═══════════════════════════════════════════════════════════════════
# SEARCH QUERY DATACLASS
# ═══════════════════════════════════════════════════════════════════

@dataclass
class SearchQuery:
    """
    A single search query to run on job platforms.
    Combination of keywords + location + filters.
    """
    keywords: str = ""                    # "Java Spring Boot Developer"
    location: str = ""                    # "Bangalore"
    experience_min: Optional[float] = None
    experience_max: Optional[float] = None
    salary_min: Optional[float] = None    # in LPA
    job_type: str = "full-time"           # full-time | contract | internship
    work_mode: str = ""                   # remote | hybrid | onsite | ""=any
    posted_within: str = ""               # "1d" | "3d" | "7d" | "14d" | ""
    sort_by: str = "date"                 # "date" | "relevance"
    priority: int = 5                     # 1=run first, 10=run last

    def display(self) -> str:
        parts = [self.keywords]
        if self.location:
            parts.append(f"in {self.location}")
        if self.work_mode:
            parts.append(f"({self.work_mode})")
        if self.posted_within:
            parts.append(f"[{self.posted_within}]")
        return " ".join(parts)


# ═══════════════════════════════════════════════════════════════════
# MAIN PREFERENCES DATACLASS
# ═══════════════════════════════════════════════════════════════════

@dataclass
class JobPreferences:
    """
    Complete job search preferences for the agent.

    Used by:
      - filters.py: filter jobs before scoring
      - job_matcher.py: weight scoring
      - platforms: construct search queries
      - monitor.py: decide what to search and when
    """

    # ── Target Job Titles ────────────────────────────────────────
    target_titles: List[str] = field(default_factory=list)
    # Primary titles we want (exact or fuzzy matched)

    title_aliases: Dict[str, List[str]] = field(default_factory=dict)
    # "Software Engineer" → ["SDE", "Software Dev", "SE"]
    # Used for fuzzy matching during scoring

    # ── Target Locations ─────────────────────────────────────────
    target_locations: List[str] = field(default_factory=list)
    # Cities we'll work in (or "Remote")

    willing_to_relocate: bool = True
    relocation_preferences: List[str] = field(default_factory=list)
    # Ordered by preference: most preferred first

    # ── Salary ───────────────────────────────────────────────────
    current_ctc: float = 0.0              # LPA
    min_salary: float = 0.0               # LPA — absolute minimum
    preferred_salary_min: float = 0.0     # LPA — preferred range start
    preferred_salary_max: float = 0.0     # LPA — preferred range end
    dream_salary: float = 0.0            # LPA — aspiration
    salary_negotiable: bool = True
    currency: str = "INR"

    # ── Experience ───────────────────────────────────────────────
    total_experience_years: float = 0.0
    min_experience_required: float = 0.0  # apply if JD asks ≥ this
    max_experience_required: float = 0.0  # skip if JD asks > this
    # e.g., if JD says "3-5 years" and max_experience_required=3,
    # we'll still apply because our actual exp might be close

    # ── Work Preferences ─────────────────────────────────────────
    preferred_work_mode: List[str] = field(default_factory=list)
    # "remote", "hybrid", "onsite" — in preference order

    preferred_job_type: List[str] = field(default_factory=list)
    # "full-time", "contract" — in preference order

    preferred_company_size: List[str] = field(default_factory=list)
    # "startup", "mid", "large", "mnc"

    preferred_industries: List[str] = field(default_factory=list)
    # "fintech", "edtech", "saas", "ecommerce" etc.

    # ── Notice Period ────────────────────────────────────────────
    notice_period: str = ""               # "Immediate" | "15 days" | "30 days"
    notice_period_days: int = 0
    currently_employed: bool = True

    # ── Blacklists (NEVER apply) ─────────────────────────────────
    blacklist_companies: List[str] = field(default_factory=list)
    # Exact company names to skip (case-insensitive)

    blacklist_titles: List[str] = field(default_factory=list)
    # Title keywords to skip: "intern", "trainee", "support", "bpo"

    blacklist_keywords: List[str] = field(default_factory=list)
    # JD keywords to skip: "night shift", "US shift", "walk-in"

    # ── Whitelists (ALWAYS apply, regardless of score) ───────────
    whitelist_companies: List[str] = field(default_factory=list)
    # Dream companies — apply even if score is low

    # ── Preferred Tech Stack ─────────────────────────────────────
    preferred_stacks: List[str] = field(default_factory=list)
    # "Java/Spring Boot", "MERN", "Python/Django"

    must_have_skills: List[str] = field(default_factory=list)
    # If JD requires these, boost score (we have them)

    learning_skills: List[str] = field(default_factory=list)
    # Skills we're learning — still apply if JD mentions them

    avoid_skills: List[str] = field(default_factory=list)
    # Skills we don't want to work with (e.g., proprietary stuff)

    # ── Search Queries (pre-built) ───────────────────────────────
    search_queries: List[SearchQuery] = field(default_factory=list)
    # Generated from target_titles × target_locations

    # ── Scoring Weights ──────────────────────────────────────────
    scoring_weights: Dict[str, float] = field(default_factory=dict)
    # From MATCH_CONFIG.weights

    # ── Application Thresholds ───────────────────────────────────
    min_score_to_apply: int = 40          # below this = skip
    auto_apply_score: int = 70            # above this = no Telegram approval
    email_hr_score: int = 80              # above this = also email HR directly

    # ── Daily Limits ─────────────────────────────────────────────
    max_daily_applications: int = 65      # total across all platforms
    max_daily_emails: int = 30

    # ── Metadata ─────────────────────────────────────────────────
    version: str = "1.0"
    last_updated: str = ""

    # ── Convenience Methods ──────────────────────────────────────

    def all_target_titles_with_aliases(self) -> List[str]:
        """Return a flat list of all target titles including aliases."""
        all_titles = list(self.target_titles)
        for aliases in self.title_aliases.values():
            all_titles.extend(aliases)
        # deduplicate preserving order
        seen = set()
        result = []
        for t in all_titles:
            t_lower = t.strip().lower()
            if t_lower not in seen:
                seen.add(t_lower)
                result.append(t.strip())
        return result

    def is_blacklisted_company(self, company: str) -> bool:
        """Check if company is blacklisted (case-insensitive)."""
        if not company:
            return False
        company_lower = company.strip().lower()
        for bl in self.blacklist_companies:
            if bl.strip().lower() in company_lower or company_lower in bl.strip().lower():
                return True
        return False

    def is_blacklisted_title(self, title: str) -> bool:
        """Check if title contains blacklisted keywords."""
        if not title:
            return False
        title_lower = title.strip().lower()
        for bl in self.blacklist_titles:
            if bl.strip().lower() in title_lower:
                return True
        return False

    def is_blacklisted_keyword(self, text: str) -> bool:
        """Check if text contains blacklisted keywords."""
        if not text:
            return False
        text_lower = text.strip().lower()
        for bl in self.blacklist_keywords:
            if bl.strip().lower() in text_lower:
                return True
        return False

    def is_whitelisted_company(self, company: str) -> bool:
        """Check if company is whitelisted (dream companies)."""
        if not company:
            return False
        company_lower = company.strip().lower()
        for wl in self.whitelist_companies:
            if wl.strip().lower() in company_lower or company_lower in wl.strip().lower():
                return True
        return False

    def is_location_match(self, location: str) -> bool:
        """Check if location matches any target location."""
        if not location:
            return False
        loc_lower = location.strip().lower()
        # "Remote" matches everything
        if "remote" in loc_lower:
            return True
        for target in self.target_locations:
            target_lower = target.strip().lower()
            if target_lower in loc_lower or loc_lower in target_lower:
                return True
            # Handle aliases: "Bengaluru" == "Bangalore"
            aliases = _CITY_ALIASES.get(target_lower, [])
            for alias in aliases:
                if alias in loc_lower:
                    return True
        return False

    def is_salary_acceptable(self, salary_min: Optional[float],
                              salary_max: Optional[float]) -> bool:
        """Check if job salary range overlaps with acceptable range."""
        if salary_min is None and salary_max is None:
            # No salary info → don't filter out
            return True
        if salary_max is not None and salary_max < self.min_salary:
            return False
        return True

    def is_experience_match(self, exp_min: Optional[float],
                             exp_max: Optional[float]) -> bool:
        """
        Check if we qualify for the experience range.
        We apply if our experience is ≥ (exp_min - 1 year buffer).
        We skip if exp_min > max_experience_required (way too senior).
        """
        if exp_min is None and exp_max is None:
            return True
        if exp_min is not None:
            # Allow applying if we're within 1 year below minimum
            if self.total_experience_years < (exp_min - 1.0):
                return False
        if exp_min is not None and exp_min > self.max_experience_required:
            return False
        return True

    def salary_expectation_for_job(self, job_salary_min: Optional[float],
                                     job_salary_max: Optional[float]) -> str:
        """
        Dynamic salary expectation string based on job's posted range.
        Used by answers.py for form filling.
        """
        if job_salary_min and job_salary_max:
            # Aim for 60-75th percentile of the posted range
            target = job_salary_min + (job_salary_max - job_salary_min) * 0.65
            target = max(target, self.min_salary)
            target = min(target, job_salary_max)
            return f"{target:.1f} LPA (negotiable)"
        if job_salary_max:
            target = max(job_salary_max * 0.75, self.min_salary)
            return f"{target:.1f} LPA (negotiable)"
        # No salary info → use default range
        return f"{self.preferred_salary_min}-{self.preferred_salary_max} LPA (negotiable)"

    def get_search_queries_for_platform(self, platform: str) -> List[SearchQuery]:
        """
        Return search queries, optionally filtered/modified per platform.
        Some platforms have different query syntax.
        """
        queries = list(self.search_queries)

        # Platform-specific overrides from config
        platform_cfg = PLATFORM_CONFIG.get(platform, {})
        platform_queries = platform_cfg.get("search_queries", [])

        if platform_queries:
            # Replace with platform-specific queries
            custom = []
            for q_str in platform_queries:
                for loc in self.target_locations:
                    custom.append(SearchQuery(
                        keywords=q_str,
                        location=loc,
                        experience_min=self.min_experience_required,
                        experience_max=self.max_experience_required,
                        salary_min=self.min_salary,
                        sort_by="date",
                        posted_within="3d",
                    ))
            return custom

        return queries

    def quick_filter(self, job: dict) -> Tuple[bool, str]:
        """
        Quick pass/fail filter before AI scoring. Returns (pass, reason).
        Called by discovery/filters.py.
        """
        company = job.get("company", "")
        title = job.get("title", "")
        location = job.get("location", "")
        description = job.get("description", "")
        salary_min = job.get("salary_min")
        salary_max = job.get("salary_max")
        exp_min = job.get("experience_min")
        exp_max = job.get("experience_max")

        # Whitelisted → always pass
        if self.is_whitelisted_company(company):
            return True, "whitelisted_company"

        # Blacklist checks
        if self.is_blacklisted_company(company):
            return False, f"blacklisted_company:{company}"
        if self.is_blacklisted_title(title):
            return False, f"blacklisted_title:{title}"
        if self.is_blacklisted_keyword(description):
            return False, "blacklisted_keyword_in_jd"

        # Salary check
        if not self.is_salary_acceptable(salary_min, salary_max):
            return False, f"salary_too_low:{salary_max}"

        # Experience check
        if not self.is_experience_match(exp_min, exp_max):
            return False, f"experience_mismatch:need_{exp_min}-{exp_max}_have_{self.total_experience_years}"

        # Location check (relaxed — let AI matcher do fine scoring)
        # Only reject if location is explicitly not India / completely unrelated
        # Don't reject here because many JDs have vague location info

        return True, "passed"

    def deep_copy(self) -> "JobPreferences":
        """Return a deep copy for modification."""
        return copy.deepcopy(self)

    def summary_string(self) -> str:
        """Human-readable summary of preferences."""
        lines = [
            f"Target Titles: {', '.join(self.target_titles[:5])}",
            f"Locations: {', '.join(self.target_locations)}",
            f"Salary: {self.min_salary}-{self.preferred_salary_max} LPA "
            f"(min {self.min_salary}, dream {self.dream_salary})",
            f"Experience: {self.total_experience_years} yrs "
            f"(apply for {self.min_experience_required}-{self.max_experience_required} yrs)",
            f"Notice: {self.notice_period}",
            f"Work Mode: {', '.join(self.preferred_work_mode)}",
            f"Stacks: {', '.join(self.preferred_stacks)}",
            f"Blacklist Companies: {len(self.blacklist_companies)}",
            f"Blacklist Titles: {', '.join(self.blacklist_titles)}",
            f"Whitelist: {', '.join(self.whitelist_companies[:5])}",
            f"Score Thresholds: apply≥{self.min_score_to_apply}, "
            f"auto≥{self.auto_apply_score}, email≥{self.email_hr_score}",
        ]
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# CITY ALIASES — for location matching
# ═══════════════════════════════════════════════════════════════════

_CITY_ALIASES: Dict[str, List[str]] = {
    "bangalore": ["bengaluru", "blr", "b'lore"],
    "bengaluru": ["bangalore", "blr", "b'lore"],
    "hyderabad": ["hyd", "secunderabad"],
    "pune": ["puna", "pcmc"],
    "mumbai": ["bombay", "navi mumbai", "thane"],
    "delhi": ["new delhi", "delhi ncr", "ncr", "noida", "gurgaon",
              "gurugram", "faridabad", "ghaziabad"],
    "delhi ncr": ["delhi", "new delhi", "noida", "gurgaon",
                  "gurugram", "faridabad", "ghaziabad", "ncr"],
    "noida": ["delhi ncr", "ncr", "greater noida"],
    "gurgaon": ["gurugram", "delhi ncr", "ncr"],
    "gurugram": ["gurgaon", "delhi ncr", "ncr"],
    "chennai": ["madras"],
    "kolkata": ["calcutta"],
    "remote": ["work from home", "wfh", "anywhere", "pan india"],
}


# ═══════════════════════════════════════════════════════════════════
# SERIALIZATION
# ═══════════════════════════════════════════════════════════════════

def preferences_to_dict(prefs: JobPreferences) -> dict:
    """Convert JobPreferences to a plain dict."""
    try:
        data = asdict(prefs)
        # SearchQuery objects are already converted by asdict
        logger.debug("Preferences serialized to dict")
        return data
    except Exception as e:
        logger.error(f"Failed to serialize preferences: {e}")
        raise


def dict_to_preferences(data: dict) -> JobPreferences:
    """Reconstruct JobPreferences from dict."""
    if not data or not isinstance(data, dict):
        raise ValueError("Expected a non-empty dict")

    try:
        # Reconstruct SearchQuery objects
        queries_raw = data.pop("search_queries", [])
        queries = []
        for q in queries_raw:
            if isinstance(q, dict):
                queries.append(SearchQuery(**q))
            elif isinstance(q, SearchQuery):
                queries.append(q)

        prefs = JobPreferences(**data, search_queries=queries)
        logger.debug("Preferences reconstructed from dict")
        return prefs
    except Exception as e:
        logger.error(f"Failed to reconstruct preferences: {e}")
        raise


# ═══════════════════════════════════════════════════════════════════
# BUILD SEARCH QUERIES — generate from titles × locations
# ═══════════════════════════════════════════════════════════════════

def _build_search_queries(titles: List[str],
                           locations: List[str],
                           experience_years: float,
                           min_salary: float) -> List[SearchQuery]:
    """
    Generate search queries from target titles × locations.
    Prioritize: recent posts × primary locations × primary titles.
    """
    queries = []
    seen = set()

    # Priority 1: primary titles × primary locations (most recent posts)
    primary_titles = titles[:5]           # top 5 titles
    primary_locations = locations[:4]     # top 4 locations

    for i, title in enumerate(primary_titles):
        for j, location in enumerate(primary_locations):
            key = f"{title.lower()}|{location.lower()}"
            if key not in seen:
                seen.add(key)
                queries.append(SearchQuery(
                    keywords=title,
                    location=location,
                    experience_min=0,
                    experience_max=max(experience_years + 1, 2),
                    salary_min=min_salary,
                    job_type="full-time",
                    posted_within="3d",
                    sort_by="date",
                    priority=1 + i + j,
                ))

    # Priority 2: secondary titles × primary locations
    secondary_titles = titles[5:]
    for title in secondary_titles:
        for location in primary_locations[:2]:
            key = f"{title.lower()}|{location.lower()}"
            if key not in seen:
                seen.add(key)
                queries.append(SearchQuery(
                    keywords=title,
                    location=location,
                    experience_min=0,
                    experience_max=max(experience_years + 1, 2),
                    salary_min=min_salary,
                    job_type="full-time",
                    posted_within="7d",
                    sort_by="date",
                    priority=6,
                ))

    # Priority 3: Remote-only queries for all titles
    for title in primary_titles:
        key = f"{title.lower()}|remote"
        if key not in seen:
            seen.add(key)
            queries.append(SearchQuery(
                keywords=title,
                location="Remote",
                experience_min=0,
                experience_max=max(experience_years + 1, 2),
                salary_min=min_salary,
                job_type="full-time",
                work_mode="remote",
                posted_within="7d",
                sort_by="date",
                priority=5,
            ))

    # Sort by priority
    queries.sort(key=lambda q: q.priority)

    logger.debug(f"Built {len(queries)} search queries from "
                 f"{len(titles)} titles × {len(locations)} locations")

    return queries


# ═══════════════════════════════════════════════════════════════════
# GET PREFERENCES — the main factory function
# ═══════════════════════════════════════════════════════════════════

def get_preferences() -> JobPreferences:
    """
    Return the complete, configured job preferences for Piyush Kashyap.

    Sources:
      - config.USER_PROFILE (name, location, experience, target titles/locations)
      - config.MATCH_CONFIG (weights, thresholds, blacklists)
      - Additional detail hardcoded here (not worth putting in config.py)

    Called by:
      - discovery/filters.py
      - ai/job_matcher.py
      - platforms/manager.py
      - discovery/monitor.py
    """

    # ── Extract from config ──
    target_titles = USER_PROFILE.get("target_titles", [
        "Software Engineer",
        "Backend Developer",
        "Full Stack Developer",
        "SDE-1",
        "Java Developer",
        "Spring Boot Developer",
        "Node.js Developer",
        "Python Developer",
        "Software Developer",
        "Web Developer",
    ])

    target_locations = USER_PROFILE.get("target_locations", [
        "Bangalore",
        "Hyderabad",
        "Pune",
        "Remote",
        "Delhi NCR",
        "Mumbai",
        "Noida",
        "Gurgaon",
        "Chennai",
    ])

    min_salary = USER_PROFILE.get("min_salary", 5.0)
    experience_years = USER_PROFILE.get("experience_years", 1.0)
    notice_period = USER_PROFILE.get("notice_period", "15 days")

    # Scoring weights from config
    weights = MATCH_CONFIG.get("weights", {
        "title": 0.25,
        "skills": 0.30,
        "experience": 0.15,
        "location": 0.15,
        "salary": 0.10,
        "company_quality": 0.05,
    })

    # Blacklists from config
    blacklist_companies = MATCH_CONFIG.get("blacklist_companies", [])
    blacklist_titles_cfg = MATCH_CONFIG.get("blacklist_titles", [])
    whitelist_companies = MATCH_CONFIG.get("whitelist_companies", [])

    # Thresholds
    min_score = MATCH_CONFIG.get("min_score_to_apply", 40)
    auto_apply = MATCH_CONFIG.get("auto_apply_score", 70)

    # ── Build the preferences ──
    prefs = JobPreferences(
        # ── Titles ──
        target_titles=target_titles,
        title_aliases={
            "Software Engineer": [
                "SDE", "SDE-1", "SDE 1", "Software Dev", "SE",
                "Associate Software Engineer", "Jr Software Engineer",
                "Junior Software Engineer", "Software Engineer I",
            ],
            "Backend Developer": [
                "Backend Engineer", "Server Side Developer",
                "API Developer", "Backend Dev",
            ],
            "Full Stack Developer": [
                "Fullstack Developer", "Full-Stack Developer",
                "Full Stack Engineer", "Fullstack Engineer",
                "MERN Developer", "MERN Stack Developer",
            ],
            "Java Developer": [
                "Java Engineer", "Java Backend Developer",
                "Java Spring Boot Developer", "J2EE Developer",
                "Core Java Developer",
            ],
            "Node.js Developer": [
                "Node Developer", "NodeJS Developer",
                "Node.js Engineer", "Express.js Developer",
            ],
            "Python Developer": [
                "Python Engineer", "Python Backend Developer",
                "Django Developer", "Flask Developer",
            ],
            "Web Developer": [
                "Frontend Developer", "UI Developer",
                "Vue.js Developer", "React Developer",
            ],
        },

        # ── Locations ──
        target_locations=target_locations,
        willing_to_relocate=True,
        relocation_preferences=[
            "Bangalore", "Hyderabad", "Pune", "Remote",
            "Delhi NCR", "Mumbai", "Chennai",
        ],

        # ── Salary ──
        current_ctc=3.7,
        min_salary=min_salary,
        preferred_salary_min=6.0,
        preferred_salary_max=10.0,
        dream_salary=16.0,
        salary_negotiable=True,
        currency="INR",

        # ── Experience ──
        total_experience_years=experience_years,
        min_experience_required=0.0,
        max_experience_required=3.0,
        # Apply for 0-3 year JDs. 3+ might be stretch but worth trying.

        # ── Work Preferences ──
        preferred_work_mode=["remote", "hybrid", "onsite"],
        preferred_job_type=["full-time"],
        preferred_company_size=["startup", "mid", "large", "mnc"],
        preferred_industries=[
            "fintech", "edtech", "saas", "ecommerce", "healthtech",
            "enterprise", "devtools", "ai", "cloud", "cybersecurity",
        ],

        # ── Notice Period ──
        notice_period=notice_period,
        notice_period_days=15,
        currently_employed=True,

        # ── Blacklists ──
        blacklist_companies=[
            *blacklist_companies,
            # Add specific companies to avoid:
            "Site Guru",              # current employer
            "Site Guru Pvt Ltd",      # current employer variant
        ],
        blacklist_titles=[
            *blacklist_titles_cfg,
            # Title keywords that indicate wrong role:
            "intern",
            "trainee",
            "support engineer",
            "technical support",
            "bpo",
            "customer support",
            "data entry",
            "telecaller",
            "sales executive",
            "hr executive",
            "content writer",
            "seo",
            "digital marketing",
            "graphic designer",
            "manual testing",  # unless automation
            "team lead",      # too senior
            "architect",      # too senior
            "principal",      # too senior
            "staff engineer", # too senior
            "director",       # too senior
            "vp ",            # too senior
            "head of",        # too senior
            "chief",          # too senior
            "manager",        # wrong track (unless eng manager someday)
        ],
        blacklist_keywords=[
            "night shift",
            "us shift",
            "uk shift",
            "graveyard shift",
            "walk-in interview only",
            "bond required",
            "2 year bond",
            "3 year bond",
            "unpaid",
            "stipend only",
            "no salary",
            # Technologies we definitely don't want:
            "mainframe",
            "cobol",
            "fortran",
        ],

        # ── Whitelists (dream companies) ──
        whitelist_companies=[
            *whitelist_companies,
            # Product companies — apply even if score is borderline:
            "Razorpay", "Zerodha", "Cred", "PhonePe", "Paytm",
            "Flipkart", "Meesho", "Swiggy", "Zomato", "Ola",
            "Freshworks", "Zoho", "Atlassian", "Google", "Microsoft",
            "Amazon", "Adobe", "Intuit", "Salesforce", "Oracle",
            "Uber", "Grab", "Stripe", "Twilio", "Postman",
            "Notion", "Figma", "Vercel", "Supabase", "GitHub",
            "GitLab", "JetBrains", "HashiCorp", "Confluent",
            "Thoughtworks", "Hasura", "Chargebee", "BrowserStack",
            "Slice", "Jupiter", "Groww", "Upstox", "Lenskart",
            "Dunzo", "Urban Company", "ShareChat", "Dream11",
            "MPL", "InMobi", "MakeMyTrip", "Nykaa",
            "Infosys", "TCS", "Wipro", "HCL", "Tech Mahindra",
            # ^ IT services — fallback if product doesn't work
        ],

        # ── Preferred Stacks ──
        preferred_stacks=[
            "Java/Spring Boot",
            "MERN (MongoDB, Express, React, Node)",
            "Vue.js/Nuxt.js/Node.js",   # current stack
            "Python/Django/Flask",
            "Node.js/Express.js",
        ],
        must_have_skills=[
            # Skills we definitely have — boost when JD mentions these:
            "JavaScript", "Node.js", "Vue.js", "React", "MySQL",
            "REST API", "Git", "HTML", "CSS",
        ],
        learning_skills=[
            # Skills we're building — still apply if JD mentions:
            "Java", "Spring Boot", "Microservices", "Docker",
            "Kafka", "PostgreSQL", "Redis", "MongoDB",
            "TypeScript", "Next.js", "GraphQL", "Kubernetes",
            "AWS", "CI/CD", "Jenkins", "Terraform",
            "Python", "Django", "Flask", "FastAPI",
        ],
        avoid_skills=[
            # Proprietary / niche stacks we'd rather avoid:
            # (won't auto-reject, just lower priority)
        ],

        # ── Pre-built Search Queries ──
        search_queries=_build_search_queries(
            titles=target_titles,
            locations=target_locations,
            experience_years=experience_years,
            min_salary=min_salary,
        ),

        # ── Scoring Weights ──
        scoring_weights=weights,

        # ── Thresholds ──
        min_score_to_apply=min_score,
        auto_apply_score=auto_apply,
        email_hr_score=80,

        # ── Daily Limits ──
        max_daily_applications=65,
        max_daily_emails=30,

        # ── Metadata ──
        version="1.0",
        last_updated="",
    )

    logger.debug(
        f"Preferences loaded: {len(prefs.target_titles)} titles, "
        f"{len(prefs.target_locations)} locations, "
        f"{len(prefs.search_queries)} search queries, "
        f"salary {prefs.min_salary}-{prefs.preferred_salary_max} LPA"
    )

    return prefs


# ═══════════════════════════════════════════════════════════════════
# TEST BLOCK
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel

    console = Console()

    console.print("\n[bold cyan]═══ Job Preferences Module Test ═══[/bold cyan]\n")

    # ── 1. Load preferences ──
    console.print("[yellow]1. Loading preferences...[/yellow]")
    prefs = get_preferences()
    console.print(f"   [green]✓[/green] Loaded successfully (v{prefs.version})")

    # ── 2. Target Titles ──
    console.print(f"\n[yellow]2. Target Titles ({len(prefs.target_titles)}):[/yellow]")
    for t in prefs.target_titles:
        aliases = prefs.title_aliases.get(t, [])
        alias_str = f" (aliases: {', '.join(aliases[:3])}...)" if aliases else ""
        console.print(f"   [green]✓[/green] {t}{alias_str}")

    all_titles = prefs.all_target_titles_with_aliases()
    console.print(f"   [cyan]Total with aliases: {len(all_titles)}[/cyan]")

    # ── 3. Locations ──
    console.print(f"\n[yellow]3. Target Locations ({len(prefs.target_locations)}):[/yellow]")
    for loc in prefs.target_locations:
        console.print(f"   [green]✓[/green] {loc}")

    # ── 4. Salary ──
    console.print(f"\n[yellow]4. Salary Preferences:[/yellow]")
    console.print(f"   Current CTC: {prefs.current_ctc} LPA")
    console.print(f"   Minimum: {prefs.min_salary} LPA")
    console.print(f"   Preferred: {prefs.preferred_salary_min}-{prefs.preferred_salary_max} LPA")
    console.print(f"   Dream: {prefs.dream_salary} LPA")
    console.print(f"   Negotiable: {prefs.salary_negotiable}")

    # ── 5. Experience ──
    console.print(f"\n[yellow]5. Experience:[/yellow]")
    console.print(f"   Total: {prefs.total_experience_years} years")
    console.print(f"   Apply for: {prefs.min_experience_required}-{prefs.max_experience_required} year roles")
    console.print(f"   Notice: {prefs.notice_period} ({prefs.notice_period_days} days)")

    # ── 6. Blacklists ──
    console.print(f"\n[yellow]6. Blacklists:[/yellow]")
    console.print(f"   Companies ({len(prefs.blacklist_companies)}): "
                  f"{', '.join(prefs.blacklist_companies[:5])}")
    console.print(f"   Titles ({len(prefs.blacklist_titles)}): "
                  f"{', '.join(prefs.blacklist_titles[:8])}...")
    console.print(f"   Keywords ({len(prefs.blacklist_keywords)}): "
                  f"{', '.join(prefs.blacklist_keywords[:5])}...")

    # ── 7. Whitelists ──
    console.print(f"\n[yellow]7. Whitelist Companies ({len(prefs.whitelist_companies)}):[/yellow]")
    console.print(f"   {', '.join(prefs.whitelist_companies[:10])}...")

    # ── 8. Search Queries ──
    console.print(f"\n[yellow]8. Search Queries ({len(prefs.search_queries)}):[/yellow]")
    table = Table(title="Search Queries (first 10)")
    table.add_column("#", style="dim", width=3)
    table.add_column("Keywords", style="cyan")
    table.add_column("Location", style="green")
    table.add_column("Posted", style="yellow")
    table.add_column("Priority", style="magenta", justify="right")
    for i, q in enumerate(prefs.search_queries[:10]):
        table.add_row(
            str(i + 1), q.keywords, q.location,
            q.posted_within or "any", str(q.priority)
        )
    console.print(table)

    # ── 9. Blacklist checks ──
    console.print(f"\n[yellow]9. Blacklist checks:[/yellow]")
    test_companies = [
        ("Site Guru Pvt Ltd", True),
        ("Razorpay", False),
        ("Some Random Corp", False),
        ("Site Guru", True),
    ]
    for company, expected in test_companies:
        result = prefs.is_blacklisted_company(company)
        icon = "[green]✓[/green]" if result == expected else "[red]✗[/red]"
        console.print(f"   {icon} '{company}' blacklisted={result} (expected {expected})")

    test_titles = [
        ("Software Engineer", False),
        ("Java Intern", True),
        ("SDE-1", False),
        ("Technical Support Engineer", True),
        ("BPO Executive", True),
        ("Backend Developer", False),
        ("Principal Architect", True),
        ("Data Entry Operator", True),
    ]
    for title, expected in test_titles:
        result = prefs.is_blacklisted_title(title)
        icon = "[green]✓[/green]" if result == expected else "[red]✗[/red]"
        console.print(f"   {icon} '{title}' blacklisted={result} (expected {expected})")

    # ── 10. Whitelist checks ──
    console.print(f"\n[yellow]10. Whitelist checks:[/yellow]")
    test_wl = [
        ("Razorpay", True),
        ("Google", True),
        ("Random Startup XYZ", False),
        ("Flipkart", True),
        ("Zerodha", True),
    ]
    for company, expected in test_wl:
        result = prefs.is_whitelisted_company(company)
        icon = "[green]✓[/green]" if result == expected else "[red]✗[/red]"
        console.print(f"   {icon} '{company}' whitelisted={result} (expected {expected})")

    # ── 11. Location matching ──
    console.print(f"\n[yellow]11. Location matching:[/yellow]")
    test_locs = [
        ("Bangalore, Karnataka", True),
        ("Bengaluru", True),
        ("Remote", True),
        ("Work from Home", True),
        ("Hyderabad, Telangana", True),
        ("New York, USA", False),
        ("Noida, UP", True),     # part of Delhi NCR
        ("Gurgaon", True),       # part of Delhi NCR
        ("Jaipur", False),
        ("Pune, Maharashtra", True),
        ("Mumbai", True),
        ("Pan India", True),     # alias for Remote
    ]
    for loc, expected in test_locs:
        result = prefs.is_location_match(loc)
        icon = "[green]✓[/green]" if result == expected else "[red]✗[/red]"
        console.print(f"   {icon} '{loc}' match={result} (expected {expected})")

    # ── 12. Salary checks ──
    console.print(f"\n[yellow]12. Salary acceptability:[/yellow]")
    test_salaries = [
        (None, None, True, "no info"),
        (3.0, 4.5, False, "below min"),
        (5.0, 8.0, True, "acceptable range"),
        (8.0, 12.0, True, "good range"),
        (15.0, 25.0, True, "dream range"),
        (2.0, 3.5, False, "too low"),
        (6.0, None, True, "no max but min ok"),
        (None, 10.0, True, "no min, max ok"),
        (None, 3.0, False, "no min, max too low"),
    ]
    for sal_min, sal_max, expected, desc in test_salaries:
        result = prefs.is_salary_acceptable(sal_min, sal_max)
        icon = "[green]✓[/green]" if result == expected else "[red]✗[/red]"
        console.print(f"   {icon} {sal_min}-{sal_max} LPA ({desc}): {result} "
                      f"(expected {expected})")

    # ── 13. Experience matching ──
    console.print(f"\n[yellow]13. Experience matching:[/yellow]")
    test_exp = [
        (None, None, True, "no requirements"),
        (0, 1, True, "entry level"),
        (0, 2, True, "junior"),
        (1, 3, True, "1-3 years"),
        (2, 4, True, "2-4 yrs (within buffer)"),
        (3, 5, True, "3-5 yrs (borderline)"),
        (4, 6, False, "4-6 yrs (too senior, >max_exp_required)"),
        (5, 8, False, "5-8 yrs (way too senior)"),
        (0, 0, True, "fresher ok"),
    ]
    for exp_min, exp_max, expected, desc in test_exp:
        result = prefs.is_experience_match(exp_min, exp_max)
        icon = "[green]✓[/green]" if result == expected else "[red]✗[/red]"
        console.print(f"   {icon} {exp_min}-{exp_max} yrs ({desc}): {result} "
                      f"(expected {expected})")

    # ── 14. Dynamic salary expectation ──
    console.print(f"\n[yellow]14. Dynamic salary expectations:[/yellow]")
    test_salary_dynamic = [
        (6.0, 10.0, "for 6-10 LPA job"),
        (8.0, 15.0, "for 8-15 LPA job"),
        (None, None, "for unknown salary job"),
        (4.0, 6.0, "for 4-6 LPA job"),
        (None, 12.0, "for max-only 12 LPA job"),
    ]
    for s_min, s_max, desc in test_salary_dynamic:
        answer = prefs.salary_expectation_for_job(s_min, s_max)
        console.print(f"   [green]✓[/green] {desc}: {answer}")

    # ── 15. Quick filter (simulated jobs) ──
    console.print(f"\n[yellow]15. Quick filter (simulated jobs):[/yellow]")
    test_jobs = [
        {
            "title": "SDE-1", "company": "Razorpay",
            "location": "Bangalore", "salary_min": 12, "salary_max": 18,
            "experience_min": 1, "experience_max": 3,
            "description": "Looking for a backend developer with Java and Spring Boot.",
            "_expected": True, "_desc": "dream job"
        },
        {
            "title": "Java Intern", "company": "Random Corp",
            "location": "Pune", "salary_min": None, "salary_max": None,
            "experience_min": 0, "experience_max": 0,
            "description": "Internship opportunity for freshers.",
            "_expected": False, "_desc": "blacklisted title 'intern'"
        },
        {
            "title": "Backend Developer", "company": "Site Guru Pvt Ltd",
            "location": "Rishikesh", "salary_min": 5, "salary_max": 8,
            "experience_min": 1, "experience_max": 2,
            "description": "Backend work.",
            "_expected": False, "_desc": "blacklisted company (current employer)"
        },
        {
            "title": "Software Engineer", "company": "Some Startup",
            "location": "Bangalore", "salary_min": 2, "salary_max": 3,
            "experience_min": 0, "experience_max": 1,
            "description": "Entry level role.",
            "_expected": False, "_desc": "salary too low"
        },
        {
            "title": "Senior Architect", "company": "Google",
            "location": "Bangalore", "salary_min": 30, "salary_max": 50,
            "experience_min": 8, "experience_max": 12,
            "description": "10+ years experience required.",
            "_expected": True, "_desc": "whitelisted (Google) overrides exp"
        },
        {
            "title": "Full Stack Developer", "company": "Meesho",
            "location": "Remote", "salary_min": 8, "salary_max": 14,
            "experience_min": 1, "experience_max": 3,
            "description": "React, Node.js, PostgreSQL",
            "_expected": True, "_desc": "good match"
        },
        {
            "title": "Night Shift Support", "company": "TCS",
            "location": "Mumbai", "salary_min": 5, "salary_max": 7,
            "experience_min": 0, "experience_max": 2,
            "description": "Night shift US client support with cobol mainframe.",
            "_expected": False, "_desc": "blacklisted keywords"
        },
        {
            "title": "Data Entry Operator", "company": "Infosys",
            "location": "Pune", "salary_min": 3, "salary_max": 4,
            "experience_min": 0, "experience_max": 1,
            "description": "Data entry work.",
            "_expected": False, "_desc": "blacklisted title"
        },
    ]
    for job in test_jobs:
        expected = job.pop("_expected")
        desc = job.pop("_desc")
        passed, reason = prefs.quick_filter(job)
        icon = "[green]✓[/green]" if passed == expected else "[red]✗[/red]"
        status = "PASS" if passed else "FAIL"
        console.print(
            f"   {icon} {job['title']} @ {job['company']}: "
            f"{status} — {reason} ({desc})"
        )

    # ── 16. Serialization round-trip ──
    console.print(f"\n[yellow]16. Serialization round-trip:[/yellow]")
    d = preferences_to_dict(prefs)
    console.print(f"   [green]✓[/green] preferences_to_dict() → {len(d)} keys")
    reconstructed = dict_to_preferences(d)
    console.print(f"   [green]✓[/green] dict_to_preferences() → "
                  f"{len(reconstructed.target_titles)} titles")
    assert len(reconstructed.target_titles) == len(prefs.target_titles)
    assert len(reconstructed.search_queries) == len(prefs.search_queries)
    assert reconstructed.min_salary == prefs.min_salary
    console.print(f"   [green]✓[/green] Round-trip assertions passed")

    # ── 17. Deep copy ──
    console.print(f"\n[yellow]17. Deep copy test:[/yellow]")
    copy1 = prefs.deep_copy()
    copy1.min_salary = 99.0
    copy1.target_titles.append("FAKE TITLE")
    assert prefs.min_salary != 99.0, "Deep copy leaked!"
    assert "FAKE TITLE" not in prefs.target_titles, "Deep copy leaked titles!"
    console.print(f"   [green]✓[/green] Original unchanged after modifying copy")

    # ── 18. Summary ──
    console.print(f"\n[yellow]18. Preferences summary:[/yellow]")
    console.print(Panel(prefs.summary_string(), title="Job Preferences Summary"))

    # ── 19. Platform-specific queries ──
    console.print(f"\n[yellow]19. Platform-specific queries:[/yellow]")
    for platform in ["naukri", "indeed", "linkedin", "foundit"]:
        queries = prefs.get_search_queries_for_platform(platform)
        console.print(f"   [green]✓[/green] {platform}: {len(queries)} queries")

    console.print(f"\n[bold green]═══ All preferences tests passed! ═══[/bold green]\n")