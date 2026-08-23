#!/usr/bin/env python3
"""
tracking/reports.py — Daily / weekly report generation and CSV export.

Provides:
    ReportGenerator.daily_report()   → dict with today's stats
    ReportGenerator.weekly_report()  → dict with funnel + breakdown
    ReportGenerator.export_csv()     → path to exported CSV
"""

import os
import csv
import json
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

from config import BASE_DIR, USER_PROFILE, PLATFORM_CONFIG
from core.logger import get_logger
from core.db import get_db

logger = get_logger("reports")


class ReportGenerator:
    """Generate analytics reports from the jobs / applications database."""

    def __init__(self):
        self.db = get_db()
        self.logger = logger
        self._export_dir = os.path.join(BASE_DIR, "cache", "reports")
        os.makedirs(self._export_dir, exist_ok=True)
        self.logger.info("ReportGenerator ready (export → %s)", self._export_dir)

    # ══════════════════════════════════════════════════════════
    #  DAILY REPORT
    # ══════════════════════════════════════════════════════════
    def daily_report(self) -> Dict[str, Any]:
        """
        Build a dict summarising today's activity.

        Keys returned:
            date, today_discovered, today_matched, today_applied,
            today_emailed, today_responses, total_applied,
            pending_follow_ups, per_platform, top_matches,
            pipeline, error_count, session
        """
        today = datetime.now().strftime("%Y-%m-%d")
        report: Dict[str, Any] = {"date": today}

        conn = self._conn()
        if not conn:
            return report

        # ── today counts ──────────────────────────────────────
        report["today_discovered"] = self._count(
            conn,
            "SELECT COUNT(*) FROM jobs WHERE DATE(discovered_at)=?",
            (today,),
        )
        report["today_matched"] = self._count(
            conn,
            "SELECT COUNT(*) FROM jobs WHERE DATE(discovered_at)=? "
            "AND status IN ('matched','applied')",
            (today,),
        )
        report["today_applied"] = self._count(
            conn,
            "SELECT COUNT(*) FROM applications WHERE DATE(applied_at)=?",
            (today,),
        )
        report["today_emailed"] = self._count(
            conn,
            "SELECT COUNT(*) FROM emails WHERE DATE(sent_at)=? AND status='sent'",
            (today,),
        )
        report["today_responses"] = self._count(
            conn,
            "SELECT COUNT(*) FROM applications "
            "WHERE DATE(response_date)=? AND status NOT IN ('submitted','ghosted')",
            (today,),
        )

        # ── totals ────────────────────────────────────────────
        report["total_jobs"] = self._count(conn, "SELECT COUNT(*) FROM jobs")
        report["total_applied"] = self._count(
            conn, "SELECT COUNT(*) FROM applications"
        )
        report["total_emailed"] = self._count(
            conn, "SELECT COUNT(*) FROM emails WHERE status='sent'"
        )

        # ── pending follow-ups ────────────────────────────────
        report["pending_follow_ups"] = self._count(
            conn,
            "SELECT COUNT(*) FROM applications "
            "WHERE status='submitted' AND next_follow_up IS NOT NULL "
            "AND DATE(next_follow_up) <= ?",
            (today,),
        )

        # ── per-platform breakdown ────────────────────────────
        per_platform: Dict[str, Dict[str, int]] = {}
        try:
            rows = conn.execute(
                "SELECT platform, "
                "SUM(CASE WHEN DATE(discovered_at)=? THEN 1 ELSE 0 END) as disc, "
                "SUM(CASE WHEN status='applied' THEN 1 ELSE 0 END) as appl "
                "FROM jobs GROUP BY platform",
                (today,),
            ).fetchall()
            for r in rows:
                per_platform[r[0]] = {
                    "discovered_today": r[1],
                    "total_applied": r[2],
                }
        except Exception:
            pass
        report["per_platform"] = per_platform

        # ── top matches (today, score desc) ───────────────────
        top_matches: List[Dict[str, Any]] = []
        try:
            rows = conn.execute(
                "SELECT id, title, company, platform, match_score, status "
                "FROM jobs WHERE DATE(discovered_at)=? AND match_score>0 "
                "ORDER BY match_score DESC LIMIT 10",
                (today,),
            ).fetchall()
            for r in rows:
                top_matches.append({
                    "id": r[0],
                    "title": r[1],
                    "company": r[2],
                    "platform": r[3],
                    "score": r[4],
                    "status": r[5],
                })
        except Exception:
            pass
        report["top_matches"] = top_matches

        # ── pipeline funnel ───────────────────────────────────
        report["pipeline"] = self._get_pipeline(conn)

        # ── errors today ──────────────────────────────────────
        report["error_count"] = self._count(
            conn,
            "SELECT COUNT(*) FROM errors WHERE DATE(timestamp)=?",
            (today,),
        )

        # ── score distribution ────────────────────────────────
        score_dist: Dict[str, int] = {
            "0-25": 0, "26-50": 0, "51-70": 0, "71-85": 0, "86-100": 0
        }
        try:
            rows = conn.execute(
                "SELECT match_score FROM jobs "
                "WHERE DATE(discovered_at)=? AND match_score>0",
                (today,),
            ).fetchall()
            for (s,) in rows:
                if s <= 25:
                    score_dist["0-25"] += 1
                elif s <= 50:
                    score_dist["26-50"] += 1
                elif s <= 70:
                    score_dist["51-70"] += 1
                elif s <= 85:
                    score_dist["71-85"] += 1
                else:
                    score_dist["86-100"] += 1
        except Exception:
            pass
        report["score_distribution"] = score_dist

        self.logger.info(
            "Daily report: discovered=%d applied=%d responses=%d",
            report["today_discovered"],
            report["today_applied"],
            report["today_responses"],
        )
        return report

    # ══════════════════════════════════════════════════════════
    #  WEEKLY REPORT
    # ══════════════════════════════════════════════════════════
    def weekly_report(self) -> Dict[str, Any]:
        """
        Build a dict summarising the past 7 days.

        Keys:
            period_start, period_end, daily_breakdown[],
            funnel, best_platforms, conversion_rates,
            avg_score, top_companies
        """
        end = datetime.now()
        start = end - timedelta(days=7)
        report: Dict[str, Any] = {
            "period_start": start.strftime("%Y-%m-%d"),
            "period_end": end.strftime("%Y-%m-%d"),
        }

        conn = self._conn()
        if not conn:
            return report

        start_s = start.strftime("%Y-%m-%d")
        end_s = end.strftime("%Y-%m-%d")

        # ── daily breakdown ───────────────────────────────────
        daily: List[Dict[str, Any]] = []
        for i in range(7):
            day = (start + timedelta(days=i + 1)).strftime("%Y-%m-%d")
            disc = self._count(
                conn,
                "SELECT COUNT(*) FROM jobs WHERE DATE(discovered_at)=?",
                (day,),
            )
            appl = self._count(
                conn,
                "SELECT COUNT(*) FROM applications WHERE DATE(applied_at)=?",
                (day,),
            )
            resp = self._count(
                conn,
                "SELECT COUNT(*) FROM applications "
                "WHERE DATE(response_date)=? AND status NOT IN ('submitted','ghosted')",
                (day,),
            )
            daily.append({
                "date": day,
                "discovered": disc,
                "applied": appl,
                "responses": resp,
            })
        report["daily_breakdown"] = daily

        # ── funnel ────────────────────────────────────────────
        report["funnel"] = {
            "discovered": self._count(
                conn,
                "SELECT COUNT(*) FROM jobs WHERE discovered_at>=?",
                (start_s,),
            ),
            "matched": self._count(
                conn,
                "SELECT COUNT(*) FROM jobs "
                "WHERE discovered_at>=? AND status IN ('matched','applied')",
                (start_s,),
            ),
            "applied": self._count(
                conn,
                "SELECT COUNT(*) FROM applications WHERE applied_at>=?",
                (start_s,),
            ),
            "response": self._count(
                conn,
                "SELECT COUNT(*) FROM applications "
                "WHERE applied_at>=? AND status NOT IN ('submitted','ghosted')",
                (start_s,),
            ),
            "interview": self._count(
                conn,
                "SELECT COUNT(*) FROM applications "
                "WHERE applied_at>=? AND status='interview'",
                (start_s,),
            ),
            "offer": self._count(
                conn,
                "SELECT COUNT(*) FROM applications "
                "WHERE applied_at>=? AND status='offer'",
                (start_s,),
            ),
        }

        # ── conversion rates ──────────────────────────────────
        funnel = report["funnel"]
        disc = max(funnel["discovered"], 1)
        appl = max(funnel["applied"], 1)
        report["conversion_rates"] = {
            "discovered_to_matched": round(funnel["matched"] / disc * 100, 1),
            "matched_to_applied": round(
                funnel["applied"] / max(funnel["matched"], 1) * 100, 1
            ),
            "applied_to_response": round(funnel["response"] / appl * 100, 1),
            "applied_to_interview": round(funnel["interview"] / appl * 100, 1),
        }

        # ── best platforms ────────────────────────────────────
        best_platforms: Dict[str, Dict[str, int]] = {}
        try:
            rows = conn.execute(
                "SELECT platform, COUNT(*) as cnt "
                "FROM applications WHERE applied_at>=? "
                "GROUP BY platform ORDER BY cnt DESC",
                (start_s,),
            ).fetchall()
            for r in rows:
                resp_cnt = self._count(
                    conn,
                    "SELECT COUNT(*) FROM applications "
                    "WHERE platform=? AND applied_at>=? "
                    "AND status NOT IN ('submitted','ghosted')",
                    (r[0], start_s),
                )
                best_platforms[r[0]] = {
                    "applied": r[1],
                    "responses": resp_cnt,
                    "response_rate": round(resp_cnt / max(r[1], 1) * 100, 1),
                }
        except Exception:
            pass
        report["best_platforms"] = best_platforms

        # ── average score ─────────────────────────────────────
        try:
            row = conn.execute(
                "SELECT AVG(match_score) FROM jobs "
                "WHERE discovered_at>=? AND match_score>0",
                (start_s,),
            ).fetchone()
            report["avg_score"] = round(row[0], 1) if row and row[0] else 0
        except Exception:
            report["avg_score"] = 0

        # ── top companies applied to ──────────────────────────
        top_companies: List[Dict[str, Any]] = []
        try:
            rows = conn.execute(
                "SELECT j.company, COUNT(*) as cnt, "
                "AVG(j.match_score) as avg_s "
                "FROM applications a JOIN jobs j ON a.job_id=j.id "
                "WHERE a.applied_at>=? "
                "GROUP BY j.company ORDER BY cnt DESC LIMIT 10",
                (start_s,),
            ).fetchall()
            for r in rows:
                top_companies.append({
                    "company": r[0],
                    "applications": r[1],
                    "avg_score": round(r[2], 1) if r[2] else 0,
                })
        except Exception:
            pass
        report["top_companies"] = top_companies

        self.logger.info(
            "Weekly report: discovered=%d applied=%d responses=%d",
            funnel["discovered"],
            funnel["applied"],
            funnel["response"],
        )
        return report

    # ══════════════════════════════════════════════════════════
    #  EXPORT CSV
    # ══════════════════════════════════════════════════════════
    def export_csv(self, period: str = "all") -> str:
        """
        Export jobs + applications to CSV.

        Args:
            period: "today", "week", "month", "all"

        Returns:
            Path to generated CSV file.
        """
        conn = self._conn()
        if not conn:
            raise RuntimeError("Database not available")

        # date filter
        where = ""
        params: tuple = ()
        if period == "today":
            where = "WHERE DATE(j.discovered_at) = DATE('now')"
        elif period == "week":
            where = "WHERE j.discovered_at >= DATE('now', '-7 days')"
        elif period == "month":
            where = "WHERE j.discovered_at >= DATE('now', '-30 days')"
        # "all" → no filter

        query = f"""
            SELECT
                j.id, j.platform, j.title, j.company, j.location,
                j.salary_min, j.salary_max, j.experience_min, j.experience_max,
                j.match_score, j.status, j.discovered_at, j.url,
                a.id as app_id, a.method, a.status as app_status,
                a.applied_at, a.follow_up_count
            FROM jobs j
            LEFT JOIN applications a ON a.job_id = j.id
            {where}
            ORDER BY j.id DESC
        """

        try:
            rows = conn.execute(query, params).fetchall()
        except Exception as exc:
            self.logger.error("CSV export query failed: %s", exc)
            raise

        filename = f"jobs_export_{period}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        filepath = os.path.join(self._export_dir, filename)

        headers = [
            "job_id", "platform", "title", "company", "location",
            "salary_min", "salary_max", "exp_min", "exp_max",
            "match_score", "job_status", "discovered_at", "url",
            "app_id", "apply_method", "app_status",
            "applied_at", "follow_up_count",
        ]

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(row)

        self.logger.info(
            "Exported %d rows to %s (period=%s)", len(rows), filepath, period
        )
        return filepath

    # ══════════════════════════════════════════════════════════
    #  INTERNAL HELPERS
    # ══════════════════════════════════════════════════════════
    def _conn(self):
        """Get raw sqlite3 connection."""
        try:
            if hasattr(self.db, "conn"):
                return self.db.conn
            return None
        except Exception:
            return None

    @staticmethod
    def _count(conn, sql: str, params: tuple = ()) -> int:
        """Execute a COUNT query and return integer."""
        try:
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row and row[0] else 0
        except Exception:
            return 0

    def _get_pipeline(self, conn) -> Dict[str, int]:
        """Get pipeline funnel counts."""
        pipeline: Dict[str, int] = {}
        statuses = [
            "new", "matched", "queued", "applied",
            "viewed", "shortlisted", "interview",
            "rejected", "offer", "ghosted", "skipped",
        ]
        for s in statuses:
            pipeline[s] = self._count(
                conn,
                "SELECT COUNT(*) FROM jobs WHERE status=?",
                (s,),
            )
        # also count application-level statuses
        for s in ["submitted", "viewed", "shortlisted", "interview",
                   "rejected", "offer", "ghosted"]:
            cnt = self._count(
                conn,
                "SELECT COUNT(*) FROM applications WHERE status=?",
                (s,),
            )
            key = f"app_{s}"
            pipeline[key] = cnt
        return pipeline


# ═══════════════════════════════════════════════════════════════
#  STANDALONE TEST
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("  ReportGenerator — standalone test")
    print("=" * 60)

    rg = ReportGenerator()

    print("\n--- Daily Report ---")
    daily = rg.daily_report()
    for k, v in daily.items():
        if isinstance(v, (list, dict)):
            print(f"  {k}: {json.dumps(v, default=str)[:120]}")
        else:
            print(f"  {k}: {v}")

    print("\n--- Weekly Report ---")
    weekly = rg.weekly_report()
    for k, v in weekly.items():
        if isinstance(v, (list, dict)):
            print(f"  {k}: {json.dumps(v, default=str)[:120]}")
        else:
            print(f"  {k}: {v}")

    print("\n--- CSV Export ---")
    try:
        path = rg.export_csv("all")
        print(f"  Exported to: {path}")
    except Exception as e:
        print(f"  Export error: {e}")

    print("\nDone.")