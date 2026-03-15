"""
discovery/dedup.py — Cross-platform job deduplication

Deduplication strategies (checked in order):
  1. Exact platform + platform_job_id match (same listing re-scraped)
  2. Exact normalized URL match (same link found via different query)
  3. Fuzzy company + title match >= 85% (same job on different platforms)

Uses rapidfuzz for fuzzy string matching, falls back to difflib.SequenceMatcher.
"""

import re
import sys
import os
from typing import List, Dict, Optional, Tuple, Set
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

# ── project imports ──────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.logger import get_logger
from core.db import get_db

logger = get_logger("dedup")

# ── fuzzy matching: prefer rapidfuzz, fall back to difflib ───────
try:
    from rapidfuzz import fuzz as rf_fuzz
    FUZZY_ENGINE = "rapidfuzz"
    logger.debug("Using rapidfuzz for fuzzy matching")
except ImportError:
    import difflib
    FUZZY_ENGINE = "difflib"
    logger.info(
        "rapidfuzz not installed — using difflib (slower). "
        "Install with: pip install rapidfuzz"
    )


# ═════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════

# Common company‐name suffixes / noise to strip before comparison
_COMPANY_NOISE = re.compile(
    r"\b("
    r"pvt|private|ltd|limited|llp|inc|incorporated|corp|corporation|"
    r"co|company|solutions|services|technologies|technology|tech|"
    r"software|systems|group|global|india|labs|studio|studios|"
    r"consulting|consultancy|enterprises|ventures|infotech|"
    r"it\s*services|digital|analytics"
    r")\b",
    re.IGNORECASE,
)

# Characters to strip
_SPECIAL_CHARS = re.compile(r"[^\w\s]")
_MULTI_SPACE = re.compile(r"\s{2,}")


def _normalize_text(text: str) -> str:
    """Lowercase, strip punctuation / noise words, collapse whitespace."""
    if not text:
        return ""
    t = text.lower().strip()
    t = _SPECIAL_CHARS.sub(" ", t)
    t = _MULTI_SPACE.sub(" ", t).strip()
    return t


def _normalize_company(name: str) -> str:
    """Extra company‐specific normalization: remove legal suffixes etc."""
    if not name:
        return ""
    t = name.lower().strip()
    t = _SPECIAL_CHARS.sub(" ", t)
    t = _COMPANY_NOISE.sub(" ", t)
    t = _MULTI_SPACE.sub(" ", t).strip()
    return t


def _normalize_title(title: str) -> str:
    """Normalize job title: lowercase, strip seniority synonyms, etc."""
    if not title:
        return ""
    t = title.lower().strip()
    # Standardize common variants
    replacements = {
        "sr.": "senior",
        "sr ": "senior ",
        "jr.": "junior",
        "jr ": "junior ",
        "sde-1": "sde 1",
        "sde-2": "sde 2",
        "sde-3": "sde 3",
        "sde1": "sde 1",
        "sde2": "sde 2",
        "sde3": "sde 3",
        "sde - 1": "sde 1",
        "sde - 2": "sde 2",
        "sde - 3": "sde 3",
        "full-stack": "fullstack",
        "full stack": "fullstack",
        "back-end": "backend",
        "back end": "backend",
        "front-end": "frontend",
        "front end": "frontend",
        "dev ops": "devops",
        "dev-ops": "devops",
    }
    for old, new in replacements.items():
        t = t.replace(old, new)
    t = _SPECIAL_CHARS.sub(" ", t)
    t = _MULTI_SPACE.sub(" ", t).strip()
    return t


def _normalize_url(url: str) -> str:
    """
    Normalize a job URL for comparison.

    - Strip tracking params (utm_*, ref, from, tk, fccid, …)
    - Remove trailing slashes
    - Lowercase scheme + host
    - Keep only meaningful path + query params
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
        # lowercase scheme + host
        scheme = (parsed.scheme or "https").lower()
        host = (parsed.netloc or "").lower()
        path = parsed.path.rstrip("/")

        # Filter query params — keep only ones likely to be job identifiers
        tracking_prefixes = (
            "utm_", "ref", "from", "tk", "fccid", "advn",
            "icmpid", "mkt_tok", "trk", "rcpnt", "source",
            "clickSource", "originalSubdomain", "eBP", "midToken",
            "campaignId", "redirect", "cl_li_", "session",
        )
        qs = parse_qs(parsed.query, keep_blank_values=False)
        filtered_qs = {
            k: v for k, v in qs.items()
            if not any(k.lower().startswith(p.lower()) for p in tracking_prefixes)
        }
        clean_query = urlencode(filtered_qs, doseq=True) if filtered_qs else ""

        normalized = urlunparse((scheme, host, path, "", clean_query, ""))
        return normalized
    except Exception:
        return url.strip().lower()


def _fuzzy_ratio(a: str, b: str) -> float:
    """Return similarity ratio 0‑100 between two strings."""
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    if FUZZY_ENGINE == "rapidfuzz":
        return rf_fuzz.ratio(a, b)
    else:
        return difflib.SequenceMatcher(None, a, b).ratio() * 100.0


def _fuzzy_token_sort_ratio(a: str, b: str) -> float:
    """Token‐sort ratio (order‐independent word matching) 0‑100."""
    if not a or not b:
        return 0.0
    if FUZZY_ENGINE == "rapidfuzz":
        return rf_fuzz.token_sort_ratio(a, b)
    else:
        # manual token sort for difflib
        sa = " ".join(sorted(a.split()))
        sb = " ".join(sorted(b.split()))
        return difflib.SequenceMatcher(None, sa, sb).ratio() * 100.0


# ═════════════════════════════════════════════════════════════════
#  DEDUPLICATOR
# ═════════════════════════════════════════════════════════════════

class Deduplicator:
    """
    Cross‑platform job deduplication.

    Usage:
        dedup = Deduplicator()
        if dedup.is_duplicate(job_dict):
            print("Already seen this job")

        unique_jobs = dedup.merge_duplicates(list_of_jobs)
    """

    # ── tunables ─────────────────────────────────────────────────
    FUZZY_COMPANY_THRESHOLD: float = 85.0   # min company similarity
    FUZZY_TITLE_THRESHOLD: float = 85.0     # min title similarity
    FUZZY_COMBINED_THRESHOLD: float = 85.0  # min (company+title) combined
    LOCATION_BONUS: float = 5.0             # extra score if location matches

    def __init__(
        self,
        company_threshold: float = 85.0,
        title_threshold: float = 85.0,
    ):
        self.FUZZY_COMPANY_THRESHOLD = company_threshold
        self.FUZZY_TITLE_THRESHOLD = title_threshold
        self.db = get_db()

        # In‑memory caches (rebuilt from DB on init)
        self._platform_ids: Set[str] = set()      # "platform::platform_job_id"
        self._urls: Set[str] = set()               # normalized URLs
        self._company_titles: List[Tuple[str, str, int]] = []  # (norm_company, norm_title, job_id)

        self._load_cache()
        logger.info(
            "Deduplicator ready — %d platform IDs, %d URLs, %d company+title pairs cached "
            "(engine=%s, company_thresh=%.0f%%, title_thresh=%.0f%%)",
            len(self._platform_ids),
            len(self._urls),
            len(self._company_titles),
            FUZZY_ENGINE,
            self.FUZZY_COMPANY_THRESHOLD,
            self.FUZZY_TITLE_THRESHOLD,
        )

    # ── cache management ─────────────────────────────────────────

    def _load_cache(self) -> None:
        """Load existing jobs from DB into in‑memory cache for fast lookup."""
        try:
            jobs = self.db.get_jobs(limit=50000)  # load all
            for job in jobs:
                self._index_job(job)
        except Exception as e:
            logger.warning("Failed to pre‑load dedup cache: %s", e)

    def _index_job(self, job: Dict) -> None:
        """Add a single job to all in‑memory caches."""
        platform = (job.get("platform") or "").strip().lower()
        platform_job_id = (job.get("platform_job_id") or "").strip()
        url = job.get("url") or ""
        company = job.get("company") or ""
        title = job.get("title") or ""
        job_id = job.get("id", 0)

        if platform and platform_job_id:
            self._platform_ids.add(f"{platform}::{platform_job_id}")

        norm_url = _normalize_url(url)
        if norm_url:
            self._urls.add(norm_url)

        norm_company = _normalize_company(company)
        norm_title = _normalize_title(title)
        if norm_company and norm_title:
            self._company_titles.append((norm_company, norm_title, job_id))

    def refresh_cache(self) -> None:
        """Force reload of cache from DB."""
        self._platform_ids.clear()
        self._urls.clear()
        self._company_titles.clear()
        self._load_cache()
        logger.info("Dedup cache refreshed — %d entries", len(self._company_titles))

    # ── primary API ──────────────────────────────────────────────

    def is_duplicate(self, job: Dict) -> bool:
        """
        Check whether *job* already exists in the database.

        Checks (in order of speed):
          1. platform + platform_job_id exact match
          2. Normalized URL exact match
          3. Fuzzy company + title match (>= thresholds)

        Args:
            job: dict with at least {platform, platform_job_id, url,
                 title, company}. Extra keys ignored.

        Returns:
            True if duplicate found, False if new.
        """
        result = self.find_duplicate(job)
        return result is not None

    def find_duplicate(self, job: Dict) -> Optional[Dict]:
        """
        Like is_duplicate but returns details about the match.

        Returns:
            None if unique, or dict:
            {
                "reason": "platform_id" | "url" | "fuzzy",
                "existing_job_id": int | None,
                "similarity": float (for fuzzy, else 100.0),
                "existing_company": str,
                "existing_title": str,
            }
        """
        platform = (job.get("platform") or "").strip().lower()
        platform_job_id = (job.get("platform_job_id") or "").strip()
        url = job.get("url") or ""
        company = job.get("company") or ""
        title = job.get("title") or ""

        # ── Strategy 1: exact platform + platform_job_id ─────────
        if platform and platform_job_id:
            cache_key = f"{platform}::{platform_job_id}"
            if cache_key in self._platform_ids:
                logger.debug(
                    "Duplicate [platform_id]: %s on %s", platform_job_id, platform
                )
                return {
                    "reason": "platform_id",
                    "existing_job_id": self._lookup_job_id_by_platform(
                        platform, platform_job_id
                    ),
                    "similarity": 100.0,
                    "existing_company": company,
                    "existing_title": title,
                }

        # ── Strategy 2: normalized URL ───────────────────────────
        norm_url = _normalize_url(url)
        if norm_url and norm_url in self._urls:
            logger.debug("Duplicate [url]: %s", norm_url[:80])
            return {
                "reason": "url",
                "existing_job_id": None,  # URL match doesn't track ID cheaply
                "similarity": 100.0,
                "existing_company": company,
                "existing_title": title,
            }

        # ── Strategy 3: fuzzy company + title ────────────────────
        if company and title:
            match = self._fuzzy_match(company, title)
            if match:
                existing_company, existing_title, existing_id, score = match
                logger.debug(
                    "Duplicate [fuzzy %.1f%%]: '%s @ %s' ≈ '%s @ %s'",
                    score, title, company, existing_title, existing_company,
                )
                return {
                    "reason": "fuzzy",
                    "existing_job_id": existing_id,
                    "similarity": score,
                    "existing_company": existing_company,
                    "existing_title": existing_title,
                }

        return None

    def mark_seen(self, job: Dict) -> None:
        """
        Add a job to the in‑memory cache without requiring it to be in DB yet.
        Call after you save a new job to keep cache consistent.
        """
        self._index_job(job)

    # ── batch operations ─────────────────────────────────────────

    def merge_duplicates(self, jobs: List[Dict]) -> List[Dict]:
        """
        Deduplicate a list of jobs *within* the list AND against DB.

        For each group of duplicates, keep the version with the most
        complete data (longest description, most fields filled).

        Args:
            jobs: list of job dicts from scraping.

        Returns:
            Deduplicated list (subset of input). Order preserved (first
            occurrence kept by default, unless a later one has better data).
        """
        if not jobs:
            return []

        unique: List[Dict] = []
        seen_platform_ids: Set[str] = set()
        seen_urls: Set[str] = set()
        seen_pairs: List[Tuple[str, str]] = []  # (norm_company, norm_title)

        stats = {"total": len(jobs), "db_dup": 0, "list_dup": 0, "unique": 0}

        for job in jobs:
            # ── check against DB first ───────────────────────────
            if self.is_duplicate(job):
                stats["db_dup"] += 1
                continue

            # ── check within this batch ──────────────────────────
            platform = (job.get("platform") or "").strip().lower()
            platform_job_id = (job.get("platform_job_id") or "").strip()
            url = job.get("url") or ""
            company = job.get("company") or ""
            title = job.get("title") or ""

            dup_in_list = False

            # platform_id check
            if platform and platform_job_id:
                pk = f"{platform}::{platform_job_id}"
                if pk in seen_platform_ids:
                    dup_in_list = True
                else:
                    seen_platform_ids.add(pk)

            # url check
            if not dup_in_list:
                norm_url = _normalize_url(url)
                if norm_url:
                    if norm_url in seen_urls:
                        dup_in_list = True
                    else:
                        seen_urls.add(norm_url)

            # fuzzy company+title check within batch
            if not dup_in_list and company and title:
                nc = _normalize_company(company)
                nt = _normalize_title(title)
                for sc, st in seen_pairs:
                    comp_sim = _fuzzy_ratio(nc, sc)
                    title_sim = _fuzzy_token_sort_ratio(nt, st)
                    if (
                        comp_sim >= self.FUZZY_COMPANY_THRESHOLD
                        and title_sim >= self.FUZZY_TITLE_THRESHOLD
                    ):
                        # Duplicate within batch — check if new one is better
                        existing_idx = self._find_in_list(unique, sc, st)
                        if existing_idx is not None:
                            existing = unique[existing_idx]
                            if self._data_quality_score(job) > self._data_quality_score(existing):
                                unique[existing_idx] = job
                                logger.debug(
                                    "Replaced inferior duplicate in batch: '%s @ %s'",
                                    title, company,
                                )
                        dup_in_list = True
                        break

                if not dup_in_list:
                    seen_pairs.append((nc, nt))

            if dup_in_list:
                stats["list_dup"] += 1
                continue

            unique.append(job)
            stats["unique"] += 1

        logger.info(
            "Dedup batch: %d total → %d unique (%d DB dups, %d list dups)",
            stats["total"], stats["unique"], stats["db_dup"], stats["list_dup"],
        )
        return unique

    def filter_new(self, jobs: List[Dict]) -> List[Dict]:
        """
        Convenience: return only jobs that are NOT duplicates (against DB).
        Does NOT deduplicate within the list — use merge_duplicates for that.
        """
        return [j for j in jobs if not self.is_duplicate(j)]

    # ── internal helpers ─────────────────────────────────────────

    def _fuzzy_match(
        self, company: str, title: str
    ) -> Optional[Tuple[str, str, int, float]]:
        """
        Check (company, title) against cached company+title pairs.

        Returns:
            None if no match, or (existing_company, existing_title,
            existing_job_id, combined_score).
        """
        nc = _normalize_company(company)
        nt = _normalize_title(title)

        if not nc or not nt:
            return None

        best_score = 0.0
        best_match = None

        for cached_company, cached_title, cached_id in self._company_titles:
            # Quick length check to skip obvious non-matches
            if abs(len(nc) - len(cached_company)) > max(len(nc), len(cached_company)) * 0.5:
                continue

            company_sim = _fuzzy_ratio(nc, cached_company)
            if company_sim < self.FUZZY_COMPANY_THRESHOLD:
                continue

            # Company matched — now check title
            title_sim = _fuzzy_token_sort_ratio(nt, cached_title)
            if title_sim < self.FUZZY_TITLE_THRESHOLD:
                continue

            # Combined score (weighted: title matters more)
            combined = (company_sim * 0.4) + (title_sim * 0.6)
            if combined > best_score:
                best_score = combined
                best_match = (cached_company, cached_title, cached_id, combined)

        return best_match

    def _lookup_job_id_by_platform(
        self, platform: str, platform_job_id: str
    ) -> Optional[int]:
        """Look up job_id from DB by platform + platform_job_id."""
        try:
            existing = self.db.get_job_by_platform_id(platform, platform_job_id)
            if existing:
                return existing.get("id")
        except Exception:
            pass
        return None

    @staticmethod
    def _data_quality_score(job: Dict) -> int:
        """
        Score how 'complete' a job dict is. Higher = more data.
        Used to decide which duplicate version to keep.
        """
        score = 0
        # Has description (and it's long)
        desc = job.get("description") or ""
        score += min(len(desc), 2000)  # cap at 2000 chars worth of score

        # Has key fields filled
        for field in (
            "title", "company", "location", "url", "platform_job_id",
            "salary_min", "salary_max", "experience_min", "experience_max",
            "job_type", "work_mode", "posted_date",
        ):
            val = job.get(field)
            if val is not None and val != "" and val != 0:
                score += 50

        # Has skills list
        skills = job.get("skills")
        if skills:
            if isinstance(skills, list):
                score += len(skills) * 10
            elif isinstance(skills, str) and len(skills) > 2:
                score += 30

        return score

    @staticmethod
    def _find_in_list(
        jobs: List[Dict], norm_company: str, norm_title: str
    ) -> Optional[int]:
        """Find index of a job in list by normalized company+title."""
        for i, j in enumerate(jobs):
            jc = _normalize_company(j.get("company") or "")
            jt = _normalize_title(j.get("title") or "")
            if jc == norm_company and jt == norm_title:
                return i
        return None


# ═════════════════════════════════════════════════════════════════
#  MODULE‑LEVEL CONVENIENCE
# ═════════════════════════════════════════════════════════════════

_dedup_instance: Optional[Deduplicator] = None


def get_deduplicator() -> Deduplicator:
    """Singleton access to Deduplicator."""
    global _dedup_instance
    if _dedup_instance is None:
        _dedup_instance = Deduplicator()
    return _dedup_instance


# ═════════════════════════════════════════════════════════════════
#  TEST BLOCK
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 65)
    print("  discovery/dedup.py — Deduplication Module Test")
    print("=" * 65)
    print(f"\n  Fuzzy engine : {FUZZY_ENGINE}")

    # ── test normalizers ─────────────────────────────────────────
    print("\n── Normalizer Tests ─────────────────────────────")

    tests_company = [
        ("Razorpay Software Pvt. Ltd.", "razorpay"),
        ("Tata Consultancy Services Limited", "tata consultancy"),
        ("Infosys Technologies Ltd", "infosys"),
        ("GOOGLE LLC", "google llc"),
        ("  Flipkart  India  Pvt  Ltd  ", "flipkart india"),
    ]
    for raw, expected in tests_company:
        norm = _normalize_company(raw)
        status = "✅" if expected in norm else "⚠️"
        print(f"  {status} Company: '{raw}' → '{norm}'")

    tests_title = [
        ("Senior Full-Stack Developer", "senior fullstack developer"),
        ("SDE-1", "sde 1"),
        ("Back End Engineer", "backend engineer"),
        ("Sr. Java Developer", "senior java developer"),
        ("Full Stack Developer (Node.js)", "fullstack developer nodejs"),
    ]
    for raw, expected in tests_title:
        norm = _normalize_title(raw)
        status = "✅" if expected in norm or norm in expected else "⚠️"
        print(f"  {status} Title : '{raw}' → '{norm}'")

    tests_url = [
        (
            "https://www.naukri.com/job/sde-1-12345?utm_source=google&ref=abc",
            "https://www.naukri.com/job/sde-1-12345",
        ),
        (
            "https://indeed.com/viewjob?jk=abc123&tk=xxx&from=serp",
            "https://indeed.com/viewjob?jk=abc123",
        ),
    ]
    for raw, expected in tests_url:
        norm = _normalize_url(raw)
        # Just check that tracking params are stripped
        has_utm = "utm_" in norm
        status = "✅" if not has_utm else "⚠️"
        print(f"  {status} URL   : ...→ '{norm[:70]}...'")

    # ── test fuzzy matching ──────────────────────────────────────
    print("\n── Fuzzy Matching Tests ─────────────────────────")

    pairs = [
        ("razorpay", "razorpay software", True),
        ("infosys", "infosys limited", True),
        ("google", "amazon", False),
        ("tata consultancy services", "tcs", False),  # too different
        ("flipkart", "flipkart internet", True),
    ]
    for a, b, should_match in pairs:
        score = _fuzzy_ratio(a, b)
        matched = score >= 85.0
        status = "✅" if matched == should_match else "❌"
        print(f"  {status} '{a}' vs '{b}' → {score:.1f}% (expect {'match' if should_match else 'no match'})")

    title_pairs = [
        ("sde 1", "sde 1", True),
        ("senior fullstack developer", "fullstack developer senior", True),
        ("backend engineer", "frontend engineer", False),
        ("java developer", "java developer", True),
        ("data scientist", "java developer", False),
    ]
    for a, b, should_match in title_pairs:
        score = _fuzzy_token_sort_ratio(a, b)
        matched = score >= 85.0
        status = "✅" if matched == should_match else "❌"
        print(f"  {status} '{a}' vs '{b}' → {score:.1f}% (token_sort)")

    # ── test deduplicator ────────────────────────────────────────
    print("\n── Deduplicator Tests ──────────────────────────")

    dedup = Deduplicator()
    print(f"  Cache loaded: {len(dedup._platform_ids)} platform IDs, "
          f"{len(dedup._urls)} URLs, {len(dedup._company_titles)} pairs")

    # Create test jobs
    job1 = {
        "platform": "naukri",
        "platform_job_id": "TEST001",
        "url": "https://naukri.com/job/test-001?utm_source=test",
        "title": "Senior Full Stack Developer",
        "company": "Razorpay Software Pvt Ltd",
        "location": "Bangalore",
        "description": "We are looking for a senior full stack developer...",
    }

    job2_same_platform_id = {
        "platform": "naukri",
        "platform_job_id": "TEST001",
        "url": "https://naukri.com/job/test-001-different",
        "title": "Sr. Full-Stack Dev",
        "company": "Razorpay",
        "location": "Bangalore",
    }

    job3_same_url = {
        "platform": "indeed",
        "platform_job_id": "IND999",
        "url": "https://naukri.com/job/test-001?ref=indeed",
        "title": "Full Stack Developer",
        "company": "Razorpay Pvt. Ltd.",
        "location": "Bangalore",
    }

    job4_fuzzy_match = {
        "platform": "foundit",
        "platform_job_id": "FND555",
        "url": "https://foundit.in/job/razorpay-sde-555",
        "title": "Sr Full Stack Developer",
        "company": "Razorpay Software Private Limited",
        "location": "Bengaluru",
        "description": "Detailed JD here " * 100,
    }

    job5_different = {
        "platform": "naukri",
        "platform_job_id": "TEST002",
        "url": "https://naukri.com/job/test-002",
        "title": "Backend Engineer",
        "company": "Zomato",
        "location": "Gurugram",
    }

    job6_different_2 = {
        "platform": "indeed",
        "platform_job_id": "IND888",
        "url": "https://indeed.com/job/ind-888",
        "title": "Java Developer",
        "company": "PhonePe",
        "location": "Pune",
    }

    # Test merge_duplicates with batch
    batch = [job1, job2_same_platform_id, job3_same_url, job4_fuzzy_match,
             job5_different, job6_different_2]

    print(f"\n  Batch of {len(batch)} jobs:")
    for j in batch:
        print(f"    • {j['title']} @ {j['company']} [{j['platform']}:{j['platform_job_id']}]")

    unique = dedup.merge_duplicates(batch)
    print(f"\n  After dedup: {len(unique)} unique jobs:")
    for j in unique:
        print(f"    ✅ {j['title']} @ {j['company']} [{j['platform']}:{j['platform_job_id']}]")

    # Test mark_seen + is_duplicate
    print("\n── mark_seen + is_duplicate ─────────────────────")
    dedup.mark_seen(job5_different)
    is_dup = dedup.is_duplicate(job5_different)
    print(f"  After mark_seen, is_duplicate(job5) = {is_dup}  {'✅' if is_dup else '❌'}")

    dup_info = dedup.find_duplicate(job5_different)
    if dup_info:
        print(f"  Reason: {dup_info['reason']}, similarity: {dup_info['similarity']:.1f}%")

    # ── summary ──────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  All dedup tests completed!")
    print(f"  Engine: {FUZZY_ENGINE}")
    print(f"  Company threshold: {dedup.FUZZY_COMPANY_THRESHOLD}%")
    print(f"  Title threshold  : {dedup.FUZZY_TITLE_THRESHOLD}%")
    print("=" * 65)