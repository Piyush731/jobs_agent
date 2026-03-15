#!/usr/bin/env python3
"""
ai/job_matcher.py — Hybrid rule-based + LLM job-scoring engine.

Scores every discovered job 0-100 against the owner's profile using
weighted components (title, skills, experience, location, salary,
company quality).  Rule-based scoring is FREE and instant; LLM is used
only when budget allows for richer skill extraction + reasoning text.

Weights come from config.MATCH_CONFIG.
"""

import json
import re
from typing import Any, Dict, List, Optional, Set

from config import AI_CONFIG, MATCH_CONFIG, USER_PROFILE
from core.logger import get_logger

logger = get_logger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fuzzy matching — prefer rapidfuzz, fall back to difflib
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
try:
    from rapidfuzz import fuzz as _fuzz

    def _fuzzy_ratio(a: str, b: str) -> float:
        return _fuzz.token_set_ratio(a.lower(), b.lower())
except ImportError:
    from difflib import SequenceMatcher as _SM

    def _fuzzy_ratio(a: str, b: str) -> float:          # noqa: E302
        return _SM(None, a.lower(), b.lower()).ratio() * 100


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Skill normalisation tables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# canonical_name → [alias, alias, …]
_SKILL_ALIASES: Dict[str, List[str]] = {
    # ── JavaScript / Frontend ─────────────────────────
    'javascript':    ['js', 'es6', 'es6+', 'ecmascript', 'vanilla js', 'es2015'],
    'typescript':    ['ts'],
    'react':         ['reactjs', 'react.js', 'react js', 'react 18'],
    'vue':           ['vuejs', 'vue.js', 'vue js', 'vue2', 'vue3', 'vue 3'],
    'angular':       ['angularjs', 'angular.js', 'angular js'],
    'next.js':       ['nextjs', 'next js', 'next 14'],
    'nuxt.js':       ['nuxtjs', 'nuxt js', 'nuxt', 'nuxt3'],
    'svelte':        ['sveltekit'],
    'html':          ['html5'],
    'css':           ['css3'],
    'tailwind css':  ['tailwindcss', 'tailwind'],
    'bootstrap':     [],
    'sass':          ['scss'],
    'vuetify':       [],
    'material ui':   ['mui'],
    'jquery':        [],
    # ── Node / Backend JS ─────────────────────────────
    'node.js':       ['nodejs', 'node js', 'node'],
    'express':       ['expressjs', 'express.js'],
    'nestjs':        ['nest.js', 'nest'],
    # ── Java ecosystem ────────────────────────────────
    'java':          [],
    'spring boot':   ['springboot', 'spring-boot', 'spring boot 3'],
    'spring':        ['spring framework', 'spring mvc'],
    'hibernate':     ['jpa', 'jakarta persistence'],
    'maven':         [],
    'gradle':        [],
    # ── Python ecosystem ──────────────────────────────
    'python':        ['py', 'python3', 'python 3'],
    'django':        ['django rest framework', 'drf'],
    'flask':         [],
    'fastapi':       ['fast api'],
    # ── Databases ─────────────────────────────────────
    'mysql':         ['mariadb'],
    'postgresql':    ['postgres', 'pg', 'psql'],
    'mongodb':       ['mongo', 'mongoose'],
    'redis':         [],
    'sqlite':        [],
    'sql':           ['structured query language', 'sql server', 'mssql'],
    'oracle db':     ['oracle database', 'oracle'],
    'elasticsearch': ['elastic', 'elk'],
    # ── DevOps / Cloud ────────────────────────────────
    'docker':        ['containerisation', 'containers', 'dockerfile'],
    'kubernetes':    ['k8s', 'kube'],
    'git':           ['github', 'gitlab', 'bitbucket', 'version control'],
    'ci/cd':         ['cicd', 'ci cd', 'continuous integration',
                      'continuous deployment', 'jenkins pipeline'],
    'jenkins':       [],
    'aws':           ['amazon web services', 'ec2', 's3', 'lambda'],
    'azure':         ['microsoft azure'],
    'gcp':           ['google cloud', 'google cloud platform'],
    'terraform':     [],
    'nginx':         [],
    'linux':         ['unix', 'ubuntu', 'centos', 'debian'],
    # ── Messaging / Streaming ─────────────────────────
    'kafka':         ['apache kafka'],
    'rabbitmq':      ['rabbit mq'],
    # ── APIs / Protocols ──────────────────────────────
    'rest api':      ['restful', 'rest apis', 'restful api', 'restful apis',
                      'rest', 'rest services'],
    'graphql':       [],
    'grpc':          [],
    'websocket':     ['websockets', 'socket.io', 'ws', 'web socket'],
    # ── Auth / Security ───────────────────────────────
    'jwt':           ['json web token', 'json web tokens'],
    'oauth':         ['oauth2', 'oauth 2.0'],
    # ── Concepts ──────────────────────────────────────
    'microservices': ['micro services', 'microservice', 'micro-services'],
    'data structures': ['dsa', 'algorithms'],
    'agile':         ['scrum', 'kanban'],
    'unit testing':  ['jest', 'mocha', 'pytest', 'junit', 'testing'],
    # ── Other ─────────────────────────────────────────
    'razorpay':      [],
    'stripe':        [],
    'salesforce':    ['sfdc', 'apex', 'lwc'],
    'c++':           ['cpp'],
    'c#':            ['csharp', 'c sharp'],
    '.net':          ['dotnet', 'asp.net'],
    'go':            ['golang'],
    'rust':          [],
    'kotlin':        [],
    'swift':         [],
}

# Reverse map: alias → canonical
_ALIAS_MAP: Dict[str, str] = {}
for _canon, _aliases in _SKILL_ALIASES.items():
    _ALIAS_MAP[_canon.lower()] = _canon
    for _a in _aliases:
        _ALIAS_MAP[_a.lower()] = _canon

# Flat set of every searchable term (for quick text scanning)
_ALL_SKILL_TERMS: Set[str] = set(_ALIAS_MAP.keys())

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Location aliases (Indian cities)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_LOCATION_ALIASES: Dict[str, List[str]] = {
    'bangalore':  ['bengaluru', 'blr', 'karnataka'],
    'hyderabad':  ['secunderabad', 'telangana'],
    'pune':       ['maharashtra'],
    'mumbai':     ['bombay', 'navi mumbai', 'thane'],
    'delhi ncr':  ['delhi', 'new delhi', 'noida', 'gurgaon', 'gurugram',
                   'faridabad', 'ghaziabad', 'greater noida'],
    'chennai':    ['madras', 'tamil nadu'],
    'kolkata':    ['calcutta'],
    'remote':     ['work from home', 'wfh', 'anywhere', 'pan india',
                   'india remote', 'fully remote'],
}

_LOC_ALIAS_MAP: Dict[str, str] = {}
for _loc, _aliases in _LOCATION_ALIASES.items():
    _LOC_ALIAS_MAP[_loc] = _loc
    for _a in _aliases:
        _LOC_ALIAS_MAP[_a] = _loc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Title keywords that indicate seniority mismatch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_SENIOR_KEYWORDS  = {'senior', 'sr', 'sr.', 'lead', 'principal', 'staff',
                     'architect', 'director', 'vp', 'head', 'manager',
                     'sde-3', 'sde3', 'sde-2', 'sde2', 'l3', 'l4', 'l5'}
_JUNIOR_KEYWORDS  = {'intern', 'trainee', 'fresher', 'apprentice'}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _normalise_skill(skill: str) -> str:
    """Map any skill string to its canonical form, or return lowercase."""
    return _ALIAS_MAP.get(skill.lower().strip(), skill.lower().strip())


def _normalise_location(loc: str) -> str:
    loc_l = loc.lower().strip()
    return _LOC_ALIAS_MAP.get(loc_l, loc_l)


def _to_lpa(value) -> float:
    """Convert salary to LPA (Lakhs Per Annum).  Handles None and strings."""
    if value is None:
        return 0.0
    try:
        v = float(value)
    except (ValueError, TypeError):
        return 0.0
    if v > 100_000:        # looks like absolute INR
        return v / 100_000
    if v > 1000:           # might be monthly
        return v * 12 / 100_000
    return v               # already in LPA


def _skill_in_text(skill: str, text: str) -> bool:
    """Check if *skill* appears as a distinct token in *text*."""
    sl = skill.lower().strip()
    # Special chars that break \b
    if sl in ('c++', 'c#', '.net', 'c/c++'):
        return sl in text.lower()
    try:
        return bool(re.search(r'\b' + re.escape(sl) + r'\b', text,
                              re.IGNORECASE))
    except re.error:
        return sl in text.lower()


def _extract_skills_from_text(text: str) -> Set[str]:
    """
    Fast keyword-based skill extraction — searches *text* for every
    known skill / alias and returns canonical names.  FREE, no LLM.
    """
    if not text:
        return set()
    found: Set[str] = set()
    text_lower = text.lower()
    for term, canonical in _ALIAS_MAP.items():
        if _skill_in_text(term, text_lower):
            found.add(canonical)
    return found


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# JobMatcher
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class JobMatcher:
    """
    Score a job posting 0-100 against the owner profile.

    Hybrid approach
    ---------------
    * **Rule-based** (always runs, zero cost):
      title fuzzy match, keyword skill extraction, experience range,
      location, salary, blacklist / whitelist.
    * **LLM-enhanced** (optional, when budget allows):
      deeper skill extraction from JD prose, reasoning paragraph.

    Usage
    -----
        from ai.llm_client import LLMClient
        client  = LLMClient()
        matcher = JobMatcher(client)
        result  = matcher.score_job(job_dict)
        if matcher.should_apply(result):
            ...
    """

    def __init__(self, llm_client=None):
        """
        Parameters
        ----------
        llm_client : LLMClient or None
            If None, only rule-based scoring is available.
        """
        self._llm = llm_client
        self._weights: Dict[str, float] = MATCH_CONFIG.get('weights', {
            'title': 0.25, 'skills': 0.30, 'experience': 0.15,
            'location': 0.15, 'salary': 0.10, 'company_quality': 0.05,
        })
        self._min_score   = MATCH_CONFIG.get('min_score_to_apply', 40)
        self._auto_score  = MATCH_CONFIG.get('auto_apply_score', 70)
        self._blacklist_c = {c.lower() for c in MATCH_CONFIG.get('blacklist_companies', [])}
        self._blacklist_t = {t.lower() for t in MATCH_CONFIG.get('blacklist_titles', [])}
        self._whitelist_c = {c.lower() for c in MATCH_CONFIG.get('whitelist_companies', [])}

        # Owner profile — normalised
        self._profile           = USER_PROFILE
        self._target_titles     = [t.lower() for t in
                                   USER_PROFILE.get('target_titles', [])]
        self._target_locations  = [_normalise_location(l) for l in
                                   USER_PROFILE.get('target_locations', [])]
        self._profile_skills    = {_normalise_skill(s) for s in
                                   USER_PROFILE.get('skills', [])}
        self._experience_years  = float(USER_PROFILE.get('experience_years', 1))
        self._min_salary        = float(USER_PROFILE.get('min_salary', 5))

        logger.info("JobMatcher ready — %d profile skills, %d target titles, "
                     "weights=%s",
                     len(self._profile_skills), len(self._target_titles),
                     self._weights)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC — score a single job
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def score_job(
        self,
        job: Dict[str, Any],
        profile: Optional[Dict[str, Any]] = None,
        use_llm: bool = True,
    ) -> Dict[str, Any]:
        """
        Score *job* dict against owner profile.

        Parameters
        ----------
        job : dict
            Row from ``jobs`` table (title, company, location, salary_min,
            salary_max, experience_min, experience_max, description, skills …).
        profile : dict, optional
            Override for USER_PROFILE.
        use_llm : bool
            If True **and** LLM client is available + has budget, use it for
            deeper skill extraction and reasoning.  Default True.

        Returns
        -------
        dict
            score (0-100), per-component scores, skills_found, skills_missing,
            recommendation, reasoning.
        """
        # ── Resolve profile ───────────────────────────────
        if profile:
            p_skills    = {_normalise_skill(s) for s in profile.get('skills', [])}
            p_titles    = [t.lower() for t in profile.get('target_titles', [])]
            p_locations = [_normalise_location(l)
                           for l in profile.get('target_locations', [])]
            p_exp       = float(profile.get('experience_years', 1))
            p_min_sal   = float(profile.get('min_salary', 5))
        else:
            p_skills    = self._profile_skills
            p_titles    = self._target_titles
            p_locations = self._target_locations
            p_exp       = self._experience_years
            p_min_sal   = self._min_salary

        # ── Extract JD skills ────────────────────────────
        jd_text = job.get('description', '') or ''

        # Merge explicit skills list + keyword extraction from description
        explicit_skills = self._parse_skills_field(job.get('skills'))
        keyword_skills  = _extract_skills_from_text(jd_text)
        jd_skills       = explicit_skills | keyword_skills

        # Optional LLM enhancement
        llm_skills: Set[str] = set()
        reasoning = ''
        if (use_llm and self._llm and self._llm.can_call()
                and len(jd_text) > 80):
            llm_skills, reasoning = self._llm_enhance(job, jd_text, p_skills)
            jd_skills |= llm_skills

        # ── Component scores ─────────────────────────────
        title_score     = self._score_title(job, p_titles)
        skills_result   = self._score_skills(jd_skills, p_skills)
        exp_score       = self._score_experience(job, p_exp)
        loc_score       = self._score_location(job, p_locations)
        sal_score       = self._score_salary(job, p_min_sal)
        company_score   = self._score_company(job)

        # ── Hard blocks ──────────────────────────────────
        company_lower = (job.get('company') or '').lower()
        title_lower   = (job.get('title') or '').lower()
        if company_lower in self._blacklist_c:
            logger.debug("Blacklisted company: %s", job.get('company'))
            return self._build_result(0, title_score, 0, exp_score,
                                      loc_score, sal_score, 0,
                                      skills_result, 'skip',
                                      'Company is blacklisted.')
        if any(bl in title_lower for bl in self._blacklist_t):
            logger.debug("Blacklisted title: %s", job.get('title'))
            return self._build_result(0, title_score, 0, exp_score,
                                      loc_score, sal_score, 0,
                                      skills_result, 'skip',
                                      'Title is blacklisted.')

        # ── Weighted final score ─────────────────────────
        w = self._weights
        raw = (title_score   * w.get('title', 0.25)
             + skills_result['score'] * w.get('skills', 0.30)
             + exp_score     * w.get('experience', 0.15)
             + loc_score     * w.get('location', 0.15)
             + sal_score     * w.get('salary', 0.10)
             + company_score * w.get('company_quality', 0.05))

        final_score = round(min(100, max(0, raw)), 1)
        rec = self._recommendation(final_score)

        if not reasoning:
            reasoning = self._auto_reasoning(
                job, final_score, title_score, skills_result,
                exp_score, loc_score, sal_score)

        result = self._build_result(
            final_score, title_score, skills_result['score'],
            exp_score, loc_score, sal_score, company_score,
            skills_result, rec, reasoning)

        logger.info("Scored %-50s → %5.1f  [%s]",
                     f"{job.get('title','')} @ {job.get('company','')}",
                     final_score, rec)
        return result

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC — batch + helpers
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def batch_score(
        self,
        jobs: List[Dict[str, Any]],
        profile: Optional[Dict[str, Any]] = None,
        use_llm: bool = False,
    ) -> List[Dict[str, Any]]:
        """Score many jobs (LLM off by default to save budget).
        Returns list sorted by score descending."""
        results = []
        for job in jobs:
            r = self.score_job(job, profile=profile, use_llm=use_llm)
            r['job'] = job           # attach original job for convenience
            results.append(r)
        results.sort(key=lambda x: x['score'], reverse=True)
        return results

    def should_apply(self, result: Dict[str, Any]) -> bool:
        """True if score meets minimum threshold."""
        return result.get('score', 0) >= self._min_score

    def needs_review(self, result: Dict[str, Any]) -> bool:
        """True if score is in the 'maybe' band (between min and auto)."""
        s = result.get('score', 0)
        return self._min_score <= s < self._auto_score

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # COMPONENT SCORERS  (each returns 0-100)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _score_title(self, job: dict, target_titles: List[str]) -> float:
        """Fuzzy-match job title against target titles.  0-100."""
        title = (job.get('title') or '').lower().strip()
        if not title or not target_titles:
            return 50.0  # neutral if unknown

        title_words = set(title.split())

        # Penalise clear seniority mismatch
        if title_words & _SENIOR_KEYWORDS:
            return 25.0  # too senior — could still attempt, penalty
        if title_words & _JUNIOR_KEYWORDS:
            return 30.0  # intern/trainee usually < expectations

        best = 0.0
        for target in target_titles:
            ratio = _fuzzy_ratio(title, target)
            best = max(best, ratio)

        # Boost if core keywords match ("backend", "fullstack", "sde")
        core_kw = {'developer', 'engineer', 'sde', 'backend', 'fullstack',
                    'full stack', 'full-stack', 'software'}
        if title_words & core_kw:
            best = min(100, best + 10)

        return round(min(100, best), 1)

    def _score_skills(
        self, jd_skills: Set[str], profile_skills: Set[str],
    ) -> Dict[str, Any]:
        """Compare JD skills with profile.  Returns dict with score + lists."""
        if not jd_skills:
            # Can't evaluate — neutral
            return {'score': 50.0, 'found': [], 'missing': [],
                    'jd_skills': [], 'overlap_pct': 0}

        jd_norm  = {_normalise_skill(s) for s in jd_skills}
        pro_norm = {_normalise_skill(s) for s in profile_skills}

        found   = jd_norm & pro_norm
        missing = jd_norm - pro_norm

        if len(jd_norm) == 0:
            pct = 0
        else:
            pct = len(found) / len(jd_norm) * 100

        # Score with diminishing returns after 70 %
        if pct >= 80:
            score = 90 + (pct - 80) * 0.5      # 90-100
        elif pct >= 50:
            score = 60 + (pct - 50) * 1.0       # 60-90
        elif pct >= 25:
            score = 30 + (pct - 25) * 1.2       # 30-60
        else:
            score = pct * 1.2                    # 0-30

        return {
            'score':       round(min(100, score), 1),
            'found':       sorted(found),
            'missing':     sorted(missing),
            'jd_skills':   sorted(jd_norm),
            'overlap_pct': round(pct, 1),
        }

    def _score_experience(self, job: dict, profile_exp: float) -> float:
        """Check if profile experience is within job's range.  0-100."""
        exp_min = self._safe_float(job.get('experience_min'))
        exp_max = self._safe_float(job.get('experience_max'))

        # No experience info — neutral
        if exp_min is None and exp_max is None:
            return 60.0

        exp_min = exp_min if exp_min is not None else 0
        exp_max = exp_max if exp_max is not None else 99

        if exp_min <= profile_exp <= exp_max:
            return 100.0
        elif profile_exp < exp_min:
            gap = exp_min - profile_exp
            if gap <= 0.5:
                return 85.0     # close enough
            elif gap <= 1:
                return 65.0
            elif gap <= 2:
                return 40.0
            else:
                return max(10, 40 - gap * 10)
        else:  # over-qualified
            gap = profile_exp - exp_max
            if gap <= 1:
                return 80.0
            elif gap <= 3:
                return 60.0
            else:
                return 40.0

    def _score_location(self, job: dict, target_locations: List[str]) -> float:
        """Match job location to target list.  0-100."""
        loc_raw = (job.get('location') or '').lower().strip()
        work_mode = (job.get('work_mode') or '').lower().strip()

        if not loc_raw and not work_mode:
            return 50.0  # neutral

        # Remote is always a match
        if work_mode in ('remote', 'work from home', 'wfh'):
            return 100.0
        if any(kw in loc_raw for kw in ('remote', 'wfh', 'work from home',
                                         'anywhere', 'pan india')):
            return 100.0

        # Hybrid bonus
        hybrid = work_mode == 'hybrid'

        loc_norm = _normalise_location(loc_raw)

        # Check each target location
        for target in target_locations:
            target_norm = _normalise_location(target)
            if target_norm == 'remote':
                continue  # handled above
            # Exact canonical match
            if loc_norm == target_norm:
                return 100.0 if not hybrid else 95.0
            # Check if loc_raw contains the target city name
            if target_norm in loc_raw or target in loc_raw:
                return 90.0
            # Check aliases
            for alias_city, aliases in _LOCATION_ALIASES.items():
                if target_norm == alias_city:
                    for a in aliases:
                        if a in loc_raw:
                            return 85.0

        return 10.0  # no match

    def _score_salary(self, job: dict, min_salary_lpa: float) -> float:
        """Check if job salary meets minimum expectation.  0-100."""
        sal_min = _to_lpa(job.get('salary_min'))
        sal_max = _to_lpa(job.get('salary_max'))

        # No salary info — neutral-positive (most Indian jobs hide salary)
        if sal_min == 0 and sal_max == 0:
            return 55.0

        best = max(sal_min, sal_max)

        if best >= min_salary_lpa * 1.5:
            return 100.0        # great salary
        elif best >= min_salary_lpa:
            return 80.0 + (best - min_salary_lpa) / (min_salary_lpa * 0.5) * 20
        elif best >= min_salary_lpa * 0.8:
            return 50.0         # slightly below but negotiable
        else:
            return max(0, 30 - (min_salary_lpa - best) * 5)

    def _score_company(self, job: dict) -> float:
        """Whitelist / blacklist / neutral.  0-100."""
        company = (job.get('company') or '').lower().strip()
        if not company:
            return 50.0
        if company in self._whitelist_c:
            return 100.0
        if company in self._blacklist_c:
            return 0.0
        return 50.0

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # LLM enhancement (optional)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _llm_enhance(
        self, job: dict, jd_text: str, profile_skills: Set[str],
    ) -> tuple:
        """Use LLM to extract skills + generate match reasoning.

        Returns (extra_skills: set, reasoning: str).
        """
        title   = job.get('title', 'Unknown')
        company = job.get('company', 'Unknown')

        prompt = f"""Analyse this job description and respond in JSON.

Job: {title} at {company}

Description (first 2000 chars):
\"\"\"
{jd_text[:2000]}
\"\"\"

Candidate skills: {', '.join(sorted(profile_skills))}

Return JSON:
{{
  "required_skills": ["skill1", "skill2"],
  "preferred_skills": ["skill3"],
  "reasoning": "2-3 sentence match assessment"
}}"""

        system = ("You are a technical recruiter.  Extract skills mentioned "
                  "in the JD and assess candidate fit.  Be concise.")

        try:
            data = self._llm.generate_json(prompt, system_prompt=system,
                                           max_tokens=600, temperature=0.2)
            extra: Set[str] = set()
            for key in ('required_skills', 'preferred_skills'):
                for s in data.get(key, []):
                    extra.add(_normalise_skill(s))
            reasoning = data.get('reasoning', '')
            return extra, reasoning
        except Exception as exc:
            logger.warning("LLM enhance failed: %s", exc)
            return set(), ''

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Result building / helpers
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    @staticmethod
    def _recommendation(score: float) -> str:
        if score >= 80:
            return 'strong'
        if score >= 65:
            return 'good'
        if score >= 45:
            return 'partial'
        if score >= 25:
            return 'weak'
        return 'skip'

    @staticmethod
    def _build_result(
        score, title_match, skills_score, exp_match,
        loc_match, sal_match, company_score,
        skills_detail, recommendation, reasoning,
    ) -> Dict[str, Any]:
        return {
            'score':           round(score, 1),
            'title_match':     round(title_match, 1),
            'skills_match':    round(skills_score, 1),
            'skills_found':    skills_detail.get('found', []),
            'skills_missing':  skills_detail.get('missing', []),
            'jd_skills':       skills_detail.get('jd_skills', []),
            'overlap_pct':     skills_detail.get('overlap_pct', 0),
            'experience_match': round(exp_match, 1),
            'location_match':  round(loc_match, 1),
            'salary_match':    round(sal_match, 1),
            'company_score':   round(company_score, 1),
            'recommendation':  recommendation,
            'reasoning':       reasoning,
        }

    def _auto_reasoning(
        self, job, score, title_s, skills_r, exp_s, loc_s, sal_s,
    ) -> str:
        """Build a short reasoning string without LLM."""
        parts = []
        title   = job.get('title', '?')
        company = job.get('company', '?')

        if title_s >= 80:
            parts.append(f"Title '{title}' is a strong match")
        elif title_s >= 50:
            parts.append(f"Title '{title}' partially matches targets")
        else:
            parts.append(f"Title '{title}' is a weak match")

        found_n   = len(skills_r.get('found', []))
        missing_n = len(skills_r.get('missing', []))
        parts.append(f"{found_n} skills match, {missing_n} missing")

        if loc_s >= 90:
            parts.append("location matches")
        elif loc_s < 30:
            parts.append(f"location '{job.get('location','?')}' doesn't match")

        if sal_s >= 80:
            parts.append("salary acceptable")
        elif sal_s < 40:
            parts.append("salary below expectations")

        return f"{company}: " + "; ".join(parts) + f". Overall {score:.0f}/100."

    @staticmethod
    def _parse_skills_field(raw) -> Set[str]:
        """Parse the jobs.skills column (JSON string or list)."""
        if not raw:
            return set()
        if isinstance(raw, list):
            return {_normalise_skill(s) for s in raw if s}
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return {_normalise_skill(s) for s in parsed if s}
            except (json.JSONDecodeError, TypeError):
                # Might be comma-separated
                return {_normalise_skill(s.strip())
                        for s in raw.split(',') if s.strip()}
        return set()

    @staticmethod
    def _safe_float(val) -> Optional[float]:
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Self-test
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    print("=" * 60)
    print("  Job Matcher — Self-Test (rule-based, no LLM needed)")
    print("=" * 60)

    matcher = JobMatcher(llm_client=None)

    # ── Test jobs ─────────────────────────────────────────
    jobs = [
        {
            'title': 'Full Stack Developer',
            'company': 'Razorpay',
            'location': 'Bangalore',
            'salary_min': 8, 'salary_max': 14,       # LPA
            'experience_min': 0, 'experience_max': 2,
            'work_mode': 'hybrid',
            'description': (
                'Looking for a Full Stack Developer with experience in '
                'React, Node.js, MongoDB, REST APIs, Docker, Git. '
                'Must have strong JavaScript fundamentals. Knowledge of '
                'microservices architecture preferred.'
            ),
            'skills': '["React", "Node.js", "MongoDB", "JavaScript", "Docker"]',
        },
        {
            'title': 'Senior Staff Engineer',
            'company': 'Google',
            'location': 'Hyderabad',
            'salary_min': 50, 'salary_max': 80,
            'experience_min': 10, 'experience_max': 15,
            'work_mode': 'onsite',
            'description': (
                'Design and build large-scale distributed systems. '
                'Requires Java, Kubernetes, Go, system design expertise.'
            ),
            'skills': '["Java", "Kubernetes", "Go", "System Design"]',
        },
        {
            'title': 'Backend Developer - Java/Spring Boot',
            'company': 'Flipkart',
            'location': 'Bangalore',
            'salary_min': 6, 'salary_max': 10,
            'experience_min': 0, 'experience_max': 3,
            'work_mode': 'hybrid',
            'description': (
                'Build scalable APIs using Java, Spring Boot, PostgreSQL, '
                'Kafka, Docker.  REST API design, unit testing required. '
                'MySQL experience is a plus. Git for version control.'
            ),
            'skills': '["Java", "Spring Boot", "PostgreSQL", "Kafka", "Docker"]',
        },
        {
            'title': 'SDE-1 (Node.js)',
            'company': 'Startup XYZ',
            'location': 'Remote',
            'salary_min': 5, 'salary_max': 8,
            'experience_min': 0, 'experience_max': 1,
            'work_mode': 'remote',
            'description': (
                'Node.js backend developer.  Express, MongoDB, Redis, '
                'WebSocket, JWT auth.  Must know JavaScript well.'
            ),
            'skills': '["Node.js", "MongoDB", "Redis", "Express"]',
        },
        {
            'title': 'Data Scientist',
            'company': 'Analytics Corp',
            'location': 'Mumbai',
            'salary_min': 12, 'salary_max': 20,
            'experience_min': 3, 'experience_max': 6,
            'work_mode': 'onsite',
            'description': (
                'ML/AI role.  Python, TensorFlow, pandas, scikit-learn, '
                'deep learning, NLP, statistics, R.'
            ),
            'skills': '["Python", "TensorFlow", "ML", "NLP"]',
        },
    ]

    print(f"\nProfile skills: {sorted(matcher._profile_skills)}")
    print(f"Target titles : {matcher._target_titles}")
    print(f"Target locs   : {matcher._target_locations}")
    print(f"Min salary    : {matcher._min_salary} LPA")
    print(f"Experience    : {matcher._experience_years} yrs")
    print()

    results = matcher.batch_score(jobs, use_llm=False)

    print(f"{'#':<3} {'Score':>5}  {'Rec':<8}  {'Title':<35}  {'Company':<15}  "
          f"{'Skills':>6}  {'Missing'}")
    print("─" * 110)

    for i, r in enumerate(results, 1):
        j = r['job']
        print(f"{i:<3} {r['score']:>5.1f}  {r['recommendation']:<8}  "
              f"{j['title']:<35}  {j['company']:<15}  "
              f"{len(r['skills_found']):>3}/{len(r['jd_skills']):<3}  "
              f"{', '.join(r['skills_missing'][:5])}")

    print()
    for r in results:
        j = r['job']
        apply_str = "✅ APPLY" if matcher.should_apply(r) else "❌ SKIP"
        review    = " 👁 REVIEW" if matcher.needs_review(r) else ""
        print(f"  {apply_str}{review}  {j['title']} @ {j['company']}  "
              f"({r['score']:.0f})")
        print(f"    T:{r['title_match']:.0f}  S:{r['skills_match']:.0f}  "
              f"E:{r['experience_match']:.0f}  L:{r['location_match']:.0f}  "
              f"$:{r['salary_match']:.0f}  C:{r['company_score']:.0f}")
        print(f"    {r['reasoning']}")
        print()

    print("✅  Job Matcher self-test complete!")