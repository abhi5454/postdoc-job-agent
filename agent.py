#!/usr/bin/env python3
"""
Free Mathematics Postdoc Job Agent
====================================
Tuned for: Combinatorics / Statistical Mechanics / Dimer Models (IISc profile)
Stack:     GitHub Actions (scheduler) + requests/BS4 (scraper) + SQLite (DB) + Telegram (alerts)
Cost:      $0 — no paid APIs, no proxies, no subscriptions

Setup:
  pip install requests beautifulsoup4 feedparser
  export TELEGRAM_BOT_TOKEN="your_bot_token"   # from @BotFather on Telegram
  export TELEGRAM_CHAT_ID="your_chat_id"       # send /start to your bot, then check getUpdates

Sources scraped:
  1. MathJobs.org         – THE primary global math postdoc board
  2. jobs.ac.uk           – UK primary academic jobs
  3. EMS Jobs             – European Mathematical Society
  4. MathHire.org         – EU-focused math positions
  5. EURAXESS             – EU/UK research jobs (bonus)
"""

import requests
from bs4 import BeautifulSoup
import sqlite3
import os
import time
import hashlib
import re
from datetime import datetime, date
from urllib.parse import urljoin, urlencode

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG  ── edit these, or set as environment variables
# ─────────────────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
DB_PATH            = os.getenv("DB_PATH", "postdocs.db")

# ── Researcher profile ──────────────────────────────────────────────────────
# Tuned for: monopole-dimer model, high-dimensional grids, combinatorics/stat mech
# Add/remove keywords to match her evolving interests

PRIMARY_KEYWORDS = [
    # Her exact thesis area
    "dimer",
    "monopole",
    "monopole-dimer",
    "tiling",
    "domino",
    "lozenge",
    # Core field
    "combinatorics",
    "algebraic combinatorics",
    "enumerative combinatorics",
    "statistical mechanics",
    # Closely related
    "partition function",
    "lattice model",
    "random tiling",
    "height function",
    "arctic circle",
    "random matrix",
    "exactly solvable",
    "integrable",
    "transfer matrix",
]

SECONDARY_KEYWORDS = [
    "discrete mathematics",
    "graph theory",
    "probability",
    "mathematical physics",
    "representation theory",
    "matching theory",
    "pfaffian",
    "determinantal",
    "cluster algebra",
    "plane partition",
    "young tableau",
    "symmetric function",
    "topological",       # Möbius/Klein grid topology angle
]

# ── Title patterns that confirm a role IS a postdoc ─────────────────────────
# Checked against the job TITLE only (not the full description).
POSTDOC_TITLE_PATTERNS = re.compile(
    r"postdoc|post-doc|post doc|"
    r"postdoctoral|post-doctoral|"
    r"research fellow|"
    r"pdra|"                               # UK shorthand for Postdoctoral Research Associate
    r"junior\s+research\s+(chair|fellow)|"
    r"marie\s+(sk.odowska-)?curie|"        # EU fellowship
    r"newton\s+fellow|"
    r"royal\s+society\s+fellow|"
    r"research\s+associate.*\bphd\b|"     # RA explicitly requiring a completed PhD
    # ── Entry-level faculty (scope-equivalent to a postdoc) ────────────────
    r"assistant\s+professor|"             # standard US / EU / Asia entry faculty
    r"junior\s+professor|"               # Juniorprofessor W1 (Germany)
    r"tenure.track|"                     # TT mentioned anywhere in title
    r"visiting\s+assistant\s+professor|" # VAP — common postdoc bridge role
    r"visiting\s+lecturer|"
    r"lecturer\s+in\s+math|"            # UK Lecturer = entry faculty, NOT teaching-only RA
    r"lecturer\s+in\s+pure|"
    r"lecturer\s+in\s+applied|"
    r"lecturer\s+in\s+probability|"
    r"lecturer\s+in\s+combinatorics|"
    r"lecturer\s+in\s+statistics|"
    r"faculty\s+position|"
    r"instructor.*math",                 # some US/CA depts use Instructor for entry roles
    re.IGNORECASE,
)

# ── Keywords that strongly suggest this is NOT a postdoc ────────────────────
# A single match doesn't disqualify; 2+ matches apply a score penalty.
# Note: "assistant professor", "lecturer in" intentionally removed —
# those are now accepted as entry-level faculty equivalent to a postdoc.
NEGATIVE_KEYWORDS = [
    "phd studentship",
    "phd scholarship",
    "phd position",
    "doctoral student",
    "msc student",
    "undergraduate",
    "research technician",
    "laboratory manager",
    "lab manager",
    "senior lecturer",       # senior = not entry-level
    "associate professor",   # mid-career, above postdoc band
    "full professor",
    "professor of",          # "Professor of Mathematics" = senior chair
    "chair of",
    "data scientist",        # industry-facing RA roles
    "software engineer",
    "part-time",
    "0.5 fte",
    "0.6 fte",
]

# ── Salary thresholds ────────────────────────────────────────────────────────
MIN_SALARY_GBP = 30000   # filter out stipend-only or part-time
MIN_SALARY_EUR = 35000

# ── HTTP headers — identify as an academic bot ───────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; PostdocSearchBot/1.0; "
        "Academic job search for mathematics postdocs)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.5",
}

POLITE_DELAY = 2.0   # seconds between requests (be a good citizen)

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────

def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id               TEXT PRIMARY KEY,
            title            TEXT NOT NULL,
            institution      TEXT,
            location         TEXT,
            country          TEXT,
            salary_raw       TEXT,
            salary_min       REAL,
            salary_max       REAL,
            currency         TEXT,
            deadline         TEXT,
            url              TEXT,
            source           TEXT,
            keywords_matched TEXT,
            relevance_score  INTEGER DEFAULT 0,
            first_seen       TEXT,
            notified         INTEGER DEFAULT 0,
            status           TEXT DEFAULT 'active'   -- active | expired | removed
        )
    """)
    # ── Migrate existing DBs that predate the status column ─────────────────
    existing_cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "status" not in existing_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN status TEXT DEFAULT 'active'")
    conn.commit()
    return conn


def job_id(url: str, title: str) -> str:
    """Stable 12-char hash to deduplicate across runs."""
    return hashlib.md5(f"{url.strip()}{title.strip()}".encode()).hexdigest()[:12]


def is_new(conn: sqlite3.Connection, jid: str) -> bool:
    return conn.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone() is None


def insert_job(conn: sqlite3.Connection, job: dict) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO jobs
          (id, title, institution, location, country, salary_raw,
           salary_min, salary_max, currency, deadline, url, source,
           keywords_matched, relevance_score, first_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            job["id"], job["title"], job["institution"], job["location"],
            job["country"], job["salary_raw"], job["salary_min"], job["salary_max"],
            job["currency"], job["deadline"], job["url"], job["source"],
            ",".join(job["keywords_matched"]), job["relevance_score"],
            date.today().isoformat(),
        ),
    )
    conn.commit()


def mark_notified(conn: sqlite3.Connection, jid: str) -> None:
    conn.execute("UPDATE jobs SET notified=1 WHERE id=?", (jid,))
    conn.commit()


def get_active_jobs(conn: sqlite3.Connection) -> list[dict]:
    """Return all jobs with status='active', best relevance first."""
    rows = conn.execute(
        """SELECT id, title, institution, location, country,
                  salary_raw, deadline, url, source,
                  keywords_matched, relevance_score, first_seen
           FROM jobs
           WHERE status = 'active'
           ORDER BY relevance_score DESC, first_seen DESC"""
    ).fetchall()
    cols = ["id", "title", "institution", "location", "country",
            "salary_raw", "deadline", "url", "source",
            "keywords_matched", "relevance_score", "first_seen"]
    return [dict(zip(cols, r)) for r in rows]


def set_job_status(conn: sqlite3.Connection, jid: str, status: str) -> None:
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, jid))
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# RELEVANCE SCORING
# ─────────────────────────────────────────────────────────────────────────────

def score_relevance(text: str) -> tuple[int, list[str]]:
    """
    Returns (score, matched_keywords).
    Primary keywords (dimer/combinatorics area): +3 each.
    Secondary keywords: +1 each.
    Explicit postdoc title words: +4 bonus.
    Generic 'research associate' without postdoc context: +1 only.
    Negative keywords: -3 each (caps at -9 total).
    """
    text_lower = text.lower()
    matched = []
    score = 0

    for kw in PRIMARY_KEYWORDS:
        if kw in text_lower:
            matched.append(kw)
            score += 3

    for kw in SECONDARY_KEYWORDS:
        if kw in text_lower and kw not in matched:
            matched.append(kw)
            score += 1

    # Strong postdoc signal
    if re.search(
        r"postdoc|post-doc|post\s+doc|postdoctoral|post-doctoral|"
        r"\bpdra\b|research\s+fellow|marie\s+curie|newton\s+fellow",
        text_lower,
    ):
        score += 4
    elif re.search(r"research\s+associate", text_lower):
        # Generic RA — could be postdoc (UK norm) or not; minimal bonus
        score += 1

    # Negative keyword penalty
    neg_hits = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text_lower)
    score -= min(neg_hits * 3, 9)

    return score, matched


# ─────────────────────────────────────────────────────────────────────────────
# SALARY EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

_SALARY_PATTERNS = [
    # GBP ranges: £37,694–£46,049  or  £37,694 - £46,049
    (r'£\s*([\d,]+)\s*(?:–|-|to)\s*£\s*([\d,]+)', "GBP", True),
    # EUR ranges
    (r'€\s*([\d,]+)\s*(?:–|-|to)\s*€\s*([\d,]+)', "EUR", True),
    # Single GBP
    (r'£\s*([\d,]+)', "GBP", False),
    # Single EUR
    (r'€\s*([\d,]+)', "EUR", False),
    # Explicit currency suffix
    (r'([\d,]+)\s*GBP', "GBP", False),
    (r'([\d,]+)\s*EUR', "EUR", False),
    # SEK, NOK, DKK (Scandinavian) — report as-is, no conversion
    (r'([\d,]+)\s*(?:SEK|NOK|DKK)', "SCN", False),
]


def extract_salary(text: str) -> tuple[str, float | None, float | None, str | None]:
    """Returns (raw_string, min_amount, max_amount, currency)."""
    for pattern, currency, is_range in _SALARY_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            raw = m.group(0).strip()
            if is_range:
                lo = float(m.group(1).replace(",", ""))
                hi = float(m.group(2).replace(",", ""))
                # Sanity check: realistic postdoc salaries 15k–200k
                if 15_000 <= lo <= 200_000:
                    return raw, lo, hi, currency
            else:
                val = float(m.group(1).replace(",", ""))
                if 15_000 <= val <= 200_000:
                    return raw, val, val, currency
    return "", None, None, None


# ─────────────────────────────────────────────────────────────────────────────
# DEADLINE PARSING & JOB EXPIRY
# ─────────────────────────────────────────────────────────────────────────────

_DEADLINE_FORMATS = [
    "%Y-%m-%d",          # 2026-06-30  (ISO)
    "%d/%m/%Y",          # 30/06/2026  (UK)
    "%d-%m-%Y",          # 30-06-2026
    "%d %B %Y",          # 30 June 2026
    "%d %b %Y",          # 30 Jun 2026
    "%B %d, %Y",         # June 30, 2026  (US)
    "%b %d, %Y",         # Jun 30, 2026
    "%d %B, %Y",         # 30 June, 2026
    "%Y/%m/%d",          # 2026/06/30
]


def parse_deadline(raw: str) -> date | None:
    """
    Try to parse a deadline string into a date object.
    Returns None if the string is empty or unrecognisable.
    Strips ordinal suffixes (1st, 2nd, 3rd, 4th…) before trying formats.
    """
    if not raw:
        return None
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    for fmt in _DEADLINE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def purge_expired_jobs(conn: sqlite3.Connection) -> tuple[int, int]:
    """
    Sweep all active jobs and mark them expired or removed:
      - expired  → deadline is in the past
      - removed  → URL returns 4xx/5xx or times out (posting taken down)

    Returns (n_expired, n_removed) for reporting.
    """
    today = date.today()
    active = conn.execute(
        "SELECT id, url, deadline FROM jobs WHERE status = 'active'"
    ).fetchall()

    n_expired = 0
    n_removed = 0

    for jid, url, deadline_raw in active:
        # ── 1. Deadline check ────────────────────────────────────────────────
        dl = parse_deadline(deadline_raw)
        if dl and dl < today:
            set_job_status(conn, jid, "expired")
            n_expired += 1
            continue   # no need to ping the URL as well

        # ── 2. URL liveness check ────────────────────────────────────────────
        # Use HEAD first (cheap); fall back to GET if server rejects HEAD.
        if not url:
            continue
        try:
            r = requests.head(url, headers=HEADERS, timeout=8, allow_redirects=True)
            if r.status_code == 405:   # Method Not Allowed → retry with GET
                r = requests.get(url, headers=HEADERS, timeout=10, stream=True)
            if r.status_code in (404, 410, 403, 401):
                set_job_status(conn, jid, "removed")
                n_removed += 1
        except requests.RequestException:
            # Network error ≠ removed posting; leave as active for next check
            pass

        time.sleep(0.3)   # gentle pace — checking many URLs in sequence

    return n_expired, n_removed


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None, timeout: int = 20) -> requests.Response | None:
    """Polite GET with error handling."""
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        print(f"    ⚠ GET failed for {url}: {e}")
        return None


def is_likely_postdoc(title: str, full_text: str) -> bool:
    """
    Hard gate: returns False for roles that are clearly not postdocs.
    Checked before inserting into the DB or sending any notification.

    Logic:
      1. If the title explicitly matches a known postdoc/entry-faculty pattern → True
         (includes assistant professor, junior professor, VAP, tenure-track, UK Lecturer)
      2. If the title contains senior-role signals → False
         (senior lecturer, associate professor, full professor, PhD student, technician)
      3. In the UK, 'Research Associate' IS the standard postdoc title at Grade 6/7,
         so allow it — but only when the full text mentions a PhD requirement.
      4. Everything else without an explicit signal → False
    """
    title_lower = title.lower()
    text_lower  = full_text.lower()

    # 1. Explicit postdoc / entry-level faculty language in title → always accept
    if POSTDOC_TITLE_PATTERNS.search(title):
        return True

    # 2. Hard negatives in title → always reject
    #    Note: "lecturer" alone is NOT here — "Lecturer in Mathematics" is entry faculty.
    #    "senior lecturer", "associate professor", "professor of" ARE here.
    hard_negatives = [
        "phd student", "studentship", "scholarship",
        "senior lecturer", "associate professor", "full professor",
        "professor of", "chair of", "emeritus",
        "technician", "manager", "engineer", "data scientist",
        "msc student", "master student", "undergraduate", "part-time",
    ]
    if any(neg in title_lower for neg in hard_negatives):
        return False

    # 3. 'Research Associate' in title → accept only if job requires a completed PhD
    if "research associate" in title_lower:
        phd_required = re.search(
            r"(completed|awarded|hold a|holding a|have a|recent)\s+ph\.?d|"
            r"ph\.?d\s+(in|required|essential|qualification)|"
            r"doctorate\s+(in|required|awarded)|"
            r"must\s+have.*ph\.?d|"
            r"applicants.*ph\.?d",
            text_lower,
        )
        return bool(phd_required)

    # 4. Anything else without an explicit postdoc/faculty signal → reject
    return False


def _make_job(
    *,
    url: str,
    title: str,
    institution: str = "",
    location: str = "",
    country: str = "",
    full_text: str = "",
    salary_raw: str = "",
    deadline: str = "",
    source: str,
) -> dict:
    """Build a normalised job dict."""
    sal_raw, sal_min, sal_max, currency = extract_salary(salary_raw + " " + full_text)
    if not sal_raw:
        sal_raw = salary_raw
    score, matched = score_relevance(title + " " + full_text)
    return {
        "id": job_id(url, title),
        "title": title,
        "institution": institution,
        "location": location,
        "country": country,
        "salary_raw": sal_raw,
        "salary_min": sal_min,
        "salary_max": sal_max,
        "currency": currency or "",
        "deadline": deadline,
        "url": url,
        "source": source,
        "keywords_matched": matched,
        "relevance_score": score,
    }


# ── 1. MathJobs.org ───────────────────────────────────────────────────────────

def scrape_mathjobs() -> list[dict]:
    """
    MathJobs.org is THE primary global math postdoc board (run by AMS).
    We search the POSTDOC category and parse the HTML listing table.
    """
    jobs = []
    base = "https://www.mathjobs.org"
    queries = [
        f"{base}/jobs/list?jobtype=POSTDOC&keywords=combinatorics",
        f"{base}/jobs/list?jobtype=POSTDOC&keywords=statistical+mechanics",
        f"{base}/jobs/list?jobtype=POSTDOC&keywords=dimer",
        f"{base}/jobs/list?jobtype=POSTDOC&keywords=mathematical+physics",
        f"{base}/jobs/list?jobtype=POSTDOC",   # broad sweep
    ]
    seen = set()

    for url in queries:
        resp = _get(url)
        if not resp:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")

        # MathJobs uses a table — rows have class 'std-row' or similar
        rows = soup.select("tr") or []
        for row in rows:
            link = row.select_one("a[href*='/jobs/']")
            if not link:
                continue
            title = link.get_text(strip=True)
            href  = link.get("href", "")
            if not title or not href or href in seen:
                continue
            seen.add(href)
            job_url = urljoin(base, href)

            cells = row.find_all("td")
            institution = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            location    = cells[2].get_text(strip=True) if len(cells) > 2 else ""
            deadline    = cells[-1].get_text(strip=True) if cells else ""

            full_text = row.get_text(" ", strip=True)
            jobs.append(
                _make_job(
                    url=job_url, title=title, institution=institution,
                    location=location, country="", full_text=full_text,
                    deadline=deadline, source="MathJobs.org",
                )
            )
        time.sleep(POLITE_DELAY)

    return jobs


# ── 2. jobs.ac.uk ─────────────────────────────────────────────────────────────

def scrape_jobs_ac_uk() -> list[dict]:
    """
    jobs.ac.uk — UK's leading academic job board.
    Uses their JSON search endpoint (no auth required).
    """
    jobs = []
    base_url = "https://www.jobs.ac.uk/search/json"
    seen = set()

    query_sets = [
        "postdoc mathematics combinatorics",
        "postdoctoral research associate mathematics",
        "research fellow pure mathematics",
        "postdoc mathematical physics",
        "PDRA mathematics",
    ]

    for query in query_sets:
        params = {
            "keywords": query,
            "academicDiscipline": "mathematics-and-statistics",
            "rows": 50,
        }
        resp = _get(base_url, params=params)
        if not resp:
            continue

        try:
            data = resp.json()
        except Exception:
            continue

        for item in data.get("jobs", []):
            job_url = item.get("jobUrl") or item.get("url", "")
            if not job_url or job_url in seen:
                continue
            seen.add(job_url)

            title       = item.get("jobTitle", "")
            institution = item.get("employerName", "")
            location    = item.get("location", "")
            salary_raw  = item.get("salary", "")
            deadline    = item.get("closingDate", "")
            description = item.get("jobDescription", "")

            # ── POSTDOC GATE ────────────────────────────────────────────────
            # jobs.ac.uk is the noisiest source; apply the hard gate here.
            full_text_combined = f"{title} {description}"
            if not is_likely_postdoc(title, full_text_combined):
                continue
            # ────────────────────────────────────────────────────────────────

            jobs.append(
                _make_job(
                    url=job_url, title=title, institution=institution,
                    location=location, country="UK",
                    full_text=full_text_combined,
                    salary_raw=salary_raw, deadline=deadline,
                    source="jobs.ac.uk",
                )
            )
        time.sleep(POLITE_DELAY)

    return jobs


# ── 3. EMS Jobs (European Mathematical Society) ───────────────────────────────

def scrape_ems_jobs() -> list[dict]:
    """
    euromathsoc.org/jobs — EMS aggregates EU + UK math positions.
    Simple HTML listing, easy to parse.
    """
    jobs = []
    url = "https://euromathsoc.org/jobs"
    resp = _get(url)
    if not resp:
        return jobs

    soup = BeautifulSoup(resp.text, "html.parser")

    # EMS wraps each posting in a card/article with a title link
    for item in soup.select("article, .job-item, .position, li.vacancy, .listing-item"):
        link = item.select_one("a[href]")
        if not link:
            continue
        title = link.get_text(strip=True)
        href  = link.get("href", "")
        if not title:
            continue
        job_url = urljoin(url, href)

        full_text   = item.get_text(" ", strip=True)
        institution = ""
        for el in item.select(".institution, .university, .employer, .org"):
            institution = el.get_text(strip=True)
            break

        deadline = ""
        for el in item.select(".deadline, .closing, time"):
            deadline = el.get_text(strip=True)
            break

        jobs.append(
            _make_job(
                url=job_url, title=title, institution=institution,
                location="", country="Europe", full_text=full_text,
                deadline=deadline, source="EMS Jobs",
            )
        )

    time.sleep(POLITE_DELAY)
    return jobs


# ── 4. MathHire.org ───────────────────────────────────────────────────────────

def scrape_mathhire() -> list[dict]:
    """
    mathhire.org — recommended by EMS; EU-heavy, clean HTML.
    """
    jobs = []
    url  = "https://mathhire.org/jobs"
    resp = _get(url)
    if not resp:
        return jobs

    soup = BeautifulSoup(resp.text, "html.parser")

    for item in soup.select(".job-listing, .position, article, .card"):
        link = item.select_one("h2 a, h3 a, .title a, a.job-title, a[href*='/job']")
        if not link:
            continue
        title = link.get_text(strip=True)
        href  = link.get("href", "")
        if not title:
            continue
        job_url = urljoin(url, href)

        full_text = item.get_text(" ", strip=True)
        jobs.append(
            _make_job(
                url=job_url, title=title, institution="",
                location="", country="Europe", full_text=full_text,
                source="MathHire.org",
            )
        )

    time.sleep(POLITE_DELAY)
    return jobs


# ── 5. EURAXESS (EU Research Portal) ─────────────────────────────────────────

def scrape_euraxess() -> list[dict]:
    """
    euraxess.ec.europa.eu — EC portal covering 40+ countries.
    Searches via their public search URL.
    """
    jobs  = []
    base  = "https://euraxess.ec.europa.eu"
    url   = f"{base}/jobs/search"
    params = {
        "query": "postdoc mathematics combinatorics",
        "format": "json",   # may or may not be supported; fallback to HTML parse
    }

    resp = _get(url, params={"query": "postdoc mathematics"})
    if not resp:
        return jobs

    soup = BeautifulSoup(resp.text, "html.parser")

    for item in soup.select(".job-item, .views-row, article.node--type-jobs"):
        link = item.select_one("a[href]")
        if not link:
            continue
        title = link.get_text(strip=True)
        href  = link.get("href", "")
        if not title:
            continue
        job_url = urljoin(base, href)

        full_text = item.get_text(" ", strip=True)
        jobs.append(
            _make_job(
                url=job_url, title=title, institution="",
                location="", country="Europe", full_text=full_text,
                source="EURAXESS",
            )
        )

    time.sleep(POLITE_DELAY)
    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM NOTIFIER
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> None:
    """Send a message to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # No Telegram configured — just print
        print("\n── NOTIFICATION ──")
        print(message)
        print("──────────────────\n")
        return

    endpoint = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload  = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(endpoint, json=payload, timeout=10)
        if not r.ok:
            print(f"Telegram error: {r.status_code} {r.text}")
    except Exception as e:
        print(f"Telegram send failed: {e}")


def format_job_message(job: dict) -> str:
    stars = "⭐" * min(5, job["relevance_score"] // 3)
    if not stars:
        stars = "·"

    salary_line = ""
    if job["salary_raw"]:
        salary_line = f"\n💰 <b>Salary:</b> {job['salary_raw']}"

    deadline_line = ""
    if job["deadline"]:
        deadline_line = f"\n📅 <b>Deadline:</b> {job['deadline']}"

    kw_line = ""
    if job["keywords_matched"]:
        kw_line = f"\n🔬 <b>Keywords:</b> {', '.join(job['keywords_matched'][:5])}"

    loc = f" — {job['location']}" if job["location"] else ""
    if not loc and job["country"]:
        loc = f" — {job['country']}"

    return (
        f"🎓 <b>New Postdoc</b> {stars}\n"
        f"📌 {job['title']}\n"
        f"🏛️ {job['institution']}{loc}"
        f"{salary_line}{deadline_line}{kw_line}\n"
        f"🔗 <a href='{job['url']}'>View posting</a>\n"
        f"<i>via {job['source']}</i>"
    )


def format_active_jobs_list(jobs: list[dict]) -> list[str]:
    """
    Build a compact current-openings list, chunked into Telegram-safe
    messages (≤ 4000 chars each, leaving headroom for the header).

    Each entry:
        ⭐ Title — Institution
            📅 deadline  💰 salary  🔗 link
    """
    CHUNK_LIMIT = 4000

    def entry(job: dict) -> str:
        stars   = "⭐" * min(3, job["relevance_score"] // 3) or "·"
        inst    = f" — {job['institution']}" if job["institution"] else ""
        dl      = f"  📅 {job['deadline']}" if job["deadline"] else ""
        sal     = f"  💰 {job['salary_raw']}" if job["salary_raw"] else ""
        country = f" ({job['country']})" if job["country"] else ""
        return (
            f"{stars} <a href='{job['url']}'>{job['title']}</a>"
            f"{inst}{country}\n"
            f"<i>{dl}{sal}</i>\n"
        )

    pages: list[str] = []
    current = ""
    for i, job in enumerate(jobs):
        line = entry(job)
        if len(current) + len(line) > CHUNK_LIMIT:
            pages.append(current)
            current = line
        else:
            current += line
    if current:
        pages.append(current)

    # Prepend header to first page, continuation note to rest
    if pages:
        total = len(jobs)
        pages[0] = (
            f"📋 <b>All Current Openings ({total})</b>\n"
            f"<i>Sorted by relevance · expired &amp; removed listings auto-cleaned</i>\n\n"
        ) + pages[0]
        for i in range(1, len(pages)):
            pages[i] = f"📋 <b>Current Openings (cont. {i+1}/{len(pages)})</b>\n\n" + pages[i]

    return pages


def paginate_and_send(messages: list[str]) -> None:
    """Send a list of pre-formatted message strings with rate-limit delay."""
    for msg in messages:
        send_telegram(msg)
        time.sleep(1.2)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run() -> None:
    print(f"\n{'='*60}")
    print(f"  Postdoc Agent — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    conn = init_db(DB_PATH)

    # ── Step 1: Purge expired / removed jobs before anything else ───────────
    print("  Checking existing listings for expiry / removed URLs ...")
    n_expired, n_removed = purge_expired_jobs(conn)
    print(f"    → Marked expired: {n_expired}  |  Removed (404): {n_removed}")

    # ── Step 2: Scrape all sources ──────────────────────────────────────────
    scrapers = [
        ("MathJobs.org",   scrape_mathjobs),
        ("jobs.ac.uk",     scrape_jobs_ac_uk),
        ("EMS Jobs",       scrape_ems_jobs),
        ("MathHire.org",   scrape_mathhire),
        ("EURAXESS",       scrape_euraxess),
    ]

    all_jobs: list[dict] = []
    for name, fn in scrapers:
        print(f"  Scraping {name} ...")
        try:
            results = fn()
            print(f"    → {len(results)} listings fetched")
            all_jobs.extend(results)
        except Exception as e:
            print(f"    → Scraper error: {e}")

    print(f"\n  Total fetched:  {len(all_jobs)}")

    # ── Step 3: Deduplicate, gate, persist ──────────────────────────────────
    new_jobs: list[dict] = []
    skipped = 0
    for job in all_jobs:
        if not is_likely_postdoc(job["title"], job.get("title", "") + " " + job.get("institution", "")):
            if job["relevance_score"] <= 0:
                skipped += 1
                continue
        if is_new(conn, job["id"]):
            insert_job(conn, job)
            new_jobs.append(job)

    print(f"  Skipped (not postdoc): {skipped}")
    print(f"  New this run:          {len(new_jobs)}")

    new_jobs.sort(key=lambda j: j["relevance_score"], reverse=True)
    high_relevance = [j for j in new_jobs if j["relevance_score"] >= 6]
    any_relevance  = [j for j in new_jobs if j["relevance_score"] > 0]

    # ── Step 4: Fetch current active jobs (for bottom section) ──────────────
    active_jobs = get_active_jobs(conn)
    print(f"  Active in DB:          {len(active_jobs)}")

    # ── Step 5: Notify ──────────────────────────────────────────────────────
    if not new_jobs:
        # ── Heartbeat: no new jobs ───────────────────────────────────────────
        send_telegram(
            f"✅ <b>Postdoc Agent — {date.today().isoformat()}</b>\n\n"
            f"No new listings found today.\n"
            f"🗑️ Cleaned: {n_expired} expired · {n_removed} removed\n"
            f"📦 Active listings in tracker: <b>{len(active_jobs)}</b>\n"
            f"<i>Next check tomorrow at 07:30 UTC</i>"
        )
        time.sleep(1.2)
        print("  Heartbeat sent.")
    else:
        # ── Header summary ───────────────────────────────────────────────────
        send_telegram(
            f"📬 <b>Postdoc Hunt — {date.today().isoformat()}</b>\n\n"
            f"🆕 New listings today: <b>{len(new_jobs)}</b>\n"
            f"  • 🔬 High relevance (dimer/combinatorics): <b>{len(high_relevance)}</b>\n"
            f"  • 📐 Any relevance match: <b>{len(any_relevance)}</b>\n\n"
            f"🗑️ Cleaned today: {n_expired} expired · {n_removed} removed\n"
            f"📦 Total active after cleanup: <b>{len(active_jobs)}</b>"
        )
        time.sleep(1.2)

        # ── New jobs — full detail cards, top 10 ────────────────────────────
        for job in new_jobs[:10]:
            send_telegram(format_job_message(job))
            mark_notified(conn, job["id"])
            time.sleep(1.2)

    # ── Step 6: Current openings list (always sent, new jobs or not) ────────
    if active_jobs:
        pages = format_active_jobs_list(active_jobs)
        paginate_and_send(pages)
    else:
        send_telegram("📋 <b>Current Openings</b>\n\nNo active listings in tracker yet.")

    # ── Local console summary ────────────────────────────────────────────────
    print("\n  ── Today's top hits ──")
    for job in new_jobs[:5]:
        print(f"  [{job['relevance_score']:2d}] {job['title'][:60]}")
        print(f"       {job['institution']} | {job['source']}")
        if job["salary_raw"]:
            print(f"       {job['salary_raw']}")
        print()

    conn.close()
    print("  Done.\n")


if __name__ == "__main__":
    run()
