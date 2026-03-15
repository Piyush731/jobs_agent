#!/usr/bin/env python3
"""
ai/cover_letter.py — Personalised cover letter & email body generator.

Generates:
  - Cover letters for job applications (professional/enthusiastic/technical)
  - Email bodies for direct HR outreach
  - Follow-up email bodies

Uses LLM when available; falls back to template-based generation.
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import RESUME_CONFIG, USER_PROFILE
from core.logger import get_logger

logger = get_logger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Profile shorthand
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_NAME     = USER_PROFILE.get('name', 'Piyush Kashyap')
_EMAIL    = USER_PROFILE.get('email', 'piyushkashyap3247@gmail.com')
_PHONE    = USER_PROFILE.get('phone', '+91 73107 03247')
_LOCATION = USER_PROFILE.get('location', 'Rishikesh, Uttarakhand')
_TITLE    = USER_PROFILE.get('current_title', 'Full Stack Developer L1')
_LINKEDIN = USER_PROFILE.get('linkedin_url', 'linkedin.com/in/piyush-kashyap731')
_GITHUB   = USER_PROFILE.get('github_url', 'github.com/Piyush731')
_EXP_YRS  = USER_PROFILE.get('experience_years', 1)
_SKILLS   = USER_PROFILE.get('skills', [])

# Key achievements for template insertion
_ACHIEVEMENTS = [
    "sole developer on 10+ production applications across fintech, ERP, edtech, and CRM domains",
    "designed multi-tenant ERP system with 57+ database tables and 5 user roles",
    "built RTO service handling 1,000+ daily API requests with META WhatsApp integration",
    "developed real-time trading platform with WebSocket price feeds and MT5 integration",
    "published mobile application on Google Play Store",
    "solved 100+ problems on LeetCode/GFG covering Arrays, Trees, Graphs, and DP",
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _pick_matching_skills(job: Dict[str, Any], max_n: int = 6) -> List[str]:
    """Find skills from profile that appear in the JD."""
    jd = (job.get('description', '') or '').lower()
    title = (job.get('title', '') or '').lower()
    text = jd + ' ' + title

    matching = []
    for skill in _SKILLS:
        sl = skill.lower()
        # Handle special chars
        if sl in ('c++', 'c#', '.net'):
            if sl in text:
                matching.append(skill)
        else:
            try:
                if re.search(r'\b' + re.escape(sl) + r'\b', text):
                    matching.append(skill)
            except re.error:
                if sl in text:
                    matching.append(skill)
        if len(matching) >= max_n:
            break

    # If few matches from JD, add core skills
    if len(matching) < 3:
        core = ['JavaScript', 'Node.js', 'Vue.js', 'MySQL', 'REST APIs',
                'Java', 'Spring Boot', 'React.js', 'MongoDB']
        for s in core:
            if s not in matching:
                matching.append(s)
                if len(matching) >= max_n:
                    break

    return matching


def _pick_relevant_achievement(job: Dict[str, Any]) -> str:
    """Pick the most relevant achievement for the JD."""
    jd = (job.get('description', '') or '').lower()

    score_map = []
    keywords_per_achievement = [
        ['production', 'fintech', 'erp', 'crm', 'edtech', 'full stack',
         'end-to-end', 'sole developer'],
        ['multi-tenant', 'database', 'erp', 'tables', 'system design',
         'backend', 'architecture'],
        ['api', 'whatsapp', 'integration', 'third-party', 'rest',
         'requests', 'daily'],
        ['real-time', 'trading', 'websocket', 'socket', 'streaming',
         'live', 'mt5'],
        ['mobile', 'play store', 'android', 'app', 'publish'],
        ['leetcode', 'dsa', 'algorithm', 'problem solving', 'coding'],
    ]

    for i, kws in enumerate(keywords_per_achievement):
        score = sum(1 for kw in kws if kw in jd)
        score_map.append((score, i))

    score_map.sort(key=lambda x: x[0], reverse=True)
    best_idx = score_map[0][1]
    return _ACHIEVEMENTS[best_idx]


def _get_signature() -> str:
    """Standard email/letter signature."""
    return f"""{_NAME}
{_TITLE}
📧 {_EMAIL} | 📱 {_PHONE}
🔗 {_LINKEDIN}
💻 {_GITHUB}"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Templates (fallback when LLM unavailable)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_COVER_LETTER_TEMPLATES = {
    'professional': """Dear Hiring Manager,

I am writing to express my strong interest in the {title} position at {company}. As a {current_title} with hands-on experience building {exp_years}+ year(s) of production applications across fintech, ERP, edtech, and CRM domains, I am confident in my ability to contribute effectively to your team.

In my current role, I serve as the {achievement}. This experience has given me deep proficiency in {skills_str}, which align closely with the requirements of this role.

{relevance_paragraph}

I am eager to bring my end-to-end development experience and passion for building scalable systems to {company}. I am available to start within 15 days and welcome the opportunity to discuss how my background aligns with your team's goals.

Thank you for considering my application.

Best regards,
{name}
{email} | {phone}
{linkedin}""",

    'enthusiastic': """Dear Hiring Team at {company},

I'm excited to apply for the {title} role! Building production software is what I love — and having shipped 10+ applications as a sole developer, I'm ready to bring that energy to {company}.

What makes me a strong fit: I've been the {achievement}. Working with {skills_str}, I've delivered everything from multi-tenant ERP systems to real-time trading platforms with WebSocket feeds.

{relevance_paragraph}

I'd love to chat about how my hands-on experience can help {company} build amazing products. Available to start in 15 days!

Cheers,
{name}
{email} | {phone}
{linkedin}""",

    'technical': """Dear Hiring Manager,

I am applying for the {title} position at {company}. Below is a brief overview of my relevant technical experience:

• {achievement}
• Tech stack: {skills_str}
• Built multi-tenant ERP (57+ DB tables, 5 user types, role-based access)
• Integrated third-party APIs: META WhatsApp, Razorpay, RTO API, IOTrades
• Implemented real-time WebSocket price feeds for trading platform
• Published production application on Google Play Store

{relevance_paragraph}

I have {exp_years} year(s) of professional experience and am available within 15 days. I welcome the opportunity to discuss this role further.

Regards,
{name}
{email} | {phone}
{linkedin}""",
}

_EMAIL_TEMPLATES = {
    'application': """Dear {hr_name},

I recently came across the {title} opening at {company} and I'm very interested. As a {current_title} with experience in {skills_str}, I believe I'd be a strong addition to your team.

{highlight}

I've attached my resume for your review. I'm available within 15 days and would appreciate the opportunity to discuss this role further.

Best regards,
{signature}""",

    'follow_up_d3': """Dear {hr_name},

I wanted to follow up on my application for the {title} position at {company}, submitted on {applied_date}. I remain very enthusiastic about this opportunity and would welcome any updates on the hiring process.

Please don't hesitate to reach out if you need any additional information.

Best regards,
{signature}""",

    'follow_up_d7': """Dear {hr_name},

I hope you're doing well. I'm writing to reiterate my interest in the {title} role at {company}. Since applying on {applied_date}, I've continued to sharpen my skills in the relevant technologies.

I'd be grateful for the chance to discuss how my experience with 10+ production applications could benefit your team.

Best regards,
{signature}""",

    'follow_up_d14': """Dear {hr_name},

I'm following up once more regarding the {title} position at {company}. I understand the hiring process takes time, and I remain keen on this opportunity.

If the position has been filled or my profile isn't the right fit this time, I completely understand. I'd appreciate any feedback or consideration for future openings.

Thank you for your time.

Best regards,
{signature}""",

    'cold_outreach': """Dear {hr_name},

I'm {name}, a {current_title} looking for my next opportunity. I admire {company}'s work and wanted to reach out directly.

I've built 10+ production applications as a sole developer — from multi-tenant ERP systems to real-time trading platforms. My stack includes {skills_str}.

If {company} has any open {title} or similar roles, I'd love to be considered. My resume is attached for your review.

Best regards,
{signature}""",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CoverLetterGenerator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class CoverLetterGenerator:
    """
    Generate personalised cover letters and email bodies.

    Uses LLM when available for higher quality; falls back to
    template-based generation that still personalises company,
    role, skills, and achievements.

    Usage
    -----
        from ai.llm_client import LLMClient
        gen = CoverLetterGenerator(LLMClient())
        letter = gen.generate(job_dict, profile_dict, tone='professional')
        email  = gen.generate_email_body(job_dict, profile_dict, 'application')
    """

    def __init__(self, llm_client=None):
        self._llm = llm_client
        logger.info("CoverLetterGenerator ready — llm=%s",
                     'available' if llm_client else 'template-only')

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC — generate cover letter
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def generate(
        self,
        job: Dict[str, Any],
        profile: Optional[Dict[str, Any]] = None,
        tone: str = 'professional',
    ) -> str:
        """
        Generate a personalised cover letter.

        Parameters
        ----------
        job : dict
            From jobs table (title, company, description, location …).
        profile : dict, optional
            Override USER_PROFILE. If None, uses global profile.
        tone : str
            'professional' | 'enthusiastic' | 'technical'

        Returns
        -------
        str — complete cover letter text.
        """
        title = job.get('title', 'Software Developer')
        company = job.get('company', 'your company')
        jd = job.get('description', '') or ''

        p = profile or USER_PROFILE
        name = p.get('name', _NAME)
        email = p.get('email', _EMAIL)
        phone = p.get('phone', _PHONE)
        current_title = p.get('current_title', _TITLE)
        exp_years = p.get('experience_years', _EXP_YRS)
        linkedin = p.get('linkedin_url', _LINKEDIN)

        matching_skills = _pick_matching_skills(job)
        skills_str = ', '.join(matching_skills)
        achievement = _pick_relevant_achievement(job)

        # ── Try LLM first ────────────────────────────────
        if self._llm and self._llm.can_call() and len(jd) > 80:
            llm_result = self._llm_generate_cover_letter(
                job, name, current_title, skills_str,
                achievement, exp_years, tone)
            if llm_result and len(llm_result) > 100:
                logger.info("LLM generated cover letter for %s @ %s "
                             "(%d chars, tone=%s)",
                             title, company, len(llm_result), tone)
                return llm_result

        # ── Template fallback ────────────────────────────
        logger.debug("Using template for cover letter (tone=%s)", tone)
        return self._template_cover_letter(
            job, name, email, phone, current_title,
            exp_years, linkedin, skills_str, achievement, tone)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC — generate email body
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def generate_email_body(
        self,
        job: Dict[str, Any],
        profile: Optional[Dict[str, Any]] = None,
        email_type: str = 'application',
        **kwargs,
    ) -> str:
        """
        Generate an email body.

        Parameters
        ----------
        job : dict
        profile : dict, optional
        email_type : str
            'application' | 'follow_up_d3' | 'follow_up_d7' |
            'follow_up_d14' | 'cold_outreach'
        **kwargs
            hr_name, applied_date, etc.

        Returns
        -------
        str — email body text.
        """
        title = job.get('title', 'Software Developer')
        company = job.get('company', 'your company')
        jd = job.get('description', '') or ''

        p = profile or USER_PROFILE
        name = p.get('name', _NAME)
        current_title = p.get('current_title', _TITLE)

        hr_name = kwargs.get('hr_name', 'Hiring Manager')
        applied_date = kwargs.get('applied_date', '')

        matching_skills = _pick_matching_skills(job, max_n=4)
        skills_str = ', '.join(matching_skills)
        achievement = _pick_relevant_achievement(job)
        signature = _get_signature()

        # ── Try LLM for application / cold_outreach ──────
        if (email_type in ('application', 'cold_outreach')
                and self._llm and self._llm.can_call()
                and len(jd) > 80):
            llm_result = self._llm_generate_email(
                job, name, current_title, skills_str,
                achievement, email_type, hr_name)
            if llm_result and len(llm_result) > 50:
                # Append signature if not present
                if name not in llm_result:
                    llm_result = llm_result.rstrip() + '\n\n' + signature
                logger.info("LLM generated %s email for %s @ %s",
                             email_type, title, company)
                return llm_result

        # ── Template fallback ────────────────────────────
        template = _EMAIL_TEMPLATES.get(email_type)
        if not template:
            logger.warning("Unknown email_type '%s' — using application",
                           email_type)
            template = _EMAIL_TEMPLATES['application']

        # Build highlight for application emails
        highlight = f"I've been the {achievement}, working with {skills_str}."

        body = template.format(
            title=title,
            company=company,
            name=name,
            current_title=current_title,
            skills_str=skills_str,
            highlight=highlight,
            hr_name=hr_name,
            applied_date=applied_date or datetime.now().strftime('%B %d, %Y'),
            signature=signature,
        )

        logger.debug("Template email generated (%s) for %s @ %s",
                      email_type, title, company)
        return body

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC — generate email subject line
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def generate_subject(
        self,
        job: Dict[str, Any],
        email_type: str = 'application',
    ) -> str:
        """Generate a concise email subject line."""
        title = job.get('title', 'Software Developer')
        company = job.get('company', '')

        subjects = {
            'application': f"Application for {title} — {_NAME}",
            'follow_up_d3': f"Following up: {title} Application — {_NAME}",
            'follow_up_d7': f"Re: {title} Position — {_NAME}",
            'follow_up_d14': f"Final Follow-up: {title} — {_NAME}",
            'cold_outreach': f"Experienced Full Stack Developer — {title} Interest",
        }

        return subjects.get(email_type,
                            f"Application for {title} — {_NAME}")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PRIVATE — template-based cover letter
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _template_cover_letter(
        self, job, name, email, phone, current_title,
        exp_years, linkedin, skills_str, achievement, tone,
    ) -> str:
        """Fill cover letter template with personalised values."""
        title = job.get('title', 'Software Developer')
        company = job.get('company', 'your company')
        jd = (job.get('description', '') or '').lower()

        template = _COVER_LETTER_TEMPLATES.get(
            tone, _COVER_LETTER_TEMPLATES['professional'])

        # Build a relevance paragraph based on JD keywords
        relevance = self._build_relevance_paragraph(job, skills_str)

        return template.format(
            title=title,
            company=company,
            name=name,
            email=email,
            phone=phone,
            current_title=current_title,
            exp_years=exp_years,
            linkedin=linkedin,
            skills_str=skills_str,
            achievement=achievement,
            relevance_paragraph=relevance,
        )

    def _build_relevance_paragraph(
        self, job: Dict[str, Any], skills_str: str
    ) -> str:
        """Build a paragraph connecting experience to JD requirements."""
        jd = (job.get('description', '') or '').lower()
        title = job.get('title', '')
        sentences = []

        # Backend emphasis
        if any(kw in jd for kw in ['backend', 'api', 'microservices',
                                    'server', 'spring', 'node.js']):
            sentences.append(
                "My backend experience includes building REST APIs, "
                "implementing WebSocket real-time communication, and "
                "integrating third-party services like Razorpay and "
                "META WhatsApp API.")

        # Database emphasis
        if any(kw in jd for kw in ['database', 'sql', 'mysql', 'postgresql',
                                    'mongodb', 'schema', 'data']):
            sentences.append(
                "I have designed complex database schemas — my BizHub ERP "
                "project alone involved 57+ tables with multi-tenant "
                "architecture and role-based access control.")

        # Java/Spring emphasis
        if any(kw in jd for kw in ['java', 'spring boot', 'spring']):
            sentences.append(
                "I've built an Invoice Microservice using Java, Spring Boot, "
                "PostgreSQL, Kafka, and Docker with event-driven architecture "
                "and PDF export capabilities.")

        # React/Frontend emphasis
        if any(kw in jd for kw in ['react', 'frontend', 'front-end', 'ui']):
            sentences.append(
                "On the frontend, I've built interactive UIs with React "
                "and Vue.js, including a Collaborative Workspace supporting "
                "50+ concurrent users with real-time updates via Socket.io.")

        # Full stack emphasis
        if any(kw in jd for kw in ['full stack', 'fullstack', 'full-stack']):
            sentences.append(
                "As a sole full-stack developer, I've independently handled "
                "everything from client requirements gathering and database "
                "design to frontend development and production deployment.")

        # DevOps emphasis
        if any(kw in jd for kw in ['docker', 'kubernetes', 'ci/cd',
                                    'devops', 'deployment', 'cloud']):
            sentences.append(
                "I'm experienced with Docker containerisation and have "
                "production deployment authority at my current company.")

        if not sentences:
            sentences.append(
                f"My experience as a {_TITLE} — handling everything from "
                f"database design to frontend development — has prepared "
                f"me well for this role.")

        return ' '.join(sentences[:2])  # Keep it concise: max 2 sentences

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PRIVATE — LLM cover letter
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _llm_generate_cover_letter(
        self, job, name, current_title, skills_str,
        achievement, exp_years, tone,
    ) -> str:
        """Use LLM for a high-quality personalised cover letter."""
        title = job.get('title', 'Software Developer')
        company = job.get('company', 'the company')
        jd = job.get('description', '') or ''

        tone_instruction = {
            'professional': 'Professional, polished, and confident tone.',
            'enthusiastic': 'Enthusiastic, energetic, but still professional.',
            'technical': 'Technical and detail-oriented with specific examples.',
        }.get(tone, 'Professional tone.')

        prompt = f"""Write a cover letter for this job application.

APPLICANT:
- Name: {name}
- Current Role: {current_title}
- Experience: {exp_years} year(s) professional
- Key Achievement: {achievement}
- Relevant Skills: {skills_str}
- Other: Sole developer on 10+ production apps, B.Tech CS CGPA 7.79,
  100+ LeetCode problems, published Play Store app

JOB:
- Title: {title}
- Company: {company}
- Description (first 1200 chars):
\"\"\"{jd[:1200]}\"\"\"

RULES:
1. {tone_instruction}
2. 250-350 words maximum
3. 3-4 paragraphs
4. Address "Dear Hiring Manager" (we don't know the name)
5. Reference the SPECIFIC role and company name
6. Highlight 2-3 matching skills from the applicant's background
7. Include ONE specific project/achievement that's relevant
8. Do NOT fabricate experience or skills the applicant doesn't have
9. End with availability (15 days notice) and call-to-action
10. Sign off with just the name (no contact info — that goes separately)
11. Do NOT use buzzwords like "synergy" or "paradigm shift"

Return ONLY the cover letter text."""

        system = ("You are a professional resume/cover letter writer. "
                  "Write genuine, personalised letters that highlight "
                  "real experience. Never fabricate.")

        try:
            result = self._llm.generate(prompt, system_prompt=system,
                                        max_tokens=700, temperature=0.4)
            if result:
                result = result.strip()
                # Ensure it ends with the name
                if name not in result.split('\n')[-3:]:
                    result += f"\n\n{name}"
                return result
        except Exception as exc:
            logger.warning("LLM cover letter generation failed: %s", exc)

        return ''

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PRIVATE — LLM email body
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _llm_generate_email(
        self, job, name, current_title, skills_str,
        achievement, email_type, hr_name,
    ) -> str:
        """Use LLM for application/outreach email body."""
        title = job.get('title', 'Software Developer')
        company = job.get('company', 'the company')
        jd = job.get('description', '') or ''

        type_desc = {
            'application': 'a job application email (resume attached)',
            'cold_outreach': 'a cold outreach email to HR (introducing yourself)',
        }.get(email_type, 'a job application email')

        prompt = f"""Write {type_desc} for this position.

APPLICANT: {name}, {current_title}
KEY: {achievement}
SKILLS: {skills_str}

JOB: {title} at {company}
JD EXCERPT: \"\"\"{jd[:800]}\"\"\"

RULES:
1. Address "{hr_name}"
2. 100-150 words — concise and respectful of their time
3. Mention the specific role and company
4. Highlight 1-2 directly relevant skills/achievements
5. Mention resume is attached
6. Available in 15 days
7. Professional but warm tone
8. Do NOT be spammy or overly salesy
9. Do NOT fabricate experience
10. End with just the name (signature added separately)

Return ONLY the email body text."""

        system = "You write concise, professional job application emails."

        try:
            result = self._llm.generate(prompt, system_prompt=system,
                                        max_tokens=400, temperature=0.3)
            return result.strip() if result else ''
        except Exception as exc:
            logger.warning("LLM email generation failed: %s", exc)
            return ''


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Self-test
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    print("=" * 65)
    print("  Cover Letter Generator — Self-Test (template mode)")
    print("=" * 65)

    gen = CoverLetterGenerator(llm_client=None)

    # ── Test jobs ─────────────────────────────────────────
    test_jobs = [
        {
            'title': 'Backend Developer - Java/Spring Boot',
            'company': 'Flipkart',
            'location': 'Bangalore',
            'description': (
                'Build scalable microservices using Java, Spring Boot, '
                'PostgreSQL, Kafka, Docker. REST API design, unit testing. '
                'Knowledge of Redis, MongoDB, and CI/CD pipelines preferred.'
            ),
        },
        {
            'title': 'Full Stack Developer',
            'company': 'Razorpay',
            'location': 'Bangalore',
            'description': (
                'Looking for a Full Stack Developer with React, Node.js, '
                'MongoDB, REST APIs. Must understand frontend and backend. '
                'WebSocket experience is a plus.'
            ),
        },
        {
            'title': 'SDE-1 (Node.js)',
            'company': 'Swiggy',
            'location': 'Remote',
            'description': (
                'Node.js backend developer. Express, MongoDB, Redis, '
                'WebSocket, JWT auth, Docker. Must know JavaScript well. '
                'Database design and API development.'
            ),
        },
    ]

    # ── Test cover letters ────────────────────────────────
    tones = ['professional', 'enthusiastic', 'technical']
    for i, job in enumerate(test_jobs):
        tone = tones[i % len(tones)]
        print(f"\n{'─' * 60}")
        print(f"📝 Cover Letter: {job['title']} @ {job['company']} "
              f"(tone={tone})")
        print('─' * 60)
        letter = gen.generate(job, tone=tone)
        # Print first 500 chars + word count
        preview = letter[:500] + ('…' if len(letter) > 500 else '')
        print(preview)
        word_count = len(letter.split())
        print(f"\n   [Words: {word_count}, Chars: {len(letter)}]")

    # ── Test email bodies ─────────────────────────────────
    email_types = ['application', 'follow_up_d3', 'follow_up_d7',
                   'follow_up_d14', 'cold_outreach']
    job = test_jobs[0]
    for et in email_types:
        print(f"\n{'─' * 60}")
        print(f"📧 Email ({et}): {job['title']} @ {job['company']}")
        print('─' * 60)
        body = gen.generate_email_body(
            job, email_type=et,
            hr_name='Ms. Priya Sharma',
            applied_date='July 10, 2025')
        preview = body[:400] + ('…' if len(body) > 400 else '')
        print(preview)
        print(f"\n   [Words: {len(body.split())}, Chars: {len(body)}]")

    # ── Test subject lines ────────────────────────────────
    print(f"\n{'─' * 60}")
    print("📋 Subject Lines")
    print('─' * 60)
    for et in email_types:
        subj = gen.generate_subject(test_jobs[0], email_type=et)
        print(f"  {et:18s}: {subj}")

    print(f"\n✅  Cover Letter Generator self-test complete!")