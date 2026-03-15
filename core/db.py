"""
core/db.py — SQLite database with WAL mode
Singleton pattern. All data flows through here.

Usage:
    from core.db import get_db
    db = get_db()
    db.save_job({...})
"""

import json
import sqlite3
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from config import DB_PATH
    from core.logger import get_logger
except ImportError:
    DB_PATH = Path("data/job_agent.db")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    import logging
    def get_logger(name):
        logging.basicConfig(level=logging.INFO)
        return logging.getLogger(name)

logger = get_logger("core.db")

# ═══════════════════════════════════════════════════════════
# SCHEMA
# ═══════════════════════════════════════════════════════════
SCHEMA_SQL = """
-- Jobs discovered from all platforms
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_job_id TEXT NOT NULL,
    url TEXT,
    title TEXT,
    company TEXT,
    location TEXT,
    salary_min REAL,
    salary_max REAL,
    currency TEXT DEFAULT 'INR',
    experience_min REAL,
    experience_max REAL,
    job_type TEXT,
    work_mode TEXT,
    description TEXT,
    skills TEXT,
    posted_date TEXT,
    discovered_at TEXT,
    match_score REAL DEFAULT 0,
    match_details TEXT,
    status TEXT DEFAULT 'new',
    priority INTEGER DEFAULT 0,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(platform, platform_job_id)
);

-- Applications submitted
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    platform TEXT,
    method TEXT,
    resume_version TEXT,
    cover_letter TEXT,
    tailoring_mode TEXT,
    applied_at TEXT,
    status TEXT DEFAULT 'submitted',
    response_date TEXT,
    response_notes TEXT,
    follow_up_count INTEGER DEFAULT 0,
    next_follow_up TEXT,
    interview_date TEXT,
    interview_notes TEXT,
    salary_offered REAL,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

-- HR / recruiter contacts
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT,
    name TEXT,
    title TEXT,
    email TEXT,
    linkedin_url TEXT,
    phone TEXT,
    source TEXT,
    verified INTEGER DEFAULT 0,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Emails sent (application + follow-up)
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER,
    contact_id INTEGER,
    email_type TEXT,
    to_email TEXT,
    subject TEXT,
    body TEXT,
    attachments TEXT,
    sent_at TEXT,
    status TEXT DEFAULT 'queued',
    reply_received INTEGER DEFAULT 0,
    reply_date TEXT,
    reply_content TEXT,
    error_message TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (application_id) REFERENCES applications(id),
    FOREIGN KEY (contact_id) REFERENCES contacts(id)
);

-- Platform session state
CREATE TABLE IF NOT EXISTS platform_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT UNIQUE NOT NULL,
    logged_in INTEGER DEFAULT 0,
    last_login TEXT,
    cookies_path TEXT,
    daily_applied INTEGER DEFAULT 0,
    daily_reset TEXT,
    total_applied INTEGER DEFAULT 0,
    last_error TEXT,
    status TEXT DEFAULT 'active',
    cooldown_until TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Error log
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    module TEXT,
    error_type TEXT,
    message TEXT,
    traceback TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_platform ON jobs(platform);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(match_score);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_emails_status ON emails(status);
CREATE INDEX IF NOT EXISTS idx_emails_type ON emails(email_type);
CREATE INDEX IF NOT EXISTS idx_errors_module ON errors(module);
"""


# ═══════════════════════════════════════════════════════════
# DATABASE CLASS
# ═══════════════════════════════════════════════════════════
class Database:
    """Thread-safe SQLite database with WAL mode."""

    def __init__(self, db_path: str = None):
        self.db_path = str(db_path or DB_PATH)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                self.db_path,
                timeout=30,
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn.execute("PRAGMA busy_timeout=5000")
        return self._local.conn

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        logger.info(f"Database initialized at {self.db_path}")

    def _now(self) -> str:
        """Current UTC timestamp."""
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    def _row_to_dict(self, row: sqlite3.Row) -> Dict:
        """Convert Row to dict, parsing JSON fields."""
        if row is None:
            return None
        d = dict(row)
        # Parse JSON fields
        for key in ["skills", "match_details", "attachments"]:
            if key in d and d[key] and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def _rows_to_list(self, rows) -> List[Dict]:
        """Convert list of Rows to list of dicts."""
        return [self._row_to_dict(r) for r in rows]

    # ───────────────────────────────────────────────────────
    # JOBS
    # ───────────────────────────────────────────────────────
    def save_job(self, data: Dict) -> int:
        """
        Insert or update a job.
        Returns: job id (new or existing).
        """
        conn = self._get_conn()

        # Serialize JSON fields
        skills = data.get("skills")
        if isinstance(skills, (list, dict)):
            skills = json.dumps(skills)

        match_details = data.get("match_details")
        if isinstance(match_details, (list, dict)):
            match_details = json.dumps(match_details)

        now = self._now()

        try:
            with self._lock:
                cursor = conn.execute(
                    """
                    INSERT INTO jobs (
                        platform, platform_job_id, url, title, company,
                        location, salary_min, salary_max, currency,
                        experience_min, experience_max, job_type, work_mode,
                        description, skills, posted_date, discovered_at,
                        match_score, match_details, status, priority, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, platform_job_id) DO UPDATE SET
                        url = COALESCE(excluded.url, url),
                        title = COALESCE(excluded.title, title),
                        company = COALESCE(excluded.company, company),
                        location = COALESCE(excluded.location, location),
                        salary_min = COALESCE(excluded.salary_min, salary_min),
                        salary_max = COALESCE(excluded.salary_max, salary_max),
                        description = COALESCE(excluded.description, description),
                        skills = COALESCE(excluded.skills, skills),
                        match_score = CASE
                            WHEN excluded.match_score > 0 THEN excluded.match_score
                            ELSE match_score END,
                        match_details = COALESCE(excluded.match_details, match_details)
                    """,
                    (
                        data.get("platform", ""),
                        data.get("platform_job_id", ""),
                        data.get("url"),
                        data.get("title"),
                        data.get("company"),
                        data.get("location"),
                        data.get("salary_min"),
                        data.get("salary_max"),
                        data.get("currency", "INR"),
                        data.get("experience_min"),
                        data.get("experience_max"),
                        data.get("job_type"),
                        data.get("work_mode"),
                        data.get("description"),
                        skills,
                        data.get("posted_date"),
                        data.get("discovered_at", now),
                        data.get("match_score", 0),
                        match_details,
                        data.get("status", "new"),
                        data.get("priority", 0),
                        data.get("notes"),
                    ),
                )
                conn.commit()
                job_id = cursor.lastrowid

                # If it was an update (conflict), get the existing id
                if job_id == 0:
                    row = conn.execute(
                        "SELECT id FROM jobs WHERE platform=? AND platform_job_id=?",
                        (data.get("platform"), data.get("platform_job_id")),
                    ).fetchone()
                    job_id = row["id"] if row else 0

                return job_id

        except Exception as e:
            logger.error(f"Error saving job: {e}")
            self.save_error("core.db", "save_job", str(e), traceback.format_exc())
            return 0

    def get_jobs(
        self,
        platform: str = None,
        status: str = None,
        min_score: float = None,
        limit: int = 100,
        order_by: str = "match_score DESC",
    ) -> List[Dict]:
        """Get jobs with optional filters."""
        conn = self._get_conn()
        conditions = []
        params = []

        if platform:
            conditions.append("platform = ?")
            params.append(platform)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if min_score is not None:
            conditions.append("match_score >= ?")
            params.append(min_score)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM jobs {where} ORDER BY {order_by} LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return self._rows_to_list(rows)

    def get_job_by_id(self, job_id: int) -> Optional[Dict]:
        """Get single job by ID."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_dict(row)

    def get_job_by_platform_id(self, platform: str, platform_job_id: str) -> Optional[Dict]:
        """Get job by platform-specific ID. Used for dedup."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM jobs WHERE platform = ? AND platform_job_id = ?",
            (platform, platform_job_id),
        ).fetchone()
        return self._row_to_dict(row)

    def update_job_status(self, job_id: int, status: str, notes: str = None):
        """Update job status."""
        conn = self._get_conn()
        with self._lock:
            if notes:
                conn.execute(
                    "UPDATE jobs SET status = ?, notes = ? WHERE id = ?",
                    (status, notes, job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status = ? WHERE id = ?", (status, job_id)
                )
            conn.commit()

    def update_job_score(self, job_id: int, score: float, details: Dict = None):
        """Update match score and details."""
        conn = self._get_conn()
        details_json = json.dumps(details) if details else None
        with self._lock:
            conn.execute(
                "UPDATE jobs SET match_score = ?, match_details = ? WHERE id = ?",
                (score, details_json, job_id),
            )
            conn.commit()

    def get_new_jobs(self, limit: int = 50) -> List[Dict]:
        """Get unscored new jobs."""
        return self.get_jobs(status="new", limit=limit, order_by="discovered_at DESC")

    def get_matched_jobs(self, limit: int = 20) -> List[Dict]:
        """Get scored/matched jobs ready for application."""
        return self.get_jobs(status="matched", limit=limit, order_by="match_score DESC")

    def get_queued_jobs(self, limit: int = 10) -> List[Dict]:
        """Get jobs queued for application."""
        return self.get_jobs(status="queued", limit=limit, order_by="priority DESC, match_score DESC")

    def search_jobs(self, query: str, limit: int = 50) -> List[Dict]:
        """Full-text search across title, company, description."""
        conn = self._get_conn()
        pattern = f"%{query}%"
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE title LIKE ? OR company LIKE ? OR description LIKE ?
               ORDER BY match_score DESC LIMIT ?""",
            (pattern, pattern, pattern, limit),
        ).fetchall()
        return self._rows_to_list(rows)

    # ───────────────────────────────────────────────────────
    # APPLICATIONS
    # ───────────────────────────────────────────────────────
    def save_application(self, data: Dict) -> int:
        """Save a new application record."""
        conn = self._get_conn()
        now = self._now()
        try:
            with self._lock:
                cursor = conn.execute(
                    """INSERT INTO applications (
                        job_id, platform, method, resume_version,
                        cover_letter, tailoring_mode, applied_at,
                        status, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        data.get("job_id"),
                        data.get("platform"),
                        data.get("method", "quick_apply"),
                        data.get("resume_version"),
                        data.get("cover_letter"),
                        data.get("tailoring_mode", "light"),
                        data.get("applied_at", now),
                        data.get("status", "submitted"),
                        data.get("notes"),
                    ),
                )
                conn.commit()

                # Also update job status
                job_id = data.get("job_id")
                if job_id:
                    conn.execute(
                        "UPDATE jobs SET status = 'applied' WHERE id = ?", (job_id,)
                    )
                    conn.commit()

                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Error saving application: {e}")
            self.save_error("core.db", "save_application", str(e), traceback.format_exc())
            return 0

    def get_applications(
        self,
        platform: str = None,
        status: str = None,
        limit: int = 100,
    ) -> List[Dict]:
        """Get applications with optional filters."""
        conn = self._get_conn()
        conditions = []
        params = []

        if platform:
            conditions.append("a.platform = ?")
            params.append(platform)
        if status:
            conditions.append("a.status = ?")
            params.append(status)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT a.*, j.title as job_title, j.company as job_company,
                   j.url as job_url, j.match_score
            FROM applications a
            LEFT JOIN jobs j ON a.job_id = j.id
            {where}
            ORDER BY a.applied_at DESC LIMIT ?
        """
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return self._rows_to_list(rows)

    def update_application_status(self, app_id: int, status: str, notes: str = None):
        """Update application status."""
        conn = self._get_conn()
        now = self._now()
        with self._lock:
            updates = "status = ?"
            params = [status]

            if status in ("shortlisted", "interview", "rejected", "offer"):
                updates += ", response_date = ?"
                params.append(now)

            if notes:
                updates += ", response_notes = ?"
                params.append(notes)

            params.append(app_id)
            conn.execute(f"UPDATE applications SET {updates} WHERE id = ?", params)
            conn.commit()

    def get_due_follow_ups(self) -> List[Dict]:
        """Get applications with follow-ups due."""
        conn = self._get_conn()
        now = self._now()
        rows = conn.execute(
            """SELECT a.*, j.title as job_title, j.company as job_company,
                      j.url as job_url
               FROM applications a
               LEFT JOIN jobs j ON a.job_id = j.id
               WHERE a.next_follow_up IS NOT NULL
               AND a.next_follow_up <= ?
               AND a.status IN ('submitted', 'viewed')
               AND a.follow_up_count < 3
               ORDER BY a.next_follow_up ASC""",
            (now,),
        ).fetchall()
        return self._rows_to_list(rows)

    def update_follow_up(self, app_id: int, next_date: str = None):
        """Increment follow-up count and set next date."""
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                """UPDATE applications
                   SET follow_up_count = follow_up_count + 1,
                       next_follow_up = ?
                   WHERE id = ?""",
                (next_date, app_id),
            )
            conn.commit()

    # ───────────────────────────────────────────────────────
    # CONTACTS
    # ───────────────────────────────────────────────────────
    def save_contact(self, data: Dict) -> int:
        """Save HR/recruiter contact."""
        conn = self._get_conn()
        try:
            with self._lock:
                cursor = conn.execute(
                    """INSERT INTO contacts (
                        company, name, title, email, linkedin_url,
                        phone, source, verified, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        data.get("company"),
                        data.get("name"),
                        data.get("title"),
                        data.get("email"),
                        data.get("linkedin_url"),
                        data.get("phone"),
                        data.get("source"),
                        data.get("verified", 0),
                        data.get("notes"),
                    ),
                )
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Error saving contact: {e}")
            return 0

    def get_contacts(self, company: str = None) -> List[Dict]:
        """Get contacts, optionally filtered by company."""
        conn = self._get_conn()
        if company:
            rows = conn.execute(
                "SELECT * FROM contacts WHERE company LIKE ? ORDER BY created_at DESC",
                (f"%{company}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM contacts ORDER BY created_at DESC"
            ).fetchall()
        return self._rows_to_list(rows)

    def find_contact_by_email(self, email: str) -> Optional[Dict]:
        """Find contact by email."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM contacts WHERE email = ?", (email,)
        ).fetchone()
        return self._row_to_dict(row)

    # ───────────────────────────────────────────────────────
    # EMAILS
    # ───────────────────────────────────────────────────────
    def save_email(self, data: Dict) -> int:
        """Save email record."""
        conn = self._get_conn()
        attachments = data.get("attachments")
        if isinstance(attachments, (list, dict)):
            attachments = json.dumps(attachments)

        try:
            with self._lock:
                cursor = conn.execute(
                    """INSERT INTO emails (
                        application_id, contact_id, email_type,
                        to_email, subject, body, attachments,
                        sent_at, status, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        data.get("application_id"),
                        data.get("contact_id"),
                        data.get("email_type", "application"),
                        data.get("to_email"),
                        data.get("subject"),
                        data.get("body"),
                        attachments,
                        data.get("sent_at"),
                        data.get("status", "queued"),
                        data.get("error_message"),
                    ),
                )
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Error saving email: {e}")
            return 0

    def get_emails(
        self,
        status: str = None,
        email_type: str = None,
        limit: int = 100,
    ) -> List[Dict]:
        """Get emails with optional filters."""
        conn = self._get_conn()
        conditions = []
        params = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if email_type:
            conditions.append("email_type = ?")
            params.append(email_type)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM emails {where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return self._rows_to_list(rows)

    def update_email_status(self, email_id: int, status: str, error: str = None):
        """Update email status."""
        conn = self._get_conn()
        now = self._now()
        with self._lock:
            if status == "sent":
                conn.execute(
                    "UPDATE emails SET status = ?, sent_at = ? WHERE id = ?",
                    (status, now, email_id),
                )
            elif error:
                conn.execute(
                    "UPDATE emails SET status = ?, error_message = ? WHERE id = ?",
                    (status, error, email_id),
                )
            else:
                conn.execute(
                    "UPDATE emails SET status = ? WHERE id = ?", (status, email_id)
                )
            conn.commit()

    # ───────────────────────────────────────────────────────
    # PLATFORM SESSIONS
    # ───────────────────────────────────────────────────────
    def get_platform_session(self, platform: str) -> Dict:
        """Get platform session state. Creates if not exists."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM platform_sessions WHERE platform = ?", (platform,)
        ).fetchone()

        if row is None:
            now = self._now()
            with self._lock:
                conn.execute(
                    """INSERT INTO platform_sessions (platform, daily_reset, created_at)
                       VALUES (?, ?, ?)""",
                    (platform, now, now),
                )
                conn.commit()
            row = conn.execute(
                "SELECT * FROM platform_sessions WHERE platform = ?", (platform,)
            ).fetchone()

        return self._row_to_dict(row)

    def update_platform_session(self, platform: str, updates: Dict):
        """Update platform session fields."""
        conn = self._get_conn()
        if not updates:
            return

        set_parts = []
        params = []
        for key, value in updates.items():
            set_parts.append(f"{key} = ?")
            params.append(value)

        params.append(platform)
        query = f"UPDATE platform_sessions SET {', '.join(set_parts)} WHERE platform = ?"

        with self._lock:
            conn.execute(query, params)
            conn.commit()

    def reset_daily_counts(self):
        """Reset daily application counts. Called at midnight."""
        conn = self._get_conn()
        now = self._now()
        with self._lock:
            conn.execute(
                "UPDATE platform_sessions SET daily_applied = 0, daily_reset = ?",
                (now,),
            )
            conn.commit()
        logger.info("Daily application counts reset")

    # ───────────────────────────────────────────────────────
    # ERRORS
    # ───────────────────────────────────────────────────────
    def save_error(
        self,
        module: str,
        error_type: str,
        message: str,
        tb: str = None,
    ):
        """Log error to database."""
        conn = self._get_conn()
        now = self._now()
        try:
            with self._lock:
                conn.execute(
                    """INSERT INTO errors (timestamp, module, error_type, message, traceback)
                       VALUES (?, ?, ?, ?, ?)""",
                    (now, module, error_type, message, tb),
                )
                conn.commit()
        except Exception:
            # Don't recurse if error logging itself fails
            pass

    def get_errors(self, module: str = None, limit: int = 50) -> List[Dict]:
        """Get recent errors."""
        conn = self._get_conn()
        if module:
            rows = conn.execute(
                "SELECT * FROM errors WHERE module = ? ORDER BY timestamp DESC LIMIT ?",
                (module, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM errors ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return self._rows_to_list(rows)

    # ───────────────────────────────────────────────────────
    # STATS & ANALYTICS
    # ───────────────────────────────────────────────────────
    def get_stats(self, period: str = "today") -> Dict:
        """
        Get application statistics.
        period: 'today', 'week', 'month', 'total'
        """
        conn = self._get_conn()

        if period == "today":
            date_filter = datetime.utcnow().strftime("%Y-%m-%d")
            time_clause = f"DATE(created_at) = '{date_filter}'"
        elif period == "week":
            week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
            time_clause = f"DATE(created_at) >= '{week_ago}'"
        elif period == "month":
            month_ago = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
            time_clause = f"DATE(created_at) >= '{month_ago}'"
        else:
            time_clause = "1=1"

        stats = {}

        # Jobs
        row = conn.execute(
            f"SELECT COUNT(*) as total, "
            f"SUM(CASE WHEN status='new' THEN 1 ELSE 0 END) as new_count, "
            f"SUM(CASE WHEN status='matched' THEN 1 ELSE 0 END) as matched, "
            f"SUM(CASE WHEN status='applied' THEN 1 ELSE 0 END) as applied, "
            f"SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) as skipped, "
            f"AVG(match_score) as avg_score "
            f"FROM jobs WHERE {time_clause}"
        ).fetchone()
        stats["jobs"] = dict(row) if row else {}

        # Applications
        row = conn.execute(
            f"SELECT COUNT(*) as total, "
            f"SUM(CASE WHEN status='submitted' THEN 1 ELSE 0 END) as submitted, "
            f"SUM(CASE WHEN status='viewed' THEN 1 ELSE 0 END) as viewed, "
            f"SUM(CASE WHEN status='shortlisted' THEN 1 ELSE 0 END) as shortlisted, "
            f"SUM(CASE WHEN status='interview' THEN 1 ELSE 0 END) as interviews, "
            f"SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) as rejected, "
            f"SUM(CASE WHEN status='offer' THEN 1 ELSE 0 END) as offers "
            f"FROM applications WHERE {time_clause}"
        ).fetchone()
        stats["applications"] = dict(row) if row else {}

        # Emails
        row = conn.execute(
            f"SELECT COUNT(*) as total, "
            f"SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) as sent, "
            f"SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed, "
            f"SUM(CASE WHEN reply_received=1 THEN 1 ELSE 0 END) as replies "
            f"FROM emails WHERE {time_clause}"
        ).fetchone()
        stats["emails"] = dict(row) if row else {}

        # Per-platform breakdown
        rows = conn.execute(
            f"SELECT platform, COUNT(*) as count "
            f"FROM applications WHERE {time_clause} GROUP BY platform"
        ).fetchall()
        stats["per_platform"] = {r["platform"]: r["count"] for r in rows}

        # Errors
        row = conn.execute(
            f"SELECT COUNT(*) as count FROM errors WHERE {time_clause}"
        ).fetchone()
        stats["errors"] = row["count"] if row else 0

        return stats

    def get_pipeline(self) -> Dict:
        """Get application funnel/pipeline."""
        conn = self._get_conn()

        pipeline = {}
        for status in ["new", "matched", "queued", "applying", "applied", "skipped", "expired", "duplicate"]:
            row = conn.execute(
                "SELECT COUNT(*) as count FROM jobs WHERE status = ?", (status,)
            ).fetchone()
            pipeline[status] = row["count"] if row else 0

        for status in ["submitted", "viewed", "shortlisted", "interview", "rejected", "offer", "ghosted"]:
            row = conn.execute(
                "SELECT COUNT(*) as count FROM applications WHERE status = ?", (status,)
            ).fetchone()
            pipeline[f"app_{status}"] = row["count"] if row else 0

        return pipeline

    def get_table_info(self) -> Dict[str, int]:
        """Get row counts for all tables."""
        conn = self._get_conn()
        tables = ["jobs", "applications", "contacts", "emails", "platform_sessions", "errors"]
        info = {}
        for table in tables:
            row = conn.execute(f"SELECT COUNT(*) as count FROM {table}").fetchone()
            info[table] = row["count"] if row else 0
        return info

    def close(self):
        """Close the database connection for this thread."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
            logger.debug("Database connection closed")


# ═══════════════════════════════════════════════════════════
# SINGLETON
# ═══════════════════════════════════════════════════════════
_db_instance = None
_db_lock = threading.Lock()


def get_db() -> Database:
    """Get singleton database instance."""
    global _db_instance
    if _db_instance is None:
        with _db_lock:
            if _db_instance is None:
                _db_instance = Database()
    return _db_instance


# ═══════════════════════════════════════════════════════════
# TEST BLOCK
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("  Database Test")
    print("=" * 60)
    print()

    db = get_db()

    # Test save_job
    print("📝 Testing save_job...")
    job_id = db.save_job({
        "platform": "naukri",
        "platform_job_id": "test_001",
        "url": "https://naukri.com/job/test-001",
        "title": "SDE-1 Java Backend",
        "company": "Razorpay",
        "location": "Bangalore",
        "salary_min": 8.0,
        "salary_max": 14.0,
        "experience_min": 0,
        "experience_max": 2,
        "job_type": "Full-time",
        "work_mode": "Hybrid",
        "description": "We are looking for a Java developer with Spring Boot experience...",
        "skills": ["Java", "Spring Boot", "MySQL", "REST APIs", "Kafka"],
        "posted_date": "2025-01-15",
        "status": "new",
    })
    print(f"   ✅ Job saved with ID: {job_id}")

    # Test second job
    job_id2 = db.save_job({
        "platform": "indeed",
        "platform_job_id": "indeed_001",
        "url": "https://indeed.com/job/indeed-001",
        "title": "Full Stack Developer",
        "company": "Flipkart",
        "location": "Bangalore",
        "salary_min": 10.0,
        "salary_max": 18.0,
        "experience_min": 1,
        "experience_max": 3,
        "description": "Looking for full stack developer with React and Node.js...",
        "skills": ["React", "Node.js", "MongoDB", "TypeScript"],
        "status": "new",
    })
    print(f"   ✅ Job 2 saved with ID: {job_id2}")

    # Test dedup (re-insert same job)
    dup_id = db.save_job({
        "platform": "naukri",
        "platform_job_id": "test_001",
        "title": "SDE-1 Java Backend (Updated)",
        "company": "Razorpay",
    })
    print(f"   ✅ Duplicate handled, returned ID: {dup_id}")

    # Test get_jobs
    print("\n📋 Testing get_jobs...")
    jobs = db.get_jobs()
    print(f"   Total jobs: {len(jobs)}")
    for j in jobs:
        print(f"   - [{j['platform']}] {j['title']} @ {j['company']} (score: {j['match_score']})")

    # Test get_job_by_platform_id
    print("\n🔍 Testing get_job_by_platform_id...")
    found = db.get_job_by_platform_id("naukri", "test_001")
    print(f"   Found: {found['title']} @ {found['company']}" if found else "   Not found")

    # Test update score
    print("\n📊 Testing update_job_score...")
    db.update_job_score(job_id, 87.5, {
        "title_match": 0.9,
        "skills_match": 0.85,
        "recommendation": "strong",
    })
    job = db.get_job_by_id(job_id)
    print(f"   Score updated: {job['match_score']}")

    # Test update status
    db.update_job_status(job_id, "matched")
    matched = db.get_matched_jobs()
    print(f"   Matched jobs: {len(matched)}")

    # Test save_application
    print("\n📨 Testing save_application...")
    app_id = db.save_application({
        "job_id": job_id,
        "platform": "naukri",
        "method": "quick_apply",
        "resume_version": "resume/output/razorpay_sde1_20250115/resume.docx",
        "tailoring_mode": "light",
        "status": "submitted",
    })
    print(f"   ✅ Application saved with ID: {app_id}")

    # Test get_applications
    apps = db.get_applications()
    print(f"   Total applications: {len(apps)}")
    for a in apps:
        print(f"   - {a.get('job_title', 'N/A')} @ {a.get('job_company', 'N/A')} [{a['status']}]")

    # Test save_contact
    print("\n👤 Testing save_contact...")
    contact_id = db.save_contact({
        "company": "Razorpay",
        "name": "HR Team",
        "email": "careers@razorpay.com",
        "source": "career_page",
    })
    print(f"   ✅ Contact saved with ID: {contact_id}")

    # Test save_email
    print("\n📧 Testing save_email...")
    email_id = db.save_email({
        "application_id": app_id,
        "contact_id": contact_id,
        "email_type": "application",
        "to_email": "careers@razorpay.com",
        "subject": "Application for SDE-1 Position",
        "body": "Dear Hiring Manager...",
        "status": "queued",
    })
    print(f"   ✅ Email saved with ID: {email_id}")

    # Test platform sessions
    print("\n🔧 Testing platform_sessions...")
    session = db.get_platform_session("naukri")
    print(f"   Naukri session: logged_in={session['logged_in']}, daily={session['daily_applied']}")

    db.update_platform_session("naukri", {
        "logged_in": 1,
        "daily_applied": 5,
        "last_login": db._now(),
    })
    session = db.get_platform_session("naukri")
    print(f"   Updated: logged_in={session['logged_in']}, daily={session['daily_applied']}")

    # Test save_error
    print("\n❌ Testing save_error...")
    db.save_error("test", "TestError", "This is a test error")
    errors = db.get_errors()
    print(f"   Total errors: {len(errors)}")

    # Test stats
    print("\n📊 Testing get_stats...")
    stats = db.get_stats("total")
    print(f"   Jobs: {stats['jobs']}")
    print(f"   Applications: {stats['applications']}")
    print(f"   Emails: {stats['emails']}")
    print(f"   Errors: {stats['errors']}")

    # Test pipeline
    print("\n🔄 Testing get_pipeline...")
    pipeline = db.get_pipeline()
    for k, v in pipeline.items():
        if v > 0:
            print(f"   {k}: {v}")

    # Test table info
    print("\n📋 Testing get_table_info...")
    info = db.get_table_info()
    for table, count in info.items():
        print(f"   {table}: {count} rows")

    # Cleanup test data
    print("\n🧹 Cleaning up test data...")
    conn = db._get_conn()
    conn.execute("DELETE FROM emails WHERE id = ?", (email_id,))
    conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
    conn.execute("DELETE FROM jobs WHERE platform_job_id IN ('test_001', 'indeed_001')")
    conn.execute("DELETE FROM errors WHERE module = 'test'")
    conn.execute("DELETE FROM platform_sessions WHERE platform = 'naukri'")
    conn.commit()
    print("   ✅ Test data cleaned up")

    db.close()
    print(f"\n✅ Database test complete! DB at: {db.db_path}")