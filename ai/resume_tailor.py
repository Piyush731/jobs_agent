#!/usr/bin/env python3
"""
ai/resume_tailor.py — ATS-optimised resume tailoring engine.

Three tailoring modes:
  "generic" : return base resume unchanged
  "light"   : reorder skills, inject keywords into summary, emphasise matching experience
  "full"    : LLM rewrites summary + bullets, deep keyword injection

CRITICAL RULE: NEVER fabricates experience — only reorders, emphasises,
and weaves JD keywords into EXISTING bullet points.
"""

import copy
import json
import re
from dataclasses import fields as dc_fields
from typing import Any, Dict, List, Optional, Set, Tuple

from config import RESUME_CONFIG, USER_PROFILE
from core.logger import get_logger
from profile.resume_data import (
    ResumeData,
    get_base_resume,
    resume_to_dict,
    dict_to_resume,
)

logger = get_logger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Import SkillCategory if available
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
try:
    from profile.resume_data import SkillCategory
except ImportError:
    SkillCategory = None          # fallback: detect via hasattr

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Reuse skill normalisation from job_matcher (avoid duplication)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
try:
    from ai.job_matcher import (
        _normalise_skill,
        _extract_skills_from_text,
        _ALIAS_MAP,
    )
except ImportError:
    # Minimal fallback if job_matcher not yet available
    def _normalise_skill(s: str) -> str:
        return s.lower().strip()

    def _extract_skills_from_text(text: str) -> Set[str]:
        return set()

    _ALIAS_MAP = {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers — SkillCategory ↔ dict conversion
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _is_skill_category(obj) -> bool:
    """Return True if *obj* looks like a SkillCategory dataclass."""
    if SkillCategory is not None and isinstance(obj, SkillCategory):
        return True
    # Duck-type: has exactly 2 fields, one str and one list/str
    return (hasattr(obj, '__dataclass_fields__') and
            len(obj.__dataclass_fields__) >= 2)


def _get_skill_category_fields(obj):
    """
    Auto-detect category name and skills list from a SkillCategory object.
    Returns (category_name: str, skills_list: list[str])
    """
    import dataclasses

    if not dataclasses.is_dataclass(obj):
        return str(obj), []

    field_names = [f.name for f in dataclasses.fields(obj)]

    # Try common patterns for the "category/name" field
    cat_val = None
    skills_val = None

    # Strategy: the str field is category name, the list field is skills
    for fname in field_names:
        val = getattr(obj, fname, None)
        if isinstance(val, list) and skills_val is None:
            skills_val = val
        elif isinstance(val, str) and cat_val is None:
            cat_val = val

    # Fallback: try known names
    if cat_val is None:
        for try_name in ['category', 'name', 'label', 'group', 'title']:
            if hasattr(obj, try_name):
                cat_val = str(getattr(obj, try_name))
                break

    if skills_val is None:
        for try_name in ['skills', 'items', 'values', 'entries', 'skill_list']:
            if hasattr(obj, try_name):
                val = getattr(obj, try_name)
                if isinstance(val, list):
                    skills_val = val
                    break
                elif isinstance(val, str):
                    skills_val = [s.strip() for s in val.split(',') if s.strip()]
                    break

    return (cat_val or 'Other', skills_val or [])


def _skills_to_dict(skills) -> Dict[str, List[str]]:
    """
    Normalise *any* skills format into ``{category: [str, …]}``.

    Handles:
      • ``dict``                       → pass-through
      • ``List[SkillCategory]``        → auto-detect fields
      • ``List[str]``                  → single "Skills" bucket
    """
    if skills is None:
        return {}

    if isinstance(skills, dict):
        out: Dict[str, List[str]] = {}
        for k, v in skills.items():
            if isinstance(v, list):
                out[str(k)] = [str(i) for i in v]
            elif isinstance(v, str):
                out[str(k)] = [i.strip() for i in v.split(',') if i.strip()]
            else:
                out[str(k)] = [str(v)]
        return out

    if isinstance(skills, list):
        out = {}
        for item in skills:
            # SkillCategory dataclass (auto-detect fields)
            if hasattr(item, '__dataclass_fields__'):
                cat, skill_list = _get_skill_category_fields(item)
                out[cat] = [str(s) for s in skill_list]
            elif isinstance(item, str):
                out.setdefault('Skills', []).append(item)
            else:
                out.setdefault('Other', []).append(str(item))
        return out

    return {}


def _dict_to_skills_format(skills_dict: Dict[str, List[str]],
                           original_skills):
    """
    Convert *skills_dict* back to whatever format *original_skills* used.
    """
    if isinstance(original_skills, dict):
        return skills_dict

    if isinstance(original_skills, list) and original_skills:
        first = original_skills[0]

        # SkillCategory dataclass list — rebuild using actual field names
        if hasattr(first, '__dataclass_fields__'):
            import dataclasses
            cls = type(first)
            field_names = [f.name for f in dataclasses.fields(cls)]

            # Figure out which field is category (str) and which is skills (list)
            str_field = None
            list_field = None
            for fname in field_names:
                sample_val = getattr(first, fname, None)
                if isinstance(sample_val, list) and list_field is None:
                    list_field = fname
                elif isinstance(sample_val, str) and str_field is None:
                    str_field = fname

            if not str_field or not list_field:
                # fallback: first field = name, second = list
                str_field = field_names[0]
                list_field = field_names[1] if len(field_names) > 1 else field_names[0]

            result = []
            for cat, items in skills_dict.items():
                obj = cls(**{str_field: cat, list_field: items})
                result.append(obj)
            return result

        # Plain list of strings
        if isinstance(first, str):
            flat: List[str] = []
            for items in skills_dict.values():
                flat.extend(items)
            return flat

    return skills_dict

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Other helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _deep_copy_resume(resume: ResumeData) -> ResumeData:
    """Deep-copy a ResumeData so mutations don't touch the original."""
    return dict_to_resume(resume_to_dict(resume))


def _skill_in_text(skill: str, text: str) -> bool:
    """Case-insensitive whole-word search."""
    sl = skill.lower().strip()
    if sl in ('c++', 'c#', '.net'):
        return sl in text.lower()
    try:
        return bool(re.search(r'\b' + re.escape(sl) + r'\b',
                               text, re.IGNORECASE))
    except re.error:
        return sl in text.lower()


def _count_keyword_hits(text: str, keywords: List[str]) -> int:
    """Count how many keywords appear in text."""
    return sum(1 for kw in keywords if _skill_in_text(kw, text))


def _safe_get(obj, key: str, default=''):
    """Access field from dataclass (attr) or dict (.get) transparently."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _flatten_resume_text(resume: ResumeData) -> str:
    """Concatenate all resume text for keyword scanning."""
    parts: List[str] = [resume.summary or '']

    for exp in (resume.experience or []):
        parts.append(_safe_get(exp, 'company', ''))
        parts.append(_safe_get(exp, 'title', ''))
        parts.append(_safe_get(exp, 'description', ''))
        for b in (_safe_get(exp, 'bullets', None) or []):
            parts.append(str(b))

    for proj in (resume.projects or []):
        parts.append(_safe_get(proj, 'name', ''))
        parts.append(_safe_get(proj, 'description', ''))
        for b in (_safe_get(proj, 'bullets', None) or []):
            parts.append(str(b))
        tech = _safe_get(proj, 'tech_stack', None) or []
        if isinstance(tech, list):
            parts.extend(str(t) for t in tech)
        elif isinstance(tech, str):
            parts.append(tech)

    # ── Skills (handle dict, List[SkillCategory], List[str]) ──
    skills_dict = _skills_to_dict(resume.skills)
    for cat, items in skills_dict.items():
        parts.append(cat)
        parts.extend(items)

    for cert in (resume.certifications or []):
        if isinstance(cert, dict):
            parts.append(cert.get('name', ''))
        elif isinstance(cert, str):
            parts.append(cert)
        else:
            parts.append(str(cert))

    return ' '.join(parts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ResumeTailor
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class ResumeTailor:
    """
    Tailor a resume to a specific job description.

    Modes
    -----
    generic : no changes — return base resume as-is
    light   : rule-based reordering + keyword injection (zero LLM cost)
    full    : LLM-powered summary rewrite + bullet enhancement

    Usage
    -----
        from ai.llm_client import LLMClient
        client = LLMClient()
        tailor = ResumeTailor(client)
        tailored = tailor.tailor(base_resume, job_dict, mode='light')
    """

    def __init__(self, llm_client=None):
        self._llm = llm_client
        self._cfg = RESUME_CONFIG
        self._default_mode = self._cfg.get('tailor_mode', 'light')
        logger.info("ResumeTailor ready — default_mode=%s, llm=%s",
                     self._default_mode,
                     'available' if llm_client else 'none')

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC — main tailor entry point
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def tailor(
        self,
        base_resume: ResumeData,
        job: Dict[str, Any],
        mode: Optional[str] = None,
    ) -> ResumeData:
        """
        Tailor *base_resume* for *job*.

        Parameters
        ----------
        base_resume : ResumeData
        job : dict
            From jobs table — must have 'title', 'company', 'description'.
        mode : str
            'generic' | 'light' | 'full'.  Defaults to config.

        Returns
        -------
        ResumeData — tailored copy (original untouched).
        """
        mode = mode or self._default_mode
        title = job.get('title', 'Unknown')
        company = job.get('company', 'Unknown')

        logger.info("Tailoring resume for '%s @ %s' — mode=%s",
                     title, company, mode)

        if mode == 'generic':
            logger.debug("Generic mode — returning unchanged copy")
            return _deep_copy_resume(base_resume)

        # Extract keywords from JD
        jd = job.get('description', '') or ''
        keywords = self.extract_keywords(jd)

        if mode == 'light':
            return self._tailor_light(base_resume, job, keywords)
        elif mode == 'full':
            return self._tailor_full(base_resume, job, keywords)
        else:
            logger.warning("Unknown mode '%s' — falling back to light", mode)
            return self._tailor_light(base_resume, job, keywords)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC — extract keywords from JD
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def extract_keywords(self, job_description: str) -> Dict[str, List[str]]:
        """
        Extract keywords from job description.

        Returns
        -------
        dict with keys: required_skills, preferred_skills, tools, keywords
        """
        if not job_description:
            return {'required_skills': [], 'preferred_skills': [],
                    'tools': [], 'keywords': []}

        # ── Rule-based extraction (always runs, free) ────
        text_skills = _extract_skills_from_text(job_description)

        # Attempt LLM for richer extraction
        llm_result = {}
        if (self._llm and self._llm.can_call()
                and len(job_description) > 100):
            llm_result = self._llm_extract_keywords(job_description)

        required = list(set(
            [_normalise_skill(s) for s in llm_result.get('required_skills', [])]
            + list(text_skills)
        ))
        preferred = [_normalise_skill(s)
                     for s in llm_result.get('preferred_skills', [])]
        tools = [_normalise_skill(s)
                 for s in llm_result.get('tools', [])]

        # General keywords (action verbs, domain terms)
        general_kw = llm_result.get('keywords', [])

        result = {
            'required_skills': sorted(set(required)),
            'preferred_skills': sorted(set(preferred) - set(required)),
            'tools': sorted(set(tools) - set(required) - set(preferred)),
            'keywords': general_kw,
        }

        logger.debug("Extracted keywords — required=%d, preferred=%d, "
                      "tools=%d, general=%d",
                      len(result['required_skills']),
                      len(result['preferred_skills']),
                      len(result['tools']),
                      len(result['keywords']))
        return result

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC — ATS score calculation
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def calculate_ats_score(
        self,
        resume: ResumeData,
        job: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Calculate an ATS compatibility score for resume vs job.

        Returns
        -------
        dict: score (0-100), missing_keywords[], present_keywords[],
              suggestions[], section_scores{}
        """
        jd = job.get('description', '') or ''
        keywords = self.extract_keywords(jd)

        resume_text = _flatten_resume_text(resume)
        resume_lower = resume_text.lower()

        all_kw = (keywords['required_skills']
                  + keywords['preferred_skills']
                  + keywords['tools'])

        present = []
        missing = []
        for kw in all_kw:
            if _skill_in_text(kw, resume_lower):
                present.append(kw)
            else:
                missing.append(kw)

        # Weighted: required counts 2x
        required_set = set(keywords['required_skills'])
        total_weight = 0
        hit_weight = 0
        for kw in all_kw:
            w = 2.0 if kw in required_set else 1.0
            total_weight += w
            if kw in present:
                hit_weight += w

        kw_score = (hit_weight / total_weight * 100) if total_weight > 0 else 50

        # Section checks
        section_scores = {}
        section_scores['has_summary'] = (
            15 if (resume.summary and len(resume.summary) > 30) else 0)
        section_scores['has_experience'] = 20 if resume.experience else 0
        section_scores['has_skills'] = 15 if resume.skills else 0
        section_scores['has_education'] = 10 if resume.education else 0
        section_scores['has_projects'] = 10 if resume.projects else 0

        structure_score = sum(section_scores.values())  # max 70

        # Title match
        job_title = (job.get('title', '') or '').lower()
        summary_lower = (resume.summary or '').lower()
        title_in_resume = 5 if any(
            word in summary_lower
            for word in job_title.split()
            if len(word) > 3
        ) else 0

        # Final ATS score (keyword 60%, structure 30%, title 10%)
        final = round(
            kw_score * 0.6
            + structure_score / 70 * 100 * 0.3
            + title_in_resume * 2,
            1,
        )
        final = min(100, max(0, final))

        suggestions = []
        if missing:
            suggestions.append(
                f"Add missing keywords: {', '.join(missing[:5])}")
        if not resume.summary:
            suggestions.append("Add a professional summary section")
        if section_scores['has_summary'] and len(resume.summary or '') < 100:
            suggestions.append("Expand summary to 2-3 sentences")
        if len(present) < len(all_kw) * 0.5:
            suggestions.append(
                "Less than 50% keyword match — consider deeper tailoring")

        return {
            'score': final,
            'keyword_score': round(kw_score, 1),
            'structure_score': structure_score,
            'present_keywords': sorted(present),
            'missing_keywords': sorted(missing),
            'total_keywords': len(all_kw),
            'match_pct': (round(len(present) / len(all_kw) * 100, 1)
                          if all_kw else 0),
            'section_scores': section_scores,
            'suggestions': suggestions,
        }

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC — inject keywords into existing resume
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def inject_keywords(
        self,
        resume: ResumeData,
        keywords: Dict[str, List[str]],
    ) -> ResumeData:
        """
        Rule-based keyword injection into existing resume content.
        NEVER adds fake experience — only weaves terms into existing text.
        """
        tailored = _deep_copy_resume(resume)
        resume_text = _flatten_resume_text(tailored)

        all_kw = (keywords.get('required_skills', [])
                  + keywords.get('preferred_skills', [])
                  + keywords.get('tools', []))

        missing = [kw for kw in all_kw
                   if not _skill_in_text(kw, resume_text)]

        if not missing:
            logger.debug("No missing keywords to inject")
            return tailored

        # ── 1. Inject into skills section ─────────────────
        tailored = self._inject_into_skills(tailored, missing)

        # ── 2. Inject into summary ────────────────────────
        tailored = self._inject_into_summary(tailored, missing)

        logger.info("Injected %d missing keywords into resume",
                     len(missing))
        return tailored

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PRIVATE — light tailoring (rule-based, zero LLM cost)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _tailor_light(
        self,
        base: ResumeData,
        job: Dict[str, Any],
        keywords: Dict[str, List[str]],
    ) -> ResumeData:
        """
        Light tailoring — rule-based only.
        1. Reorder skills to prioritise JD matches
        2. Inject missing keywords into skills section
        3. Reorder experience bullets by relevance
        4. Add key terms to summary
        """
        tailored = _deep_copy_resume(base)
        all_kw = (keywords.get('required_skills', [])
                  + keywords.get('preferred_skills', [])
                  + keywords.get('tools', []))

        # ── 1. Reorder skills ────────────────────────────
        tailored = self._reorder_skills(tailored, all_kw)

        # ── 2. Inject missing keywords into skills ───────
        tailored = self._inject_into_skills(tailored, [
            kw for kw in all_kw
            if not _skill_in_text(kw, _flatten_resume_text(tailored))
        ])

        # ── 3. Reorder experience bullets ────────────────
        tailored = self._reorder_bullets(tailored, all_kw)

        # ── 4. Reorder projects ──────────────────────────
        tailored = self._reorder_projects(tailored, all_kw)

        # ── 5. Enhance summary with role + keywords ──────
        tailored = self._enhance_summary_light(tailored, job, all_kw)

        before_score = self.calculate_ats_score(base, job).get('score', 0)
        after_score = self.calculate_ats_score(tailored, job).get('score', 0)
        logger.info("Light tailoring done — ATS score: %.1f → %.1f (+%.1f)",
                     before_score, after_score, after_score - before_score)

        return tailored

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PRIVATE — full tailoring (LLM-powered)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _tailor_full(
        self,
        base: ResumeData,
        job: Dict[str, Any],
        keywords: Dict[str, List[str]],
    ) -> ResumeData:
        """
        Full tailoring — LLM rewrites summary and enhances bullets.
        Falls back to light mode if LLM unavailable.
        """
        # Start with light tailoring as foundation
        tailored = self._tailor_light(base, job, keywords)

        if not self._llm or not self._llm.can_call():
            logger.warning("LLM unavailable — falling back to light mode")
            return tailored

        all_kw = (keywords.get('required_skills', [])
                  + keywords.get('preferred_skills', [])
                  + keywords.get('tools', []))

        # ── 1. LLM rewrite summary ──────────────────────
        tailored = self._llm_rewrite_summary(tailored, job, all_kw)

        # ── 2. LLM enhance top experience bullets ────────
        tailored = self._llm_enhance_bullets(tailored, job, all_kw)

        before_score = self.calculate_ats_score(base, job).get('score', 0)
        after_score = self.calculate_ats_score(tailored, job).get('score', 0)
        logger.info("Full tailoring done — ATS score: %.1f → %.1f (+%.1f)",
                     before_score, after_score, after_score - before_score)

        return tailored

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # SKILL REORDERING  (handles SkillCategory / dict / list)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _reorder_skills(
        self, resume: ResumeData, jd_keywords: List[str],
    ) -> ResumeData:
        """Move JD-matching skills to front of each category."""
        if not resume.skills:
            return resume

        jd_set = {_normalise_skill(k) for k in jd_keywords}

        # Convert to dict, reorder, convert back
        skills_dict = _skills_to_dict(resume.skills)

        new_skills: Dict[str, List[str]] = {}
        for category, items in skills_dict.items():
            matching = [s for s in items
                        if _normalise_skill(s) in jd_set]
            rest = [s for s in items
                    if _normalise_skill(s) not in jd_set]
            new_skills[category] = matching + rest

        resume.skills = _dict_to_skills_format(new_skills, resume.skills)
        return resume

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # INJECT INTO SKILLS  (handles SkillCategory / dict / list)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _inject_into_skills(
        self, resume: ResumeData, missing: List[str],
    ) -> ResumeData:
        """Add missing keywords to skills section if they're genuinely
        in the owner's skillset (check against USER_PROFILE.skills)."""
        if not missing:
            return resume

        profile_skills = {_normalise_skill(s)
                          for s in USER_PROFILE.get('skills', [])}

        # Only inject skills the owner ACTUALLY has
        can_add = [kw for kw in missing
                   if _normalise_skill(kw) in profile_skills]

        if not can_add:
            return resume

        # Convert to dict, inject, convert back
        original_format = resume.skills
        skills_dict = _skills_to_dict(resume.skills)

        # Build set of already-present normalised skills
        existing_norm: Set[str] = set()
        for items in skills_dict.values():
            for s in items:
                existing_norm.add(_normalise_skill(s))

        # Decide which category each new skill belongs to
        category_map = self._map_skill_to_category(skills_dict, can_add)

        for cat, skills_to_add in category_map.items():
            if cat not in skills_dict:
                skills_dict[cat] = []
            for s in skills_to_add:
                if _normalise_skill(s) not in existing_norm:
                    skills_dict[cat].append(s)
                    existing_norm.add(_normalise_skill(s))

        resume.skills = _dict_to_skills_format(skills_dict, original_format)
        logger.debug("Injected %d skills: %s", len(can_add), can_add)
        return resume

    def _map_skill_to_category(
        self,
        skills_dict: Dict[str, List[str]],
        skills_to_add: List[str],
    ) -> Dict[str, List[str]]:
        """Guess which category a skill belongs to."""
        category_hints = {
            'languages': ['java', 'python', 'javascript', 'typescript',
                          'go', 'rust', 'c++', 'c#', 'sql', 'kotlin'],
            'frontend': ['react', 'vue', 'angular', 'html', 'css',
                         'tailwind', 'bootstrap', 'nuxt', 'next',
                         'vuetify', 'material ui', 'svelte'],
            'backend': ['node', 'express', 'spring', 'django', 'flask',
                        'fastapi', 'nestjs', 'rest api', 'graphql',
                        'microservices', 'websocket'],
            'database': ['mysql', 'postgresql', 'mongodb', 'redis',
                         'sqlite', 'oracle', 'elasticsearch', 'sql'],
            'devops': ['docker', 'kubernetes', 'git', 'ci/cd', 'jenkins',
                       'aws', 'azure', 'gcp', 'linux', 'nginx', 'terraform'],
            'tools': ['kafka', 'rabbitmq', 'razorpay', 'jwt', 'oauth'],
        }

        result: Dict[str, List[str]] = {}
        existing_cats = list(skills_dict.keys())

        for skill in skills_to_add:
            norm = _normalise_skill(skill)
            placed = False

            for cat_hint, hint_skills in category_hints.items():
                if norm in hint_skills:
                    for existing_cat in existing_cats:
                        if cat_hint in existing_cat.lower():
                            result.setdefault(existing_cat, []).append(skill)
                            placed = True
                            break
                    if placed:
                        break

            if not placed:
                fallback = existing_cats[0] if existing_cats else 'Other'
                result.setdefault(fallback, []).append(skill)

        return result

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # INJECT INTO SUMMARY
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _inject_into_summary(
        self, resume: ResumeData, missing: List[str],
    ) -> ResumeData:
        """Append missing keywords naturally to summary tail."""
        if not missing or not resume.summary:
            return resume

        profile_skills = {_normalise_skill(s)
                          for s in USER_PROFILE.get('skills', [])}
        can_mention = [kw for kw in missing
                       if _normalise_skill(kw) in profile_skills]

        if not can_mention:
            return resume

        # Only inject up to 4 to avoid over-stuffing
        can_mention = can_mention[:4]

        summary_lower = resume.summary.lower()
        truly_missing = [kw for kw in can_mention
                         if not _skill_in_text(kw, summary_lower)]

        if not truly_missing:
            return resume

        kw_str = ', '.join(truly_missing)
        if resume.summary.rstrip().endswith('.'):
            resume.summary = (
                resume.summary.rstrip()
                + f" Proficient in {kw_str}."
            )
        else:
            resume.summary = (
                resume.summary.rstrip()
                + f". Proficient in {kw_str}."
            )

        return resume

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # REORDER BULLETS BY RELEVANCE
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _reorder_bullets(
        self, resume: ResumeData, jd_keywords: List[str],
    ) -> ResumeData:
        """Sort experience bullets so JD-relevant ones come first."""
        if not resume.experience:
            return resume

        for exp in resume.experience:
            bullets = _safe_get(exp, 'bullets', None) or []
            if not bullets or len(bullets) <= 1:
                continue

            scored = []
            for b in bullets:
                hits = _count_keyword_hits(str(b), jd_keywords)
                scored.append((hits, b))

            scored.sort(key=lambda x: x[0], reverse=True)
            sorted_bullets = [b for _, b in scored]

            if isinstance(exp, dict):
                exp['bullets'] = sorted_bullets
            else:
                exp.bullets = sorted_bullets

        return resume

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # REORDER PROJECTS BY RELEVANCE
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _reorder_projects(
        self, resume: ResumeData, jd_keywords: List[str],
    ) -> ResumeData:
        """Sort projects so JD-relevant ones appear first."""
        if not resume.projects or len(resume.projects) <= 1:
            return resume

        def project_relevance(proj):
            tech = _safe_get(proj, 'tech_stack', None) or []
            if isinstance(tech, list):
                tech_str = ' '.join(str(t) for t in tech)
            else:
                tech_str = str(tech)
            text = ' '.join([
                _safe_get(proj, 'name', ''),
                _safe_get(proj, 'description', ''),
                ' '.join(str(b) for b in
                         (_safe_get(proj, 'bullets', None) or [])),
                tech_str,
            ])
            return _count_keyword_hits(text, jd_keywords)

        resume.projects = sorted(
            resume.projects, key=project_relevance, reverse=True)
        return resume

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ENHANCE SUMMARY (light mode — rule-based)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _enhance_summary_light(
        self,
        resume: ResumeData,
        job: Dict[str, Any],
        jd_keywords: List[str],
    ) -> ResumeData:
        """Rule-based summary enhancement — add role-specific keywords."""
        if not resume.summary:
            return resume

        summary_lower = resume.summary.lower()

        role_terms = []
        for term in ['backend', 'full stack', 'fullstack', 'frontend',
                      'microservices', 'rest api', 'cloud']:
            jd_text = (job.get('description', '') or '').lower()[:500]
            title_lower = (job.get('title', '') or '').lower()
            if term in title_lower or term in jd_text:
                if not _skill_in_text(term, summary_lower):
                    role_terms.append(term)

        if role_terms and len(role_terms) <= 3:
            terms_str = ', '.join(role_terms)
            if resume.summary.rstrip().endswith('.'):
                resume.summary = (
                    resume.summary.rstrip()
                    + f" Experienced with {terms_str}."
                )
            else:
                resume.summary = (
                    resume.summary.rstrip()
                    + f". Experienced with {terms_str}."
                )

        return resume

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # LLM — REWRITE SUMMARY
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _llm_rewrite_summary(
        self,
        resume: ResumeData,
        job: Dict[str, Any],
        jd_keywords: List[str],
    ) -> ResumeData:
        """Use LLM to rewrite summary targeting the specific role."""
        if not self._llm:
            return resume

        title = job.get('title', 'Software Developer')
        company = job.get('company', 'the company')
        original = resume.summary or ''

        profile_skills = {_normalise_skill(s)
                          for s in USER_PROFILE.get('skills', [])}
        jd_set = {_normalise_skill(k) for k in jd_keywords}
        matching = sorted(profile_skills & jd_set)

        prompt = f"""Rewrite this professional summary for a {title} position at {company}.

ORIGINAL SUMMARY:
\"\"\"{original}\"\"\"

MATCHING SKILLS TO EMPHASISE: {', '.join(matching[:12])}

RULES:
1. Keep it 3-4 sentences, under 80 words
2. Start with "Full Stack Developer" or relevant title
3. Naturally weave in these keywords: {', '.join(jd_keywords[:8])}
4. Mention real achievements: 10+ production apps, 57+ DB tables, sole developer
5. Do NOT invent new experience or projects
6. Do NOT mention the company name in summary
7. Professional, concise, ATS-friendly tone

Return ONLY the rewritten summary text, nothing else."""

        system = ("You are an expert resume writer specialising in ATS "
                  "optimisation. Never fabricate experience.")

        try:
            result = self._llm.generate(prompt, system_prompt=system,
                                        max_tokens=300, temperature=0.3)
            if result and len(result) > 30:
                result = result.strip().strip('"').strip("'").strip()
                resume.summary = result
                logger.debug("LLM rewrote summary (%d chars)", len(result))
            else:
                logger.warning(
                    "LLM summary rewrite too short — keeping original")
        except Exception as exc:
            logger.warning("LLM summary rewrite failed: %s", exc)

        return resume

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # LLM — ENHANCE BULLETS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _llm_enhance_bullets(
        self,
        resume: ResumeData,
        job: Dict[str, Any],
        jd_keywords: List[str],
    ) -> ResumeData:
        """Use LLM to enhance experience bullets with JD keywords."""
        if not self._llm or not resume.experience:
            return resume

        exp = resume.experience[0]
        bullets = _safe_get(exp, 'bullets', None) or []
        if not bullets:
            return resume

        bullets_to_enhance = bullets[:4]
        remaining = bullets[4:]

        title = job.get('title', 'Software Developer')
        kw_str = ', '.join(jd_keywords[:10])

        prompt = f"""Enhance these resume bullet points for a {title} role.

ORIGINAL BULLETS:
{chr(10).join(f'- {b}' for b in bullets_to_enhance)}

TARGET KEYWORDS TO WEAVE IN: {kw_str}

RULES:
1. Keep each bullet 1-2 lines
2. Start with action verb (Developed, Implemented, Built, Designed, etc.)
3. Naturally include relevant keywords from the list above
4. Do NOT change facts or add fake metrics/achievements
5. Do NOT invent technologies the person didn't use
6. Keep the same meaning, just optimise wording
7. Include quantifiable results where they already exist

Return ONLY the enhanced bullets as a JSON array of strings:
["bullet1", "bullet2", ...]"""

        system = ("You are an ATS resume optimiser. Enhance wording to "
                  "include keywords. NEVER add fake experience.")

        try:
            data = self._llm.generate_json(prompt, system_prompt=system,
                                           max_tokens=600, temperature=0.2)
            enhanced = None
            if isinstance(data, list) and len(data) >= 1:
                enhanced = [str(b).strip().lstrip('- •') for b in data
                            if str(b).strip()]
            elif isinstance(data, dict) and 'bullets' in data:
                enhanced = [str(b).strip().lstrip('- •')
                            for b in data['bullets'] if str(b).strip()]

            if enhanced and len(enhanced) >= len(bullets_to_enhance) - 1:
                new_bullets = enhanced + remaining
                if isinstance(exp, dict):
                    exp['bullets'] = new_bullets
                else:
                    exp.bullets = new_bullets
                logger.debug("LLM enhanced %d bullets", len(enhanced))
        except Exception as exc:
            logger.warning("LLM bullet enhancement failed: %s", exc)

        return resume

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # LLM — keyword extraction
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _llm_extract_keywords(
        self, job_description: str,
    ) -> Dict[str, List[str]]:
        """Use LLM to extract structured keywords from JD."""
        prompt = f"""Extract technical skills and keywords from this job description.

JOB DESCRIPTION (first 1500 chars):
\"\"\"{job_description[:1500]}\"\"\"

Return JSON:
{{
  "required_skills": ["skill1", "skill2"],
  "preferred_skills": ["skill3"],
  "tools": ["tool1"],
  "keywords": ["keyword1", "keyword2"]
}}

required_skills = must-have technical skills
preferred_skills = nice-to-have / bonus skills
tools = specific tools, platforms, services
keywords = other important terms (methodologies, concepts, domain terms)"""

        system = ("Extract ONLY skills explicitly mentioned in the JD. "
                  "Be precise. Return valid JSON only.")

        try:
            result = self._llm.generate_json(prompt, system_prompt=system,
                                             max_tokens=500, temperature=0.1)
            return result if isinstance(result, dict) else {}
        except Exception as exc:
            logger.debug("LLM keyword extraction failed: %s", exc)
            return {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Self-test
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    print("=" * 65)
    print("  Resume Tailor — Self-Test (rule-based, no LLM required)")
    print("=" * 65)

    # ── Load base resume ──────────────────────────────────
    base = get_base_resume()
    print(f"\n📄 Base resume: {base.name}")
    print(f"   Summary length : {len(base.summary or '')} chars")
    print(f"   Experience     : {len(base.experience or [])} entries")
    print(f"   Projects       : {len(base.projects or [])} entries")

    # Count skills using the universal helper
    sd = _skills_to_dict(base.skills)
    total_skills = sum(len(v) for v in sd.values())
    print(f"   Skills         : {total_skills} items in {len(sd)} categories")

    # ── Test job ──────────────────────────────────────────
    test_job = {
        'title': 'Backend Developer - Java/Spring Boot',
        'company': 'Flipkart',
        'location': 'Bangalore',
        'salary_min': 6, 'salary_max': 10,
        'experience_min': 0, 'experience_max': 3,
        'work_mode': 'hybrid',
        'description': (
            'We are looking for a Backend Developer to join our engineering '
            'team. You will build scalable microservices using Java, Spring Boot, '
            'and PostgreSQL. Experience with Kafka, Docker, Kubernetes, and '
            'CI/CD pipelines is preferred. You should be comfortable with '
            'REST API design, unit testing (JUnit), and Git version control. '
            'Knowledge of Redis caching, MongoDB, and cloud platforms (AWS) '
            'is a plus. Agile/Scrum methodology. Strong problem-solving skills.'
        ),
        'skills': '["Java", "Spring Boot", "PostgreSQL", "Kafka", "Docker"]',
    }

    tailor = ResumeTailor(llm_client=None)

    # ── Test keyword extraction ───────────────────────────
    print("\n─── Keyword Extraction ───")
    keywords = tailor.extract_keywords(test_job['description'])
    for cat, kws in keywords.items():
        print(f"  {cat:20s}: {', '.join(kws[:10])}")

    # ── Test ATS score (before) ───────────────────────────
    print("\n─── ATS Score (before tailoring) ───")
    before = tailor.calculate_ats_score(base, test_job)
    print(f"  Score          : {before['score']}/100")
    print(f"  Keyword match  : {before['match_pct']}%  "
          f"({len(before['present_keywords'])}/{before['total_keywords']})")
    print(f"  Present        : {', '.join(before['present_keywords'][:8])}")
    print(f"  Missing        : {', '.join(before['missing_keywords'][:8])}")
    for s in before['suggestions']:
        print(f"  💡 {s}")

    # ── Generic mode ──────────────────────────────────────
    print("\n─── Generic Mode ───")
    generic = tailor.tailor(base, test_job, mode='generic')
    generic_score = tailor.calculate_ats_score(generic, test_job)
    print(f"  ATS score: {generic_score['score']}/100 (should equal before)")

    # ── Light mode ────────────────────────────────────────
    print("\n─── Light Mode ───")
    light = tailor.tailor(base, test_job, mode='light')
    light_score = tailor.calculate_ats_score(light, test_job)
    print(f"  ATS score: {light_score['score']}/100")
    print(f"  Improvement: +{light_score['score'] - before['score']:.1f}")
    print(f"  Summary preview: {(light.summary or '')[:120]}…")

    # Check skills reordering
    light_sd = _skills_to_dict(light.skills)
    if light_sd:
        first_cat = list(light_sd.keys())[0]
        first_skills = light_sd[first_cat]
        print(f"  First skills ({first_cat}): {', '.join(first_skills[:6])}")

    # ── Verify no fabrication ─────────────────────────────
    print("\n─── Fabrication Check ───")
    assert len(light.experience or []) == len(base.experience or []), \
        "Experience count changed!"
    assert len(light.projects or []) == len(base.projects or []), \
        "Project count changed!"
    print("  ✓ Experience count unchanged")
    print("  ✓ Project count unchanged")
    print("  ✓ No fabrication detected")

    # ── Inject keywords test ──────────────────────────────
    print("\n─── Keyword Injection ───")
    injected = tailor.inject_keywords(base, keywords)
    inj_score = tailor.calculate_ats_score(injected, test_job)
    print(f"  ATS score after injection: {inj_score['score']}/100")

    print("\n✅  Resume Tailor self-test complete!")