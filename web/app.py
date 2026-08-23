#!/usr/bin/env python3
"""
web/app.py — Flask web dashboard for the Job Application AI Agent.

Routes:
    /                  Dashboard (overview)
    /jobs              Job listings with filters
    /jobs/<id>         Job detail
    /applications      Application history
    /pipeline          Funnel visualisation
    /platforms         Platform status
    /api/stats         JSON stats endpoint
    /api/jobs          JSON job list

Start:
    python -m web.app
    # or from main.py menu option 10
"""

import os
import sys
import json
from datetime import datetime
from typing import Dict, Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from flask import Flask, render_template_string, request, jsonify
from config import USER_PROFILE, PLATFORM_CONFIG
from core.logger import get_logger
from core.db import get_db

logger = get_logger("web")

app = Flask(__name__)
app.secret_key = os.urandom(24)


def _db():
    try:
        return get_db()
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
#  LAYOUT HELPER — replaces broken {% extends base %}
# ═══════════════════════════════════════════════════════════════

_LAYOUT = """<!DOCTYPE html>
<html lang="en" class="bg-gray-50">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ page_title }} — Job Agent</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  .stat-card{transition:transform .15s}.stat-card:hover{transform:translateY(-2px)}
  .score-high{color:#16a34a;font-weight:700}
  .score-mid{color:#ca8a04;font-weight:600}
  .score-low{color:#dc2626}
  .bar{border-radius:4px;min-width:2px}
</style>
</head>
<body class="text-gray-800 min-h-screen flex flex-col">

<nav class="bg-white border-b shadow-sm sticky top-0 z-50">
<div class="max-w-7xl mx-auto px-4 flex items-center justify-between h-14">
  <a href="/" class="font-bold text-lg text-cyan-700">🤖 Job Agent</a>
  <div class="flex gap-4 text-sm font-medium">
    <a href="/" class="hover:text-cyan-600 {{ 'text-cyan-600 underline' if active=='dashboard' else '' }}">Dashboard</a>
    <a href="/jobs" class="hover:text-cyan-600 {{ 'text-cyan-600 underline' if active=='jobs' else '' }}">Jobs</a>
    <a href="/applications" class="hover:text-cyan-600 {{ 'text-cyan-600 underline' if active=='apps' else '' }}">Applications</a>
    <a href="/pipeline" class="hover:text-cyan-600 {{ 'text-cyan-600 underline' if active=='pipeline' else '' }}">Pipeline</a>
    <a href="/platforms" class="hover:text-cyan-600 {{ 'text-cyan-600 underline' if active=='platforms' else '' }}">Platforms</a>
  </div>
  <span class="text-xs text-gray-400">{{ now }}</span>
</div>
</nav>

<main class="max-w-7xl mx-auto px-4 py-6 flex-1 w-full">
{{ content_html | safe }}
</main>

<footer class="text-center text-xs text-gray-400 py-4 border-t mt-8">
  Job Application AI Agent &middot; {{ user_name }}
</footer>
</body>
</html>"""


def _render(page_title: str, active: str, content_html: str):
    """Wrap content HTML inside the base layout."""
    return render_template_string(
        _LAYOUT,
        page_title=page_title,
        active=active,
        content_html=content_html,
        now=datetime.now().strftime("%d %b %Y, %I:%M %p"),
        user_name=USER_PROFILE.get("name", "Agent"),
    )


def _score_class(score):
    """Return CSS class for a match score."""
    if not score:
        return "text-gray-400"
    s = float(score)
    if s >= 70:
        return "score-high"
    if s >= 40:
        return "score-mid"
    return "score-low"


def _fmt_score(score):
    """Format score for display."""
    if not score:
        return "—"
    return f"{float(score):.0f}%"


def _trunc(text, length=30):
    """Truncate text."""
    if not text:
        return "—"
    text = str(text).replace("\n", " ").strip()
    return text[:length - 1] + "…" if len(text) > length else text


# ═══════════════════════════════════════════════════════════════
#  DASHBOARD
# ═══════════════════════════════════════════════════════════════

_DASHBOARD_CONTENT = """
<h1 class="text-2xl font-bold mb-4">📊 Dashboard</h1>

<!-- stat cards -->
<div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
{% for label, val, color in cards %}
  <div class="stat-card bg-white rounded-lg shadow p-4 border-l-4 border-{{ color }}-500">
    <div class="text-2xl font-bold text-{{ color }}-600">{{ val }}</div>
    <div class="text-sm text-gray-500">{{ label }}</div>
  </div>
{% endfor %}
</div>

<!-- pipeline mini -->
<div class="bg-white rounded-lg shadow p-4 mb-6">
  <h2 class="font-semibold mb-3">Pipeline Overview</h2>
  <div class="flex items-end gap-2" style="height:140px">
  {% for label, count, color in pipeline_bars %}
    <div class="flex flex-col items-center flex-1">
      <div class="text-xs font-bold mb-1">{{ count }}</div>
      <div class="bar bg-{{ color }}-500 w-full" style="height:{{ [count * scale, 2] | max }}px"></div>
      <div class="text-xs mt-1 text-gray-500 text-center">{{ label }}</div>
    </div>
  {% endfor %}
  </div>
</div>

<!-- recent jobs -->
<div class="bg-white rounded-lg shadow p-4">
  <h2 class="font-semibold mb-3">🆕 Recent Jobs</h2>
  {% if recent %}
  <div class="overflow-x-auto">
  <table class="w-full text-sm">
    <thead>
      <tr class="border-b text-left text-gray-500">
        <th class="py-2 px-1">Platform</th>
        <th class="px-1">Company</th>
        <th class="px-1">Title</th>
        <th class="px-1">Location</th>
        <th class="px-1 text-center">Score</th>
        <th class="px-1">Status</th>
      </tr>
    </thead>
    <tbody>
    {% for j in recent %}
      <tr class="border-b hover:bg-gray-50">
        <td class="py-2 px-1">{{ j.platform }}</td>
        <td class="px-1">{{ j.company[:20] if j.company else '—' }}</td>
        <td class="px-1">
          <a href="/jobs/{{ j.id }}" class="text-cyan-600 hover:underline">
            {{ j.title[:35] if j.title else '—' }}
          </a>
        </td>
        <td class="px-1 text-gray-500">{{ (j.location or '—')[:15] }}</td>
        <td class="px-1 text-center {{ score_class(j.match_score) }}">
          {{ fmt_score(j.match_score) }}
        </td>
        <td class="px-1">
          <span class="px-1.5 py-0.5 rounded text-xs
            {% if j.status == 'applied' %}bg-green-100 text-green-700
            {% elif j.status == 'matched' %}bg-yellow-100 text-yellow-700
            {% elif j.status == 'skipped' %}bg-gray-100 text-gray-500
            {% else %}bg-blue-100 text-blue-700{% endif %}">
            {{ j.status }}
          </span>
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% else %}
  <p class="text-gray-400 text-sm">No jobs discovered yet. Run Discovery to start.</p>
  {% endif %}
</div>
"""


@app.route("/")
def dashboard():
    db = _db()
    stats = {}
    pipeline = {}
    recent = []

    if db:
        try:
            stats = db.get_stats("today") or {}
        except Exception:
            pass
        try:
            pipeline = db.get_pipeline() or {}
        except Exception:
            pass
        try:
            recent = db.get_jobs(limit=10)
        except Exception:
            pass

    cards = [
        ("Jobs Discovered", stats.get("discovered", stats.get("today_discovered", 0)), "cyan"),
        ("Matched", stats.get("matched", stats.get("today_matched", 0)), "yellow"),
        ("Applied", stats.get("applied", stats.get("today_applied", 0)), "green"),
        ("Responses", stats.get("responses", stats.get("today_responses", 0)), "purple"),
    ]

    p_bars = [
        ("New", pipeline.get("new", 0), "blue"),
        ("Matched", pipeline.get("matched", 0), "yellow"),
        ("Applied", pipeline.get("applied", 0), "green"),
        ("Interview", pipeline.get("interview", 0), "purple"),
        ("Offer", pipeline.get("offer", 0), "amber"),
    ]
    max_p = max((c for _, c, _ in p_bars), default=1) or 1
    scale = round(80 / max_p, 2)

    content = render_template_string(
        _DASHBOARD_CONTENT,
        cards=cards,
        pipeline_bars=p_bars,
        scale=scale,
        recent=recent,
        score_class=_score_class,
        fmt_score=_fmt_score,
    )
    return _render("Dashboard", "dashboard", content)


# ═══════════════════════════════════════════════════════════════
#  JOBS LIST
# ═══════════════════════════════════════════════════════════════

_JOBS_CONTENT = """
<h1 class="text-2xl font-bold mb-4">🔍 Jobs ({{ total }})</h1>

<!-- filters -->
<form class="flex gap-2 mb-4 flex-wrap" method="get">
  <select name="platform" class="border rounded px-2 py-1 text-sm bg-white">
    <option value="">All platforms</option>
    {% for p in ['naukri','indeed','foundit','linkedin'] %}
    <option value="{{ p }}" {{ 'selected' if fplatform == p else '' }}>{{ p | capitalize }}</option>
    {% endfor %}
  </select>
  <select name="status" class="border rounded px-2 py-1 text-sm bg-white">
    <option value="">All statuses</option>
    {% for s in ['new','matched','applied','skipped','expired'] %}
    <option value="{{ s }}" {{ 'selected' if fstatus == s else '' }}>{{ s }}</option>
    {% endfor %}
  </select>
  <select name="min_score" class="border rounded px-2 py-1 text-sm bg-white">
    <option value="">Any score</option>
    <option value="40" {{ 'selected' if fmin == '40' else '' }}>40+</option>
    <option value="60" {{ 'selected' if fmin == '60' else '' }}>60+</option>
    <option value="80" {{ 'selected' if fmin == '80' else '' }}>80+</option>
  </select>
  <button class="bg-cyan-600 text-white px-3 py-1 rounded text-sm hover:bg-cyan-700">Filter</button>
  {% if fplatform or fstatus or fmin %}
  <a href="/jobs" class="text-sm text-gray-500 hover:text-red-500 self-center">✕ Clear</a>
  {% endif %}
</form>

<div class="bg-white rounded-lg shadow overflow-x-auto">
<table class="w-full text-sm">
  <thead>
    <tr class="border-b text-left text-gray-500 bg-gray-50">
      <th class="p-2">ID</th><th class="p-2">Platform</th>
      <th class="p-2">Company</th><th class="p-2">Title</th>
      <th class="p-2">Location</th><th class="p-2">Salary</th>
      <th class="p-2 text-center">Score</th><th class="p-2">Status</th>
      <th class="p-2">Date</th>
    </tr>
  </thead>
  <tbody>
  {% for j in jobs %}
    <tr class="border-b hover:bg-blue-50 {{ 'bg-green-50' if j.status == 'applied' else '' }}">
      <td class="p-2 text-gray-400">{{ j.id }}</td>
      <td class="p-2">{{ j.platform }}</td>
      <td class="p-2">{{ (j.company or '—')[:18] }}</td>
      <td class="p-2">
        <a href="/jobs/{{ j.id }}" class="text-cyan-600 hover:underline">
          {{ (j.title or '—')[:32] }}
        </a>
      </td>
      <td class="p-2 text-gray-500">{{ (j.location or '—')[:14] }}</td>
      <td class="p-2">
        {% if j.salary_min and j.salary_max %}
          {{ j.salary_min }}-{{ j.salary_max }} LPA
        {% elif j.salary_min %}
          {{ j.salary_min }}+ LPA
        {% else %}—{% endif %}
      </td>
      <td class="p-2 text-center {{ score_class(j.match_score) }}">
        {{ fmt_score(j.match_score) }}
      </td>
      <td class="p-2">
        <span class="px-1.5 py-0.5 rounded text-xs
          {% if j.status == 'applied' %}bg-green-100 text-green-700
          {% elif j.status == 'matched' %}bg-yellow-100 text-yellow-700
          {% elif j.status == 'skipped' %}bg-gray-100 text-gray-500
          {% else %}bg-blue-100 text-blue-700{% endif %}">
          {{ j.status }}
        </span>
      </td>
      <td class="p-2 text-gray-400 text-xs">{{ (j.discovered_at or '')[:10] }}</td>
    </tr>
  {% endfor %}
  {% if not jobs %}
    <tr><td colspan="9" class="p-6 text-center text-gray-400">No jobs match your filters.</td></tr>
  {% endif %}
  </tbody>
</table>
</div>

<!-- pagination -->
<div class="flex gap-2 mt-4 justify-center">
  {% if page > 1 %}
  <a href="?page={{ page-1 }}&platform={{ fplatform or '' }}&status={{ fstatus or '' }}&min_score={{ fmin or '' }}"
     class="px-3 py-1 bg-white border rounded text-sm hover:bg-gray-50">← Prev</a>
  {% endif %}
  <span class="px-3 py-1 text-sm text-gray-500">Page {{ page }}</span>
  {% if has_next %}
  <a href="?page={{ page+1 }}&platform={{ fplatform or '' }}&status={{ fstatus or '' }}&min_score={{ fmin or '' }}"
     class="px-3 py-1 bg-white border rounded text-sm hover:bg-gray-50">Next →</a>
  {% endif %}
</div>
"""


@app.route("/jobs")
def jobs_list():
    db = _db()
    if not db:
        return "Database unavailable", 500

    fplatform = request.args.get("platform", "")
    fstatus = request.args.get("status", "")
    fmin = request.args.get("min_score", "")
    page = request.args.get("page", 1, type=int)
    per_page = 30

    kwargs = {"limit": per_page * 20}
    if fplatform:
        kwargs["platform"] = fplatform
    if fstatus:
        kwargs["status"] = fstatus
    if fmin:
        try:
            kwargs["min_score"] = float(fmin)
        except ValueError:
            pass

    try:
        all_jobs = db.get_jobs(**kwargs)
    except Exception:
        all_jobs = []

    total = len(all_jobs)
    start = (page - 1) * per_page
    page_jobs = all_jobs[start:start + per_page]
    has_next = start + per_page < total

    content = render_template_string(
        _JOBS_CONTENT,
        jobs=page_jobs,
        total=total,
        page=page,
        has_next=has_next,
        fplatform=fplatform,
        fstatus=fstatus,
        fmin=fmin,
        score_class=_score_class,
        fmt_score=_fmt_score,
    )
    return _render("Jobs", "jobs", content)


# ═══════════════════════════════════════════════════════════════
#  JOB DETAIL
# ═══════════════════════════════════════════════════════════════

_JOB_DETAIL_CONTENT = """
<a href="/jobs" class="text-cyan-600 text-sm hover:underline">← Back to jobs</a>

<div class="bg-white rounded-lg shadow p-6 mt-2">
  <h1 class="text-xl font-bold">{{ job.title or 'Untitled' }}</h1>
  <p class="text-gray-500 mt-1">
    {{ job.company or 'Unknown' }} &middot; {{ job.location or 'N/A' }}
    &middot; {{ job.platform }}
  </p>

  <div class="flex flex-wrap gap-3 mt-3 text-sm">
    <span class="px-2 py-1 rounded bg-gray-100">
      Score: <b class="{{ score_class(job.match_score) }}">{{ fmt_score(job.match_score) }}</b>
    </span>
    <span class="px-2 py-1 rounded bg-gray-100">
      Status: <b>{{ job.status }}</b>
    </span>
    {% if job.salary_min %}
    <span class="px-2 py-1 rounded bg-gray-100">
      Salary: {{ job.salary_min }}–{{ job.salary_max or '?' }} LPA
    </span>
    {% endif %}
    {% if job.experience_min is not none %}
    <span class="px-2 py-1 rounded bg-gray-100">
      Exp: {{ job.experience_min }}–{{ job.experience_max or '?' }} yrs
    </span>
    {% endif %}
    {% if job.work_mode %}
    <span class="px-2 py-1 rounded bg-gray-100">{{ job.work_mode }}</span>
    {% endif %}
  </div>

  {% if job.url %}
  <p class="mt-3">
    <a href="{{ job.url }}" target="_blank" rel="noopener"
       class="text-cyan-600 text-sm hover:underline">
      Open on {{ job.platform }} ↗
    </a>
  </p>
  {% endif %}
</div>

{% if skills %}
<div class="bg-white rounded-lg shadow p-4 mt-4">
  <h2 class="font-semibold mb-2">🛠 Skills</h2>
  <div class="flex flex-wrap gap-2">
    {% for s in skills %}
    <span class="bg-green-100 text-green-700 px-2 py-1 rounded text-xs">{{ s }}</span>
    {% endfor %}
  </div>
</div>
{% endif %}

{% if match_details %}
<div class="bg-white rounded-lg shadow p-4 mt-4">
  <h2 class="font-semibold mb-2">🎯 Match Details</h2>
  <div class="text-sm space-y-1">
    {% for k, v in match_details.items() %}
    <div><span class="text-gray-500">{{ k }}:</span> {{ v }}</div>
    {% endfor %}
  </div>
</div>
{% endif %}

{% if job.description %}
<div class="bg-white rounded-lg shadow p-4 mt-4">
  <h2 class="font-semibold mb-2">📝 Job Description</h2>
  <div class="text-sm whitespace-pre-wrap text-gray-700 max-h-96 overflow-y-auto">{{ job.description[:5000] }}</div>
</div>
{% endif %}
"""


@app.route("/jobs/<int:job_id>")
def job_detail(job_id: int):
    db = _db()
    if not db:
        return "Database unavailable", 500

    job = None
    try:
        all_jobs = db.get_jobs(limit=100000)
        for j in all_jobs:
            if j.get("id") == job_id:
                job = j
                break
    except Exception:
        pass

    if not job:
        return _render("Not Found", "jobs", "<p class='text-red-500'>Job not found.</p>"), 404

    # Parse skills
    skills = job.get("skills", [])
    if isinstance(skills, str):
        try:
            skills = json.loads(skills)
        except Exception:
            skills = [s.strip() for s in skills.split(",") if s.strip()]
    if not isinstance(skills, list):
        skills = []

    # Parse match details
    match_details = job.get("match_details")
    if isinstance(match_details, str):
        try:
            match_details = json.loads(match_details)
        except Exception:
            match_details = None
    if not isinstance(match_details, dict) or match_details == {}:
        match_details = None

    content = render_template_string(
        _JOB_DETAIL_CONTENT,
        job=job,
        skills=skills,
        match_details=match_details,
        score_class=_score_class,
        fmt_score=_fmt_score,
    )
    return _render(f"Job #{job_id}", "jobs", content)


# ═══════════════════════════════════════════════════════════════
#  APPLICATIONS
# ═══════════════════════════════════════════════════════════════

_APPLICATIONS_CONTENT = """
<h1 class="text-2xl font-bold mb-4">📝 Applications ({{ apps | length }})</h1>

<div class="bg-white rounded-lg shadow overflow-x-auto">
<table class="w-full text-sm">
  <thead>
    <tr class="border-b text-left text-gray-500 bg-gray-50">
      <th class="p-2">ID</th><th class="p-2">Job</th>
      <th class="p-2">Platform</th><th class="p-2">Method</th>
      <th class="p-2">Status</th><th class="p-2">Applied</th>
      <th class="p-2 text-center">Follow-ups</th>
    </tr>
  </thead>
  <tbody>
  {% for a in apps %}
    <tr class="border-b hover:bg-blue-50">
      <td class="p-2">{{ a.id }}</td>
      <td class="p-2">
        <a href="/jobs/{{ a.job_id }}" class="text-cyan-600 hover:underline">#{{ a.job_id }}</a>
      </td>
      <td class="p-2">{{ a.platform }}</td>
      <td class="p-2">{{ a.method }}</td>
      <td class="p-2">
        <span class="px-1.5 py-0.5 rounded text-xs
          {% if a.status == 'interview' %}bg-green-100 text-green-700
          {% elif a.status == 'shortlisted' %}bg-green-100 text-green-700
          {% elif a.status == 'rejected' %}bg-red-100 text-red-700
          {% elif a.status == 'offer' %}bg-yellow-100 text-yellow-800 font-bold
          {% elif a.status == 'ghosted' %}bg-gray-100 text-gray-500
          {% else %}bg-blue-100 text-blue-700{% endif %}">
          {{ a.status }}
        </span>
      </td>
      <td class="p-2 text-gray-400">{{ (a.applied_at or '')[:10] }}</td>
      <td class="p-2 text-center">{{ a.follow_up_count or 0 }}</td>
    </tr>
  {% endfor %}
  {% if not apps %}
    <tr><td colspan="7" class="p-6 text-center text-gray-400">No applications yet.</td></tr>
  {% endif %}
  </tbody>
</table>
</div>
"""


@app.route("/applications")
def applications():
    db = _db()
    apps = []
    if db:
        try:
            apps = db.get_applications(limit=100)
        except Exception:
            pass

    content = render_template_string(_APPLICATIONS_CONTENT, apps=apps)
    return _render("Applications", "apps", content)


# ═══════════════════════════════════════════════════════════════
#  PIPELINE
# ═══════════════════════════════════════════════════════════════

_PIPELINE_CONTENT = """
<h1 class="text-2xl font-bold mb-4">📈 Application Pipeline</h1>

<div class="bg-white rounded-lg shadow p-6">
{% for label, count, color in stages %}
  <div class="flex items-center mb-3">
    <div class="w-36 text-sm font-medium">{{ label }}</div>
    <div class="flex-1 bg-gray-100 rounded h-7 mr-3 overflow-hidden">
      <div class="bar bg-{{ color }}-500 h-7 flex items-center pl-2"
           style="width:{{ (count / max_val * 100) | round(1) if max_val else 0 }}%">
        {% if count > 0 %}
        <span class="text-white text-xs font-bold">{{ count }}</span>
        {% endif %}
      </div>
    </div>
    <div class="w-10 text-right font-bold text-sm">{{ count }}</div>
  </div>
{% endfor %}
</div>

<!-- summary -->
<div class="grid grid-cols-2 md:grid-cols-4 gap-4 mt-6">
  {% for label, count, color in stages[:4] %}
  <div class="bg-white rounded-lg shadow p-3 text-center">
    <div class="text-2xl font-bold text-{{ color }}-600">{{ count }}</div>
    <div class="text-xs text-gray-500">{{ label }}</div>
  </div>
  {% endfor %}
</div>
"""


@app.route("/pipeline")
def pipeline():
    db = _db()
    pipe = {}
    if db:
        try:
            pipe = db.get_pipeline() or {}
        except Exception:
            pass

    stages = [
        ("🔍 Discovered", pipe.get("new", 0), "cyan"),
        ("🎯 Matched", pipe.get("matched", 0), "yellow"),
        ("📤 Applied", pipe.get("applied", 0), "green"),
        ("👁 Viewed", pipe.get("viewed", 0), "blue"),
        ("⭐ Shortlisted", pipe.get("shortlisted", 0), "indigo"),
        ("🎤 Interview", pipe.get("interview", 0), "purple"),
        ("🎁 Offer", pipe.get("offer", 0), "amber"),
        ("❌ Rejected", pipe.get("rejected", 0), "red"),
        ("👻 Ghosted", pipe.get("ghosted", 0), "gray"),
        ("⏭ Skipped", pipe.get("skipped", 0), "gray"),
    ]
    max_val = max((c for _, c, _ in stages), default=1) or 1

    content = render_template_string(
        _PIPELINE_CONTENT, stages=stages, max_val=max_val
    )
    return _render("Pipeline", "pipeline", content)


# ═══════════════════════════════════════════════════════════════
#  PLATFORMS
# ═══════════════════════════════════════════════════════════════

_PLATFORMS_CONTENT = """
<h1 class="text-2xl font-bold mb-4">🌐 Platform Status</h1>

<div class="grid grid-cols-1 md:grid-cols-2 gap-4">
{% for p in platforms %}
  <div class="bg-white rounded-lg shadow p-4 border-l-4
    {% if p.logged_in %}border-green-500
    {% elif p.enabled %}border-yellow-400
    {% else %}border-gray-300{% endif %}">

    <div class="flex justify-between items-center">
      <h3 class="font-bold text-lg">{{ p.name }}</h3>
      <span class="text-xs px-2 py-1 rounded
        {% if p.status == 'active' %}bg-green-100 text-green-700
        {% elif p.status == 'cooldown' %}bg-yellow-100 text-yellow-700
        {% elif p.status == 'banned' %}bg-red-100 text-red-700
        {% else %}bg-gray-100 text-gray-500{% endif %}">
        {{ p.status }}
      </span>
    </div>

    <div class="text-sm text-gray-500 mt-2 space-x-2">
      <span>Today: <b>{{ p.daily_applied }}/{{ p.max_daily }}</b></span>
      <span>&middot;</span>
      <span>Total: <b>{{ p.total_applied }}</b></span>
      <span>&middot;</span>
      <span>
        {% if p.logged_in %}✅ Logged in
        {% elif p.enabled %}🔑 Not logged in
        {% else %}⬜ Disabled{% endif %}
      </span>
    </div>
  </div>
{% endfor %}
</div>
"""


@app.route("/platforms")
def platforms():
    db = _db()
    platform_data = []
    names = {
        "naukri": "Naukri", "indeed": "Indeed",
        "foundit": "Foundit", "linkedin": "LinkedIn",
    }

    for key, name in names.items():
        pconf = PLATFORM_CONFIG.get(key, {})
        info = {
            "name": name,
            "key": key,
            "enabled": pconf.get("enabled", False),
            "max_daily": pconf.get("max_daily_applications", 0),
            "logged_in": False,
            "daily_applied": 0,
            "total_applied": 0,
            "status": "disabled",
        }

        if db and info["enabled"]:
            try:
                sess = db.get_platform_session(key)
                if sess:
                    info["logged_in"] = bool(sess.get("logged_in"))
                    info["daily_applied"] = sess.get("daily_applied", 0)
                    info["total_applied"] = sess.get("total_applied", 0)
                    info["status"] = sess.get("status", "active")
                else:
                    info["status"] = "no session"
            except Exception:
                pass

        platform_data.append(info)

    content = render_template_string(
        _PLATFORMS_CONTENT, platforms=platform_data
    )
    return _render("Platforms", "platforms", content)


# ═══════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.route("/api/stats")
def api_stats():
    db = _db()
    if not db:
        return jsonify({"error": "db unavailable"}), 500
    try:
        return jsonify({
            "stats": db.get_stats("today") or {},
            "pipeline": db.get_pipeline() or {},
            "tables": db.get_table_info() or {},
            "timestamp": datetime.now().isoformat(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/jobs")
def api_jobs():
    db = _db()
    if not db:
        return jsonify({"error": "db unavailable"}), 500

    kwargs = {"limit": min(request.args.get("limit", 50, type=int), 500)}
    if request.args.get("platform"):
        kwargs["platform"] = request.args["platform"]
    if request.args.get("status"):
        kwargs["status"] = request.args["status"]
    if request.args.get("min_score"):
        try:
            kwargs["min_score"] = float(request.args["min_score"])
        except ValueError:
            pass

    try:
        jobs = db.get_jobs(**kwargs)
        return jsonify({"jobs": jobs, "count": len(jobs)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 50)
    print("  Job Agent Web Dashboard")
    print("  http://localhost:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False)