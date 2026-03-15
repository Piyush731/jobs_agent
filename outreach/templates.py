#!/usr/bin/env python3
"""
outreach/templates.py — Email templates for job applications.

Provides structured templates for:
  • Application emails (initial contact with HR)
  • Follow-up emails (D3, D7, D14 after application)
  • Cold outreach emails (direct contact with no prior application)
  • Subject lines for all email types
  • Referral request emails

All templates are personalized with:
  • Job title, company name
  • Matching skills from profile
  • Quantified achievements from resume
  • Appropriate tone and length

Usage:
    from outreach.templates import (
        get_application_template,
        get_follow_up_template,
        get_cold_outreach_template,
        get_subject_line,
    )

    template = get_application_template(job, profile)
    # → {subject: str, body: str}

    followup = get_follow_up_template(application, follow_up_num=1)
    # → {subject: str, body: str}
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import USER_PROFILE
from core.logger import get_logger

logger = get_logger("outreach.templates")


# ═══════════════════════════════════════════════════════════════════
# PROFILE HELPERS
# ═══════════════════════════════════════════════════════════════════

def _get_name() -> str:
    return USER_PROFILE.get("name", "Piyush Kashyap")


def _get_first_name() -> str:
    return _get_name().split()[0]


def _get_email() -> str:
    return USER_PROFILE.get("email", "piyushkashyap3247@gmail.com")


def _get_phone() -> str:
    return USER_PROFILE.get("phone", "+91 73107 03247")


def _get_linkedin() -> str:
    return USER_PROFILE.get("linkedin_url",
                            "linkedin.com/in/piyush-kashyap731")


def _get_github() -> str:
    return USER_PROFILE.get("github_url", "github.com/Piyush731")


def _get_title() -> str:
    return USER_PROFILE.get("current_title",
                            "Full Stack Developer L1")


def _get_experience() -> float:
    return USER_PROFILE.get("experience_years", 1.0)


def _get_skills() -> List[str]:
    return USER_PROFILE.get("skills", [])


def _get_signature() -> str:
    """Standard email signature block."""
    return (
        f"{_get_name()}\n"
        f"{_get_title()}\n"
        f"📧 {_get_email()}\n"
        f"📱 {_get_phone()}\n"
        f"🔗 {_get_linkedin()}\n"
        f"💻 {_get_github()}"
    )


def _match_skills(job: Dict) -> List[str]:
    """Find skills that match between profile and job."""
    profile_skills = {s.lower() for s in _get_skills()}

    job_skills = set()
    # From skills field
    raw_skills = job.get("skills", [])
    if isinstance(raw_skills, str):
        try:
            import json
            raw_skills = json.loads(raw_skills)
        except Exception:
            raw_skills = [s.strip() for s in raw_skills.split(",")
                          if s.strip()]
    for s in raw_skills:
        job_skills.add(s.lower())

    # From description
    description = (job.get("description", "") or "").lower()
    for s in profile_skills:
        if s in description:
            job_skills.add(s)

    matched = sorted(profile_skills & job_skills)
    return matched[:8]  # top 8


def _get_key_achievement() -> str:
    """Single best achievement for email brevity."""
    return ("built 10+ production applications as sole developer "
            "across fintech, ERP, and CRM domains")


def _get_highlights() -> List[str]:
    """Bullet-point highlights for email body."""
    return [
        "Sole developer on 10+ production applications "
        "(fintech, ERP, CRM, edtech)",
        "Built multi-tenant ERP with 57+ database tables "
        "and 5 user roles",
        "Real-time WebSocket integrations, third-party APIs "
        "(META WhatsApp, Razorpay, RTO)",
        "Published app on Google Play Store",
        "Proficient in JavaScript, Java, Python, Vue.js, "
        "Node.js, Spring Boot",
    ]


def _salutation(contact_name: str = "",
                contact_title: str = "") -> str:
    """Generate appropriate salutation."""
    if contact_name:
        # Use title prefix if we know it
        if contact_title:
            title_lower = contact_title.lower()
            if any(t in title_lower for t in ["mr", "sir"]):
                return f"Dear Mr. {contact_name.split()[-1]},"
            elif any(t in title_lower for t in
                     ["ms", "mrs", "ma'am", "madam"]):
                return f"Dear Ms. {contact_name.split()[-1]},"
        return f"Dear {contact_name.split()[0]},"
    return "Dear Hiring Manager,"


# ═══════════════════════════════════════════════════════════════════
# APPLICATION TEMPLATE
# ═══════════════════════════════════════════════════════════════════

def get_application_template(job: Dict,
                              profile: Optional[Dict] = None,
                              contact_name: str = "",
                              contact_title: str = "") -> Dict:
    """
    Generate an application email template.

    Args:
        job: Job dict (title, company, description, skills, url).
        profile: Profile dict (falls back to USER_PROFILE).
        contact_name: HR/recruiter name (if known).
        contact_title: HR/recruiter title (if known).

    Returns:
        {subject: str, body: str}
    """
    title = job.get("title", "the open position")
    company = job.get("company", "your company")
    name = _get_name()
    current_title = _get_title()

    # Find matching skills
    matched = _match_skills(job)
    skills_str = ", ".join(matched[:6]) if matched else (
        "JavaScript, Node.js, Java, Spring Boot")

    subject = f"Application for {title} — {name}"

    sal = _salutation(contact_name, contact_title)

    body = f"""{sal}

I am writing to express my strong interest in the {title} position at {company}. As a {current_title} with hands-on experience building {_get_experience():.0f}+ year(s) of production applications across fintech, ERP, edtech, and CRM domains, I am confident in my ability to contribute effectively to your team.

In my current role at Site Guru Pvt Ltd, I serve as the sole developer responsible for 10+ end-to-end production applications. Key highlights include:

• Built multi-tenant ERP system with 57+ database tables and 5 user roles
• Developed RTO service handling 1,000+ daily API requests with META WhatsApp integration
• Implemented real-time WebSocket price feeds for trading platform
• Integrated third-party APIs: Razorpay, META WhatsApp, RTO, IOTrades
• Published application on Google Play Store

My technical skills include {skills_str}, which align well with the requirements for this role.

I have attached my resume for your review and would welcome the opportunity to discuss how my experience can contribute to {company}'s goals.

Thank you for your time and consideration.

Best regards,
{_get_signature()}"""

    return {"subject": subject, "body": body}


# ═══════════════════════════════════════════════════════════════════
# FOLLOW-UP TEMPLATES
# ═══════════════════════════════════════════════════════════════════

def get_follow_up_template(application: Dict,
                            follow_up_num: int,
                            contact_name: str = "") -> Dict:
    """
    Generate a follow-up email template.

    Args:
        application: Application dict (title, company, applied_at,
                     contact_name).
        follow_up_num: 1 (D3), 2 (D7), or 3 (D14).
        contact_name: Override contact name.

    Returns:
        {subject: str, body: str}
    """
    title = application.get("title",
                            application.get("job_title", "the position"))
    company = application.get("company", "your company")
    name = _get_name()
    applied_at = application.get("applied_at", "recently")

    # Clean date
    if isinstance(applied_at, str) and len(applied_at) > 10:
        applied_at = applied_at[:10]

    cn = contact_name or application.get("contact_name", "")
    sal = _salutation(cn)

    if follow_up_num == 1:
        # ── Day 3 — Polite check-in ──
        subject = (f"Following up: {title} Application — {name}")
        body = f"""{sal}

I wanted to follow up on my application for the {title} position at {company}, submitted on {applied_at}. I remain very enthusiastic about this opportunity and would welcome any updates on the hiring process.

Please don't hesitate to reach out if you need any additional information from my side.

Best regards,
{_get_signature()}"""

    elif follow_up_num == 2:
        # ── Day 7 — Value reinforcement ──
        subject = f"Re: {title} Position — {name}"
        body = f"""{sal}

I hope you're doing well. I'm writing to reiterate my interest in the {title} role at {company}. Since applying on {applied_at}, I've continued to work on production applications and sharpen my skills in the relevant technologies.

I'd be grateful for the chance to discuss how my experience with 10+ production applications — including multi-tenant ERP systems, real-time integrations, and third-party API work — could benefit your team.

Looking forward to hearing from you.

Best regards,
{_get_signature()}"""

    else:
        # ── Day 14 — Final graceful follow-up ──
        subject = f"Final Follow-up: {title} — {name}"
        body = f"""{sal}

I'm following up once more regarding the {title} position at {company}. I understand the hiring process takes time, and I remain keen on this opportunity.

If the position has been filled or my profile isn't the right fit this time, I completely understand. I'd appreciate any feedback or consideration for future openings at {company}.

Thank you for your time and consideration throughout this process.

Best regards,
{_get_signature()}"""

    return {"subject": subject, "body": body}


# ═══════════════════════════════════════════════════════════════════
# COLD OUTREACH TEMPLATE
# ═══════════════════════════════════════════════════════════════════

def get_cold_outreach_template(job: Dict,
                                profile: Optional[Dict] = None,
                                contact_name: str = "",
                                contact_title: str = "") -> Dict:
    """
    Generate a cold outreach email (no prior application).

    Shorter, more direct than application email. Used when we find
    HR contact but haven't formally applied yet.

    Args:
        job: Job dict.
        profile: Profile dict.
        contact_name: Recruiter/HR name.
        contact_title: Recruiter/HR title.

    Returns:
        {subject: str, body: str}
    """
    title = job.get("title", "Software Developer")
    company = job.get("company", "your company")
    name = _get_name()
    current_title = _get_title()

    matched = _match_skills(job)
    skills_str = ", ".join(matched[:5]) if matched else (
        "JavaScript, Node.js, Java, Spring Boot")

    subject = (f"Experienced {current_title} — "
               f"{title} Interest")

    sal = _salutation(contact_name, contact_title)

    body = f"""{sal}

I'm {name}, a {current_title} looking for my next opportunity. I admire {company}'s work and wanted to reach out directly.

I've built 10+ production applications as a sole developer — from multi-tenant ERP systems (57+ DB tables) to real-time trading platforms with WebSocket feeds. My stack includes {skills_str}.

If {company} has any open {title} positions or similar roles, I'd love to be considered. I'm available to join within 15 days.

I've attached my resume for your reference. Happy to discuss further at your convenience.

Best regards,
{_get_signature()}"""

    return {"subject": subject, "body": body}


# ═══════════════════════════════════════════════════════════════════
# REFERRAL REQUEST TEMPLATE
# ═══════════════════════════════════════════════════════════════════

def get_referral_template(job: Dict,
                           contact_name: str) -> Dict:
    """
    Generate a referral request email (to someone inside the company).

    Args:
        job: Job dict.
        contact_name: Name of the person inside the company.

    Returns:
        {subject: str, body: str}
    """
    title = job.get("title", "Software Developer")
    company = job.get("company", "the company")
    name = _get_name()
    job_url = job.get("url", "")

    subject = f"Referral Request: {title} at {company}"

    body = f"""Hi {contact_name.split()[0]},

I hope you're doing well! I came across a {title} opening at {company} and I'm very interested in applying.

I've been working as a Full Stack Developer, building 10+ production applications independently — including multi-tenant ERP systems, real-time trading platforms, and third-party API integrations (META WhatsApp, Razorpay).

Would you be open to referring me for this role? I'd be happy to send my resume and any other details you might need.

{f'Job Link: {job_url}' if job_url else ''}

Thank you for considering — I really appreciate it!

Best,
{name}
{_get_email()} | {_get_phone()}"""

    return {"subject": subject, "body": body}


# ═══════════════════════════════════════════════════════════════════
# SUBJECT LINES
# ═══════════════════════════════════════════════════════════════════

def get_subject_line(job: Dict,
                     email_type: str = "application") -> str:
    """
    Generate an appropriate subject line.

    Args:
        job: Job dict (title, company).
        email_type: "application" | "follow_up_d3" | "follow_up_d7"
                    | "follow_up_d14" | "cold_outreach" | "referral"

    Returns:
        Subject line string.
    """
    title = job.get("title", "Software Developer")
    company = job.get("company", "")
    name = _get_name()

    subjects = {
        "application": (
            f"Application for {title} — {name}"),
        "follow_up_d3": (
            f"Following up: {title} Application — {name}"),
        "follow_up_d7": (
            f"Re: {title} Position — {name}"),
        "follow_up_d14": (
            f"Final Follow-up: {title} — {name}"),
        "cold_outreach": (
            f"Experienced {_get_title()} — {title} Interest"),
        "referral": (
            f"Referral Request: {title} at {company}"),
    }

    return subjects.get(email_type, subjects["application"])


# ═══════════════════════════════════════════════════════════════════
# THANK-YOU / INTERVIEW TEMPLATES
# ═══════════════════════════════════════════════════════════════════

def get_thank_you_template(job: Dict,
                            interviewer_name: str = "",
                            interview_type: str = "interview"
                            ) -> Dict:
    """
    Generate a post-interview thank-you email.

    Args:
        job: Job dict.
        interviewer_name: Name of the interviewer.
        interview_type: "phone_screen" | "interview" | "final_round"

    Returns:
        {subject: str, body: str}
    """
    title = job.get("title", "the position")
    company = job.get("company", "your company")
    name = _get_name()

    sal = _salutation(interviewer_name)

    subject = f"Thank You — {title} {interview_type.replace('_', ' ').title()}"

    body = f"""{sal}

Thank you for taking the time to speak with me about the {title} role at {company}. I truly enjoyed our conversation and am even more excited about the opportunity to contribute to your team.

The discussion reinforced my enthusiasm for the role, and I'm confident that my experience building production applications end-to-end would allow me to make a meaningful impact.

Please don't hesitate to reach out if you need any additional information. I look forward to hearing about the next steps.

Best regards,
{_get_signature()}"""

    return {"subject": subject, "body": body}


# ═══════════════════════════════════════════════════════════════════
# WITHDRAWAL TEMPLATE
# ═══════════════════════════════════════════════════════════════════

def get_withdrawal_template(job: Dict,
                             contact_name: str = "",
                             reason: str = "") -> Dict:
    """Generate a polite application withdrawal email."""
    title = job.get("title", "the position")
    company = job.get("company", "your company")
    name = _get_name()

    sal = _salutation(contact_name)

    reason_text = ""
    if reason:
        reason_text = f" {reason}"

    subject = f"Application Withdrawal: {title} — {name}"

    body = f"""{sal}

I hope this message finds you well. I am writing to respectfully withdraw my application for the {title} position at {company}.{reason_text}

I sincerely appreciate your time and consideration throughout the process. I have great respect for {company} and hope to have the opportunity to connect again in the future.

Thank you for your understanding.

Best regards,
{_get_signature()}"""

    return {"subject": subject, "body": body}


# ═══════════════════════════════════════════════════════════════════
# BATCH HELPER — Get all templates for a job
# ═══════════════════════════════════════════════════════════════════

def get_all_templates(job: Dict,
                       contact_name: str = "",
                       contact_title: str = "") -> Dict[str, Dict]:
    """
    Generate all email templates for a single job.

    Returns:
        {
            "application": {subject, body},
            "cold_outreach": {subject, body},
            "follow_up_d3": {subject, body},
            "follow_up_d7": {subject, body},
            "follow_up_d14": {subject, body},
            "thank_you": {subject, body},
            "subjects": {type: subject_line},
        }
    """
    app_data = {
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "applied_at": datetime.now().strftime("%Y-%m-%d"),
        "contact_name": contact_name,
    }

    return {
        "application": get_application_template(
            job, contact_name=contact_name,
            contact_title=contact_title),
        "cold_outreach": get_cold_outreach_template(
            job, contact_name=contact_name,
            contact_title=contact_title),
        "follow_up_d3": get_follow_up_template(
            app_data, 1, contact_name),
        "follow_up_d7": get_follow_up_template(
            app_data, 2, contact_name),
        "follow_up_d14": get_follow_up_template(
            app_data, 3, contact_name),
        "thank_you": get_thank_you_template(job),
        "subjects": {
            t: get_subject_line(job, t) for t in [
                "application", "follow_up_d3", "follow_up_d7",
                "follow_up_d14", "cold_outreach", "referral",
            ]
        },
    }


# ═══════════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 65)
    print("  Email Templates — Self Test")
    print("=" * 65)

    test_job = {
        "title": "Backend Developer - Java/Spring Boot",
        "company": "Flipkart",
        "location": "Bangalore",
        "url": "https://example.com/job/123",
        "description": (
            "Looking for a Java developer with Spring Boot, "
            "Docker, Kubernetes, REST API experience."
        ),
        "skills": ["Java", "Spring Boot", "Docker",
                    "Kubernetes", "REST API"],
    }

    # 1. Application
    print("\n─── Application Email ───")
    app = get_application_template(
        test_job, contact_name="Priya Sharma",
        contact_title="HR Manager")
    print(f"Subject: {app['subject']}")
    print(f"Body ({len(app['body'])} chars):")
    print(app['body'][:400] + "…")

    # 2. Follow-ups
    app_data = {
        "title": test_job["title"],
        "company": test_job["company"],
        "applied_at": "2025-07-10",
        "contact_name": "Priya Sharma",
    }
    for num, label in [(1, "D3"), (2, "D7"), (3, "D14")]:
        print(f"\n─── Follow-up {label} ───")
        fu = get_follow_up_template(app_data, num, "Priya Sharma")
        print(f"Subject: {fu['subject']}")
        print(f"Body ({len(fu['body'])} chars)")

    # 3. Cold outreach
    print("\n─── Cold Outreach ───")
    cold = get_cold_outreach_template(
        test_job, contact_name="Recruiter")
    print(f"Subject: {cold['subject']}")
    print(f"Body ({len(cold['body'])} chars)")

    # 4. Subject lines
    print("\n─── Subject Lines ───")
    for etype in ["application", "follow_up_d3", "follow_up_d7",
                   "follow_up_d14", "cold_outreach", "referral"]:
        subj = get_subject_line(test_job, etype)
        print(f"  {etype:20s}: {subj}")

    # 5. All templates
    print("\n─── All Templates ───")
    all_t = get_all_templates(test_job, "Priya Sharma", "HR")
    for key, val in all_t.items():
        if key == "subjects":
            print(f"  subjects: {len(val)} entries")
        else:
            print(f"  {key}: {len(val.get('body', ''))} chars")

    # 6. Skill matching
    print("\n─── Skill Matching ───")
    matched = _match_skills(test_job)
    print(f"  Matched: {matched}")

    # 7. Thank you
    print("\n─── Thank You ───")
    ty = get_thank_you_template(test_job, "John Smith")
    print(f"Subject: {ty['subject']}")
    print(f"Body ({len(ty['body'])} chars)")

    # 8. Withdrawal
    print("\n─── Withdrawal ───")
    wd = get_withdrawal_template(
        test_job, "Priya Sharma",
        "I have accepted another offer.")
    print(f"Subject: {wd['subject']}")

    print(f"\n✅ Templates test complete!\n")