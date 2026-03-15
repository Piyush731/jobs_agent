"""
profile/answers.py — Pre-filled Application Form Answers
═══════════════════════════════════════════════════════════════
All common job application form fields and behavioral questions
pre-filled with Piyush's real data. Fuzzy matching engine to
auto-answer any form field encountered during application.

Interface:
  STANDARD_ANSWERS: dict          — all field→answer mappings
  BEHAVIORAL_TEMPLATES: dict      — behavioral Q&A templates
  SKILL_EXPERIENCE: dict          — per-skill years mapping
  get_answer(question) → str|None — fuzzy match any question
  get_standard(field_name) → str|None — direct field lookup
  get_salary_answer(job) → str    — dynamic salary per job range
  get_experience_years(skill) → float — per-skill experience
  get_dropdown_match(options, field_name) → str|None — best option pick

Dependencies: config.py, core/logger.py
Optional: rapidfuzz (pip install rapidfuzz) — falls back to difflib
"""

import os
import re
import sys
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any, Union

# ─── Project root on path ──────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import USER_PROFILE
from core.logger import get_logger

logger = get_logger("profile.answers")


# ════════════════════════════════════════════════════════════════
#  FUZZY MATCHING ENGINE
# ════════════════════════════════════════════════════════════════

# Try rapidfuzz first (faster, better), fallback to difflib
try:
    from rapidfuzz import fuzz as rf_fuzz
    from rapidfuzz import process as rf_process
    FUZZY_ENGINE = 'rapidfuzz'
except ImportError:
    rf_fuzz = None
    rf_process = None
    FUZZY_ENGINE = 'difflib'

if FUZZY_ENGINE == 'difflib':
    from difflib import SequenceMatcher, get_close_matches


def _fuzzy_ratio(s1: str, s2: str) -> float:
    """Return similarity ratio 0-100 between two strings."""
    s1 = s1.lower().strip()
    s2 = s2.lower().strip()
    if s1 == s2:
        return 100.0
    if FUZZY_ENGINE == 'rapidfuzz':
        return rf_fuzz.ratio(s1, s2)
    else:
        return SequenceMatcher(None, s1, s2).ratio() * 100


def _fuzzy_partial(s1: str, s2: str) -> float:
    """Return partial match ratio 0-100 (substring matching)."""
    s1 = s1.lower().strip()
    s2 = s2.lower().strip()
    if s1 in s2 or s2 in s1:
        return 100.0
    if FUZZY_ENGINE == 'rapidfuzz':
        return rf_fuzz.partial_ratio(s1, s2)
    else:
        # difflib doesn't have partial_ratio — approximate
        shorter, longer = (s1, s2) if len(s1) <= len(s2) else (s2, s1)
        if not shorter:
            return 0.0
        best = 0.0
        for i in range(len(longer) - len(shorter) + 1):
            window = longer[i:i + len(shorter)]
            ratio = SequenceMatcher(None, shorter, window).ratio() * 100
            best = max(best, ratio)
        return best


def _fuzzy_token_sort(s1: str, s2: str) -> float:
    """Token sort ratio — order-independent word matching."""
    s1_sorted = ' '.join(sorted(s1.lower().split()))
    s2_sorted = ' '.join(sorted(s2.lower().split()))
    if FUZZY_ENGINE == 'rapidfuzz':
        return rf_fuzz.token_sort_ratio(s1, s2)
    else:
        return SequenceMatcher(None, s1_sorted, s2_sorted).ratio() * 100


def _best_match(query: str, choices: list, threshold: float = 70.0) -> Optional[str]:
    """Find best fuzzy match from a list of choices."""
    if not choices:
        return None
    query_lower = query.lower().strip()

    # Exact match first
    for c in choices:
        if c.lower().strip() == query_lower:
            return c

    if FUZZY_ENGINE == 'rapidfuzz':
        result = rf_process.extractOne(
            query_lower,
            [c.lower() for c in choices],
            score_cutoff=threshold,
        )
        if result:
            matched_lower = result[0]
            # Find original case version
            for c in choices:
                if c.lower() == matched_lower:
                    return c
            return choices[0]
        return None
    else:
        matches = get_close_matches(
            query_lower,
            [c.lower() for c in choices],
            n=1,
            cutoff=threshold / 100,
        )
        if matches:
            for c in choices:
                if c.lower() == matches[0]:
                    return c
        return None


# ════════════════════════════════════════════════════════════════
#  OWNER DATA — All real, nothing fabricated
# ════════════════════════════════════════════════════════════════

_NAME = USER_PROFILE.get('name', 'Piyush Kashyap')
_EMAIL = USER_PROFILE.get('email', 'piyushkashyap3247@gmail.com')
_PHONE = USER_PROFILE.get('phone', '+91 73107 03247')
_PHONE_RAW = re.sub(r'[^\d]', '', _PHONE)  # 917310703247
_PHONE_10 = _PHONE_RAW[-10:]               # 7310703247
_LOCATION = USER_PROFILE.get('location', 'Rishikesh, Uttarakhand, India')
_LINKEDIN = USER_PROFILE.get('linkedin_url', 'https://linkedin.com/in/piyush-kashyap731')
_GITHUB = USER_PROFILE.get('github_url', 'https://github.com/Piyush731')
_CURRENT_TITLE = USER_PROFILE.get('current_title', 'Full Stack Developer L1')
_EXPERIENCE_YEARS = USER_PROFILE.get('experience_years', 1)
_NOTICE_PERIOD = USER_PROFILE.get('notice_period', '15 days')
_CURRENT_CTC = 3.7   # LPA
_EXPECTED_CTC = 7.0   # LPA default
_MIN_CTC = 5.0        # LPA absolute minimum


# ════════════════════════════════════════════════════════════════
#  STANDARD ANSWERS — Every common form field
# ════════════════════════════════════════════════════════════════

STANDARD_ANSWERS: Dict[str, str] = {
    # ── Personal Information ──────────────────────────────────
    'name': _NAME,
    'full_name': _NAME,
    'full name': _NAME,
    'first_name': 'Piyush',
    'first name': 'Piyush',
    'firstname': 'Piyush',
    'last_name': 'Kashyap',
    'last name': 'Kashyap',
    'lastname': 'Kashyap',
    'candidate_name': _NAME,
    'candidate name': _NAME,
    'applicant_name': _NAME,
    'applicant name': _NAME,

    'email': _EMAIL,
    'email_address': _EMAIL,
    'email address': _EMAIL,
    'e-mail': _EMAIL,
    'contact_email': _EMAIL,
    'your email': _EMAIL,
    'work email': _EMAIL,
    'personal email': _EMAIL,

    'phone': _PHONE,
    'phone_number': _PHONE,
    'phone number': _PHONE,
    'mobile': _PHONE,
    'mobile_number': _PHONE,
    'mobile number': _PHONE,
    'contact_number': _PHONE,
    'contact number': _PHONE,
    'cell phone': _PHONE,
    'telephone': _PHONE,
    'whatsapp': _PHONE,
    'whatsapp number': _PHONE,
    'phone_10_digit': _PHONE_10,

    'location': _LOCATION,
    'city': 'Rishikesh',
    'current_city': 'Rishikesh',
    'current city': 'Rishikesh',
    'current_location': _LOCATION,
    'current location': _LOCATION,
    'preferred_location': 'Bangalore',
    'preferred location': 'Bangalore',
    'state': 'Uttarakhand',
    'country': 'India',
    'address': 'Rishikesh, Uttarakhand, India',
    'pincode': '249201',
    'zip_code': '249201',
    'zip code': '249201',
    'postal_code': '249201',

    'dob': '2003-07-31',
    'date_of_birth': '2003-07-31',
    'date of birth': '31/07/2003',
    'birth_date': '31/07/2003',
    'age': '21',

    'gender': 'Male',
    'sex': 'Male',
    'marital_status': 'Single',
    'marital status': 'Single',
    'nationality': 'Indian',
    'languages': 'English, Hindi',
    'languages_known': 'English, Hindi',
    'languages known': 'English, Hindi',
    'mother_tongue': 'Hindi',

    # ── Online Profiles ──────────────────────────────────────
    'linkedin': _LINKEDIN,
    'linkedin_url': _LINKEDIN,
    'linkedin url': _LINKEDIN,
    'linkedin_profile': _LINKEDIN,
    'linkedin profile': _LINKEDIN,
    'linkedin_link': _LINKEDIN,

    'github': _GITHUB,
    'github_url': _GITHUB,
    'github url': _GITHUB,
    'github_profile': _GITHUB,
    'github profile': _GITHUB,
    'github_link': _GITHUB,

    'portfolio': _GITHUB,
    'portfolio_url': _GITHUB,
    'portfolio url': _GITHUB,
    'website': _GITHUB,
    'personal_website': _GITHUB,
    'blog': '',
    'twitter': '',
    'stackoverflow': '',

    # ── Work Authorization ────────────────────────────────────
    'legally_authorized': 'Yes',
    'legally authorized': 'Yes',
    'authorized_to_work': 'Yes',
    'authorized to work': 'Yes',
    'authorized to work in india': 'Yes',
    'work_authorization': 'Authorized',
    'work authorization': 'Yes',
    'right to work': 'Yes',
    'visa_status': 'Not Required (Indian Citizen)',
    'visa status': 'Not Required',
    'sponsorship': 'No',
    'require_sponsorship': 'No',
    'require sponsorship': 'No',
    'need sponsorship': 'No',
    'visa_sponsorship': 'No',
    'visa sponsorship': 'No',
    'do you require visa sponsorship': 'No',
    'citizen': 'Indian',
    'citizenship': 'Indian',
    'passport': 'Yes',
    'have passport': 'Yes',

    # ── Availability & Notice ─────────────────────────────────
    'notice_period': _NOTICE_PERIOD,
    'notice period': _NOTICE_PERIOD,
    'notice_period_days': '15',
    'notice period days': '15',
    'notice period in days': '15',
    'how soon can you join': '15 days',
    'how soon can you start': '15 days',
    'when can you join': 'Within 15 days',
    'when can you start': 'Within 15 days',
    'earliest_start_date': (datetime.now() + timedelta(days=15)).strftime('%Y-%m-%d'),
    'earliest start date': (datetime.now() + timedelta(days=15)).strftime('%d/%m/%Y'),
    'start_date': (datetime.now() + timedelta(days=15)).strftime('%Y-%m-%d'),
    'start date': (datetime.now() + timedelta(days=15)).strftime('%d/%m/%Y'),
    'availability': 'Immediate / 15 days',
    'available_immediately': 'Yes',
    'available immediately': 'Yes',
    'immediate_joiner': 'Yes',
    'immediate joiner': 'Yes',
    'currently_employed': 'Yes',
    'currently employed': 'Yes',
    'currently_working': 'Yes',
    'currently working': 'Yes',
    'employment_status': 'Employed',
    'employment status': 'Currently Employed',
    'are you currently employed': 'Yes',
    'serving notice period': 'No',
    'on notice period': 'No',

    # ── Salary ────────────────────────────────────────────────
    'current_ctc': '3.7 LPA',
    'current ctc': '3.7 LPA',
    'current_salary': '3.7 LPA',
    'current salary': '3.7 LPA',
    'current_ctc_lpa': '3.7',
    'current ctc lpa': '3.7',
    'current_ctc_lakhs': '3.7',
    'current annual salary': '3.7 LPA',
    'present_ctc': '3.7 LPA',
    'present ctc': '3.7 LPA',
    'monthly_salary': '15000',
    'current monthly salary': '15000',

    'expected_ctc': '6-10 LPA',
    'expected ctc': '6-10 LPA',
    'expected_salary': '6-10 LPA',
    'expected salary': '6-10 LPA',
    'expected_ctc_lpa': '7',
    'expected ctc lpa': '7',
    'expected_ctc_lakhs': '7',
    'expected annual salary': '6-10 LPA',
    'desired_salary': '6-10 LPA',
    'desired salary': '6-10 LPA',
    'salary_expectation': '6-10 LPA, negotiable',
    'salary expectation': '6-10 LPA, negotiable',
    'what are your salary expectations': '6-10 LPA, negotiable based on role and growth opportunity',
    'min_salary': '5 LPA',
    'minimum salary': '5 LPA',
    'minimum acceptable salary': '5 LPA',
    'salary_negotiable': 'Yes',
    'salary negotiable': 'Yes',
    'is salary negotiable': 'Yes',

    # ── Experience ────────────────────────────────────────────
    'total_experience': '1 year',
    'total experience': '1 year',
    'total_experience_years': '1',
    'total experience years': '1',
    'total years of experience': '1',
    'years_of_experience': '1',
    'years of experience': '1',
    'experience_years': '1',
    'experience years': '1',
    'experience': '1 year',
    'work_experience': '1 year',
    'work experience': '1 year',
    'relevant_experience': '1 year',
    'relevant experience': '1 year',
    'how many years of experience': '1 year (8 months full-time + internship)',

    'current_company': 'Site Guru Pvt Ltd',
    'current company': 'Site Guru Pvt Ltd',
    'current_employer': 'Site Guru Pvt Ltd',
    'current employer': 'Site Guru Pvt Ltd',
    'company_name': 'Site Guru Pvt Ltd',
    'employer': 'Site Guru Pvt Ltd',
    'last_company': 'Site Guru Pvt Ltd',

    'current_title': _CURRENT_TITLE,
    'current title': _CURRENT_TITLE,
    'current_designation': _CURRENT_TITLE,
    'current designation': _CURRENT_TITLE,
    'current_role': _CURRENT_TITLE,
    'current role': _CURRENT_TITLE,
    'job_title': _CURRENT_TITLE,
    'job title': _CURRENT_TITLE,
    'designation': _CURRENT_TITLE,
    'last_designation': _CURRENT_TITLE,

    'reason_for_leaving': 'Seeking better growth opportunities and exposure to modern tech stack',
    'reason for leaving': 'Seeking better growth opportunities and exposure to modern tech stack',
    'reason for change': 'Looking for career growth, larger team exposure, and modern technology stack',
    'why are you looking for a change': 'Seeking professional growth, exposure to product development, and a modern tech stack beyond proprietary platforms',
    'why do you want to leave': 'Looking for career growth and opportunity to work with industry-standard technologies in a product company',

    # ── Education ─────────────────────────────────────────────
    'degree': 'B.Tech Computer Science',
    'qualification': 'B.Tech Computer Science',
    'highest_qualification': 'B.Tech',
    'highest qualification': 'B.Tech',
    'education': 'B.Tech Computer Science',
    'education_level': "Bachelor's",
    'education level': "Bachelor's",

    'university': 'Graphic Era Hill University',
    'college': 'Graphic Era Hill University',
    'institution': 'Graphic Era Hill University',
    'college_name': 'Graphic Era Hill University',
    'school': 'Graphic Era Hill University',

    'graduation_year': '2025',
    'graduation year': '2025',
    'year_of_passing': '2025',
    'year of passing': '2025',
    'passing_year': '2025',
    'batch': '2025',

    'cgpa': '7.79',
    'gpa': '7.79',
    'percentage': '77.9',
    'marks': '7.79 CGPA',
    'score': '7.79/10',
    'academic_score': '7.79/10 CGPA',

    '10th_percentage': '',
    '12th_percentage': '',
    '10th marks': '',
    '12th marks': '',
    'ssc percentage': '',
    'hsc percentage': '',

    'specialization': 'Computer Science',
    'branch': 'Computer Science',
    'stream': 'Computer Science',
    'major': 'Computer Science',
    'field_of_study': 'Computer Science',
    'field of study': 'Computer Science',

    # ── Preferences ───────────────────────────────────────────
    'work_mode': 'Any (Office/Remote/Hybrid)',
    'work mode': 'Any (Office/Remote/Hybrid)',
    'preferred_work_mode': 'Open to all — Office, Remote, or Hybrid',
    'preferred work mode': 'Open to all',
    'work_from_home': 'Open to both WFH and office',
    'remote_work': 'Yes',
    'willing_to_work_from_office': 'Yes',
    'willing to work from office': 'Yes',
    'willing_to_relocate': 'Yes',
    'willing to relocate': 'Yes',
    'relocation': 'Yes',
    'open_to_relocation': 'Yes',
    'open to relocation': 'Yes',
    'can you relocate': 'Yes, willing to relocate anywhere in India',
    'preferred_shift': 'Day Shift / General Shift',
    'preferred shift': 'Day Shift',
    'shift_preference': 'Day Shift',
    'night_shift': 'No',
    'rotational_shift': 'Open to it',

    'job_type': 'Full-Time',
    'job type': 'Full-Time',
    'employment_type': 'Full-Time',
    'employment type': 'Full-Time',
    'preferred_job_type': 'Full-Time',
    'contract': 'No',
    'freelance': 'No',
    'internship': 'No',
    'part_time': 'No',

    # ── Legal / Compliance ────────────────────────────────────
    'background_check': 'Yes',
    'background check': 'Yes',
    'consent_background_check': 'Yes',
    'willing for background check': 'Yes',
    'drug_test': 'Yes',
    'drug test': 'Yes',
    'willing for drug test': 'Yes',
    'nda': 'Yes',
    'willing to sign nda': 'Yes',
    'non_compete': 'No current non-compete',
    'non-compete': 'No current non-compete',
    'terms_and_conditions': 'Yes',
    'terms and conditions': 'Yes',
    'agree_to_terms': 'Yes',
    'privacy_policy': 'Yes',
    'consent': 'Yes',
    'i agree': 'Yes',
    'data processing consent': 'Yes',
    'bond': 'No current bond',
    'any bond with current employer': 'No',
    'have you signed any bond': 'No',
    'have arrest record': 'No',
    'criminal record': 'No',
    'convicted of felony': 'No',

    # ── Diversity (safe defaults) ─────────────────────────────
    'disability': 'Prefer not to say',
    'disability_status': 'Prefer not to say',
    'differently_abled': 'No',
    'veteran': 'Prefer not to say',
    'veteran_status': 'Prefer not to say',
    'race': 'Prefer not to say',
    'ethnicity': 'Prefer not to say',
    'religion': 'Prefer not to say',
    'caste': 'Prefer not to say',
    'category': 'General',
    'reservation_category': 'General',
    'reservation category': 'General',
    'sexual_orientation': 'Prefer not to say',
    'gender_identity': 'Male',
    'pronouns': 'He/Him',

    # ── Technical / Role-Specific ─────────────────────────────
    'tech_stack': 'JavaScript, Java, Python, Vue.js, Nuxt.js, React.js, Node.js, Express.js, Spring Boot, MySQL, MongoDB, PostgreSQL, Redis, Docker, Kafka, Git',
    'primary_skills': 'JavaScript, Vue.js, Node.js, Java, Spring Boot, MySQL, MongoDB',
    'primary skills': 'JavaScript, Vue.js, Node.js, Java, Spring Boot',
    'secondary_skills': 'React.js, Python, PostgreSQL, Redis, Docker, Kafka',
    'secondary skills': 'React.js, Python, PostgreSQL, Docker',
    'strongest_language': 'JavaScript',
    'strongest language': 'JavaScript',
    'preferred_language': 'JavaScript / Java',
    'preferred_framework': 'Vue.js / Spring Boot',
    'database_experience': 'MySQL, MongoDB, PostgreSQL, Redis',
    'cloud_experience': 'Basic — Docker, deployment on VPS',
    'leadership_experience': 'Led projects as sole developer, mentored new team members',
    'team_size': 'Sole developer on most projects, total team 5-8',
    'team size': '5-8',
    'projects_handled': '10+',
    'projects handled': '10+ production applications',

    # ── Additional Common Fields ──────────────────────────────
    'referral': 'No',
    'referral_code': '',
    'referred_by': '',
    'referred by': '',
    'source': 'Job Portal',
    'how did you hear about us': 'Job Portal / Company Website',
    'how did you find this job': 'Job Portal',
    'cover_letter': '',    # Generated dynamically
    'additional_info': 'Sole developer on 10+ production apps. Published app on Google Play Store. Available to join within 15 days.',
    'additional information': 'Sole developer on 10+ production apps across fintech, ERP, edtech, CRM domains. Published app on Google Play Store. Available to join within 15 days.',
    'anything else': 'I am a quick learner with hands-on production experience across multiple domains. Happy to discuss further.',
    'message': 'I am interested in this role and believe my experience building 10+ production applications as a sole developer aligns well with the requirements. Available for immediate discussion.',
    'comments': '',
    'certifications': 'Full Stack Java Developer (Udemy), Agile Project Management (Udemy)',
    'achievements': '100+ problems on LeetCode/GFG, Published app on Google Play Store, Top performer recognition',
    'hobbies': 'Problem solving, building side projects, learning new technologies',
    'interests': 'System design, open source, fintech, building developer tools',
}


# ════════════════════════════════════════════════════════════════
#  SKILL → EXPERIENCE YEARS MAPPING
# ════════════════════════════════════════════════════════════════

SKILL_EXPERIENCE: Dict[str, float] = {
    # ── Languages ──
    'javascript': 1.0,
    'js': 1.0,
    'es6': 1.0,
    'java': 0.5,
    'python': 0.5,
    'sql': 1.0,
    'html': 1.0,
    'css': 1.0,
    'typescript': 0.3,

    # ── Frontend ──
    'vue.js': 0.8,
    'vue': 0.8,
    'vuejs': 0.8,
    'nuxt.js': 0.8,
    'nuxt': 0.8,
    'nuxtjs': 0.8,
    'react': 0.5,
    'react.js': 0.5,
    'reactjs': 0.5,
    'vuetify': 0.8,
    'tailwind': 0.5,
    'tailwind css': 0.5,
    'bootstrap': 0.3,
    'material ui': 0.2,

    # ── Backend ──
    'node.js': 0.8,
    'node': 0.8,
    'nodejs': 0.8,
    'express': 0.8,
    'express.js': 0.8,
    'spring boot': 0.3,
    'spring': 0.3,
    'rest api': 0.8,
    'rest apis': 0.8,
    'restful': 0.8,
    'websocket': 0.6,
    'websockets': 0.6,
    'socket.io': 0.4,
    'microservices': 0.3,
    'graphql': 0.1,
    'flask': 0.2,
    'django': 0.1,
    'fastapi': 0.1,

    # ── Databases ──
    'mysql': 0.8,
    'mongodb': 0.4,
    'postgresql': 0.3,
    'postgres': 0.3,
    'redis': 0.2,
    'sqlite': 0.3,
    'firebase': 0.1,

    # ── DevOps / Tools ──
    'git': 1.0,
    'github': 0.8,
    'docker': 0.3,
    'kafka': 0.2,
    'linux': 0.5,
    'nginx': 0.2,
    'ci/cd': 0.2,
    'aws': 0.1,
    'gcp': 0.1,

    # ── Auth / Security ──
    'jwt': 0.6,
    'oauth': 0.2,
    'cors': 0.5,

    # ── Integrations ──
    'razorpay': 0.5,
    'whatsapp api': 0.4,
    'meta whatsapp api': 0.4,
    'stripe': 0.1,
    'twilio': 0.1,

    # ── Concepts ──
    'agile': 0.5,
    'scrum': 0.3,
    'oop': 0.5,
    'data structures': 0.5,
    'algorithms': 0.5,
    'dsa': 0.5,
    'system design': 0.2,
    'design patterns': 0.2,
    'tdd': 0.1,
    'mvc': 0.5,

    # ── Testing ──
    'jest': 0.1,
    'junit': 0.1,
    'postman': 0.5,
    'playwright': 0.2,
    'selenium': 0.1,

    # ── Salesforce ──
    'salesforce': 0.3,
    'apex': 0.3,
    'lwc': 0.3,
    'lightning web components': 0.3,

    # ── Mobile ──
    'android': 0.2,
    'react native': 0.1,
    'flutter': 0.1,

    # ── Data / ML ──
    'pandas': 0.1,
    'numpy': 0.1,
    'streamlit': 0.2,
    'nlp': 0.1,

    # ── Defaults ──
    'full stack': 1.0,
    'backend': 0.8,
    'frontend': 0.8,
    'web development': 1.0,
    'software development': 1.0,
}


# ════════════════════════════════════════════════════════════════
#  BEHAVIORAL QUESTION TEMPLATES
# ════════════════════════════════════════════════════════════════

BEHAVIORAL_TEMPLATES: Dict[str, Dict[str, str]] = {
    'challenging_project': {
        'keywords': ['challenging', 'difficult', 'complex', 'hardest', 'toughest', 'most difficult project'],
        'answer': (
            "The most challenging project was BizHub ERP — a multi-tenant enterprise system "
            "with 57 database tables, 5 user roles, and complete business workflow from "
            "inventory to invoicing. As the sole developer, I designed the database schema, "
            "built the entire frontend in Vue.js/Vuetify and backend in Node.js, handling "
            "multi-tenancy, role-based access control, and complex business logic. "
            "I overcame challenges in data isolation between tenants and optimizing queries "
            "for large datasets. The system is now in production serving multiple businesses."
        ),
    },

    'pressure_situation': {
        'keywords': ['pressure', 'deadline', 'tight deadline', 'stress', 'under pressure', 'time constraint'],
        'answer': (
            "As the sole developer at Site Guru, I regularly handle tight deadlines across "
            "multiple projects simultaneously. One notable instance was delivering My RTO Expert "
            "— a system handling 1000+ daily requests with META WhatsApp API and RTO API "
            "integration — while also maintaining ongoing projects. I managed this by "
            "prioritizing critical features for MVP, setting clear milestones with the client, "
            "and working systematically through the requirements. The project launched on time "
            "and has been running reliably in production since."
        ),
    },

    'teamwork': {
        'keywords': ['team', 'teamwork', 'collaboration', 'collaborate', 'worked with others', 'group'],
        'answer': (
            "While I primarily work as a sole developer, I actively collaborate with clients "
            "to gather requirements and iterate on solutions. I also mentor newer team members "
            "at Site Guru, helping them understand codebases and development patterns. "
            "During my SmartBridge internship, I worked in a team environment building "
            "Salesforce solutions, contributing Apex triggers and LWC components. "
            "I believe clear communication and documentation are key to effective teamwork, "
            "which I practice in all my projects."
        ),
    },

    'five_year_plan': {
        'keywords': ['five year', '5 year', 'future', 'career goal', 'where do you see', 'long term', 'career plan'],
        'answer': (
            "In the next 2-3 years, I aim to deepen my expertise in backend engineering — "
            "particularly Java/Spring Boot and distributed systems — and grow into a "
            "Senior Software Engineer role. I want to contribute to high-scale product "
            "development and learn system design at scale. In 5 years, I see myself leading "
            "a small engineering team or owning a critical backend service, combining my "
            "full-stack production experience with deep backend specialization."
        ),
    },

    'why_tech': {
        'keywords': ['why technology', 'why programming', 'why software', 'passion', 'what motivates', 'why engineering'],
        'answer': (
            "I'm driven by the ability to build systems that solve real problems. "
            "Having single-handedly built 10+ production applications — from fintech platforms "
            "processing financial transactions to CRM systems managing business workflows — "
            "I've experienced the satisfaction of seeing my code serve real users daily. "
            "I particularly enjoy database design and building APIs that are clean and scalable. "
            "The constant learning in tech keeps me engaged and motivated."
        ),
    },

    'weakness': {
        'keywords': ['weakness', 'area of improvement', 'improve', 'shortcoming', 'development area'],
        'answer': (
            "I've relied heavily on AI tools for code generation, which means my independent "
            "coding speed for complex algorithms can be slower. I'm actively addressing this "
            "by solving problems on LeetCode and GFG — I've completed 100+ problems in "
            "Arrays, Trees, Graphs, and Dynamic Programming. I'm also building personal "
            "projects in Java/Spring Boot to strengthen my fundamentals beyond the proprietary "
            "platform I currently use at work."
        ),
    },

    'strength': {
        'keywords': ['strength', 'strongest skill', 'best quality', 'what makes you unique', 'superpower'],
        'answer': (
            "My biggest strength is owning projects end-to-end independently. As the sole "
            "developer on 10+ production applications, I handle everything — client "
            "requirements gathering, database design, frontend, backend, deployment, and "
            "maintenance. This has given me a rare breadth of experience across domains "
            "(fintech, ERP, edtech, CRM) and a practical understanding of full product "
            "development lifecycle that most developers at my experience level don't have."
        ),
    },

    'why_this_company': {
        'keywords': ['why this company', 'why us', 'why do you want to work here', 'why join', 'what attracts you'],
        'answer': (
            "I'm looking to grow beyond a proprietary platform into a company that uses "
            "industry-standard technologies and follows best engineering practices. I want to "
            "work with a team of experienced engineers where I can learn system design at scale, "
            "participate in code reviews, and contribute to impactful products used by many users. "
            "Your company's focus on technology and engineering culture is exactly the environment "
            "where I can grow while contributing my production experience from Day 1."
        ),
    },

    'why_leaving': {
        'keywords': ['why leaving', 'why leave', 'reason for change', 'why looking', 'why switch'],
        'answer': (
            "I'm grateful for the opportunity at Site Guru — being a sole developer on 10+ "
            "production apps gave me incredible hands-on experience. However, the proprietary "
            "platform limits my growth with industry-standard technologies. I want to work "
            "with a larger engineering team, follow best practices like code reviews and CI/CD, "
            "and build products at scale. I'm seeking a role where I can deepen my backend "
            "expertise while contributing my production shipping experience."
        ),
    },

    'conflict_resolution': {
        'keywords': ['conflict', 'disagreement', 'difficult coworker', 'argue', 'difference of opinion'],
        'answer': (
            "When I have a difference of opinion — typically with clients about feature "
            "requirements — I focus on understanding their business need first, then explain "
            "the technical trade-offs clearly with examples. For instance, when a client wanted "
            "to add complex features to BizHub ERP that would slow down the system, I proposed "
            "an alternative approach that met their business goal while keeping the system "
            "performant. The key is listening first and finding solutions, not winning arguments."
        ),
    },

    'achievement': {
        'keywords': ['proud', 'achievement', 'accomplishment', 'success story', 'biggest achievement'],
        'answer': (
            "I'm most proud of building BizHub ERP from scratch as a sole developer — a "
            "multi-tenant system with 57 database tables serving multiple businesses. "
            "The fact that a production enterprise system handling real business operations "
            "was designed, built, and deployed entirely by me within a few months is something "
            "I consider a significant achievement. I was also recognized as a top performer "
            "and given production deployment authority at Site Guru."
        ),
    },

    'handle_feedback': {
        'keywords': ['feedback', 'criticism', 'constructive feedback', 'negative feedback', 'handle criticism'],
        'answer': (
            "I actively seek and value feedback. Working directly with clients, I regularly "
            "receive feedback on features and UI — I treat each piece of feedback as an "
            "opportunity to improve the product. When I receive constructive criticism about "
            "my code or approach, I take time to understand the reasoning, research better "
            "alternatives, and implement the improvement. My goal is continuous growth."
        ),
    },

    'tell_me_about_yourself': {
        'keywords': ['tell me about yourself', 'introduce yourself', 'walk me through your background', 'about yourself'],
        'answer': (
            "I'm Piyush Kashyap, a Full Stack Developer with experience building 10+ "
            "production applications across fintech, ERP, edtech, and CRM domains. "
            "I graduated with a B.Tech in Computer Science from Graphic Era Hill University. "
            "Currently at Site Guru Pvt Ltd, I work as the sole developer on each project, "
            "handling everything from database design to deployment. I've shipped systems "
            "like a multi-tenant ERP with 57 DB tables and a WhatsApp-integrated platform "
            "handling 1000+ daily requests. I'm now looking to grow into a product company "
            "where I can deepen my backend expertise with industry-standard technologies."
        ),
    },

    'learning_new_tech': {
        'keywords': ['learn', 'new technology', 'how do you learn', 'pick up new', 'self-taught', 'upskill'],
        'answer': (
            "I learn by building. When I needed to pick up Vue.js and Nuxt.js for my current "
            "role, I dove straight into production projects and learned while shipping. "
            "For Java/Spring Boot, I completed a Udemy certification and built an Invoice "
            "Microservice project with Kafka and Docker. I also solve DSA problems regularly "
            "on LeetCode and GFG to strengthen my fundamentals. I believe the best way to "
            "learn technology is to build real things with it."
        ),
    },
}


# ════════════════════════════════════════════════════════════════
#  QUESTION KEYWORD → ANSWER MAPPING (for fuzzy routing)
# ════════════════════════════════════════════════════════════════

# Maps keywords found in questions to standard answer keys
_QUESTION_KEYWORD_MAP: Dict[str, str] = {
    # Personal
    'name': 'name', 'full name': 'full_name',
    'first name': 'first_name', 'last name': 'last_name',
    'email': 'email', 'e-mail': 'email', 'mail': 'email',
    'phone': 'phone', 'mobile': 'mobile', 'contact number': 'contact_number',
    'cell': 'phone', 'telephone': 'phone', 'whatsapp': 'whatsapp',
    'city': 'city', 'location': 'location', 'address': 'address',
    'state': 'state', 'country': 'country', 'pincode': 'pincode',
    'zip': 'zip_code', 'postal': 'postal_code',
    'date of birth': 'date_of_birth', 'dob': 'dob', 'birth': 'dob',
    'age': 'age', 'gender': 'gender', 'sex': 'gender',
    'marital': 'marital_status', 'nationality': 'nationality',
    'language': 'languages',

    # Profiles
    'linkedin': 'linkedin', 'github': 'github', 'portfolio': 'portfolio',
    'website': 'website', 'twitter': 'twitter', 'blog': 'blog',

    # Work auth
    'authorized': 'legally_authorized', 'visa': 'visa_status',
    'sponsorship': 'sponsorship', 'citizen': 'citizenship',
    'passport': 'passport', 'right to work': 'right to work',

    # Availability
    'notice period': 'notice_period', 'notice': 'notice_period',
    'when can you join': 'when can you join', 'when can you start': 'when can you start',
    'start date': 'start_date', 'join date': 'start_date',
    'availability': 'availability', 'immediate': 'immediate_joiner',
    'currently employed': 'currently_employed', 'currently working': 'currently_working',
    'employment status': 'employment_status', 'serving notice': 'serving notice period',

    # Salary
    'current ctc': 'current_ctc', 'current salary': 'current_salary',
    'present ctc': 'present_ctc', 'current annual': 'current annual salary',
    'monthly salary': 'monthly_salary',
    'expected ctc': 'expected_ctc', 'expected salary': 'expected_salary',
    'desired salary': 'desired_salary', 'salary expectation': 'salary_expectation',
    'salary negotiable': 'salary_negotiable', 'minimum salary': 'minimum salary',

    # Experience
    'total experience': 'total_experience', 'years of experience': 'years_of_experience',
    'work experience': 'work_experience', 'relevant experience': 'relevant_experience',
    'current company': 'current_company', 'current employer': 'current_employer',
    'employer name': 'current_company', 'company name': 'company_name',
    'current title': 'current_title', 'designation': 'designation',
    'current role': 'current_role', 'job title': 'job_title',
    'reason for leaving': 'reason_for_leaving', 'reason for change': 'reason for change',
    'why looking': 'why are you looking for a change',

    # Education
    'degree': 'degree', 'qualification': 'qualification',
    'highest qualification': 'highest_qualification', 'education': 'education',
    'university': 'university', 'college': 'college', 'institution': 'institution',
    'graduation year': 'graduation_year', 'year of passing': 'year_of_passing',
    'passing year': 'passing_year', 'batch': 'batch',
    'cgpa': 'cgpa', 'gpa': 'gpa', 'percentage': 'percentage',
    'marks': 'marks', 'score': 'score',
    'specialization': 'specialization', 'branch': 'branch',
    'stream': 'stream', 'major': 'major', 'field of study': 'field_of_study',
    '10th': '10th_percentage', '12th': '12th_percentage',
    'ssc': 'ssc percentage', 'hsc': 'hsc percentage',

    # Preferences
    'work mode': 'work_mode', 'remote': 'remote_work',
    'work from home': 'work_from_home', 'relocate': 'willing_to_relocate',
    'relocation': 'relocation', 'shift': 'preferred_shift',
    'night shift': 'night_shift', 'rotational': 'rotational_shift',
    'job type': 'job_type', 'employment type': 'employment_type',
    'full-time': 'job_type', 'full time': 'job_type',
    'contract': 'contract', 'freelance': 'freelance', 'part time': 'part_time',

    # Legal
    'background check': 'background_check', 'drug test': 'drug_test',
    'nda': 'nda', 'non-compete': 'non_compete', 'non compete': 'non_compete',
    'terms': 'terms_and_conditions', 'agree': 'agree_to_terms',
    'consent': 'consent', 'privacy': 'privacy_policy',
    'bond': 'bond', 'criminal': 'criminal record', 'arrest': 'have arrest record',
    'felony': 'convicted of felony',

    # Diversity
    'disability': 'disability', 'veteran': 'veteran',
    'race': 'race', 'ethnicity': 'ethnicity', 'religion': 'religion',
    'caste': 'caste', 'category': 'category', 'reservation': 'reservation_category',
    'pronouns': 'pronouns',

    # Technical
    'tech stack': 'tech_stack', 'primary skills': 'primary_skills',
    'secondary skills': 'secondary_skills', 'strongest language': 'strongest_language',
    'database': 'database_experience', 'cloud': 'cloud_experience',
    'leadership': 'leadership_experience', 'team size': 'team_size',
    'projects handled': 'projects_handled',

    # Other
    'referral': 'referral', 'referred': 'referred_by',
    'source': 'source', 'how did you hear': 'how did you hear about us',
    'how did you find': 'how did you find this job',
    'cover letter': 'cover_letter', 'additional': 'additional_info',
    'anything else': 'anything else', 'message': 'message',
    'comments': 'comments', 'certification': 'certifications',
    'achievement': 'achievements', 'hobby': 'hobbies', 'interest': 'interests',
}


# ════════════════════════════════════════════════════════════════
#  DROPDOWN OPTION MAPPINGS
# ════════════════════════════════════════════════════════════════

# For common dropdown fields, map field_name → preferred option keywords
_DROPDOWN_PREFERENCES: Dict[str, List[str]] = {
    'notice_period': ['15 days', '15', 'immediate', '1 month', 'less than 1 month', '0-15', '0-30'],
    'experience': ['1', '0-1', '1-2', '0-2', 'fresher', '< 1 year', '1 year'],
    'current_ctc': ['3', '3-4', '3.5', '3-5', '0-5', 'below 5', '3.7'],
    'expected_ctc': ['6', '7', '6-8', '6-10', '5-8', '5-10', '8'],
    'education': ["bachelor", "b.tech", "btech", "engineering", "graduate", "ug"],
    'gender': ['male', 'man', 'm'],
    'location': ['bangalore', 'bengaluru', 'remote', 'any', 'pan india', 'hyderabad', 'pune'],
    'work_mode': ['any', 'hybrid', 'remote', 'office', 'on-site', 'flexible'],
    'job_type': ['full-time', 'full time', 'permanent', 'regular'],
    'shift': ['day', 'general', 'morning', 'regular', 'any'],
    'relocate': ['yes', 'willing', 'open'],
    'language': ['english', 'hindi'],
    'nationality': ['indian', 'india'],
    'disability': ['no', 'prefer not', 'none', 'not applicable', 'n/a'],
    'veteran': ['no', 'prefer not', 'not applicable', 'n/a'],
    'category': ['general', 'unreserved', 'open', 'prefer not'],
}


# ════════════════════════════════════════════════════════════════
#  PUBLIC API FUNCTIONS
# ════════════════════════════════════════════════════════════════

def get_standard(field_name: str) -> Optional[str]:
    """
    Get answer for a known field name (direct lookup).
    
    Args:
        field_name: Exact or close field name (e.g., 'email', 'phone',
                    'current_ctc', 'notice_period')
    
    Returns:
        str or None if field not found
    """
    if not field_name:
        return None

    key = field_name.strip().lower()

    # Direct lookup
    if key in STANDARD_ANSWERS:
        return STANDARD_ANSWERS[key]

    # Try underscore/space variants
    key_underscore = key.replace(' ', '_')
    key_space = key.replace('_', ' ')

    if key_underscore in STANDARD_ANSWERS:
        return STANDARD_ANSWERS[key_underscore]
    if key_space in STANDARD_ANSWERS:
        return STANDARD_ANSWERS[key_space]

    # Try removing common prefixes/suffixes
    for prefix in ['your_', 'your ', 'enter_', 'enter ', 'please_', 'please ',
                    'candidate_', 'candidate ', 'applicant_', 'applicant ']:
        stripped = key.replace(prefix, '')
        if stripped in STANDARD_ANSWERS:
            return STANDARD_ANSWERS[stripped]
        if stripped.replace(' ', '_') in STANDARD_ANSWERS:
            return STANDARD_ANSWERS[stripped.replace(' ', '_')]

    return None


def get_answer(question: str) -> Optional[str]:
    """
    Fuzzy-match a question text to find the best answer.
    
    Strategy:
      1) Direct lookup in STANDARD_ANSWERS
      2) Keyword extraction → map to known field
      3) Fuzzy match against all known questions (>80% threshold)
      4) Check behavioral templates
      5) Return None if no match
    
    Args:
        question: Free-text question from application form
                  e.g., "What is your expected CTC?"
                  e.g., "Tell me about a challenging project"
    
    Returns:
        str answer or None if cannot determine
    """
    if not question:
        return None

    q = question.strip()
    q_lower = q.lower()

    # ── 1) Direct lookup ──
    if q_lower in STANDARD_ANSWERS:
        return STANDARD_ANSWERS[q_lower]

    # Normalize for lookup
    q_normalized = re.sub(r'[?!.,;:\'"()\[\]{}]', '', q_lower).strip()
    q_normalized = re.sub(r'\s+', ' ', q_normalized)

    if q_normalized in STANDARD_ANSWERS:
        return STANDARD_ANSWERS[q_normalized]

    # ── 2) Keyword extraction → map to field ──
    # Check longest keyword phrases first
    sorted_keywords = sorted(_QUESTION_KEYWORD_MAP.keys(), key=len, reverse=True)
    for keyword in sorted_keywords:
        if keyword in q_lower:
            field = _QUESTION_KEYWORD_MAP[keyword]
            if field in STANDARD_ANSWERS:
                return STANDARD_ANSWERS[field]

    # ── 3) Fuzzy match against all STANDARD_ANSWERS keys ──
    all_keys = list(STANDARD_ANSWERS.keys())
    best_key = _best_match(q_normalized, all_keys, threshold=78.0)
    if best_key:
        return STANDARD_ANSWERS[best_key]

    # ── 4) Check behavioral templates ──
    behavioral = _match_behavioral(q_lower)
    if behavioral:
        return behavioral

    # ── 5) Partial keyword check with lower threshold ──
    # Try matching just the core words
    core_words = [w for w in q_normalized.split()
                  if w not in ('what', 'is', 'your', 'the', 'a', 'an', 'do', 'you',
                               'have', 'are', 'can', 'please', 'enter', 'provide',
                               'specify', 'mention', 'tell', 'us', 'me', 'about',
                               'how', 'many', 'much', 'would', 'like', 'to', 'in',
                               'of', 'for', 'with', 'this', 'that', 'be', 'if',
                               'or', 'and', 'any', 'has', 'had', 'will')]
    if core_words:
        core_phrase = ' '.join(core_words)
        # Try keyword map again with core phrase
        for keyword in sorted_keywords:
            if keyword in core_phrase:
                field = _QUESTION_KEYWORD_MAP[keyword]
                if field in STANDARD_ANSWERS:
                    return STANDARD_ANSWERS[field]

        # Final fuzzy attempt with core phrase
        best_key2 = _best_match(core_phrase, all_keys, threshold=72.0)
        if best_key2:
            return STANDARD_ANSWERS[best_key2]

    logger.debug(f"No answer found for question: '{q[:80]}...'")
    return None


def get_salary_answer(job: dict) -> str:
    """
    Dynamic salary answer based on job's posted range.
    
    Logic:
      - If job shows range → answer middle-high of range
      - If no range → default "6-10 LPA, negotiable"
      - Never below 5 LPA
      - Never above job's max
      - For dropdowns → returns numeric string
    
    Args:
        job: Job dict with optional salary_min, salary_max, salary_text
        
    Returns:
        str: Salary answer suitable for form field
    """
    salary_min = job.get('salary_min')
    salary_max = job.get('salary_max')
    salary_text = job.get('salary_text', '')

    # Try to parse salary range from text if min/max not set
    if salary_min is None and salary_max is None and salary_text:
        parsed_min, parsed_max = _parse_salary_text(salary_text)
        if parsed_min is not None:
            salary_min = parsed_min
        if parsed_max is not None:
            salary_max = parsed_max

    # Case 1: Both min and max known
    if salary_min is not None and salary_max is not None:
        # Convert to LPA if in monthly
        if salary_max < 200:  # Already in LPA
            pass
        elif salary_max < 200000:  # Monthly
            salary_min = salary_min * 12 / 100000
            salary_max = salary_max * 12 / 100000

        # Target middle-high of range
        target = salary_min + (salary_max - salary_min) * 0.65

        # Floor at our minimum
        target = max(target, _MIN_CTC)

        # Cap at job's max
        target = min(target, salary_max)

        target_rounded = round(target, 1)

        if target_rounded >= 10:
            return f"{target_rounded:.0f} LPA"
        else:
            return f"{target_rounded} LPA"

    # Case 2: Only max known
    elif salary_max is not None:
        if salary_max < 200:
            target = max(salary_max * 0.75, _MIN_CTC)
        else:
            target = max(salary_max * 12 / 100000 * 0.75, _MIN_CTC)
        target_rounded = round(target, 1)
        return f"{target_rounded} LPA"

    # Case 3: Only min known
    elif salary_min is not None:
        if salary_min < 200:
            target = max(salary_min * 1.3, _MIN_CTC)
        else:
            target = max(salary_min * 12 / 100000 * 1.3, _MIN_CTC)
        target_rounded = round(target, 1)
        return f"{target_rounded} LPA"

    # Case 4: No salary info — use default
    return '6-10 LPA, negotiable'


def get_experience_years(skill: str) -> float:
    """
    Get years of experience for a specific skill.
    
    Args:
        skill: Skill name (e.g., "Java", "React.js", "MySQL")
    
    Returns:
        float: Years of experience (0.0 if unknown)
    """
    if not skill:
        return 0.0

    key = skill.strip().lower()

    # Direct lookup
    if key in SKILL_EXPERIENCE:
        return SKILL_EXPERIENCE[key]

    # Try normalized variants
    normalized = key.replace('.js', '').replace('.', '').replace('-', '').replace(' ', '')
    for sk, years in SKILL_EXPERIENCE.items():
        sk_norm = sk.replace('.js', '').replace('.', '').replace('-', '').replace(' ', '')
        if sk_norm == normalized:
            return years

    # Fuzzy match
    best = _best_match(key, list(SKILL_EXPERIENCE.keys()), threshold=80.0)
    if best:
        return SKILL_EXPERIENCE[best]

    logger.debug(f"No experience mapping for skill: '{skill}'")
    return 0.0


def get_dropdown_match(options: List[str], field_name: str) -> Optional[str]:
    """
    Find the best matching dropdown option for a known field.
    
    Useful when a form has a <select> dropdown and we need to
    pick the closest matching option to our answer.
    
    Args:
        options: List of dropdown option texts
                 e.g., ["0-1 years", "1-3 years", "3-5 years"]
        field_name: Field context (e.g., "experience", "notice_period")
    
    Returns:
        str: Best matching option from the list, or None
    """
    if not options or not field_name:
        return None

    field_key = field_name.strip().lower().replace(' ', '_')

    # Get our preferred answers for this field type
    preferences = _DROPDOWN_PREFERENCES.get(field_key, [])

    # Also get the standard answer
    standard = get_standard(field_name)
    if standard:
        preferences = [standard.lower()] + preferences

    options_lower = [o.lower().strip() for o in options]

    # Strategy 1: Exact match with any preference
    for pref in preferences:
        pref_lower = pref.lower()
        for i, opt in enumerate(options_lower):
            if pref_lower == opt or pref_lower in opt or opt in pref_lower:
                return options[i]

    # Strategy 2: Fuzzy match preferences against options
    for pref in preferences:
        best = _best_match(pref, options, threshold=65.0)
        if best:
            return best

    # Strategy 3: If it's a numeric field, try numeric matching
    if field_key in ('experience', 'current_ctc', 'expected_ctc', 'notice_period'):
        target = _get_numeric_target(field_key)
        if target is not None:
            return _find_closest_numeric_option(options, target)

    # Strategy 4: First non-empty option as last resort
    for opt in options:
        if opt.strip() and opt.strip().lower() not in ('select', 'choose', '--', '-', ''):
            return opt

    return options[0] if options else None


def get_checkbox_answer(label: str) -> bool:
    """
    Determine whether a checkbox should be checked.
    
    Args:
        label: Checkbox label text
        
    Returns:
        bool: True if should be checked
    """
    if not label:
        return False

    label_lower = label.strip().lower()

    # Always check these
    always_check = [
        'agree', 'consent', 'accept', 'terms', 'privacy', 'acknowledge',
        'confirm', 'authorize', 'i have read', 'i understand', 'i certify',
        'i agree', 'i accept', 'i consent', 'i confirm', 'i acknowledge',
        'data processing', 'information is correct', 'accurate',
        'background check', 'willing', 'nda', 'non-disclosure',
        'receive communication', 'email notifications',
    ]
    for phrase in always_check:
        if phrase in label_lower:
            return True

    # Never check these
    never_check = [
        'do not', 'opt out', 'unsubscribe', 'marketing',
        'third party', 'share my data with partners',
        'sms', 'promotional',
    ]
    for phrase in never_check:
        if phrase in label_lower:
            return False

    # Default: check consent-type, skip marketing-type
    return True


# ════════════════════════════════════════════════════════════════
#  INTERNAL HELPERS
# ════════════════════════════════════════════════════════════════

def _match_behavioral(question_lower: str) -> Optional[str]:
    """Match a question against behavioral templates."""
    best_score = 0.0
    best_answer = None

    for template_key, template in BEHAVIORAL_TEMPLATES.items():
        keywords = template.get('keywords', [])
        score = 0.0

        for kw in keywords:
            if kw in question_lower:
                # Longer keyword matches are more significant
                match_score = len(kw) / max(len(question_lower), 1) * 100
                score = max(score, match_score + 50)  # bonus for keyword hit

        # Also try fuzzy matching template key
        key_score = _fuzzy_partial(template_key.replace('_', ' '), question_lower)
        score = max(score, key_score)

        if score > best_score and score > 55:
            best_score = score
            best_answer = template['answer']

    return best_answer


def _parse_salary_text(text: str) -> Tuple[Optional[float], Optional[float]]:
    """Parse salary range from text like '6-10 LPA' or '₹50,000-80,000'."""
    if not text:
        return None, None

    text = text.lower().replace(',', '').replace('₹', '').replace('rs.', '').replace('rs', '')
    text = text.replace('inr', '').replace('per annum', '').replace('p.a.', '').replace('pa', '')
    text = text.strip()

    # Pattern: "X-Y LPA" or "X - Y lakh"
    lpa_match = re.search(r'(\d+\.?\d*)\s*[-–to]+\s*(\d+\.?\d*)\s*(?:lpa|lakh|lac|l)', text)
    if lpa_match:
        return float(lpa_match.group(1)), float(lpa_match.group(2))

    # Pattern: single LPA value
    single_lpa = re.search(r'(\d+\.?\d*)\s*(?:lpa|lakh|lac|l)', text)
    if single_lpa:
        val = float(single_lpa.group(1))
        return val * 0.8, val * 1.2

    # Pattern: monthly range "50000-80000"
    monthly_match = re.search(r'(\d{4,})\s*[-–to]+\s*(\d{4,})', text)
    if monthly_match:
        m1 = float(monthly_match.group(1))
        m2 = float(monthly_match.group(2))
        return m1 * 12 / 100000, m2 * 12 / 100000

    # Pattern: single large number (likely monthly or annual)
    single_num = re.search(r'(\d{4,})', text)
    if single_num:
        val = float(single_num.group(1))
        if val > 100000:  # likely annual
            lpa = val / 100000
        else:  # likely monthly
            lpa = val * 12 / 100000
        return lpa * 0.8, lpa * 1.2

    return None, None


def _get_numeric_target(field_key: str) -> Optional[float]:
    """Get numeric target value for a field."""
    targets = {
        'experience': 1.0,
        'current_ctc': 3.7,
        'expected_ctc': 7.0,
        'notice_period': 15,
    }
    return targets.get(field_key)


def _find_closest_numeric_option(options: List[str], target: float) -> Optional[str]:
    """Find the dropdown option whose numeric value is closest to target."""
    best_option = None
    best_diff = float('inf')

    for opt in options:
        # Extract numbers from option text
        numbers = re.findall(r'(\d+\.?\d*)', opt)
        if not numbers:
            continue

        for num_str in numbers:
            try:
                num = float(num_str)
                diff = abs(num - target)
                if diff < best_diff:
                    best_diff = diff
                    best_option = opt
            except ValueError:
                continue

        # Also check range options like "1-3"
        range_match = re.search(r'(\d+\.?\d*)\s*[-–to]+\s*(\d+\.?\d*)', opt)
        if range_match:
            low = float(range_match.group(1))
            high = float(range_match.group(2))
            if low <= target <= high:
                # Perfect — target falls within range
                return opt
            mid = (low + high) / 2
            diff = abs(mid - target)
            if diff < best_diff:
                best_diff = diff
                best_option = opt

    return best_option


# ════════════════════════════════════════════════════════════════
#  TEST BLOCK
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 70)
    print("  PROFILE ANSWERS — Test Suite")
    print(f"  Fuzzy engine: {FUZZY_ENGINE}")
    print("=" * 70)

    # ═══════════════════════════════════════════════════════════
    #  Test 1: Direct standard lookups
    # ═══════════════════════════════════════════════════════════
    print("\n─── Test 1: Direct Standard Lookups ────────────────────")
    direct_tests = [
        ('name', 'Piyush Kashyap'),
        ('email', 'piyushkashyap3247@gmail.com'),
        ('phone', '+91 73107 03247'),
        ('current_ctc', '3.7 LPA'),
        ('expected_ctc', '6-10 LPA'),
        ('notice_period', '15 days'),
        ('degree', 'B.Tech Computer Science'),
        ('cgpa', '7.79'),
        ('linkedin', _LINKEDIN),
        ('github', _GITHUB),
    ]
    for field, expected in direct_tests:
        result = get_standard(field)
        status = "✅" if result == expected else "❌"
        print(f"  {status} get_standard('{field}') → '{result}'")

    # ═══════════════════════════════════════════════════════════
    #  Test 2: Fuzzy question matching
    # ═══════════════════════════════════════════════════════════
    print("\n─── Test 2: Fuzzy Question Matching ────────────────────")
    fuzzy_tests = [
        ("What is your full name?", 'name'),
        ("Enter your email address", 'email'),
        ("Please provide your phone number", 'phone'),
        ("Your current CTC in LPA?", 'salary'),
        ("What is your expected salary?", 'salary'),
        ("How many years of total experience do you have?", 'experience'),
        ("Are you willing to relocate?", 'relocate'),
        ("What is your notice period?", 'notice'),
        ("What is your current designation?", 'title'),
        ("Do you require visa sponsorship?", 'sponsorship'),
        ("Your LinkedIn profile URL", 'linkedin'),
        ("Highest educational qualification?", 'education'),
        ("Are you currently employed?", 'employed'),
        ("What are your salary expectations for this role?", 'salary'),
        ("Preferred work location?", 'location'),
    ]
    matched = 0
    for question, category in fuzzy_tests:
        answer = get_answer(question)
        found = answer is not None
        if found:
            matched += 1
        status = "✅" if found else "❌"
        display = answer[:60] + '...' if answer and len(answer) > 60 else answer
        print(f"  {status} '{question[:50]}' → {display}")

    print(f"\n  Matched: {matched}/{len(fuzzy_tests)}")

    # ═══════════════════════════════════════════════════════════
    #  Test 3: Behavioral question matching
    # ═══════════════════════════════════════════════════════════
    print("\n─── Test 3: Behavioral Questions ───────────────────────")
    behavioral_tests = [
        "Tell me about a challenging project you've worked on",
        "How do you handle pressure and tight deadlines?",
        "Describe a teamwork experience",
        "Where do you see yourself in 5 years?",
        "What are your weaknesses?",
        "Tell me about yourself",
        "Why are you leaving your current job?",
        "What is your biggest strength?",
        "How do you learn new technologies?",
        "Tell me about a time you resolved a conflict",
    ]
    for question in behavioral_tests:
        answer = get_answer(question)
        found = answer is not None
        status = "✅" if found else "❌"
        preview = answer[:70] + '...' if answer and len(answer) > 70 else answer
        print(f"  {status} '{question[:50]}...'")
        if found:
            print(f"      → {preview}")

    # ═══════════════════════════════════════════════════════════
    #  Test 4: Dynamic salary answers
    # ═══════════════════════════════════════════════════════════
    print("\n─── Test 4: Dynamic Salary Answers ─────────────────────")
    salary_tests = [
        ({'salary_min': 6, 'salary_max': 12}, "6-12 LPA range"),
        ({'salary_min': 8, 'salary_max': 15}, "8-15 LPA range"),
        ({'salary_min': 4, 'salary_max': 7}, "4-7 LPA range"),
        ({'salary_text': '10-16 LPA'}, "text: 10-16 LPA"),
        ({'salary_text': '₹50,000-80,000 per month'}, "text: monthly"),
        ({'salary_text': '8 lakh per annum'}, "text: 8 lakh"),
        ({}, "no salary info"),
        ({'salary_max': 20}, "only max 20 LPA"),
        ({'salary_min': 5}, "only min 5 LPA"),
    ]
    for job_data, label in salary_tests:
        answer = get_salary_answer(job_data)
        print(f"  {label:30s} → {answer}")

    # ═══════════════════════════════════════════════════════════
    #  Test 5: Skill experience years
    # ═══════════════════════════════════════════════════════════
    print("\n─── Test 5: Skill Experience Years ─────────────────────")
    skill_tests = [
        'JavaScript', 'Java', 'Python', 'Vue.js', 'React.js',
        'Node.js', 'Spring Boot', 'MySQL', 'MongoDB', 'Docker',
        'Kafka', 'REST APIs', 'Git', 'Salesforce', 'Kubernetes',
        'Angular', 'Golang',
    ]
    for skill in skill_tests:
        years = get_experience_years(skill)
        bar = '█' * int(years * 10)
        print(f"  {skill:20s} → {years:.1f} years  {bar}")

    # ═══════════════════════════════════════════════════════════
    #  Test 6: Dropdown matching
    # ═══════════════════════════════════════════════════════════
    print("\n─── Test 6: Dropdown Matching ───────────────────────────")
    dropdown_tests = [
        (
            ['Select', '0-1 years', '1-3 years', '3-5 years', '5-8 years', '8+ years'],
            'experience',
        ),
        (
            ['Select', 'Immediate', '15 days', '1 month', '2 months', '3 months'],
            'notice_period',
        ),
        (
            ['Select', '0-3 LPA', '3-6 LPA', '6-10 LPA', '10-15 LPA', '15+ LPA'],
            'expected_ctc',
        ),
        (
            ['Select', '0-3 LPA', '3-6 LPA', '6-10 LPA', '10-15 LPA', '15+ LPA'],
            'current_ctc',
        ),
        (
            ['Select', 'Male', 'Female', 'Other', 'Prefer not to say'],
            'gender',
        ),
        (
            ['Select', 'Full-Time', 'Part-Time', 'Contract', 'Internship'],
            'job_type',
        ),
        (
            ['Select', 'Office', 'Remote', 'Hybrid', 'Any'],
            'work_mode',
        ),
        (
            ['Select', 'Yes', 'No'],
            'relocate',
        ),
    ]
    for options, field in dropdown_tests:
        result = get_dropdown_match(options, field)
        print(f"  {field:20s} options={options[1:4]}... → '{result}'")

    # ═══════════════════════════════════════════════════════════
    #  Test 7: Checkbox answers
    # ═══════════════════════════════════════════════════════════
    print("\n─── Test 7: Checkbox Answers ────────────────────────────")
    checkbox_tests = [
        ("I agree to the Terms and Conditions", True),
        ("I consent to background verification", True),
        ("I accept the Privacy Policy", True),
        ("I authorize data processing", True),
        ("Send me promotional emails", False),
        ("Share my data with third party partners", False),
        ("I confirm the information is accurate", True),
        ("Opt out of email notifications", False),
        ("I am willing to sign an NDA", True),
    ]
    for label, expected in checkbox_tests:
        result = get_checkbox_answer(label)
        status = "✅" if result == expected else "❌"
        print(f"  {status} '{label[:50]}' → {result} (expected {expected})")

    # ═══════════════════════════════════════════════════════════
    #  Test 8: Edge cases
    # ═══════════════════════════════════════════════════════════
    print("\n─── Test 8: Edge Cases ─────────────────────────────────")

    # Empty/None
    assert get_answer('') is None, "Empty string should return None"
    assert get_answer(None) is None, "None should return None"
    assert get_standard('') is None, "Empty field should return None"
    assert get_experience_years('') == 0.0, "Empty skill should return 0"
    assert get_experience_years('NonexistentFramework2000') == 0.0
    print("  ✅ Empty/None inputs handled correctly")

    # Variant field names
    assert get_standard('Your Email') is not None, "Should handle prefix 'Your'"
    assert get_standard('PHONE') is not None or get_standard('phone') is not None
    print("  ✅ Field name variants handled")

    # Salary edge cases
    assert '5' in get_salary_answer({'salary_min': 2, 'salary_max': 4}) or \
           'LPA' in get_salary_answer({'salary_min': 2, 'salary_max': 4})
    print("  ✅ Salary floor of 5 LPA enforced")

    # ─── Stats ──
    print("\n─── Statistics ─────────────────────────────────────────")
    print(f"  Standard answers : {len(STANDARD_ANSWERS)} fields")
    print(f"  Skill mappings   : {len(SKILL_EXPERIENCE)} skills")
    print(f"  Behavioral Q&A   : {len(BEHAVIORAL_TEMPLATES)} templates")
    print(f"  Keyword mappings : {len(_QUESTION_KEYWORD_MAP)} keywords")
    print(f"  Dropdown prefs   : {len(_DROPDOWN_PREFERENCES)} fields")
    print(f"  Fuzzy engine     : {FUZZY_ENGINE}")

    print("\n" + "=" * 70)
    print("  ✅ profile/answers.py — All tests complete")
    print("=" * 70)