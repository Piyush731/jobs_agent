#!/usr/bin/env python3
"""
~/job_agent/quick_score.py
Score unscored jobs already in the database.
Run: python quick_score.py
"""

import sys, os, json, time, traceback

# ── project root on path ────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.db import get_db
from ai.llm_client import LLMClient
from ai.job_matcher import JobMatcher
from config import USER_PROFILE, MATCH_CONFIG

# ── init ─────────────────────────────────────────────────
db  = get_db()
llm = LLMClient()
matcher = JobMatcher(llm)

min_apply  = MATCH_CONFIG.get("min_score_to_apply", 40)
auto_apply = MATCH_CONFIG.get("auto_apply_score", 70)

# ── get unscored jobs ────────────────────────────────────
jobs = db.get_jobs(status="new", limit=30)       # start with 30
print(f"\n{'='*60}")
print(f"  SCORING {len(jobs)} UNSCORED JOBS")
print(f"  Gemini rate limit: 15 RPM → ~4.5 s between calls")
print(f"  Estimated time: ~{len(jobs)*5//60}m {len(jobs)*5%60}s")
print(f"{'='*60}\n")

if not jobs:
    print("No unscored jobs found. Run discovery first (python main.py --discover)")
    sys.exit(0)

matched = skipped = errors = 0

for i, job in enumerate(jobs, 1):
    jid   = job.get("id", "?")
    title = (job.get("title") or "?")[:38]
    comp  = (job.get("company") or "?")[:22]

    try:
        # ── parse skills JSON if stored as string ────────
        skills_raw = job.get("skills")
        if isinstance(skills_raw, str):
            try:
                job["skills"] = json.loads(skills_raw)
            except (json.JSONDecodeError, TypeError):
                job["skills"] = []

        # ── score ────────────────────────────────────────
        try:
            result = matcher.score_job(job, USER_PROFILE)
        except TypeError:
            # matcher might not need profile as 2nd arg
            result = matcher.score_job(job)

        score = result.get("score", 0)
        rec   = result.get("recommendation", "?")

        # ── decide status ────────────────────────────────
        if score >= min_apply:
            status = "matched"
            matched += 1
        else:
            status = "skipped"
            skipped += 1

        # ── update DB ────────────────────────────────────
        db.update_job_status(jid, status, notes=json.dumps(result))

        # try to also update match_score column directly
        try:
            conn = getattr(db, "_conn", None) or getattr(db, "conn", None)
            if conn:
                conn.execute(
                    "UPDATE jobs SET match_score=?, match_details=? WHERE id=?",
                    (score, json.dumps(result), jid),
                )
                conn.commit()
        except Exception:
            pass  # update_job_status already saved status

        # ── display ──────────────────────────────────────
        if score >= auto_apply:
            tag = "🟢"
        elif score >= min_apply:
            tag = "🟡"
        else:
            tag = "🔴"
        print(f"  [{i:2d}/{len(jobs)}] {tag} {score:3.0f}%  {title:38s}  {comp:22s}  {rec}")

    except Exception as ex:
        errors += 1
        print(f"  [{i:2d}/{len(jobs)}] ❗ ERROR: {ex}")
        traceback.print_exc()

    # ── rate-limit pause (skip after last) ───────────────
    if i < len(jobs):
        time.sleep(4.5)

# ── summary ──────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  RESULTS:  {matched} matched  |  {skipped} skipped  |  {errors} errors")
print(f"{'='*60}")

if matched:
    print(f"\n✅ {matched} jobs ready to apply! Next steps:")
    print(f"   1. Paste monitor.py / indeed.py / linkedin.py in the chat")
    print(f"   2. I fix them → you replace → run full agent")
    print(f"\nQuick check matched jobs:")
    print(f"   sqlite3 data/job_agent.db \"SELECT id, match_score, title, company FROM jobs WHERE status='matched' ORDER BY match_score DESC LIMIT 15;\"")
else:
    print(f"\n⚠ No matches. Try increasing limit to 100:")
    print(f"   Edit line: jobs = db.get_jobs(status='new', limit=100)")