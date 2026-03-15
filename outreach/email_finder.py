#!/usr/bin/env python3
"""
outreach/email_finder.py — Find HR/recruiter email addresses.

Strategies (in order):
  1. Database cache (contacts table)
  2. Parse job page for email addresses
  3. Hunter.io free API (50/month)
  4. Pattern guessing (hr@, careers@, jobs@, recruiting@)
  5. MX record verification

Usage:
    from outreach.email_finder import EmailFinder

    finder = EmailFinder()
    contacts = finder.find_hr_email("Razorpay", "razorpay.com")
    for c in contacts:
        print(f"{c['email']} ({c['confidence']}%) — {c['source']}")

    verified = finder.verify_email("hr@razorpay.com")
"""

import os
import re
import json
import time
import socket
import traceback as tb_module
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from core.logger import get_logger
from core.db import get_db

logger = get_logger("outreach.email_finder")

# ── Hunter.io API key (optional, 50 free/month) ────────────────
try:
    from config import HUNTER_API_KEY
except ImportError:
    HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")

# ── Common HR email patterns ───────────────────────────────────
_HR_PREFIXES = [
    "hr", "careers", "jobs", "recruiting", "recruitment",
    "talent", "people", "hiring", "career", "join",
    "apply", "resume", "resumes", "cv", "info",
    "contact", "admin", "humanresources",
]

# ── Common company domain patterns ─────────────────────────────
_DOMAIN_SUFFIXES = [".com", ".in", ".co.in", ".io", ".co",
                    ".org", ".net", ".tech", ".ai"]

# ── Email regex ────────────────────────────────────────────────
_EMAIL_RE = re.compile(
    r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
    re.IGNORECASE
)

# ── Blacklisted email patterns (not real HR) ───────────────────
_EMAIL_BLACKLIST = {
    "noreply", "no-reply", "do-not-reply", "donotreply",
    "mailer-daemon", "postmaster", "abuse", "support",
    "webmaster", "newsletter", "unsubscribe", "notifications",
    "alerts", "feedback", "security", "privacy",
    "example.com", "example.org", "test.com",
    "sentry.io", "github.com", "google.com",
    "facebook.com", "twitter.com", "linkedin.com",
}

# ── Blacklisted file-hosting domains ───────────────────────────
_DOMAIN_BLACKLIST = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "rediffmail.com", "protonmail.com", "ymail.com",
    "googlemail.com", "aol.com", "icloud.com",
    "github.com", "linkedin.com", "facebook.com",
    "twitter.com", "instagram.com", "naukri.com",
    "indeed.com", "foundit.in", "monster.com",
    "glassdoor.com", "ambitionbox.com",
}


# ═══════════════════════════════════════════════════════════════════
# EMAIL FINDER
# ═══════════════════════════════════════════════════════════════════

class EmailFinder:
    """
    Multi-strategy email finder for HR/recruiter contacts.

    Strategies are tried in order of reliability:
      1. DB cache (contacts table from previous finds)
      2. Job page scraping (regex extraction)
      3. Hunter.io API (if key configured)
      4. Pattern guessing with MX verification
    """

    def __init__(self):
        self.db = get_db()
        self._hunter_key = HUNTER_API_KEY
        self._cache: Dict[str, List[Dict]] = {}

        logger.info("EmailFinder ready (hunter=%s)",
                     "yes" if self._hunter_key else "no")

    # ═══════════════════════════════════════════════════════════
    # PUBLIC — Find HR Email
    # ═══════════════════════════════════════════════════════════

    def find_hr_email(self, company: str,
                      domain: Optional[str] = None,
                      job_url: Optional[str] = None,
                      job_description: Optional[str] = None,
                      ) -> List[Dict]:
        """
        Find HR/recruiter email for a company.

        Tries multiple strategies and returns all found emails
        sorted by confidence (highest first).

        Args:
            company: Company name (e.g. "Razorpay").
            domain: Company domain (e.g. "razorpay.com").
                    None → guessed from company name.
            job_url: Job listing URL (for page scraping).
            job_description: JD text (for inline email extraction).

        Returns:
            List of {email, name, title, confidence, source, verified}
            sorted by confidence descending.
        """
        results: List[Dict] = []
        company_clean = company.strip()
        if not company_clean:
            return []

        # Guess domain if not provided
        if not domain:
            domain = self._guess_domain(company_clean)

        logger.info("Finding HR email for '%s' (domain=%s)",
                     company_clean, domain or "unknown")

        # ── Strategy 1: DB cache ──
        cached = self._find_in_db(company_clean)
        if cached:
            logger.debug("  DB cache: %d contacts", len(cached))
            results.extend(cached)

        # ── Strategy 2: Extract from JD text ──
        if job_description:
            from_jd = self._extract_from_text(
                job_description, company_clean, domain)
            if from_jd:
                logger.debug("  JD extraction: %d emails",
                             len(from_jd))
                results.extend(from_jd)

        # ── Strategy 3: Scrape job page ──
        if job_url and not results:
            from_page = self._extract_from_url(
                job_url, company_clean, domain)
            if from_page:
                logger.debug("  Page scrape: %d emails",
                             len(from_page))
                results.extend(from_page)

        # ── Strategy 4: Hunter.io ──
        if self._hunter_key and domain and not results:
            from_hunter = self._hunter_search(
                domain, company_clean)
            if from_hunter:
                logger.debug("  Hunter.io: %d emails",
                             len(from_hunter))
                results.extend(from_hunter)

        # ── Strategy 5: Pattern guess ──
        if domain and not results:
            guessed = self._guess_patterns(company_clean, domain)
            if guessed:
                logger.debug("  Pattern guess: %d candidates",
                             len(guessed))
                results.extend(guessed)

        # Deduplicate by email
        seen = set()
        unique: List[Dict] = []
        for r in results:
            email = r.get("email", "").lower()
            if email and email not in seen:
                seen.add(email)
                unique.append(r)

        # Sort by confidence
        unique.sort(key=lambda x: x.get("confidence", 0),
                     reverse=True)

        # Save new contacts to DB
        for contact in unique:
            self._save_contact(contact, company_clean)

        logger.info("  Found %d unique emails for '%s'",
                     len(unique), company_clean)
        return unique

    # ═══════════════════════════════════════════════════════════
    # PUBLIC — Verify Email
    # ═══════════════════════════════════════════════════════════

    def verify_email(self, email: str) -> bool:
        """
        Verify an email address via MX record check.

        This checks if the domain has valid MX records (can receive
        mail).  It does NOT verify the specific mailbox exists.

        Returns:
            True if domain has MX records, False otherwise.
        """
        if not email or "@" not in email:
            return False

        domain = email.split("@")[1].strip().lower()

        try:
            import dns.resolver
            answers = dns.resolver.resolve(domain, "MX")
            return len(answers) > 0
        except ImportError:
            # Fallback: socket-based MX check
            return self._verify_mx_socket(domain)
        except Exception:
            return self._verify_mx_socket(domain)

    def _verify_mx_socket(self, domain: str) -> bool:
        """Fallback MX check using socket."""
        try:
            socket.setdefaulttimeout(5)
            socket.getaddrinfo(domain, 25)
            return True
        except (socket.gaierror, socket.timeout, OSError):
            pass

        # Try common mail server patterns
        for prefix in ["mx", "mail", "smtp", "mx1"]:
            try:
                socket.getaddrinfo(f"{prefix}.{domain}", 25)
                return True
            except (socket.gaierror, socket.timeout, OSError):
                continue

        return False

    # ═══════════════════════════════════════════════════════════
    # PUBLIC — Guess Patterns
    # ═══════════════════════════════════════════════════════════

    def guess_patterns(self, company: str,
                       domain: str) -> List[str]:
        """
        Generate likely HR email addresses for a domain.

        Returns:
            List of email addresses (not verified).
        """
        results = self._guess_patterns(company, domain)
        return [r["email"] for r in results]

    # ═══════════════════════════════════════════════════════════
    # STRATEGY 1 — Database Cache
    # ═══════════════════════════════════════════════════════════

    def _find_in_db(self, company: str) -> List[Dict]:
        """Look up contacts in database."""
        try:
            contacts = self.db.get_contacts(company=company)
            results = []
            for c in contacts:
                email = c.get("email", "")
                if email and self._is_useful_email(email):
                    results.append({
                        "email": email,
                        "name": c.get("name", ""),
                        "title": c.get("title", ""),
                        "confidence": 90 if c.get("verified") else 70,
                        "source": "database",
                        "verified": bool(c.get("verified")),
                    })
            return results
        except Exception as e:
            logger.debug("DB lookup failed: %s", e)
            return []

    # ═══════════════════════════════════════════════════════════
    # STRATEGY 2 — Extract from Text / JD
    # ═══════════════════════════════════════════════════════════

    def _extract_from_text(self, text: str, company: str,
                           domain: Optional[str]) -> List[Dict]:
        """Extract email addresses from text (JD, page content)."""
        if not text:
            return []

        emails = _EMAIL_RE.findall(text)
        results = []

        for email in emails:
            email = email.lower().strip()
            if not self._is_useful_email(email):
                continue

            # Score by relevance
            confidence = 50
            email_domain = email.split("@")[1]

            # Higher confidence if matches company domain
            if domain and email_domain == domain:
                confidence = 80
            elif company.lower().replace(" ", "") in email_domain:
                confidence = 75

            # HR-specific prefix boost
            prefix = email.split("@")[0]
            if any(hr in prefix for hr in
                   ["hr", "career", "recruit", "talent",
                    "hiring", "jobs", "people"]):
                confidence = min(95, confidence + 15)

            results.append({
                "email": email,
                "name": "",
                "title": "HR" if "hr" in prefix else "",
                "confidence": confidence,
                "source": "text_extraction",
                "verified": False,
            })

        return results

    # ═══════════════════════════════════════════════════════════
    # STRATEGY 3 — Scrape Job Page
    # ═══════════════════════════════════════════════════════════

    def _extract_from_url(self, url: str, company: str,
                          domain: Optional[str]) -> List[Dict]:
        """Fetch a URL and extract emails from its content."""
        try:
            req = Request(url, headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            })
            with urlopen(req, timeout=10) as resp:
                content = resp.read().decode("utf-8",
                                              errors="replace")

            return self._extract_from_text(
                content, company, domain)

        except Exception as e:
            logger.debug("URL scrape failed (%s): %s", url, e)
            return []

    # ═══════════════════════════════════════════════════════════
    # STRATEGY 4 — Hunter.io
    # ═══════════════════════════════════════════════════════════

    def _hunter_search(self, domain: str,
                       company: str) -> List[Dict]:
        """Search Hunter.io for emails at a domain."""
        if not self._hunter_key:
            return []

        try:
            url = (
                f"https://api.hunter.io/v2/domain-search?"
                f"domain={domain}&"
                f"api_key={self._hunter_key}&"
                f"type=generic&limit=5"
            )

            req = Request(url)
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if data.get("errors"):
                logger.debug("Hunter.io error: %s",
                             data["errors"])
                return []

            results = []
            for email_data in data.get("data", {}).get(
                    "emails", []):
                email = email_data.get("value", "").lower()
                if not email or not self._is_useful_email(email):
                    continue

                confidence = email_data.get("confidence", 50)
                first = email_data.get("first_name", "")
                last = email_data.get("last_name", "")
                name = f"{first} {last}".strip()
                title = email_data.get("position", "")

                # Boost HR-related titles
                if any(kw in (title or "").lower() for kw in
                       ["hr", "recruit", "talent", "people",
                        "hiring"]):
                    confidence = min(95, confidence + 10)

                results.append({
                    "email": email,
                    "name": name,
                    "title": title,
                    "confidence": confidence,
                    "source": "hunter.io",
                    "verified": True,
                })

            return results

        except Exception as e:
            logger.debug("Hunter.io search failed: %s", e)
            return []

    # ═══════════════════════════════════════════════════════════
    # STRATEGY 5 — Pattern Guessing
    # ═══════════════════════════════════════════════════════════

    def _guess_patterns(self, company: str,
                        domain: str) -> List[Dict]:
        """Generate likely HR email patterns and verify MX."""
        if not domain:
            return []

        # Check domain has MX records first
        has_mx = self.verify_email(f"test@{domain}")
        if not has_mx:
            logger.debug("Domain %s has no MX records", domain)
            return []

        results = []
        for prefix in _HR_PREFIXES[:8]:  # top 8 patterns
            email = f"{prefix}@{domain}"
            confidence = 30

            # Higher confidence for common patterns
            if prefix in ("hr", "careers", "jobs", "recruiting"):
                confidence = 45
            elif prefix in ("talent", "hiring", "people"):
                confidence = 40

            results.append({
                "email": email,
                "name": "",
                "title": "",
                "confidence": confidence,
                "source": "pattern_guess",
                "verified": False,
            })

        return results

    # ═══════════════════════════════════════════════════════════
    # INTERNAL — Helpers
    # ═══════════════════════════════════════════════════════════

    def _guess_domain(self, company: str) -> Optional[str]:
        """Guess company domain from name."""
        clean = (company.lower().strip()
                 .replace(" pvt ltd", "")
                 .replace(" pvt. ltd.", "")
                 .replace(" private limited", "")
                 .replace(" limited", "")
                 .replace(" ltd", "")
                 .replace(" inc", "")
                 .replace(" technologies", "")
                 .replace(" tech", "")
                 .replace(" solutions", "")
                 .replace(" software", "")
                 .replace(" services", "")
                 .replace(" india", "")
                 .replace(" ", "")
                 .replace(".", "")
                 .replace(",", ""))

        if not clean:
            return None

        # Common patterns: razorpay → razorpay.com
        for suffix in [".com", ".in", ".io", ".co.in"]:
            domain = f"{clean}{suffix}"
            # Quick check: can we resolve it?
            try:
                socket.setdefaulttimeout(3)
                socket.getaddrinfo(domain, 80)
                return domain
            except (socket.gaierror, socket.timeout, OSError):
                continue

        return f"{clean}.com"  # fallback guess

    def _is_useful_email(self, email: str) -> bool:
        """Filter out useless/blacklisted emails."""
        if not email or "@" not in email:
            return False

        email = email.lower()
        local = email.split("@")[0]
        domain = email.split("@")[1]

        # Check blacklisted patterns
        for bl in _EMAIL_BLACKLIST:
            if bl in local or bl in domain:
                return False

        # Check blacklisted domains
        if domain in _DOMAIN_BLACKLIST:
            return False

        # Must be a reasonable length
        if len(local) < 2 or len(domain) < 4:
            return False

        return True

    def _save_contact(self, contact: Dict,
                      company: str) -> None:
        """Save a new contact to database (skip if exists)."""
        try:
            email = contact.get("email", "")
            existing = self.db.get_contacts(company=company)
            for c in existing:
                if c.get("email", "").lower() == email.lower():
                    return  # already exists

            self.db.save_contact({
                "company": company,
                "name": contact.get("name", ""),
                "title": contact.get("title", ""),
                "email": email,
                "source": contact.get("source", ""),
                "verified": 1 if contact.get("verified") else 0,
            })
        except Exception as e:
            logger.debug("Failed to save contact: %s", e)


# ═══════════════════════════════════════════════════════════════════
# SINGLETON
# ═══════════════════════════════════════════════════════════════════

_finder_instance: Optional[EmailFinder] = None


def get_email_finder() -> EmailFinder:
    global _finder_instance
    if _finder_instance is None:
        _finder_instance = EmailFinder()
    return _finder_instance


# ═══════════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Email Finder — Self Test")
    print("=" * 60)

    finder = EmailFinder()

    # 1. Config
    print("\n[1] Config:")
    print(f"  Hunter.io key: {'✓ set' if HUNTER_API_KEY else '✗ not set (optional)'}")

    # 2. Domain guessing
    print("\n[2] Domain guessing:")
    domain_tests = [
        ("Razorpay", "razorpay"),
        ("Flipkart", "flipkart"),
        ("TCS", "tcs"),
        ("Infosys Technologies", "infosys"),
        ("Site Guru Pvt Ltd", "siteguru"),
    ]
    for company, expected_contains in domain_tests:
        domain = finder._guess_domain(company)
        ok = expected_contains in (domain or "")
        print(f"  {'✓' if ok else '?'} '{company}' → {domain}")

    # 3. Email validation
    print("\n[3] Email usefulness check:")
    email_tests = [
        ("hr@razorpay.com", True),
        ("careers@flipkart.com", True),
        ("noreply@company.com", False),
        ("test@gmail.com", False),
        ("jobs@infosys.com", True),
        ("", False),
        ("support@naukri.com", False),
    ]
    for email, expected in email_tests:
        ok = finder._is_useful_email(email)
        icon = "✓" if ok == expected else "✗"
        print(f"  {icon} '{email}' → {ok} (expected {expected})")

    # 4. Pattern generation
    print("\n[4] Pattern guessing:")
    patterns = finder.guess_patterns("Razorpay", "razorpay.com")
    print(f"  Generated {len(patterns)} patterns for razorpay.com:")
    for p in patterns[:5]:
        print(f"    • {p}")

    # 5. Text extraction
    print("\n[5] Email extraction from text:")
    test_jd = """
    We are hiring! Send your resume to careers@techcorp.in
    or contact HR at priya.sharma@techcorp.in.
    For queries: support@techcorp.in (not HR).
    Apply at noreply@techcorp.in (automated).
    """
    extracted = finder._extract_from_text(
        test_jd, "TechCorp", "techcorp.in")
    print(f"  Found {len(extracted)} useful emails:")
    for e in extracted:
        print(f"    • {e['email']} (confidence={e['confidence']}%, "
              f"source={e['source']})")

    # 6. MX verification
    print("\n[6] MX verification (requires network):")
    mx_tests = ["gmail.com", "razorpay.com",
                "thisdomaindoesnotexist12345.com"]
    for domain in mx_tests:
        try:
            ok = finder._verify_mx_socket(domain)
            print(f"  {'✓' if ok else '✗'} {domain}: MX={ok}")
        except Exception as e:
            print(f"  ? {domain}: {e}")

    # 7. Full find (combined)
    print("\n[7] Full find_hr_email test:")
    test_jd_full = ("Contact us at hr@testcompany.com "
                    "or visit careers.testcompany.com")
    contacts = finder.find_hr_email(
        "TestCompany",
        domain="testcompany.com",
        job_description=test_jd_full,
    )
    print(f"  Found {len(contacts)} contacts:")
    for c in contacts[:5]:
        print(f"    • {c['email']} — confidence={c['confidence']}% "
              f"— source={c['source']}")

    # 8. Singleton
    print("\n[8] Singleton:")
    f1 = get_email_finder()
    f2 = get_email_finder()
    print(f"  Same instance: {f1 is f2}")

    print(f"\n✅ Email Finder tests complete!\n")