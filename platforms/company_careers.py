 #!/usr/bin/env python3
"""
platforms/company_careers.py — Generic company career page scraper.

Handles common career page platforms:
    - Lever        (jobs.lever.co/*)
    - Greenhouse   (boards.greenhouse.io/*)
    - Workday      (*.myworkdayjobs.com/*)
    - Generic HTML (any page with job listings)

Usage:
    scraper = CareerPageScraper(browser_engine)
    jobs = scraper.scrape_careers_page("https://company.com/careers")
"""

import re
import time
import json
import random
from datetime import datetime
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Any, Optional

from config import USER_PROFILE, STEALTH_CONFIG
from core.logger import get_logger
from core.db import get_db

logger = get_logger("company_careers")

# Common job-related keywords for link detection
_JOB_KEYWORDS = {
    "engineer", "developer", "sde", "software", "backend", "frontend",
    "fullstack", "full-stack", "full stack", "java", "python", "node",
    "devops", "data", "analyst", "designer", "product", "manager",
    "intern", "associate", "lead", "senior", "junior", "sre",
}

# Platform patterns
_LEVER_PATTERN = re.compile(r"jobs\.lever\.co/([^/]+)", re.I)
_GREENHOUSE_PATTERN = re.compile(r"boards\.greenhouse\.io/([^/]+)", re.I)
_WORKDAY_PATTERN = re.compile(r"([\w.-]+)\.myworkdayjobs\.com", re.I)


class CareerPageScraper:
    """
    Generic career page scraper.
    Detects the career page platform and uses the appropriate extraction
    strategy.  Falls back to generic HTML link scanning.
    """

    def __init__(self, browser_engine=None):
        self.browser = browser_engine
        self.db = get_db()
        self.logger = logger
        self._page = None

    # ══════════════════════════════════════════════════════════
    #  PUBLIC: scrape career page
    # ══════════════════════════════════════════════════════════
    def scrape_careers_page(self, url: str) -> List[Dict[str, Any]]:
        """
        Scrape a company careers page for job listings.

        Args:
            url: Full URL of the company careers page.

        Returns:
            List of job dicts with keys:
                platform, platform_job_id, url, title, company,
                location, description, posted_date, salary_text,
                experience_text, skills
        """
        self.logger.info("Scraping career page: %s", url)
        jobs: List[Dict[str, Any]] = []

        try:
            # Detect platform
            platform_type = self._detect_platform(url)
            self.logger.info("  Detected platform type: %s", platform_type)

            # Launch browser
            if not self.browser:
                self.logger.error("No browser engine available")
                return jobs

            self._page = self.browser.launch("careers", headless=True)
            self._navigate(url)

            # Route to handler
            if platform_type == "lever":
                jobs = self._scrape_lever(url)
            elif platform_type == "greenhouse":
                jobs = self._scrape_greenhouse(url)
            elif platform_type == "workday":
                jobs = self._scrape_workday(url)
            else:
                jobs = self._scrape_generic(url)

            self.logger.info("  Found %d jobs on career page", len(jobs))

        except Exception as exc:
            self.logger.error("Career page scrape failed: %s", exc, exc_info=True)

        finally:
            if self._page:
                try:
                    self.browser.close("careers")
                except Exception:
                    pass
                self._page = None

        return jobs

    # ══════════════════════════════════════════════════════════
    #  PUBLIC: extract single job details
    # ══════════════════════════════════════════════════════════
    def extract_job_details(self, job_url: str) -> Dict[str, Any]:
        """
        Navigate to a single job page and extract full details.

        Returns:
            Dict with title, company, location, description,
            skills, salary_text, experience_text, etc.
        """
        self.logger.info("Extracting job details: %s", job_url)
        details: Dict[str, Any] = {"url": job_url}

        try:
            if not self.browser:
                return details

            page = self.browser.launch("careers_detail", headless=True)
            page.goto(job_url, timeout=20000, wait_until="domcontentloaded")
            time.sleep(random.uniform(1.5, 3.0))

            # Try common selectors for job content
            details["title"] = self._extract_text(page, [
                "h1.job-title", "h1.posting-headline",
                "h1[class*='title']", "h1[class*='job']",
                ".job-title h1", "h1",
            ])

            details["company"] = self._extract_text(page, [
                ".company-name", "[class*='company']",
                "[data-company]", ".employer-name",
            ]) or self._company_from_url(job_url)

            details["location"] = self._extract_text(page, [
                ".location", "[class*='location']",
                "[data-location]", ".job-location",
                "[class*='geo']",
            ])

            details["description"] = self._extract_text(page, [
                ".job-description", ".posting-description",
                "[class*='description']", ".job-details",
                "#job-description", "article",
                ".content-wrapper", "main",
            ])

            # Extract skills from description
            if details.get("description"):
                details["skills"] = self._extract_skills(details["description"])
            else:
                details["skills"] = []

            self.browser.close("careers_detail")

        except Exception as exc:
            self.logger.error("Job detail extraction failed: %s", exc)
            try:
                self.browser.close("careers_detail")
            except Exception:
                pass

        return details

    # ══════════════════════════════════════════════════════════
    #  PLATFORM DETECTION
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def _detect_platform(url: str) -> str:
        """Detect career page platform from URL."""
        if _LEVER_PATTERN.search(url):
            return "lever"
        if _GREENHOUSE_PATTERN.search(url):
            return "greenhouse"
        if _WORKDAY_PATTERN.search(url):
            return "workday"
        return "generic"

    # ══════════════════════════════════════════════════════════
    #  LEVER SCRAPER
    # ══════════════════════════════════════════════════════════
    def _scrape_lever(self, base_url: str) -> List[Dict[str, Any]]:
        """Scrape Lever-hosted career page."""
        jobs: List[Dict[str, Any]] = []
        page = self._page
        company_match = _LEVER_PATTERN.search(base_url)
        company = company_match.group(1) if company_match else "Unknown"

        try:
            # Lever lists jobs as <a> inside <div class="posting">
            postings = page.query_selector_all(".posting")
            if not postings:
                postings = page.query_selector_all("[data-qa='posting-name']")

            for posting in postings:
                try:
                    title_el = posting.query_selector(
                        ".posting-name, [data-qa='posting-name'], h5, a"
                    )
                    title = title_el.inner_text().strip() if title_el else ""

                    link_el = posting.query_selector("a.posting-title, a")
                    href = link_el.get_attribute("href") if link_el else ""
                    job_url = urljoin(base_url, href) if href else ""

                    loc_el = posting.query_selector(
                        ".posting-categories .sort-by-location, "
                        ".location, [class*='location']"
                    )
                    location = loc_el.inner_text().strip() if loc_el else ""

                    dept_el = posting.query_selector(
                        ".posting-categories .sort-by-team, "
                        "[class*='department'], [class*='team']"
                    )
                    department = dept_el.inner_text().strip() if dept_el else ""

                    if title:
                        jobs.append({
                            "platform": "company_careers",
                            "platform_job_id": self._id_from_url(job_url),
                            "url": job_url,
                            "title": title,
                            "company": company.replace("-", " ").title(),
                            "location": location,
                            "description": department,
                            "posted_date": "",
                            "salary_text": "",
                            "experience_text": "",
                            "skills": [],
                            "source_type": "lever",
                        })
                except Exception:
                    continue

        except Exception as exc:
            self.logger.error("Lever scrape error: %s", exc)

        return jobs

    # ══════════════════════════════════════════════════════════
    #  GREENHOUSE SCRAPER
    # ══════════════════════════════════════════════════════════
    def _scrape_greenhouse(self, base_url: str) -> List[Dict[str, Any]]:
        """Scrape Greenhouse-hosted career page."""
        jobs: List[Dict[str, Any]] = []
        page = self._page
        company_match = _GREENHOUSE_PATTERN.search(base_url)
        company = company_match.group(1) if company_match else "Unknown"

        try:
            # Greenhouse uses <div class="opening"> with <a>
            openings = page.query_selector_all(".opening")
            if not openings:
                openings = page.query_selector_all(
                    "[class*='job-post'], [class*='opening'], tr"
                )

            for opening in openings:
                try:
                    link_el = opening.query_selector("a")
                    if not link_el:
                        continue

                    title = link_el.inner_text().strip()
                    href = link_el.get_attribute("href") or ""
                    job_url = urljoin(base_url, href)

                    loc_el = opening.query_selector(
                        ".location, [class*='location']"
                    )
                    location = loc_el.inner_text().strip() if loc_el else ""

                    if title:
                        jobs.append({
                            "platform": "company_careers",
                            "platform_job_id": self._id_from_url(job_url),
                            "url": job_url,
                            "title": title,
                            "company": company.replace("-", " ").title(),
                            "location": location,
                            "description": "",
                            "posted_date": "",
                            "salary_text": "",
                            "experience_text": "",
                            "skills": [],
                            "source_type": "greenhouse",
                        })
                except Exception:
                    continue

        except Exception as exc:
            self.logger.error("Greenhouse scrape error: %s", exc)

        return jobs

    # ══════════════════════════════════════════════════════════
    #  WORKDAY SCRAPER
    # ══════════════════════════════════════════════════════════
    def _scrape_workday(self, base_url: str) -> List[Dict[str, Any]]:
        """Scrape Workday-hosted career page."""
        jobs: List[Dict[str, Any]] = []
        page = self._page
        company_match = _WORKDAY_PATTERN.search(base_url)
        company = company_match.group(1) if company_match else "Unknown"

        # Workday pages are heavily JS-rendered; wait for content
        time.sleep(3)

        try:
            # Workday common selectors
            listings = page.query_selector_all(
                "[data-automation-id='jobTitle'], "
                ".css-19uc56f, li[class*='job']"
            )
            if not listings:
                # Try broader approach
                listings = page.query_selector_all("a[href*='/job/']")

            for item in listings:
                try:
                    title = item.inner_text().strip()
                    href = item.get_attribute("href") or ""
                    job_url = urljoin(base_url, href) if href else ""

                    if title and len(title) > 3:
                        jobs.append({
                            "platform": "company_careers",
                            "platform_job_id": self._id_from_url(job_url),
                            "url": job_url,
                            "title": title,
                            "company": company.replace(".", " ").title(),
                            "location": "",
                            "description": "",
                            "posted_date": "",
                            "salary_text": "",
                            "experience_text": "",
                            "skills": [],
                            "source_type": "workday",
                        })
                except Exception:
                    continue

        except Exception as exc:
            self.logger.error("Workday scrape error: %s", exc)

        return jobs

    # ══════════════════════════════════════════════════════════
    #  GENERIC SCRAPER
    # ══════════════════════════════════════════════════════════
    def _scrape_generic(self, base_url: str) -> List[Dict[str, Any]]:
        """Scrape any career page by finding job-related links."""
        jobs: List[Dict[str, Any]] = []
        page = self._page
        company = self._company_from_url(base_url)

        try:
            # Scroll to load lazy content
            for _ in range(3):
                page.evaluate("window.scrollBy(0, window.innerHeight)")
                time.sleep(0.8)
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.5)

            # Strategy 1: find links that look like job listings
            all_links = page.query_selector_all("a[href]")
            seen_urls = set()

            for link in all_links:
                try:
                    href = link.get_attribute("href") or ""
                    text = link.inner_text().strip()

                    if not text or len(text) < 4 or len(text) > 200:
                        continue

                    full_url = urljoin(base_url, href)
                    if full_url in seen_urls:
                        continue

                    # Check if URL or text looks like a job
                    url_lower = full_url.lower()
                    text_lower = text.lower()

                    is_job_url = any(
                        kw in url_lower
                        for kw in (
                            "/job/", "/jobs/", "/career",
                            "/position", "/opening", "/vacancy",
                            "/apply", "jobid=", "job_id=",
                        )
                    )
                    is_job_text = any(
                        kw in text_lower for kw in _JOB_KEYWORDS
                    )

                    if is_job_url or is_job_text:
                        # Skip navigation / non-job links
                        skip_words = {
                            "home", "about", "contact", "login",
                            "sign in", "register", "blog", "news",
                            "privacy", "terms", "cookie",
                        }
                        if any(sw in text_lower for sw in skip_words):
                            continue

                        seen_urls.add(full_url)
                        jobs.append({
                            "platform": "company_careers",
                            "platform_job_id": self._id_from_url(full_url),
                            "url": full_url,
                            "title": text[:150],
                            "company": company,
                            "location": "",
                            "description": "",
                            "posted_date": "",
                            "salary_text": "",
                            "experience_text": "",
                            "skills": [],
                            "source_type": "generic",
                        })
                except Exception:
                    continue

            # Strategy 2: look for structured job cards
            card_selectors = [
                ".job-card", ".job-listing", ".job-item",
                "[class*='job-card']", "[class*='jobCard']",
                "[class*='job-listing']", "[class*='vacancy']",
                ".career-item", "[class*='position-card']",
                "li.job", "div.job",
            ]
            for selector in card_selectors:
                cards = page.query_selector_all(selector)
                if not cards:
                    continue

                for card in cards:
                    try:
                        title_el = card.query_selector(
                            "h2, h3, h4, a, .title, [class*='title']"
                        )
                        if not title_el:
                            continue
                        title = title_el.inner_text().strip()
                        if not title or len(title) < 4:
                            continue

                        link_el = card.query_selector("a[href]")
                        href = link_el.get_attribute("href") if link_el else ""
                        job_url = urljoin(base_url, href) if href else ""

                        if job_url in seen_urls:
                            continue
                        seen_urls.add(job_url)

                        loc_el = card.query_selector(
                            ".location, [class*='location']"
                        )
                        location = (
                            loc_el.inner_text().strip() if loc_el else ""
                        )

                        jobs.append({
                            "platform": "company_careers",
                            "platform_job_id": self._id_from_url(job_url),
                            "url": job_url,
                            "title": title[:150],
                            "company": company,
                            "location": location,
                            "description": "",
                            "posted_date": "",
                            "salary_text": "",
                            "experience_text": "",
                            "skills": [],
                            "source_type": "generic_card",
                        })
                    except Exception:
                        continue
                break  # found working selector, stop trying others

        except Exception as exc:
            self.logger.error("Generic scrape error: %s", exc)

        return jobs

    # ══════════════════════════════════════════════════════════
    #  INTERNAL HELPERS
    # ══════════════════════════════════════════════════════════
    def _navigate(self, url: str):
        """Navigate page with wait."""
        self._page.goto(url, timeout=25000, wait_until="domcontentloaded")
        time.sleep(random.uniform(2.0, 4.0))

    @staticmethod
    def _id_from_url(url: str) -> str:
        """Generate a stable ID from a URL."""
        if not url:
            return ""
        # Use path + query as ID
        parsed = urlparse(url)
        return f"{parsed.netloc}{parsed.path}"[:200]

    @staticmethod
    def _company_from_url(url: str) -> str:
        """Extract company name from URL domain."""
        try:
            parsed = urlparse(url)
            host = parsed.netloc.lower()
            # Remove common prefixes
            for prefix in ("www.", "careers.", "jobs.", "boards."):
                if host.startswith(prefix):
                    host = host[len(prefix):]
            # Take first part before dot
            name = host.split(".")[0]
            return name.replace("-", " ").replace("_", " ").title()
        except Exception:
            return "Unknown"

    @staticmethod
    def _extract_text(page, selectors: List[str]) -> str:
        """Try multiple selectors, return first match text."""
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el:
                    text = el.inner_text().strip()
                    if text and len(text) > 1:
                        return text
            except Exception:
                continue
        return ""

    @staticmethod
    def _extract_skills(text: str) -> List[str]:
        """Extract tech skills from job description text."""
        known_skills = [
            "Python", "Java", "JavaScript", "TypeScript", "Go", "Rust",
            "C++", "C#", "Ruby", "PHP", "Swift", "Kotlin", "Scala",
            "React", "Angular", "Vue.js", "Node.js", "Express",
            "Spring Boot", "Django", "Flask", "FastAPI", "Rails",
            "AWS", "Azure", "GCP", "Docker", "Kubernetes", "Terraform",
            "PostgreSQL", "MySQL", "MongoDB", "Redis", "Elasticsearch",
            "Kafka", "RabbitMQ", "GraphQL", "REST", "gRPC",
            "Git", "CI/CD", "Jenkins", "Linux", "Nginx",
            "Machine Learning", "Deep Learning", "TensorFlow", "PyTorch",
            "SQL", "NoSQL", "Microservices", "Agile", "Scrum",
        ]

        found = []
        text_lower = text.lower()
        for skill in known_skills:
            if skill.lower() in text_lower:
                found.append(skill)

        return list(dict.fromkeys(found))  # deduplicate preserving order


# ═══════════════════════════════════════════════════════════════
#  STANDALONE TEST
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("  CareerPageScraper — standalone test")
    print("=" * 60)

    # Test platform detection
    test_urls = [
        ("https://jobs.lever.co/stripe", "lever"),
        ("https://boards.greenhouse.io/discord", "greenhouse"),
        ("https://salesforce.myworkdayjobs.com/en-US/jobs", "workday"),
        ("https://careers.google.com/jobs", "generic"),
        ("https://www.microsoft.com/en-us/careers", "generic"),
    ]

    print("\n--- Platform detection ---")
    for url, expected in test_urls:
        detected = CareerPageScraper._detect_platform(url)
        status = "✅" if detected == expected else "❌"
        print(f"  {status} {url}")
        print(f"     Expected: {expected}, Got: {detected}")

    print("\n--- Company from URL ---")
    for url, _ in test_urls:
        company = CareerPageScraper._company_from_url(url)
        print(f"  {url} → {company}")

    print("\n--- Skill extraction ---")
    sample_desc = """
    We're looking for a Full Stack Developer with experience in
    Java, Spring Boot, React, and PostgreSQL. Knowledge of Docker,
    Kubernetes, and AWS is preferred. Must know Git and CI/CD.
    """
    skills = CareerPageScraper._extract_skills(sample_desc)
    print(f"  Found: {skills}")

    print("\nNote: Full scraping test requires BrowserEngine.")
    print("Usage: scraper = CareerPageScraper(browser); "
          "scraper.scrape_careers_page('https://...')")
    print("Done.")