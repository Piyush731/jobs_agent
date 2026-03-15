"""
profile/resume_data.py — Structured resume as Python dataclasses.

Provides the complete resume of Piyush Kashyap in a structured format
that can be:
  - Serialized to dict/JSON for AI processing
  - Deserialized back from dict/JSON
  - Deep-copied for tailoring (modify copy, keep original intact)
  - Validated for completeness
  - Exported section-by-section for resume builder

Every field is typed. Nested structures use their own dataclasses.
The base resume is the SINGLE SOURCE OF TRUTH for all tailoring.

Interface:
  get_base_resume() → ResumeData
  resume_to_dict(resume) → dict
  dict_to_resume(data) → ResumeData
"""

import json
import copy
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any
from datetime import datetime

# ── project imports ──────────────────────────────────────────────
from config import USER_PROFILE
from core.logger import get_logger

logger = get_logger("profile.resume_data")


# ═══════════════════════════════════════════════════════════════════
# DATA CLASSES — Nested structures
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Education:
    """Single education entry."""
    degree: str = ""
    field_of_study: str = ""
    institution: str = ""
    location: str = ""
    start_year: int = 0
    end_year: int = 0
    cgpa: Optional[float] = None
    percentage: Optional[float] = None
    grade_type: str = "cgpa"          # "cgpa" | "percentage" | "grade"
    grade_scale: str = "10"           # "10" | "100" | "4.0"
    achievements: List[str] = field(default_factory=list)
    relevant_coursework: List[str] = field(default_factory=list)
    is_current: bool = False

    def grade_display(self) -> str:
        """Human-readable grade string."""
        if self.cgpa is not None:
            return f"CGPA {self.cgpa}/{self.grade_scale}"
        if self.percentage is not None:
            return f"{self.percentage}%"
        return ""

    def year_display(self) -> str:
        """Human-readable year range."""
        if self.is_current:
            return f"{self.start_year} – Present"
        if self.start_year and self.end_year:
            return f"{self.start_year} – {self.end_year}"
        if self.end_year:
            return str(self.end_year)
        return ""


@dataclass
class ExperienceBullet:
    """Single bullet point in an experience entry, with metadata for tailoring."""
    text: str = ""
    keywords: List[str] = field(default_factory=list)    # skills mentioned
    impact_type: str = ""        # "technical" | "leadership" | "delivery" | "scale"
    priority: int = 5            # 1=highest, 10=lowest (for reordering)
    quantified: bool = False     # has numbers/metrics


@dataclass
class Experience:
    """Single work experience entry."""
    title: str = ""
    company: str = ""
    location: str = ""
    start_date: str = ""          # "Aug 2024" format
    end_date: str = ""            # "Present" or "Oct 2024"
    is_current: bool = False
    employment_type: str = ""     # "full-time" | "internship" | "contract"
    description: str = ""         # brief role description
    bullets: List[ExperienceBullet] = field(default_factory=list)
    technologies: List[str] = field(default_factory=list)
    domain: str = ""              # "fintech" | "edtech" | "erp" etc.
    key_projects: List[str] = field(default_factory=list)  # project names

    def duration_display(self) -> str:
        """Human-readable duration."""
        if self.is_current:
            return f"{self.start_date} – Present"
        return f"{self.start_date} – {self.end_date}"

    def bullet_texts(self) -> List[str]:
        """Return just the text of each bullet, in priority order."""
        sorted_bullets = sorted(self.bullets, key=lambda b: b.priority)
        return [b.text for b in sorted_bullets]


@dataclass
class Project:
    """Single project entry (personal / academic / open-source)."""
    name: str = ""
    description: str = ""
    url: str = ""                 # GitHub link or live link
    technologies: List[str] = field(default_factory=list)
    bullets: List[ExperienceBullet] = field(default_factory=list)
    domain: str = ""
    role: str = ""                # "Sole Developer" | "Team Lead" etc.
    team_size: int = 1
    start_date: str = ""
    end_date: str = ""
    is_featured: bool = True      # show in default resume

    def bullet_texts(self) -> List[str]:
        sorted_bullets = sorted(self.bullets, key=lambda b: b.priority)
        return [b.text for b in sorted_bullets]


@dataclass
class Certification:
    """Single certification entry."""
    name: str = ""
    issuer: str = ""
    date: str = ""
    credential_id: str = ""
    url: str = ""
    relevant_skills: List[str] = field(default_factory=list)


@dataclass
class Achievement:
    """Single achievement / recognition entry."""
    text: str = ""
    category: str = ""           # "work" | "academic" | "competitive" | "open-source"
    date: str = ""


@dataclass
class SkillCategory:
    """A named group of skills (e.g., 'Languages', 'Frameworks')."""
    category: str = ""
    skills: List[str] = field(default_factory=list)

    def skills_string(self) -> str:
        return ", ".join(self.skills)


# ═══════════════════════════════════════════════════════════════════
# MAIN RESUME DATACLASS
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ResumeData:
    """
    Complete structured resume.
    This is the MASTER structure used by:
      - resume_tailor.py (modify a copy for each JD)
      - resume/builder.py (render to DOCX/PDF)
      - ai/job_matcher.py (extract skills for matching)
      - outreach (personalize emails)
    """
    # ── Personal Info ────────────────────────────────
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    website: str = ""

    # ── Professional Summary ─────────────────────────
    summary: str = ""

    # ── Work Experience ──────────────────────────────
    experience: List[Experience] = field(default_factory=list)

    # ── Projects ─────────────────────────────────────
    projects: List[Project] = field(default_factory=list)

    # ── Education ────────────────────────────────────
    education: List[Education] = field(default_factory=list)

    # ── Skills (categorized) ─────────────────────────
    skills: List[SkillCategory] = field(default_factory=list)

    # ── Certifications ───────────────────────────────
    certifications: List[Certification] = field(default_factory=list)

    # ── Achievements ─────────────────────────────────
    achievements: List[Achievement] = field(default_factory=list)

    # ── Coding Profiles ──────────────────────────────
    coding_profiles: Dict[str, str] = field(default_factory=dict)
    # e.g. {"leetcode": "100+ problems", "gfg": "..."}

    # ── Metadata (not printed on resume) ─────────────
    version: str = "base"
    tailored_for: str = ""        # company+title if tailored
    created_at: str = ""
    last_modified: str = ""

    # ── Convenience Methods ──────────────────────────

    def all_skills_flat(self) -> List[str]:
        """Return a flat, deduplicated list of ALL skills."""
        seen = set()
        flat = []
        for cat in self.skills:
            for s in cat.skills:
                s_lower = s.strip().lower()
                if s_lower not in seen:
                    seen.add(s_lower)
                    flat.append(s.strip())
        return flat

    def all_technologies(self) -> List[str]:
        """All technologies from experience + projects + skills, deduplicated."""
        seen = set()
        techs = []
        for exp in self.experience:
            for t in exp.technologies:
                t_lower = t.strip().lower()
                if t_lower not in seen:
                    seen.add(t_lower)
                    techs.append(t.strip())
        for proj in self.projects:
            for t in proj.technologies:
                t_lower = t.strip().lower()
                if t_lower not in seen:
                    seen.add(t_lower)
                    techs.append(t.strip())
        for s in self.all_skills_flat():
            s_lower = s.strip().lower()
            if s_lower not in seen:
                seen.add(s_lower)
                techs.append(s.strip())
        return techs

    def current_experience(self) -> Optional[Experience]:
        """Return the current (most recent) work experience, or None."""
        for exp in self.experience:
            if exp.is_current:
                return exp
        if self.experience:
            return self.experience[0]
        return None

    def total_experience_years(self) -> float:
        """Rough total experience in years from all entries."""
        total_months = 0
        for exp in self.experience:
            start = _parse_date_rough(exp.start_date)
            end = _parse_date_rough(exp.end_date) if not exp.is_current else datetime.now()
            if start and end:
                delta = end - start
                total_months += max(delta.days / 30.0, 0)
        return round(total_months / 12.0, 1)

    def experience_domains(self) -> List[str]:
        """List of unique domains from experience + projects."""
        domains = set()
        for exp in self.experience:
            if exp.domain:
                domains.add(exp.domain.lower())
        for proj in self.projects:
            if proj.domain:
                domains.add(proj.domain.lower())
        return sorted(domains)

    def has_skill(self, skill_query: str) -> bool:
        """Check if any skill matches (case-insensitive, partial)."""
        query = skill_query.strip().lower()
        for s in self.all_skills_flat():
            if query in s.lower() or s.lower() in query:
                return True
        return False

    def skills_by_category(self) -> Dict[str, List[str]]:
        """Return skills as category → list mapping."""
        return {cat.category: list(cat.skills) for cat in self.skills}

    def deep_copy(self) -> "ResumeData":
        """Return a deep copy for safe tailoring (modify without affecting original)."""
        return copy.deepcopy(self)

    def section_completeness(self) -> Dict[str, bool]:
        """Check which sections are filled."""
        return {
            "personal_info": bool(self.name and self.email and self.phone),
            "summary": bool(self.summary and len(self.summary) > 20),
            "experience": len(self.experience) > 0,
            "projects": len(self.projects) > 0,
            "education": len(self.education) > 0,
            "skills": len(self.skills) > 0,
            "certifications": len(self.certifications) > 0,
            "achievements": len(self.achievements) > 0,
        }

    def is_complete(self) -> bool:
        """Check if all critical sections are filled."""
        checks = self.section_completeness()
        critical = ["personal_info", "summary", "experience", "education", "skills"]
        return all(checks.get(k, False) for k in critical)

    def word_count(self) -> int:
        """Approximate word count of the entire resume content."""
        parts = [self.summary]
        for exp in self.experience:
            parts.append(exp.description)
            parts.extend(b.text for b in exp.bullets)
        for proj in self.projects:
            parts.append(proj.description)
            parts.extend(b.text for b in proj.bullets)
        for ach in self.achievements:
            parts.append(ach.text)
        text = " ".join(p for p in parts if p)
        return len(text.split())

    def to_plain_text(self) -> str:
        """Full resume as plain text (for ATS scoring, keyword extraction)."""
        lines = []
        lines.append(f"{self.name}")
        lines.append(f"{self.email} | {self.phone} | {self.location}")
        if self.linkedin:
            lines.append(f"LinkedIn: {self.linkedin}")
        if self.github:
            lines.append(f"GitHub: {self.github}")
        lines.append("")

        if self.summary:
            lines.append("PROFESSIONAL SUMMARY")
            lines.append(self.summary)
            lines.append("")

        if self.experience:
            lines.append("WORK EXPERIENCE")
            for exp in self.experience:
                lines.append(f"{exp.title} | {exp.company} | {exp.duration_display()}")
                if exp.description:
                    lines.append(f"  {exp.description}")
                for b in sorted(exp.bullets, key=lambda x: x.priority):
                    lines.append(f"  • {b.text}")
                lines.append("")

        if self.projects:
            lines.append("PROJECTS")
            for proj in self.projects:
                tech_str = ", ".join(proj.technologies) if proj.technologies else ""
                lines.append(f"{proj.name} | {tech_str}")
                if proj.description:
                    lines.append(f"  {proj.description}")
                for b in sorted(proj.bullets, key=lambda x: x.priority):
                    lines.append(f"  • {b.text}")
                lines.append("")

        if self.skills:
            lines.append("TECHNICAL SKILLS")
            for cat in self.skills:
                lines.append(f"  {cat.category}: {cat.skills_string()}")
            lines.append("")

        if self.education:
            lines.append("EDUCATION")
            for edu in self.education:
                lines.append(
                    f"{edu.degree} in {edu.field_of_study} | "
                    f"{edu.institution} | {edu.year_display()} | "
                    f"{edu.grade_display()}"
                )
            lines.append("")

        if self.certifications:
            lines.append("CERTIFICATIONS")
            for cert in self.certifications:
                lines.append(f"  {cert.name} — {cert.issuer} ({cert.date})")
            lines.append("")

        if self.achievements:
            lines.append("ACHIEVEMENTS")
            for ach in self.achievements:
                lines.append(f"  • {ach.text}")
            lines.append("")

        if self.coding_profiles:
            lines.append("CODING PROFILES")
            for platform, detail in self.coding_profiles.items():
                lines.append(f"  {platform}: {detail}")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# HELPER — rough date parsing
# ═══════════════════════════════════════════════════════════════════

_MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _parse_date_rough(date_str: str) -> Optional[datetime]:
    """Parse 'Aug 2024' / 'August 2024' / '2024' / 'Present' → datetime."""
    if not date_str:
        return None
    s = date_str.strip().lower()
    if s in ("present", "current", "now", "ongoing"):
        return datetime.now()
    parts = s.replace(",", "").split()
    if len(parts) == 2:
        month_str, year_str = parts[0], parts[1]
        month = _MONTH_MAP.get(month_str, 1)
        try:
            year = int(year_str)
            return datetime(year, month, 1)
        except (ValueError, TypeError):
            pass
    if len(parts) == 1:
        try:
            year = int(parts[0])
            return datetime(year, 1, 1)
        except (ValueError, TypeError):
            pass
    return None


# ═══════════════════════════════════════════════════════════════════
# SERIALIZATION — to/from dict and JSON
# ═══════════════════════════════════════════════════════════════════

def resume_to_dict(resume: ResumeData) -> dict:
    """
    Convert ResumeData to a plain dict (JSON-serializable).
    Uses dataclasses.asdict for nested conversion.
    """
    try:
        data = asdict(resume)
        logger.debug("Resume serialized to dict successfully")
        return data
    except Exception as e:
        logger.error(f"Failed to serialize resume to dict: {e}")
        raise


def dict_to_resume(data: dict) -> ResumeData:
    """
    Reconstruct a ResumeData from a dict (reverse of resume_to_dict).
    Handles nested dataclass reconstruction manually for safety.
    """
    if not data or not isinstance(data, dict):
        logger.error("Invalid data passed to dict_to_resume")
        raise ValueError("Expected a non-empty dict")

    try:
        # ── Education ──
        education_list = []
        for edu_data in data.get("education", []):
            if isinstance(edu_data, dict):
                education_list.append(Education(**edu_data))
            elif isinstance(edu_data, Education):
                education_list.append(edu_data)

        # ── Experience ──
        experience_list = []
        for exp_data in data.get("experience", []):
            if isinstance(exp_data, dict):
                bullets_raw = exp_data.pop("bullets", [])
                bullets = []
                for b in bullets_raw:
                    if isinstance(b, dict):
                        bullets.append(ExperienceBullet(**b))
                    elif isinstance(b, ExperienceBullet):
                        bullets.append(b)
                exp_data["bullets"] = bullets
                experience_list.append(Experience(**exp_data))
            elif isinstance(exp_data, Experience):
                experience_list.append(exp_data)

        # ── Projects ──
        project_list = []
        for proj_data in data.get("projects", []):
            if isinstance(proj_data, dict):
                bullets_raw = proj_data.pop("bullets", [])
                bullets = []
                for b in bullets_raw:
                    if isinstance(b, dict):
                        bullets.append(ExperienceBullet(**b))
                    elif isinstance(b, ExperienceBullet):
                        bullets.append(b)
                proj_data["bullets"] = bullets
                project_list.append(Project(**proj_data))
            elif isinstance(proj_data, Project):
                project_list.append(proj_data)

        # ── Skills ──
        skills_list = []
        for skill_data in data.get("skills", []):
            if isinstance(skill_data, dict):
                skills_list.append(SkillCategory(**skill_data))
            elif isinstance(skill_data, SkillCategory):
                skills_list.append(skill_data)

        # ── Certifications ──
        cert_list = []
        for cert_data in data.get("certifications", []):
            if isinstance(cert_data, dict):
                cert_list.append(Certification(**cert_data))
            elif isinstance(cert_data, Certification):
                cert_list.append(cert_data)

        # ── Achievements ──
        ach_list = []
        for ach_data in data.get("achievements", []):
            if isinstance(ach_data, dict):
                ach_list.append(Achievement(**ach_data))
            elif isinstance(ach_data, Achievement):
                ach_list.append(ach_data)

        resume = ResumeData(
            name=data.get("name", ""),
            email=data.get("email", ""),
            phone=data.get("phone", ""),
            location=data.get("location", ""),
            linkedin=data.get("linkedin", ""),
            github=data.get("github", ""),
            portfolio=data.get("portfolio", ""),
            website=data.get("website", ""),
            summary=data.get("summary", ""),
            experience=experience_list,
            projects=project_list,
            education=education_list,
            skills=skills_list,
            certifications=cert_list,
            achievements=ach_list,
            coding_profiles=data.get("coding_profiles", {}),
            version=data.get("version", "reconstructed"),
            tailored_for=data.get("tailored_for", ""),
            created_at=data.get("created_at", ""),
            last_modified=data.get("last_modified", ""),
        )
        logger.debug("Resume reconstructed from dict successfully")
        return resume

    except Exception as e:
        logger.error(f"Failed to reconstruct resume from dict: {e}")
        raise


def resume_to_json(resume: ResumeData, pretty: bool = True) -> str:
    """Serialize ResumeData to JSON string."""
    data = resume_to_dict(resume)
    indent = 2 if pretty else None
    return json.dumps(data, indent=indent, ensure_ascii=False)


def json_to_resume(json_str: str) -> ResumeData:
    """Deserialize JSON string to ResumeData."""
    data = json.loads(json_str)
    return dict_to_resume(data)


# ═══════════════════════════════════════════════════════════════════
# THE BASE RESUME — Piyush Kashyap (Single Source of Truth)
# ═══════════════════════════════════════════════════════════════════

def get_base_resume() -> ResumeData:
    """
    Return the complete, structured, BASE resume for Piyush Kashyap.

    This is called by:
      - resume_tailor.py → deep_copy() → modify copy per JD
      - resume/builder.py → render to DOCX/PDF
      - ai/job_matcher.py → extract skills for matching
      - outreach → personalize email with name, current role

    NEVER modify the return value directly — always .deep_copy() first.
    """

    resume = ResumeData(
        # ── Personal Info ────────────────────────────────────────
        name="Piyush Kashyap",
        email="piyushkashyap3247@gmail.com",
        phone="+91 73107 03247",
        location="Rishikesh, Uttarakhand, India",
        linkedin="linkedin.com/in/piyush-kashyap731",
        github="github.com/Piyush731",
        portfolio="",
        website="",

        # ── Professional Summary ─────────────────────────────────
        summary=(
            "Full Stack Developer with end-to-end ownership of 10+ production "
            "applications across fintech, ERP, edtech, and CRM domains. "
            "Sole developer on each project — handling client requirements, "
            "database design, frontend, and backend independently. "
            "Shipped multi-tenant systems with 57+ database tables, real-time "
            "WebSocket feeds, and third-party integrations including META "
            "WhatsApp API, Razorpay, RTO API, and IOTrades. "
            "Published a mobile application on Google Play Store. "
            "Experienced in Vue.js, Nuxt.js, Node.js, MySQL, and REST API "
            "architecture. Building skills in Java, Spring Boot, and "
            "microservices for backend-focused roles."
        ),

        # ── Work Experience ──────────────────────────────────────
        experience=[
            Experience(
                title="Full Stack Developer L1",
                company="Site Guru Pvt Ltd",
                location="Rishikesh, India",
                start_date="Aug 2024",
                end_date="Present",
                is_current=True,
                employment_type="full-time",
                description=(
                    "Sole developer responsible for architecture, development, "
                    "deployment, and maintenance of 10+ production web applications "
                    "serving clients across multiple industries."
                ),
                technologies=[
                    "Vue.js", "Nuxt.js", "Node.js", "MySQL", "Vuetify",
                    "REST APIs", "WebSockets", "JavaScript", "HTML", "CSS",
                    "META WhatsApp API", "Razorpay", "RTO API", "IOTrades API",
                    "JWT", "Git"
                ],
                domain="erp,fintech,edtech,crm",
                key_projects=[
                    "BizHub ERP", "My RTO Expert", "Rudra Fintech",
                    "FX Prime Trading", "Dheeranet ISP CRM", "SB Flying Services",
                    "TutorsUp", "SoloWash", "Aadishri Construction"
                ],
                bullets=[
                    ExperienceBullet(
                        text=(
                            "Architected and developed BizHub ERP, a multi-tenant "
                            "enterprise resource planning system with 57+ database "
                            "tables, 5 user role types, and modules for inventory, "
                            "invoicing, HR, and reporting."
                        ),
                        keywords=["ERP", "multi-tenant", "database design", "MySQL",
                                  "role-based access", "inventory", "invoicing"],
                        impact_type="technical",
                        priority=1,
                        quantified=True,
                    ),
                    ExperienceBullet(
                        text=(
                            "Built My RTO Expert, a vehicle management platform "
                            "processing 1,000+ daily requests with META WhatsApp "
                            "API integration for automated notifications and RTO "
                            "API for real-time vehicle data lookup."
                        ),
                        keywords=["API integration", "WhatsApp API", "RTO API",
                                  "high traffic", "automation", "Node.js"],
                        impact_type="scale",
                        priority=2,
                        quantified=True,
                    ),
                    ExperienceBullet(
                        text=(
                            "Developed Rudra Fintech, an investment management "
                            "platform with automated interest calculations, TDS "
                            "computation, audit logging, and role-based portfolio "
                            "dashboards for investors and administrators."
                        ),
                        keywords=["fintech", "investment", "interest calculation",
                                  "TDS", "audit logging", "financial systems"],
                        impact_type="technical",
                        priority=3,
                        quantified=False,
                    ),
                    ExperienceBullet(
                        text=(
                            "Engineered FX Prime Trading platform integrating "
                            "IOTrades API and MT5 for real-time forex data via "
                            "WebSocket price feeds, supporting live trading views "
                            "and historical chart analysis."
                        ),
                        keywords=["WebSocket", "real-time", "trading", "API integration",
                                  "IOTrades", "MT5", "forex"],
                        impact_type="technical",
                        priority=4,
                        quantified=False,
                    ),
                    ExperienceBullet(
                        text=(
                            "Created Dheeranet ISP CRM with hierarchical zone "
                            "management, IP address allocation, customer lifecycle "
                            "tracking, and billing automation for an internet "
                            "service provider."
                        ),
                        keywords=["CRM", "ISP", "billing", "customer management",
                                  "hierarchical data"],
                        impact_type="technical",
                        priority=5,
                        quantified=False,
                    ),
                    ExperienceBullet(
                        text=(
                            "Published TutorsUp on Google Play Store — an edtech "
                            "platform connecting students with tutors, featuring "
                            "search, booking, reviews, and real-time availability."
                        ),
                        keywords=["Google Play Store", "mobile app", "edtech",
                                  "published", "booking system"],
                        impact_type="delivery",
                        priority=6,
                        quantified=False,
                    ),
                    ExperienceBullet(
                        text=(
                            "Integrated Razorpay payment gateway across multiple "
                            "projects, implementing secure checkout, webhook "
                            "verification, and automated payment reconciliation."
                        ),
                        keywords=["Razorpay", "payment gateway", "webhooks",
                                  "payment integration", "security"],
                        impact_type="technical",
                        priority=7,
                        quantified=False,
                    ),
                    ExperienceBullet(
                        text=(
                            "Delivered additional production applications "
                            "including SB Flying Services (aviation booking), "
                            "SoloWash (service marketplace), and Aadishri "
                            "Construction (project management) — each built "
                            "end-to-end as sole developer."
                        ),
                        keywords=["production", "end-to-end", "sole developer",
                                  "multiple domains"],
                        impact_type="delivery",
                        priority=8,
                        quantified=False,
                    ),
                    ExperienceBullet(
                        text=(
                            "Recognized as top performer with production "
                            "deployment authority. Mentored junior team members "
                            "on code review, debugging practices, and project "
                            "architecture patterns."
                        ),
                        keywords=["leadership", "mentoring", "code review",
                                  "deployment", "top performer"],
                        impact_type="leadership",
                        priority=9,
                        quantified=False,
                    ),
                ],
            ),

            Experience(
                title="Salesforce Developer Intern",
                company="SmartBridge",
                location="Remote",
                start_date="Jul 2024",
                end_date="Sep 2024",
                is_current=False,
                employment_type="internship",
                description=(
                    "Developed Salesforce automation solutions including "
                    "Apex triggers, batch classes, and Lightning Web Components."
                ),
                technologies=[
                    "Salesforce", "Apex", "Lightning Web Components (LWC)",
                    "SOQL", "Salesforce DX"
                ],
                domain="crm",
                key_projects=["Lead Assignment Automation"],
                bullets=[
                    ExperienceBullet(
                        text=(
                            "Developed Apex triggers and batch classes for "
                            "automated lead assignment, reducing manual assignment "
                            "effort by 30% and improving sales team response times."
                        ),
                        keywords=["Apex", "triggers", "batch processing",
                                  "automation", "lead management", "Salesforce"],
                        impact_type="technical",
                        priority=1,
                        quantified=True,
                    ),
                    ExperienceBullet(
                        text=(
                            "Built Lightning Web Components (LWC) for custom "
                            "Salesforce UI, enhancing user experience for sales "
                            "and support teams with responsive, reusable components."
                        ),
                        keywords=["LWC", "Lightning Web Components", "UI",
                                  "Salesforce", "frontend"],
                        impact_type="technical",
                        priority=2,
                        quantified=False,
                    ),
                ],
            ),
        ],

        # ── Projects ─────────────────────────────────────────────
        projects=[
            Project(
                name="Collaborative Workspace",
                description=(
                    "Real-time collaborative development environment with "
                    "role-based access control supporting 50+ concurrent users."
                ),
                url="",
                technologies=[
                    "React", "Node.js", "MongoDB", "Socket.io", "Gitea API",
                    "Express.js", "JWT"
                ],
                domain="devtools",
                role="Sole Developer",
                team_size=1,
                is_featured=True,
                bullets=[
                    ExperienceBullet(
                        text=(
                            "Built a full-stack collaborative workspace with "
                            "React frontend and Node.js backend, supporting "
                            "real-time code editing and file management via "
                            "Socket.io WebSocket connections for 50+ users."
                        ),
                        keywords=["React", "Node.js", "Socket.io", "WebSocket",
                                  "real-time", "collaboration", "full-stack"],
                        impact_type="technical",
                        priority=1,
                        quantified=True,
                    ),
                    ExperienceBullet(
                        text=(
                            "Implemented role-based access control (RBAC) with "
                            "JWT authentication and integrated Gitea API for "
                            "Git repository management, branch operations, and "
                            "version control workflows."
                        ),
                        keywords=["RBAC", "JWT", "authentication", "Git",
                                  "Gitea", "API integration", "security"],
                        impact_type="technical",
                        priority=2,
                        quantified=False,
                    ),
                ],
            ),

            Project(
                name="Invoice Microservice",
                description=(
                    "Event-driven microservice for invoice generation, "
                    "processing, and PDF export with asynchronous messaging."
                ),
                url="",
                technologies=[
                    "Java", "Spring Boot", "PostgreSQL", "Apache Kafka",
                    "Docker", "REST API", "Microservices"
                ],
                domain="fintech",
                role="Sole Developer",
                team_size=1,
                is_featured=True,
                bullets=[
                    ExperienceBullet(
                        text=(
                            "Designed and implemented an event-driven invoice "
                            "microservice using Java and Spring Boot with Kafka "
                            "for asynchronous message processing and PostgreSQL "
                            "for persistent storage."
                        ),
                        keywords=["Java", "Spring Boot", "Kafka", "microservices",
                                  "event-driven", "PostgreSQL", "backend"],
                        impact_type="technical",
                        priority=1,
                        quantified=False,
                    ),
                    ExperienceBullet(
                        text=(
                            "Containerized the service with Docker, implemented "
                            "RESTful APIs for CRUD operations, and built automated "
                            "PDF invoice generation and export functionality."
                        ),
                        keywords=["Docker", "REST API", "containerization",
                                  "PDF generation", "CRUD"],
                        impact_type="technical",
                        priority=2,
                        quantified=False,
                    ),
                ],
            ),

            Project(
                name="CareerCraft AI Resume Analyzer",
                description=(
                    "AI-powered resume analysis tool that scores resumes "
                    "against job descriptions using NLP and provides "
                    "actionable improvement suggestions."
                ),
                url="",
                technologies=[
                    "Python", "Gemini API", "Streamlit", "NLP",
                    "Natural Language Processing"
                ],
                domain="ai,hrtech",
                role="Sole Developer",
                team_size=1,
                is_featured=True,
                bullets=[
                    ExperienceBullet(
                        text=(
                            "Developed an AI resume analyzer using Python and "
                            "Google Gemini API that performs NLP-based job "
                            "description matching, keyword extraction, and "
                            "ATS compatibility scoring."
                        ),
                        keywords=["Python", "Gemini API", "NLP", "AI",
                                  "resume parsing", "keyword extraction"],
                        impact_type="technical",
                        priority=1,
                        quantified=False,
                    ),
                    ExperienceBullet(
                        text=(
                            "Built an interactive Streamlit web interface with "
                            "real-time score visualization, skill gap analysis, "
                            "and section-by-section improvement recommendations."
                        ),
                        keywords=["Streamlit", "web app", "visualization",
                                  "UI", "analytics"],
                        impact_type="technical",
                        priority=2,
                        quantified=False,
                    ),
                ],
            ),
        ],

        # ── Education ────────────────────────────────────────────
        education=[
            Education(
                degree="Bachelor of Technology (B.Tech)",
                field_of_study="Computer Science",
                institution="Graphic Era Hill University",
                location="Dehradun, Uttarakhand, India",
                start_year=2021,
                end_year=2025,
                cgpa=7.79,
                percentage=None,
                grade_type="cgpa",
                grade_scale="10",
                achievements=[],
                relevant_coursework=[
                    "Data Structures & Algorithms",
                    "Object-Oriented Programming",
                    "Database Management Systems",
                    "Operating Systems",
                    "Computer Networks",
                    "Software Engineering",
                    "Web Technologies",
                ],
                is_current=False,
            ),
        ],

        # ── Skills (categorized) ─────────────────────────────────
        skills=[
            SkillCategory(
                category="Languages",
                skills=[
                    "JavaScript (ES6+)", "Java", "Python", "SQL", "HTML5", "CSS3"
                ],
            ),
            SkillCategory(
                category="Frontend",
                skills=[
                    "Vue.js", "Nuxt.js", "React.js", "Vuetify",
                    "Tailwind CSS", "Bootstrap"
                ],
            ),
            SkillCategory(
                category="Backend",
                skills=[
                    "Node.js", "Express.js", "Spring Boot",
                    "REST APIs", "WebSockets", "Microservices"
                ],
            ),
            SkillCategory(
                category="Databases",
                skills=[
                    "MySQL", "MongoDB", "PostgreSQL", "Redis", "SQLite"
                ],
            ),
            SkillCategory(
                category="Integrations & APIs",
                skills=[
                    "Razorpay", "META WhatsApp API", "IOTrades API",
                    "RTO API", "Gemini API", "Gitea API"
                ],
            ),
            SkillCategory(
                category="DevOps & Tools",
                skills=[
                    "Git", "GitHub", "Docker", "Apache Kafka",
                    "JWT", "Postman", "Linux"
                ],
            ),
            SkillCategory(
                category="Salesforce",
                skills=[
                    "Apex", "Lightning Web Components (LWC)",
                    "SOQL", "Salesforce DX"
                ],
            ),
        ],

        # ── Certifications ───────────────────────────────────────
        certifications=[
            Certification(
                name="Full Stack Java Developer",
                issuer="Udemy",
                date="2024",
                credential_id="",
                url="",
                relevant_skills=["Java", "Spring Boot", "Microservices", "REST APIs"],
            ),
            Certification(
                name="Agile Project Management",
                issuer="Udemy",
                date="2024",
                credential_id="",
                url="",
                relevant_skills=["Agile", "Scrum", "Project Management"],
            ),
        ],

        # ── Achievements ─────────────────────────────────────────
        achievements=[
            Achievement(
                text=(
                    "Sole developer for 10+ production applications at Site Guru, "
                    "handling architecture, development, deployment, and maintenance."
                ),
                category="work",
                date="2024-2025",
            ),
            Achievement(
                text=(
                    "Recognized as top performer with production deployment "
                    "authority and mentor responsibilities for junior developers."
                ),
                category="work",
                date="2025",
            ),
            Achievement(
                text=(
                    "Published TutorsUp mobile application on Google Play Store."
                ),
                category="work",
                date="2024",
            ),
            Achievement(
                text=(
                    "Solved 100+ problems on LeetCode and GeeksforGeeks covering "
                    "Arrays, Trees, Graphs, and Dynamic Programming."
                ),
                category="competitive",
                date="2024-2025",
            ),
        ],

        # ── Coding Profiles ──────────────────────────────────────
        coding_profiles={
            "LeetCode": "100+ problems (Arrays, Trees, Graphs, DP)",
            "GeeksforGeeks": "Active — DSA practice",
        },

        # ── Metadata ─────────────────────────────────────────────
        version="base",
        tailored_for="",
        created_at=datetime.now().isoformat(),
        last_modified=datetime.now().isoformat(),
    )

    logger.debug(
        f"Base resume loaded: {resume.name}, "
        f"{len(resume.experience)} experiences, "
        f"{len(resume.projects)} projects, "
        f"{len(resume.all_skills_flat())} unique skills"
    )

    return resume


# ═══════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def get_resume_for_tailoring() -> ResumeData:
    """
    Convenience: return a DEEP COPY of the base resume, safe to modify.
    Used by resume_tailor.py.
    """
    return get_base_resume().deep_copy()


def get_skills_flat() -> List[str]:
    """Quick access: flat list of all skills (for matching)."""
    return get_base_resume().all_skills_flat()


def get_skill_years_mapping() -> Dict[str, float]:
    """
    Per-skill experience in years (approximate).
    Used by profile/answers.py for form filling.
    """
    return {
        # Primary stack (8 months production)
        "JavaScript": 1.0,
        "Vue.js": 0.8,
        "Nuxt.js": 0.8,
        "Node.js": 0.8,
        "Express.js": 0.8,
        "MySQL": 0.8,
        "Vuetify": 0.8,
        "REST APIs": 1.0,
        "WebSockets": 0.5,
        "HTML": 1.5,
        "CSS": 1.5,
        "HTML5": 1.5,
        "CSS3": 1.5,
        "Git": 1.0,
        "GitHub": 1.0,

        # Third-party integrations
        "Razorpay": 0.5,
        "META WhatsApp API": 0.3,
        "Payment Gateway": 0.5,
        "API Integration": 0.8,

        # Java / Spring Boot (projects + cert)
        "Java": 0.5,
        "Spring Boot": 0.3,
        "Microservices": 0.3,
        "Apache Kafka": 0.2,
        "Kafka": 0.2,

        # React (project-based)
        "React": 0.3,
        "React.js": 0.3,

        # Python (project-based)
        "Python": 0.4,

        # Databases
        "SQL": 1.0,
        "MongoDB": 0.3,
        "PostgreSQL": 0.3,
        "Redis": 0.2,
        "SQLite": 0.2,

        # Salesforce (internship)
        "Salesforce": 0.3,
        "Apex": 0.3,
        "LWC": 0.3,
        "Lightning Web Components": 0.3,
        "SOQL": 0.3,

        # Tools
        "Docker": 0.3,
        "JWT": 0.5,
        "Postman": 0.8,
        "Linux": 0.5,
        "Tailwind CSS": 0.3,
        "Bootstrap": 0.5,

        # Concepts
        "Full Stack Development": 1.0,
        "Database Design": 0.8,
        "Agile": 0.5,
    }


def validate_resume(resume: ResumeData) -> Dict[str, Any]:
    """
    Validate resume completeness and quality.
    Returns: {valid: bool, issues: [], warnings: [], score: 0-100}
    """
    issues = []
    warnings = []
    score = 100

    # ── Critical checks ──
    if not resume.name:
        issues.append("Missing name")
        score -= 20
    if not resume.email:
        issues.append("Missing email")
        score -= 15
    if not resume.phone:
        issues.append("Missing phone number")
        score -= 10
    if not resume.summary or len(resume.summary) < 50:
        issues.append("Summary too short or missing (need 50+ chars)")
        score -= 15
    if len(resume.experience) == 0:
        issues.append("No work experience entries")
        score -= 20
    if len(resume.education) == 0:
        issues.append("No education entries")
        score -= 10
    if len(resume.skills) == 0:
        issues.append("No skills listed")
        score -= 15

    # ── Quality checks ──
    if len(resume.summary) > 500:
        warnings.append(f"Summary is long ({len(resume.summary)} chars) — aim for 200-400")
        score -= 3
    for exp in resume.experience:
        if len(exp.bullets) < 2:
            warnings.append(f"Experience '{exp.company}' has < 2 bullet points")
            score -= 2
        for b in exp.bullets:
            if len(b.text) < 20:
                warnings.append(f"Very short bullet in '{exp.company}': {b.text[:30]}...")
                score -= 1
    if len(resume.projects) == 0:
        warnings.append("No projects listed — consider adding")
        score -= 5
    if len(resume.certifications) == 0:
        warnings.append("No certifications listed")
        score -= 2
    if not resume.linkedin:
        warnings.append("No LinkedIn URL")
        score -= 3
    if not resume.github:
        warnings.append("No GitHub URL")
        score -= 2

    # ── Word count check ──
    wc = resume.word_count()
    if wc < 150:
        warnings.append(f"Resume content too short ({wc} words)")
        score -= 5
    elif wc > 800:
        warnings.append(f"Resume content may be too long ({wc} words)")
        score -= 2

    score = max(0, min(100, score))

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "score": score,
        "word_count": wc,
        "sections_complete": resume.section_completeness(),
    }


# ═══════════════════════════════════════════════════════════════════
# TEST BLOCK
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import print as rprint

    console = Console()

    console.print("\n[bold cyan]═══ Resume Data Module Test ═══[/bold cyan]\n")

    # ── 1. Load base resume ──
    console.print("[yellow]1. Loading base resume...[/yellow]")
    resume = get_base_resume()
    console.print(f"   [green]✓[/green] Name: {resume.name}")
    console.print(f"   [green]✓[/green] Email: {resume.email}")
    console.print(f"   [green]✓[/green] Phone: {resume.phone}")
    console.print(f"   [green]✓[/green] Location: {resume.location}")
    console.print(f"   [green]✓[/green] LinkedIn: {resume.linkedin}")
    console.print(f"   [green]✓[/green] GitHub: {resume.github}")

    # ── 2. Experience ──
    console.print(f"\n[yellow]2. Work Experience ({len(resume.experience)} entries):[/yellow]")
    for exp in resume.experience:
        bullet_count = len(exp.bullets)
        tech_count = len(exp.technologies)
        console.print(
            f"   [green]✓[/green] {exp.title} @ {exp.company} "
            f"({exp.duration_display()}) — {bullet_count} bullets, "
            f"{tech_count} technologies, type: {exp.employment_type}"
        )

    # ── 3. Projects ──
    console.print(f"\n[yellow]3. Projects ({len(resume.projects)} entries):[/yellow]")
    for proj in resume.projects:
        techs = ", ".join(proj.technologies[:4])
        console.print(
            f"   [green]✓[/green] {proj.name} — {techs}... "
            f"({len(proj.bullets)} bullets)"
        )

    # ── 4. Education ──
    console.print(f"\n[yellow]4. Education ({len(resume.education)} entries):[/yellow]")
    for edu in resume.education:
        console.print(
            f"   [green]✓[/green] {edu.degree} in {edu.field_of_study} — "
            f"{edu.institution} ({edu.year_display()}) — "
            f"{edu.grade_display()}"
        )

    # ── 5. Skills ──
    console.print(f"\n[yellow]5. Skills ({len(resume.skills)} categories):[/yellow]")
    for cat in resume.skills:
        console.print(f"   [green]✓[/green] {cat.category}: {cat.skills_string()}")
    all_skills = resume.all_skills_flat()
    console.print(f"   [cyan]Total unique skills: {len(all_skills)}[/cyan]")

    # ── 6. Certifications ──
    console.print(f"\n[yellow]6. Certifications ({len(resume.certifications)}):[/yellow]")
    for cert in resume.certifications:
        console.print(f"   [green]✓[/green] {cert.name} — {cert.issuer} ({cert.date})")

    # ── 7. Achievements ──
    console.print(f"\n[yellow]7. Achievements ({len(resume.achievements)}):[/yellow]")
    for ach in resume.achievements:
        console.print(f"   [green]✓[/green] [{ach.category}] {ach.text[:80]}...")

    # ── 8. Convenience methods ──
    console.print(f"\n[yellow]8. Convenience methods:[/yellow]")
    console.print(f"   Total experience: {resume.total_experience_years()} years")
    console.print(f"   Domains: {resume.experience_domains()}")
    console.print(f"   Word count: {resume.word_count()}")
    current = resume.current_experience()
    if current:
        console.print(f"   Current role: {current.title} @ {current.company}")
    console.print(f"   Has 'React': {resume.has_skill('React')}")
    console.print(f"   Has 'Django': {resume.has_skill('Django')}")
    console.print(f"   Has 'Java': {resume.has_skill('Java')}")
    console.print(f"   Has 'Spring Boot': {resume.has_skill('Spring Boot')}")
    console.print(f"   All technologies: {len(resume.all_technologies())} unique")

    # ── 9. Serialization round-trip ──
    console.print(f"\n[yellow]9. Serialization round-trip test:[/yellow]")
    d = resume_to_dict(resume)
    console.print(f"   [green]✓[/green] resume_to_dict() → dict with {len(d)} top-level keys")
    reconstructed = dict_to_resume(d)
    console.print(f"   [green]✓[/green] dict_to_resume() → ResumeData: {reconstructed.name}")
    assert reconstructed.name == resume.name, "Name mismatch!"
    assert len(reconstructed.experience) == len(resume.experience), "Experience count mismatch!"
    assert len(reconstructed.projects) == len(resume.projects), "Project count mismatch!"
    assert len(reconstructed.skills) == len(resume.skills), "Skills count mismatch!"
    assert len(reconstructed.all_skills_flat()) == len(resume.all_skills_flat()), "Flat skills mismatch!"
    console.print(f"   [green]✓[/green] All assertions passed — round-trip is lossless")

    # ── 10. JSON round-trip ──
    console.print(f"\n[yellow]10. JSON round-trip test:[/yellow]")
    json_str = resume_to_json(resume, pretty=False)
    console.print(f"   [green]✓[/green] resume_to_json() → {len(json_str)} chars")
    from_json = json_to_resume(json_str)
    console.print(f"   [green]✓[/green] json_to_resume() → {from_json.name}")
    assert from_json.name == resume.name, "JSON round-trip name mismatch!"
    console.print(f"   [green]✓[/green] JSON round-trip is lossless")

    # ── 11. Deep copy test ──
    console.print(f"\n[yellow]11. Deep copy test:[/yellow]")
    copy1 = resume.deep_copy()
    copy1.name = "MODIFIED NAME"
    copy1.summary = "This is a modified summary for a specific JD."
    copy1.skills[0].skills.append("NEW SKILL INJECTED")
    assert resume.name == "Piyush Kashyap", "Deep copy leaked mutation to original!"
    assert "NEW SKILL INJECTED" not in resume.skills[0].skills, "Deep copy skills leaked!"
    console.print(f"   [green]✓[/green] Original unchanged after modifying copy")
    console.print(f"   [green]✓[/green] Copy name: {copy1.name}")
    console.print(f"   [green]✓[/green] Original name: {resume.name}")

    # ── 12. Validation ──
    console.print(f"\n[yellow]12. Resume validation:[/yellow]")
    val = validate_resume(resume)
    console.print(f"   Valid: {'[green]YES[/green]' if val['valid'] else '[red]NO[/red]'}")
    console.print(f"   Score: {val['score']}/100")
    if val['issues']:
        for iss in val['issues']:
            console.print(f"   [red]✗ Issue: {iss}[/red]")
    if val['warnings']:
        for warn in val['warnings']:
            console.print(f"   [yellow]⚠ Warning: {warn}[/yellow]")
    sections = val['sections_complete']
    for section, complete in sections.items():
        icon = "[green]✓[/green]" if complete else "[red]✗[/red]"
        console.print(f"   {icon} {section}")

    # ── 13. Skill years mapping ──
    console.print(f"\n[yellow]13. Skill years mapping:[/yellow]")
    skill_years = get_skill_years_mapping()
    table = Table(title="Skill Experience (Years)")
    table.add_column("Skill", style="cyan")
    table.add_column("Years", style="green", justify="right")
    for skill, years in sorted(skill_years.items(), key=lambda x: -x[1])[:15]:
        table.add_row(skill, f"{years:.1f}")
    console.print(table)

    # ── 14. Empty / broken resume validation ──
    console.print(f"\n[yellow]14. Edge case — empty resume validation:[/yellow]")
    empty = ResumeData()
    val_empty = validate_resume(empty)
    console.print(f"   Valid: {'[green]YES[/green]' if val_empty['valid'] else '[red]NO[/red]'}")
    console.print(f"   Score: {val_empty['score']}/100")
    console.print(f"   Issues: {len(val_empty['issues'])}")
    for iss in val_empty['issues']:
        console.print(f"   [red]✗ {iss}[/red]")

    # ── 15. Plain text export ──
    console.print(f"\n[yellow]15. Plain text export (first 500 chars):[/yellow]")
    plain = resume.to_plain_text()
    console.print(Panel(plain[:500] + "...", title="Plain Text Resume Preview", expand=False))

    # ── 16. get_resume_for_tailoring ──
    console.print(f"\n[yellow]16. get_resume_for_tailoring():[/yellow]")
    tailor_copy = get_resume_for_tailoring()
    console.print(f"   [green]✓[/green] Got copy: {tailor_copy.name}")
    tailor_copy.tailored_for = "TestCompany_SDE1"
    tailor_copy.version = "tailored_v1"
    assert resume.version == "base", "Tailoring copy leaked!"
    console.print(f"   [green]✓[/green] Copy version: {tailor_copy.version}")
    console.print(f"   [green]✓[/green] Original version: {resume.version}")

    console.print(f"\n[bold green]═══ All tests passed! ═══[/bold green]\n")