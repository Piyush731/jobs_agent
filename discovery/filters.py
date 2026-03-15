"""
discovery/filters.py
====================
Pre-filters jobs BEFORE expensive AI scoring.
Filters: salary, location, experience, blacklist/whitelist, job type,
work mode, negative title keywords.

Design principles:
  - Lenient by default: when data is missing, PASS (don't lose opportunities)
  - Hard filters: blacklist, internships, way-too-senior roles
  - Soft filters: salary (only reject if explicitly below min), location
  - Whitelist always passes (dream companies bypass everything)
  - Reduces LLM calls by 40-60% (only score what passes)
"""

import re
import sys
import os
from typing import List, Dict, Optional, Tuple, Set
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import MATCH_CONFIG, USER_PROFILE
from core.logger import get_logger
from profile.preferences import get_preferences, JobPreferences

logger = get_logger("discovery.filters")


# ═══════════════════════════════════════════════════════════
# LOCATION ALIASES — Indian cities with common variations
# ═══════════════════════════════════════════════════════════
LOCATION_ALIASES: Dict[str, List[str]] = {
    "bangalore": [
        "bengaluru", "blr", "bangalore", "bangalore urban",
        "bangalore rural", "electronic city", "whitefield",
        "marathahalli", "koramangala", "hsr layout",
    ],
    "mumbai": [
        "mumbai", "bombay", "navi mumbai", "thane", "andheri",
        "bandra", "powai", "goregaon", "malad", "bkc",
        "lower parel",
    ],
    "delhi": [
        "delhi", "new delhi", "delhi ncr", "ncr",
    ],
    "delhi ncr": [
        "delhi", "new delhi", "delhi ncr", "ncr", "noida",
        "greater noida", "gurgaon", "gurugram", "faridabad",
        "ghaziabad", "manesar",
    ],
    "noida": [
        "noida", "greater noida", "delhi ncr", "ncr",
    ],
    "gurgaon": [
        "gurgaon", "gurugram", "delhi ncr", "ncr", "manesar",
        "cyber city", "golf course road",
    ],
    "gurugram": [
        "gurgaon", "gurugram", "delhi ncr", "ncr", "manesar",
    ],
    "hyderabad": [
        "hyderabad", "hyd", "secunderabad", "hitec city",
        "hitech city", "gachibowli", "madhapur", "kondapur",
        "cyberabad",
    ],
    "pune": [
        "pune", "pimpri", "chinchwad", "pimpri-chinchwad",
        "hinjewadi", "kharadi", "magarpatta", "wakad",
        "baner", "viman nagar",
    ],
    "chennai": [
        "chennai", "madras", "sholinganallur", "omr",
        "guindy", "tambaram", "porur",
    ],
    "kolkata": [
        "kolkata", "calcutta", "salt lake", "rajarhat",
        "new town",
    ],
    "remote": [
        "remote", "work from home", "wfh", "anywhere",
        "pan india", "work from anywhere", "fully remote",
        "100% remote",
    ],
    "ahmedabad": [
        "ahmedabad", "gandhinagar", "gift city",
    ],
    "jaipur": [
        "jaipur", "sitapura",
    ],
    "chandigarh": [
        "chandigarh", "mohali", "panchkula",
    ],
    "indore": [
        "indore",
    ],
    "coimbatore": [
        "coimbatore",
    ],
    "thiruvananthapuram": [
        "thiruvananthapuram", "trivandrum", "technopark",
    ],
    "kochi": [
        "kochi", "cochin", "infopark",
    ],
}

# Normalize all keys and values to lowercase
LOCATION_ALIASES = {
    k.lower().strip(): [v.lower().strip() for v in vals]
    for k, vals in LOCATION_ALIASES.items()
}

# Build reverse alias map: alias → canonical set
_REVERSE_ALIAS: Dict[str, Set[str]] = {}
for _canonical, _aliases in LOCATION_ALIASES.items():
    for _alias in _aliases:
        if _alias not in _REVERSE_ALIAS:
            _REVERSE_ALIAS[_alias] = set()
        _REVERSE_ALIAS[_alias].add(_canonical)
        _REVERSE_ALIAS[_alias].update(_aliases)


# ═══════════════════════════════════════════════════════════
# NEGATIVE TITLE KEYWORDS — Roles that are clearly not target
# ═══════════════════════════════════════════════════════════
NEGATIVE_TITLE_KEYWORDS: List[str] = [
    # Too senior
    "director", "vp ", "vice president", "head of", "chief",
    "cto", "ceo", "cfo", "principal engineer",
    "staff engineer", "distinguished", "fellow",

    # Management (not IC dev)
    "engineering manager", "project manager", "program manager",
    "delivery manager", "scrum master", "product manager",
    "product owner", "people manager",

    # Different domain entirely
    "data scientist", "data analyst", "business analyst",
    "ml engineer", "machine learning engineer", "ai researcher",
    "deep learning", "nlp engineer", "computer vision",

    # QA / Test track
    "qa engineer", "quality assurance", "test engineer",
    "automation tester", "manual tester", "sdet",
    "test lead", "qa lead", "qa analyst",

    # DevOps / Infra (different track)
    "devops engineer", "site reliability", "sre ",
    "platform engineer", "infrastructure engineer",
    "cloud engineer", "cloud architect", "solutions architect",

    # Non-tech roles
    "sales", "marketing", "business development",
    "hr ", "human resource", "recruiter", "talent acquisition",
    "content writer", "copywriter", "seo ",
    "customer support", "customer success", "helpdesk",
    "technical support", "tech support",
    "account manager", "relationship manager",

    # Design (non-dev)
    "ui designer", "ux designer", "graphic designer",
    "visual designer", "interaction designer",

    # Hardware / Embedded
    "hardware engineer", "embedded engineer", "firmware",
    "vlsi", "asic", "fpga", "chip design",
    "network engineer", "system administrator", "sysadmin",
    "security analyst", "security engineer",

    # Data engineering (borderline — keep filtered for now)
    "data engineer", "etl developer", "bi developer",
    "tableau", "power bi developer",

    # Mobile-only (unless user adds to targets)
    "ios developer", "android developer", "flutter developer",
    "react native developer", "swift developer", "kotlin developer",
]

# Normalize
NEGATIVE_TITLE_KEYWORDS = [kw.lower().strip() for kw in NEGATIVE_TITLE_KEYWORDS]

# ═══════════════════════════════════════════════════════════
# POSITIVE TITLE KEYWORDS — If title has these, never filter
# ═══════════════════════════════════════════════════════════
POSITIVE_TITLE_KEYWORDS: List[str] = [
    "software engineer", "software developer", "sde",
    "backend developer", "backend engineer",
    "full stack developer", "full-stack developer", "fullstack developer",
    "full stack engineer", "full-stack engineer", "fullstack engineer",
    "java developer", "java engineer",
    "spring boot developer",
    "node developer", "node.js developer", "nodejs developer",
    "python developer", "python engineer",
    "web developer", "web engineer",
    "frontend developer", "front-end developer", "front end developer",
    "react developer", "vue developer", "vue.js developer",
    "mern developer", "mean developer",
    "application developer", "associate software engineer",
    "junior developer", "junior software engineer",
    "trainee software", "graduate engineer",
]

POSITIVE_TITLE_KEYWORDS = [kw.lower().strip() for kw in POSITIVE_TITLE_KEYWORDS]

# International locations (filter these out — user is India-based)
INTERNATIONAL_KEYWORDS: List[str] = [
    "usa", "united states", "uk", "united kingdom", "canada",
    "australia", "singapore", "dubai", "uae", "germany",
    "europe", "london", "new york", "san francisco", "seattle",
    "toronto", "sydney", "melbourne", "tokyo", "japan",
    "amsterdam", "berlin", "paris", "zurich", "switzerland",
    "hong kong", "ireland", "dublin",
]


class JobFilter:
    """
    Pre-filters discovered jobs before AI scoring.

    Strategy:
      - Hard reject: blacklisted companies/titles, internships, way too senior
      - Soft pass: missing salary/location/experience → always pass
      - Whitelist bypass: dream companies skip all filters
      - Lenient by default: maximize pipeline, let AI scorer do fine ranking
    """

    def __init__(self, preferences: Optional[JobPreferences] = None):
        """
        Initialize filter with preferences.

        Args:
            preferences: JobPreferences instance. If None, loads from config.
        """
        self.preferences = preferences or get_preferences()
        self._build_blacklists()
        self._build_location_set()
        self._build_title_sets()
        logger.info(
            f"JobFilter initialized | "
            f"Target locations: {len(self.target_locations)} | "
            f"Blacklist companies: {len(self.blacklist_companies)} | "
            f"Blacklist titles: {len(self.blacklist_titles)} | "
            f"Whitelist companies: {len(self.whitelist_companies)}"
        )

    # ──────────────────────────────────────────────
    # Initialization helpers
    # ──────────────────────────────────────────────

    def _build_blacklists(self) -> None:
        """Merge blacklists from preferences and MATCH_CONFIG."""
        # Blacklist companies
        config_bl_companies = [
            c.lower().strip()
            for c in MATCH_CONFIG.get("blacklist_companies", [])
        ]
        pref_bl_companies = [
            c.lower().strip()
            for c in (self.preferences.blacklist_companies or [])
        ]
        self.blacklist_companies: Set[str] = set(
            config_bl_companies + pref_bl_companies
        )

        # Blacklist titles
        config_bl_titles = [
            t.lower().strip()
            for t in MATCH_CONFIG.get("blacklist_titles", [])
        ]
        pref_bl_titles = [
            t.lower().strip()
            for t in (self.preferences.blacklist_titles or [])
        ]
        self.blacklist_titles: Set[str] = set(
            config_bl_titles + pref_bl_titles
        )

        # Whitelist companies (dream companies — always pass)
        config_wl = [
            c.lower().strip()
            for c in MATCH_CONFIG.get("whitelist_companies", [])
        ]
        pref_wl = [
            c.lower().strip()
            for c in (self.preferences.whitelist_companies or [])
        ]
        self.whitelist_companies: Set[str] = set(config_wl + pref_wl)

    def _build_location_set(self) -> None:
        """Expand target locations with all known aliases."""
        self.target_locations: Set[str] = set()

        for loc in (self.preferences.target_locations or []):
            loc_lower = loc.lower().strip()
            self.target_locations.add(loc_lower)

            # Add direct aliases
            if loc_lower in LOCATION_ALIASES:
                self.target_locations.update(LOCATION_ALIASES[loc_lower])

            # Add reverse aliases (if this location appears as an alias)
            if loc_lower in _REVERSE_ALIAS:
                self.target_locations.update(_REVERSE_ALIAS[loc_lower])

        logger.debug(
            f"Expanded target locations ({len(self.target_locations)}): "
            f"{sorted(self.target_locations)}"
        )

    def _build_title_sets(self) -> None:
        """Build target title keyword set from preferences."""
        self.target_title_keywords: Set[str] = set()
        for title in (self.preferences.target_titles or []):
            self.target_title_keywords.add(title.lower().strip())

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    def apply_filters(
        self,
        jobs: List[Dict],
        preferences: Optional[JobPreferences] = None,
    ) -> List[Dict]:
        """
        Apply all pre-filters to a list of jobs.

        Args:
            jobs: List of job dicts from platform scrapers.
                  Expected keys: title, company, location, salary_min,
                  salary_max, salary_text, experience_min, experience_max,
                  experience_text, job_type, work_mode
            preferences: Optional override preferences

        Returns:
            List of jobs that pass all filters.
            Each passing job gets 'filter_details' key added.
        """
        if preferences:
            self.preferences = preferences
            self._build_blacklists()
            self._build_location_set()
            self._build_title_sets()

        passed: List[Dict] = []
        stats = {
            "total": len(jobs),
            "passed": 0,
            "whitelisted": 0,
            "filtered_blacklist_company": 0,
            "filtered_blacklist_title": 0,
            "filtered_negative_title": 0,
            "filtered_salary": 0,
            "filtered_experience": 0,
            "filtered_location": 0,
            "filtered_work_mode": 0,
            "filtered_job_type": 0,
            "filtered_stale": 0,
            "filtered_unknown": 0,
        }

        for job in jobs:
            result = self._evaluate_job(job)

            if result["pass"]:
                job["filter_details"] = result
                passed.append(job)
                stats["passed"] += 1
                if result.get("whitelisted"):
                    stats["whitelisted"] += 1
            else:
                reason = result.get("reason", "unknown")
                stat_key = f"filtered_{reason}"
                if stat_key in stats:
                    stats[stat_key] += 1
                else:
                    stats["filtered_unknown"] += 1

                logger.debug(
                    f"FILTERED [{reason}]: "
                    f"{job.get('title', '?')} @ {job.get('company', '?')} "
                    f"— {result.get('details', {})}"
                )

        # Log summary
        filtered_count = stats["total"] - stats["passed"]
        logger.info(
            f"Filter results: {stats['passed']}/{stats['total']} passed "
            f"({filtered_count} filtered) | "
            f"Breakdown — "
            f"blacklist_co:{stats['filtered_blacklist_company']} "
            f"blacklist_title:{stats['filtered_blacklist_title']} "
            f"neg_title:{stats['filtered_negative_title']} "
            f"salary:{stats['filtered_salary']} "
            f"experience:{stats['filtered_experience']} "
            f"location:{stats['filtered_location']} "
            f"job_type:{stats['filtered_job_type']} "
            f"stale:{stats['filtered_stale']} "
            f"whitelisted:{stats['whitelisted']}"
        )

        return passed

    def is_valid(self, job: Dict) -> bool:
        """
        Check if a single job passes all filters.

        Args:
            job: Job dict with standard keys

        Returns:
            True if job passes all filters
        """
        result = self._evaluate_job(job)
        return result["pass"]

    def get_filter_result(self, job: Dict) -> Dict:
        """
        Get detailed filter evaluation for a single job.

        Args:
            job: Job dict

        Returns:
            Full evaluation dict with pass/fail, reason, details
        """
        return self._evaluate_job(job)

    def get_filter_summary(self, jobs: List[Dict]) -> Dict:
        """
        Preview what would be filtered without modifying the jobs list.

        Args:
            jobs: List of job dicts

        Returns:
            Summary dict with counts per filter reason
        """
        summary = {
            "total": len(jobs),
            "would_pass": 0,
            "would_fail": 0,
            "reasons": {},
        }

        for job in jobs:
            result = self._evaluate_job(job)
            if result["pass"]:
                summary["would_pass"] += 1
            else:
                summary["would_fail"] += 1
                reason = result.get("reason", "unknown")
                summary["reasons"][reason] = (
                    summary["reasons"].get(reason, 0) + 1
                )

        return summary

    # ──────────────────────────────────────────────
    # Core evaluation
    # ──────────────────────────────────────────────

    def _evaluate_job(self, job: Dict) -> Dict:
        """
        Run all filter checks on a single job in priority order.

        Returns:
            {
                pass: bool,
                reason: str,
                whitelisted: bool,
                details: dict
            }
        """
        company = (job.get("company") or "").lower().strip()
        title = (job.get("title") or "").lower().strip()

        # ── 1. Whitelist bypass (dream companies always pass) ──
        if self._is_whitelisted(company):
            logger.debug(f"WHITELISTED: {job.get('title')} @ {job.get('company')}")
            return {
                "pass": True,
                "reason": "whitelisted",
                "whitelisted": True,
                "details": {"company": company, "note": "dream company"},
            }

        # ── 2. Blacklist company (hard reject) ──
        if self._is_blacklisted_company(company):
            return {
                "pass": False,
                "reason": "blacklist_company",
                "whitelisted": False,
                "details": {"company": company},
            }

        # ── 3. Blacklist title keywords (hard reject) ──
        if self._is_blacklisted_title(title):
            return {
                "pass": False,
                "reason": "blacklist_title",
                "whitelisted": False,
                "details": {"title": title},
            }

        # ── 4. Negative title keywords (hard reject, unless positive match) ──
        neg_match = self._has_negative_title(title)
        if neg_match:
            return {
                "pass": False,
                "reason": "negative_title",
                "whitelisted": False,
                "details": {"title": title, "matched_keyword": neg_match},
            }

        # ── 5. Job type filter (filter internships, unpaid) ──
        type_ok, type_details = self._check_job_type(job)
        if not type_ok:
            return {
                "pass": False,
                "reason": "job_type",
                "whitelisted": False,
                "details": type_details,
            }

        # ── 6. Experience filter (skip way-too-senior roles) ──
        exp_ok, exp_details = self._check_experience(job)
        if not exp_ok:
            return {
                "pass": False,
                "reason": "experience",
                "whitelisted": False,
                "details": exp_details,
            }

        # ── 7. Salary filter (only reject if explicitly too low) ──
        sal_ok, sal_details = self._check_salary(job)
        if not sal_ok:
            return {
                "pass": False,
                "reason": "salary",
                "whitelisted": False,
                "details": sal_details,
            }

        # ── 8. Location filter (lenient) ──
        loc_ok, loc_details = self._check_location(job)
        if not loc_ok:
            return {
                "pass": False,
                "reason": "location",
                "whitelisted": False,
                "details": loc_details,
            }

        # ── 9. Work mode filter (very lenient) ──
        mode_ok, mode_details = self._check_work_mode(job)
        if not mode_ok:
            return {
                "pass": False,
                "reason": "work_mode",
                "whitelisted": False,
                "details": mode_details,
            }

        # ── 10. Staleness filter (skip very old postings) ──
        stale_ok, stale_details = self._check_staleness(job)
        if not stale_ok:
            return {
                "pass": False,
                "reason": "stale",
                "whitelisted": False,
                "details": stale_details,
            }

        # ── All passed ──
        return {
            "pass": True,
            "reason": "all_filters_passed",
            "whitelisted": False,
            "details": {
                "salary": sal_details,
                "experience": exp_details,
                "location": loc_details,
                "work_mode": mode_details,
                "job_type": type_details,
            },
        }

    # ──────────────────────────────────────────────
    # Individual filter checks
    # ──────────────────────────────────────────────

    def _is_whitelisted(self, company: str) -> bool:
        """Check if company is in whitelist (dream companies)."""
        if not company or not self.whitelist_companies:
            return False

        for wl in self.whitelist_companies:
            # Substring match both ways for flexibility
            # e.g., "razorpay" matches "Razorpay Software Private Limited"
            if wl in company or company in wl:
                return True
        return False

    def _is_blacklisted_company(self, company: str) -> bool:
        """Check if company is blacklisted."""
        if not company or not self.blacklist_companies:
            return False

        for bl in self.blacklist_companies:
            if bl in company or company in bl:
                return True
        return False

    def _is_blacklisted_title(self, title: str) -> bool:
        """Check if job title matches user-configured blacklisted terms."""
        if not title or not self.blacklist_titles:
            return False

        for bl in self.blacklist_titles:
            if bl in title:
                return True
        return False

    def _has_negative_title(self, title: str) -> Optional[str]:
        """
        Check if title contains keywords for roles we clearly don't want.

        Returns:
            The matched negative keyword, or None if no match.
        """
        if not title:
            return None

        # First check: does title match any POSITIVE keyword?
        # If yes, never filter it out regardless of negative matches
        for pos in POSITIVE_TITLE_KEYWORDS:
            if pos in title:
                return None

        # Also check target titles from preferences
        for target in self.target_title_keywords:
            if target in title:
                return None

        # Now check negative keywords
        for neg in NEGATIVE_TITLE_KEYWORDS:
            if neg in title:
                return neg

        return None

    def _check_salary(self, job: Dict) -> Tuple[bool, Dict]:
        """
        Check salary compatibility.

        Rules:
          - No salary info → PASS (most jobs don't list salary)
          - "Not disclosed" / "Confidential" → PASS
          - Job max salary explicitly below our minimum → FAIL
          - Job min salary below our min but max above → PASS
          - Everything else → PASS (be lenient, negotiate later)
        """
        salary_min = job.get("salary_min")
        salary_max = job.get("salary_max")
        salary_text = str(job.get("salary_text", "") or "")

        details = {
            "job_salary_min": salary_min,
            "job_salary_max": salary_max,
            "salary_text": salary_text,
            "our_min_salary": getattr(self.preferences, "min_salary", 0),
        }

        our_min = getattr(self.preferences, "min_salary", 0) or 0

        # If no numeric salary, try parsing from text
        if salary_min is None and salary_max is None and salary_text:
            parsed = self._parse_salary_text(salary_text)
            if parsed:
                salary_min, salary_max = parsed
                details["parsed_min"] = salary_min
                details["parsed_max"] = salary_max

        # No salary info at all → pass (don't lose opportunities)
        if salary_min is None and salary_max is None:
            details["status"] = "no_salary_info_pass"
            return True, details

        # Convert to LPA if needed (handle monthly salaries)
        salary_min = self._normalize_to_lpa(salary_min)
        salary_max = self._normalize_to_lpa(salary_max)

        # If job has a max and it's explicitly below our minimum
        if salary_max is not None and salary_max > 0 and our_min > 0:
            if salary_max < our_min:
                details["status"] = (
                    f"job_max({salary_max:.1f}LPA) < "
                    f"our_min({our_min:.1f}LPA)"
                )
                return False, details

        # If only min posted and it's reasonable → pass
        details["status"] = "salary_ok"
        return True, details

    def _parse_salary_text(self, text: str) -> Optional[Tuple[float, float]]:
        """
        Parse salary from various Indian job portal formats.

        Handles:
          - "5-10 LPA", "5 - 10 Lakhs", "₹5L - ₹10L"
          - "5,00,000 - 10,00,000", "₹5,00,000 - ₹10,00,000 PA"
          - "50000 - 80000 per month"
          - "Not Disclosed", "Confidential", "Best in Industry"
          - "Upto 8 LPA", "Up to 12 Lakhs"

        Returns:
            (min_lpa, max_lpa) or None
        """
        if not text:
            return None

        text_lower = text.lower().strip()

        # Skip uninformative salary texts
        skip_keywords = [
            "not disclosed", "confidential", "competitive",
            "best in industry", "as per industry", "negotiable",
            "as per company", "market standard",
        ]
        if any(kw in text_lower for kw in skip_keywords):
            return None

        # ── Pattern 1: X-Y LPA / Lakhs / Lac ──
        match = re.search(
            r'(\d+(?:\.\d+)?)\s*[-–to]+\s*(\d+(?:\.\d+)?)\s*'
            r'(?:lpa|lakhs?|lacs?|lakh|l)\b',
            text_lower,
        )
        if match:
            return float(match.group(1)), float(match.group(2))

        # ── Pattern 2: "Upto X LPA" / "Up to X Lakhs" ──
        match = re.search(
            r'(?:upto|up\s*to)\s*(\d+(?:\.\d+)?)\s*'
            r'(?:lpa|lakhs?|lacs?|lakh|l)\b',
            text_lower,
        )
        if match:
            val = float(match.group(1))
            return 0, val

        # ── Pattern 3: X LPA (single value) ──
        match = re.search(
            r'(\d+(?:\.\d+)?)\s*(?:lpa|lakhs?|lacs?|lakh)\b',
            text_lower,
        )
        if match:
            val = float(match.group(1))
            return val, val

        # ── Pattern 4: ₹X,XX,XXX - ₹Y,YY,YYY (Indian number format) ──
        match = re.search(
            r'₹?\s*(\d[\d,]*)\s*[-–to]+\s*₹?\s*(\d[\d,]*)',
            text_lower,
        )
        if match:
            min_val = float(match.group(1).replace(",", ""))
            max_val = float(match.group(2).replace(",", ""))

            # Detect if monthly or annual
            is_monthly = any(
                kw in text_lower
                for kw in ["per month", "monthly", "p.m.", "/month", "pm"]
            )
            if is_monthly:
                min_val = (min_val * 12) / 100000  # to LPA
                max_val = (max_val * 12) / 100000
            elif min_val > 50000:
                # Likely annual in rupees
                min_val = min_val / 100000
                max_val = max_val / 100000

            return min_val, max_val

        # ── Pattern 5: X-Y per month / monthly ──
        match = re.search(
            r'(\d[\d,]*)\s*[-–to]+\s*(\d[\d,]*)\s*'
            r'(?:per\s*month|monthly|p\.?m\.?|/\s*month)',
            text_lower,
        )
        if match:
            min_monthly = float(match.group(1).replace(",", ""))
            max_monthly = float(match.group(2).replace(",", ""))
            return (min_monthly * 12) / 100000, (max_monthly * 12) / 100000

        return None

    def _normalize_to_lpa(self, value: Optional[float]) -> Optional[float]:
        """
        Normalize salary value to LPA.
        Handles edge cases where salary might be stored as monthly or raw rupees.
        """
        if value is None:
            return None

        # If value is between 1 and 100, likely already in LPA
        if 0 < value <= 100:
            return value

        # If between 100 and 100000, could be in thousands per month
        # This is ambiguous — assume LPA if > 100 (some jobs pay 100+ LPA)
        if value > 100000:
            # Likely in rupees (annual)
            return value / 100000

        if value > 1000:
            # Could be monthly salary in rupees (e.g., 50000/month)
            # or annual in thousands
            # Assume rupees annual → LPA
            return value / 100000

        return value

    def _check_experience(self, job: Dict) -> Tuple[bool, Dict]:
        """
        Check experience compatibility.

        User has ~1 year experience.
        Rules:
          - No experience info → PASS
          - "Fresher" / 0-2 years → PASS
          - 0-5 years → PASS (can stretch; common for SDE-1)
          - Needs 5+ years AND min > user_exp + 3 → FAIL
          - Very senior (8+ min) → FAIL
        """
        exp_min = job.get("experience_min")
        exp_max = job.get("experience_max")
        exp_text = str(job.get("experience_text", "") or "")

        our_exp = float(USER_PROFILE.get("experience_years", 1))

        details = {
            "job_exp_min": exp_min,
            "job_exp_max": exp_max,
            "experience_text": exp_text,
            "our_experience": our_exp,
        }

        # Try parsing from text if numbers missing
        if exp_min is None and exp_max is None and exp_text:
            parsed = self._parse_experience_text(exp_text)
            if parsed:
                exp_min, exp_max = parsed
                details["parsed_min"] = exp_min
                details["parsed_max"] = exp_max

        # No experience info → pass
        if exp_min is None and exp_max is None:
            details["status"] = "no_exp_info_pass"
            return True, details

        # Convert to float safely
        exp_min = float(exp_min) if exp_min is not None else None
        exp_max = float(exp_max) if exp_max is not None else None

        # Fresher-friendly roles → always pass
        if exp_min is not None and exp_min <= 0:
            details["status"] = "fresher_friendly_pass"
            return True, details

        # Allow stretching: user can apply if job needs up to (our_exp + 3) years
        # e.g., user with 1 year can apply to 0-4 year roles
        stretch_limit = our_exp + 3

        if exp_min is not None and exp_min > stretch_limit:
            details["status"] = (
                f"too_senior: needs {exp_min}+ yrs, "
                f"we have {our_exp} (stretch limit: {stretch_limit})"
            )
            return False, details

        details["status"] = "experience_ok"
        return True, details

    def _parse_experience_text(
        self, text: str
    ) -> Optional[Tuple[float, float]]:
        """
        Parse experience requirements from text.

        Handles:
          - "2-5 years", "0-3 Yrs", "1 - 3 Years"
          - "Fresher", "Entry Level"
          - "3+ years", "5+ Yrs"
          - "Minimum 2 years"
        """
        if not text:
            return None

        text_lower = text.lower().strip()

        # Fresher / Entry level
        if any(kw in text_lower for kw in ["fresher", "entry level", "0 year"]):
            return 0.0, 1.0

        # Pattern: X-Y years/yrs
        match = re.search(
            r'(\d+(?:\.\d+)?)\s*[-–to]+\s*(\d+(?:\.\d+)?)\s*'
            r'(?:years?|yrs?)\b',
            text_lower,
        )
        if match:
            return float(match.group(1)), float(match.group(2))

        # Pattern: X+ years
        match = re.search(
            r'(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b',
            text_lower,
        )
        if match:
            val = float(match.group(1))
            return val, val + 5

        # Pattern: "minimum X years" / "min X years" / "at least X"
        match = re.search(
            r'(?:minimum|min|at\s*least)\s*(\d+(?:\.\d+)?)\s*'
            r'(?:years?|yrs?)?\b',
            text_lower,
        )
        if match:
            val = float(match.group(1))
            return val, val + 5

        return None

    def _check_location(self, job: Dict) -> Tuple[bool, Dict]:
        """
        Check location compatibility.

        Rules:
          - No location info → PASS
          - Remote/WFH → PASS (always)
          - Matches target location (with aliases) → PASS
          - Willing to relocate + India → PASS
          - International → FAIL (needs visa etc.)
        """
        location = (job.get("location") or "").lower().strip()
        work_mode = (job.get("work_mode") or "").lower().strip()

        details = {
            "job_location": location,
            "work_mode": work_mode,
        }

        # No location → pass
        if not location and not work_mode:
            details["status"] = "no_location_pass"
            return True, details

        # Remote → always pass
        if self._is_remote(location, work_mode):
            details["status"] = "remote_pass"
            return True, details

        # No target locations configured → pass everything
        if not self.target_locations:
            details["status"] = "no_target_locations_pass"
            return True, details

        # Check location match (with aliases)
        if self._location_matches(location):
            details["status"] = "location_match"
            return True, details

        # International → filter out
        if self._is_international(location):
            details["status"] = f"international_filtered: {location}"
            return False, details

        # Willing to relocate within India → pass
        if getattr(self.preferences, "willing_to_relocate", True):
            details["status"] = "relocation_pass"
            return True, details

        # Location doesn't match and not willing to relocate
        details["status"] = f"location_mismatch: {location}"
        return False, details

    def _is_remote(self, location: str, work_mode: str) -> bool:
        """Check if job is remote / work from home."""
        combined = f"{location} {work_mode}".lower()
        remote_keywords = [
            "remote", "work from home", "wfh", "anywhere",
            "pan india", "work from anywhere", "fully remote",
            "100% remote",
        ]
        return any(kw in combined for kw in remote_keywords)

    def _location_matches(self, job_location: str) -> bool:
        """
        Check if job location matches any target location using aliases.

        Handles multi-location strings like "Bangalore, Hyderabad, Pune"
        and "Bengaluru, Karnataka, India".
        """
        if not job_location:
            return False

        # Direct set membership
        if job_location in self.target_locations:
            return True

        # Substring match (both directions)
        for target in self.target_locations:
            if target in job_location or job_location in target:
                return True

        # Split by common delimiters and check each part
        parts = re.split(r'[,/|;]+', job_location)
        for part in parts:
            part = part.strip()
            if not part:
                continue

            if part in self.target_locations:
                return True

            # Check aliases for this part
            if part in _REVERSE_ALIAS:
                if _REVERSE_ALIAS[part] & self.target_locations:
                    return True

            # Word-level check within the part
            words = part.split()
            for word in words:
                word = word.strip().lower()
                if word in self.target_locations:
                    return True
                if word in _REVERSE_ALIAS:
                    if _REVERSE_ALIAS[word] & self.target_locations:
                        return True

        return False

    def _is_international(self, location: str) -> bool:
        """Check if location is outside India."""
        loc_lower = location.lower()
        return any(kw in loc_lower for kw in INTERNATIONAL_KEYWORDS)

    def _check_work_mode(self, job: Dict) -> Tuple[bool, Dict]:
        """
        Check work mode preference (remote/hybrid/onsite).

        Very lenient: only filter if user explicitly configured
        preferred_work_mode AND the job explicitly states a non-matching mode.
        Since user wants any job > 3.7 LPA, we pass almost everything.
        """
        work_mode = (job.get("work_mode") or "").lower().strip()

        details = {"job_work_mode": work_mode}

        # No work mode info → pass
        if not work_mode:
            details["status"] = "no_info_pass"
            return True, details

        preferred = [
            m.lower().strip()
            for m in (self.preferences.preferred_work_mode or [])
        ]

        # No preference set → pass all
        if not preferred:
            details["status"] = "no_preference_pass"
            return True, details

        # Normalize work modes for comparison
        mode_aliases = {
            "remote": [
                "remote", "work from home", "wfh",
                "fully remote", "100% remote",
            ],
            "hybrid": [
                "hybrid", "flexible", "partial remote",
                "2 days office", "3 days office",
            ],
            "onsite": [
                "onsite", "on-site", "on site", "office",
                "in-office", "in office", "work from office", "wfo",
            ],
        }

        for pref in preferred:
            # Direct match
            if pref in work_mode or work_mode in pref:
                details["status"] = "mode_match"
                return True, details

            # Alias match
            aliases = mode_aliases.get(pref, [])
            if any(alias in work_mode for alias in aliases):
                details["status"] = "mode_alias_match"
                return True, details

        # Be lenient: pass anyway (user needs jobs)
        # Only filter if explicitly configured to be strict
        # For now, always pass
        details["status"] = "mode_lenient_pass"
        return True, details

    def _check_job_type(self, job: Dict) -> Tuple[bool, Dict]:
        """
        Check job type compatibility.

        Hard filter: internships (user is past that), unpaid, volunteer.
        Pass: full-time, contract, part-time, freelance.
        """
        job_type = (job.get("job_type") or "").lower().strip()
        title = (job.get("title") or "").lower().strip()

        details = {"job_type": job_type}

        # No info → pass
        if not job_type and "intern" not in title:
            details["status"] = "no_info_pass"
            return True, details

        combined = f"{job_type} {title}"

        # Filter internships
        intern_keywords = ["intern ", "internship", "trainee intern"]
        if any(kw in combined for kw in intern_keywords):
            # But don't filter "internal" or "international"
            if "internal" not in combined and "international" not in combined:
                details["status"] = "internship_filtered"
                return False, details

        # Filter unpaid / volunteer
        unpaid_keywords = ["volunteer", "unpaid", "honorary", "stipend only"]
        if any(kw in combined for kw in unpaid_keywords):
            details["status"] = "unpaid_filtered"
            return False, details

        details["status"] = "type_ok"
        return True, details

    def _check_staleness(self, job: Dict) -> Tuple[bool, Dict]:
        """
        Filter out very old job postings (> 30 days).

        Rules:
          - No posted date → PASS (can't determine age)
          - Posted within 30 days → PASS
          - Older than 30 days → FAIL (likely filled already)
        """
        posted_date_str = job.get("posted_date")

        details = {"posted_date": posted_date_str}

        if not posted_date_str:
            details["status"] = "no_date_pass"
            return True, details

        try:
            # Try multiple date formats
            posted_date = self._parse_date(posted_date_str)
            if posted_date is None:
                details["status"] = "unparseable_date_pass"
                return True, details

            age_days = (datetime.now() - posted_date).days

            details["age_days"] = age_days

            if age_days > 30:
                details["status"] = f"stale: {age_days} days old"
                return False, details

            details["status"] = f"fresh: {age_days} days old"
            return True, details

        except Exception as e:
            logger.debug(f"Date parse error for '{posted_date_str}': {e}")
            details["status"] = "date_error_pass"
            return True, details

    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse various date formats from job portals."""
        if not date_str:
            return None

        date_str = date_str.strip()

        # Try ISO format first
        formats = [
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%d-%m-%Y",
            "%d/%m/%Y",
            "%m/%d/%Y",
            "%d %b %Y",       # "15 Jan 2025"
            "%d %B %Y",       # "15 January 2025"
            "%b %d, %Y",      # "Jan 15, 2025"
            "%B %d, %Y",      # "January 15, 2025"
            "%d-%b-%Y",       # "15-Jan-2025"
        ]

        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue

        # Handle relative dates: "X days ago", "X hours ago", "Just now", "Today"
        lower = date_str.lower().strip()

        if lower in ("just now", "today", "just posted", "few hours ago"):
            return datetime.now()

        if lower == "yesterday":
            return datetime.now() - timedelta(days=1)

        # "X days ago"
        match = re.search(r'(\d+)\s*days?\s*ago', lower)
        if match:
            return datetime.now() - timedelta(days=int(match.group(1)))

        # "X hours ago"
        match = re.search(r'(\d+)\s*hours?\s*ago', lower)
        if match:
            return datetime.now() - timedelta(hours=int(match.group(1)))

        # "X weeks ago"
        match = re.search(r'(\d+)\s*weeks?\s*ago', lower)
        if match:
            return datetime.now() - timedelta(weeks=int(match.group(1)))

        # "X months ago"
        match = re.search(r'(\d+)\s*months?\s*ago', lower)
        if match:
            return datetime.now() - timedelta(days=int(match.group(1)) * 30)

        return None

    # ──────────────────────────────────────────────
    # Dynamic management
    # ──────────────────────────────────────────────

    def add_blacklist_company(self, company: str) -> None:
        """Dynamically add a company to the blacklist."""
        normalized = company.lower().strip()
        self.blacklist_companies.add(normalized)
        logger.info(f"Added '{company}' to blacklist companies")

    def remove_blacklist_company(self, company: str) -> None:
        """Remove a company from the blacklist."""
        normalized = company.lower().strip()
        self.blacklist_companies.discard(normalized)
        logger.info(f"Removed '{company}' from blacklist companies")

    def add_whitelist_company(self, company: str) -> None:
        """Dynamically add a company to the whitelist."""
        normalized = company.lower().strip()
        self.whitelist_companies.add(normalized)
        logger.info(f"Added '{company}' to whitelist companies")

    def remove_whitelist_company(self, company: str) -> None:
        """Remove a company from the whitelist."""
        normalized = company.lower().strip()
        self.whitelist_companies.discard(normalized)
        logger.info(f"Removed '{company}' from whitelist companies")

    def add_blacklist_title(self, keyword: str) -> None:
        """Add a title keyword to the blacklist."""
        normalized = keyword.lower().strip()
        self.blacklist_titles.add(normalized)
        logger.info(f"Added '{keyword}' to blacklist title keywords")

    def add_target_location(self, location: str) -> None:
        """Add a target location (with alias expansion)."""
        loc_lower = location.lower().strip()
        self.target_locations.add(loc_lower)
        if loc_lower in LOCATION_ALIASES:
            self.target_locations.update(LOCATION_ALIASES[loc_lower])
        if loc_lower in _REVERSE_ALIAS:
            self.target_locations.update(_REVERSE_ALIAS[loc_lower])
        logger.info(f"Added target location '{location}' (expanded)")

    def get_config_summary(self) -> Dict:
        """Return current filter configuration summary."""
        return {
            "target_locations": sorted(self.target_locations),
            "blacklist_companies": sorted(self.blacklist_companies),
            "blacklist_titles": sorted(self.blacklist_titles),
            "whitelist_companies": sorted(self.whitelist_companies),
            "target_title_keywords": sorted(self.target_title_keywords),
            "min_salary": getattr(self.preferences, "min_salary", 0),
            "user_experience_years": USER_PROFILE.get("experience_years", 1),
            "willing_to_relocate": getattr(
                self.preferences, "willing_to_relocate", True
            ),
            "preferred_work_mode": getattr(
                self.preferences, "preferred_work_mode", []
            ),
            "negative_title_keywords_count": len(NEGATIVE_TITLE_KEYWORDS),
            "positive_title_keywords_count": len(POSITIVE_TITLE_KEYWORDS),
        }


# ═══════════════════════════════════════════════════════════
# Test block
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    import json

    print("=" * 70)
    print("  DISCOVERY / FILTERS — Test Suite")
    print("=" * 70)

    job_filter = JobFilter()

    # ── Print config summary ──
    config = job_filter.get_config_summary()
    print("\n📋 Filter Configuration:")
    print(f"   Target locations: {len(config['target_locations'])} expanded")
    print(f"   Blacklist companies: {config['blacklist_companies'] or '(none)'}")
    print(f"   Whitelist companies: {config['whitelist_companies'] or '(none)'}")
    print(f"   Min salary: {config['min_salary']} LPA")
    print(f"   User experience: {config['user_experience_years']} years")
    print(f"   Willing to relocate: {config['willing_to_relocate']}")

    # ── Test jobs ──
    test_jobs = [
        # ✅ Should PASS — perfect match
        {
            "title": "Software Engineer",
            "company": "Razorpay",
            "location": "Bangalore",
            "salary_min": 8, "salary_max": 15,
            "experience_min": 0, "experience_max": 3,
            "job_type": "Full-time",
            "work_mode": "Hybrid",
            "posted_date": "2 days ago",
        },
        # ✅ Should PASS — remote role
        {
            "title": "Backend Developer",
            "company": "Freshworks",
            "location": "Remote",
            "salary_min": None, "salary_max": None,
            "salary_text": "8-12 LPA",
            "experience_min": 1, "experience_max": 4,
            "job_type": "Full-time",
            "work_mode": "Remote",
            "posted_date": "Just now",
        },
        # ✅ Should PASS — no salary info (lenient)
        {
            "title": "Full Stack Developer",
            "company": "TCS",
            "location": "Hyderabad",
            "salary_min": None, "salary_max": None,
            "salary_text": "Not Disclosed",
            "experience_min": 0, "experience_max": 2,
            "job_type": "Full-time",
            "work_mode": "Onsite",
            "posted_date": "5 days ago",
        },
        # ❌ Should FAIL — salary too low
        {
            "title": "Junior Developer",
            "company": "SmallCo",
            "location": "Pune",
            "salary_min": 1.5, "salary_max": 2.5,
            "experience_min": 0, "experience_max": 1,
            "job_type": "Full-time",
            "work_mode": "Onsite",
            "posted_date": "1 day ago",
        },
        # ❌ Should FAIL — too senior
        {
            "title": "Principal Engineer",
            "company": "Google",
            "location": "Bangalore",
            "salary_min": 50, "salary_max": 80,
            "experience_min": 12, "experience_max": 20,
            "job_type": "Full-time",
            "work_mode": "Hybrid",
            "posted_date": "3 days ago",
        },
        # ❌ Should FAIL — QA role (negative title)
        {
            "title": "QA Engineer - Automation Testing",
            "company": "Infosys",
            "location": "Pune",
            "salary_min": 5, "salary_max": 8,
            "experience_min": 1, "experience_max": 3,
            "job_type": "Full-time",
            "work_mode": "Hybrid",
            "posted_date": "Today",
        },
        # ❌ Should FAIL — internship
        {
            "title": "Software Development Intern",
            "company": "Flipkart",
            "location": "Bangalore",
            "salary_min": None, "salary_max": None,
            "experience_min": 0, "experience_max": 0,
            "job_type": "Internship",
            "work_mode": "Onsite",
            "posted_date": "1 day ago",
        },
        # ✅ Should PASS — SDE-1 role
        {
            "title": "SDE-1",
            "company": "Amazon",
            "location": "Hyderabad, Telangana, India",
            "salary_min": 12, "salary_max": 20,
            "experience_min": 0, "experience_max": 3,
            "job_type": "Full-time",
            "work_mode": "Onsite",
            "posted_date": "Today",
        },
        # ❌ Should FAIL — too senior (experience)
        {
            "title": "Senior Java Developer",
            "company": "Wipro",
            "location": "Bangalore",
            "salary_min": 18, "salary_max": 25,
            "experience_min": 7, "experience_max": 12,
            "job_type": "Full-time",
            "work_mode": "Hybrid",
            "posted_date": "3 days ago",
        },
        # ❌ Should FAIL — international location
        {
            "title": "Software Engineer",
            "company": "Meta",
            "location": "London, UK",
            "salary_min": None, "salary_max": None,
            "experience_min": 2, "experience_max": 5,
            "job_type": "Full-time",
            "work_mode": "Onsite",
            "posted_date": "1 day ago",
        },
        # ✅ Should PASS — Java developer with salary text
        {
            "title": "Java Developer",
            "company": "Zoho",
            "location": "Chennai",
            "salary_min": None, "salary_max": None,
            "salary_text": "₹6,00,000 - ₹10,00,000 PA",
            "experience_min": 1, "experience_max": 3,
            "job_type": "Full-time",
            "work_mode": "Onsite",
            "posted_date": "Just posted",
        },
        # ❌ Should FAIL — stale posting (> 30 days)
        {
            "title": "Node.js Developer",
            "company": "StartupXYZ",
            "location": "Mumbai",
            "salary_min": 6, "salary_max": 10,
            "experience_min": 1, "experience_max": 3,
            "job_type": "Full-time",
            "work_mode": "Remote",
            "posted_date": "45 days ago",
        },
        # ✅ Should PASS — Spring Boot role
        {
            "title": "Spring Boot Developer",
            "company": "PayTM",
            "location": "Noida",
            "salary_min": 7, "salary_max": 12,
            "experience_min": 0, "experience_max": 2,
            "job_type": "Full-time",
            "work_mode": "Hybrid",
            "posted_date": "1 day ago",
        },
        # ❌ Should FAIL — DevOps role (negative title)
        {
            "title": "DevOps Engineer",
            "company": "Accenture",
            "location": "Pune",
            "salary_min": 6, "salary_max": 10,
            "experience_min": 1, "experience_max": 4,
            "job_type": "Full-time",
            "work_mode": "Onsite",
            "posted_date": "3 days ago",
        },
        # ✅ Should PASS — MERN stack
        {
            "title": "MERN Stack Developer",
            "company": "Zomato",
            "location": "Gurugram",
            "salary_min": 8, "salary_max": 14,
            "experience_min": 0, "experience_max": 2,
            "job_type": "Full-time",
            "work_mode": "Hybrid",
            "posted_date": "Today",
        },
        # ✅ Should PASS — no experience info (lenient)
        {
            "title": "Python Developer",
            "company": "Unknown Startup",
            "location": "Bangalore",
            "salary_min": 6, "salary_max": 9,
            "experience_min": None, "experience_max": None,
            "job_type": "Full-time",
            "work_mode": None,
            "posted_date": None,
        },
    ]

    # ── Run filters ──
    print(f"\n🔍 Running filters on {len(test_jobs)} test jobs...\n")

    passed_jobs = job_filter.apply_filters(test_jobs)

    print(f"\n{'─' * 70}")
    print(f"{'Status':<8} {'Title':<35} {'Company':<18} {'Reason'}")
    print(f"{'─' * 70}")

    for job in test_jobs:
        result = job_filter.get_filter_result(job)
        status = "✅ PASS" if result["pass"] else "❌ FAIL"
        reason = result.get("reason", "")
        title = job.get("title", "?")[:33]
        company = job.get("company", "?")[:16]
        print(f"{status}  {title:<35} {company:<18} {reason}")

    print(f"{'─' * 70}")
    print(f"\nTotal: {len(test_jobs)} | Passed: {len(passed_jobs)} | "
          f"Filtered: {len(test_jobs) - len(passed_jobs)}")

    # ── Test filter summary ──
    print(f"\n📊 Filter Summary (preview):")
    summary = job_filter.get_filter_summary(test_jobs)
    print(f"   Would pass: {summary['would_pass']}")
    print(f"   Would fail: {summary['would_fail']}")
    if summary["reasons"]:
        print(f"   Reasons:")
        for reason, count in sorted(
            summary["reasons"].items(), key=lambda x: x[1], reverse=True
        ):
            print(f"      {reason}: {count}")

    # ── Test salary parser ──
    print(f"\n💰 Salary Parser Tests:")
    salary_tests = [
        "5-10 LPA",
        "₹5,00,000 - ₹10,00,000 PA",
        "8 - 12 Lakhs",
        "50000 - 80000 per month",
        "Not Disclosed",
        "Upto 15 LPA",
        "Best in Industry",
        "3.5-7 Lacs",
        "₹8L - ₹14L",
        "Competitive",
    ]
    for salary_text in salary_tests:
        parsed = job_filter._parse_salary_text(salary_text)
        if parsed:
            print(f"   '{salary_text}' → {parsed[0]:.1f} - {parsed[1]:.1f} LPA")
        else:
            print(f"   '{salary_text}' → (not parsed / skipped)")

    # ── Test experience parser ──
    print(f"\n📅 Experience Parser Tests:")
    exp_tests = [
        "2-5 Years", "0-3 Yrs", "Fresher", "5+ years",
        "Entry Level", "Minimum 3 years", "1 - 2 Years",
    ]
    for exp_text in exp_tests:
        parsed = job_filter._parse_experience_text(exp_text)
        if parsed:
            print(f"   '{exp_text}' → {parsed[0]:.0f} - {parsed[1]:.0f} years")
        else:
            print(f"   '{exp_text}' → (not parsed)")

    # ── Test date parser ──
    print(f"\n📆 Date Parser Tests:")
    date_tests = [
        "2025-01-15", "Just now", "2 days ago", "1 week ago",
        "Today", "Yesterday", "15 Jan 2025", "3 months ago",
    ]
    for date_text in date_tests:
        parsed = job_filter._parse_date(date_text)
        if parsed:
            age = (datetime.now() - parsed).days
            print(f"   '{date_text}' → {parsed.strftime('%Y-%m-%d')} ({age}d ago)")
        else:
            print(f"   '{date_text}' → (not parsed)")

    # ── Test is_valid on single job ──
    print(f"\n🔎 Single job validation:")
    single_job = {
        "title": "Backend Developer - Java/Spring Boot",
        "company": "PhonePe",
        "location": "Bangalore, India",
        "salary_min": 10, "salary_max": 18,
        "experience_min": 1, "experience_max": 4,
        "job_type": "Full-time",
        "work_mode": "Hybrid",
        "posted_date": "Today",
    }
    valid = job_filter.is_valid(single_job)
    print(f"   {single_job['title']} @ {single_job['company']}: "
          f"{'✅ Valid' if valid else '❌ Invalid'}")

    # ── Dynamic blacklist test ──
    print(f"\n🔧 Dynamic blacklist test:")
    job_filter.add_blacklist_company("PhonePe")
    valid_after = job_filter.is_valid(single_job)
    print(f"   After blacklisting PhonePe: "
          f"{'✅ Valid' if valid_after else '❌ Filtered (correct)'}")
    job_filter.remove_blacklist_company("PhonePe")
    valid_restored = job_filter.is_valid(single_job)
    print(f"   After removing from blacklist: "
          f"{'✅ Valid (correct)' if valid_restored else '❌ Still filtered'}")

    print(f"\n{'=' * 70}")
    print("  ✅ All filter tests completed!")
    print(f"{'=' * 70}")