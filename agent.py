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
            notified         INTEGER DEFAULT 0
        )
    """)
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


def get_all_jobs(conn: sqlite3.Connection) -> list:
    """For optional reporting."""
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY relevance_score DESC, first_seen DESC"
    ).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM jobs LIMIT 0").description]
    return [dict(zip(cols, r)) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# RELEVANCE SCORING
# ─────────────────────────────────────────────────────────────────────────────

def score_relevance(text: str) -> tuple[int, list[str]]:
    """
    Returns (score, matched_keywords).
    Primary keywords: +3 each.  Secondary: +1 each.
    Bonus +2 if the word 'postdoc' or 'research associate' appears.
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

    if re.search(r"postdoc|post-doc|post doc|research associate|research fellow", text_lower):
        score += 2

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
        "research associate statistics probability",
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

            jobs.append(
                _make_job(
                    url=job_url, title=title, institution=institution,
                    location=location, country="UK",
                    full_text=f"{title} {description}",
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


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run() -> None:
    print(f"\n{'='*60}")
    print(f"  Postdoc Agent — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    conn = init_db(DB_PATH)

    # ── Scrape all sources ──────────────────────────────────────────────────
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

    # ── Deduplicate and persist ─────────────────────────────────────────────
    new_jobs: list[dict] = []
    for job in all_jobs:
        if is_new(conn, job["id"]):
            insert_job(conn, job)
            new_jobs.append(job)

    print(f"  New this run:   {len(new_jobs)}")

    # ── Sort by relevance ───────────────────────────────────────────────────
    new_jobs.sort(key=lambda j: j["relevance_score"], reverse=True)

    # ── High-relevance jobs (primary keywords matched) ──────────────────────
    high_relevance = [j for j in new_jobs if j["relevance_score"] >= 6]
    any_relevance  = [j for j in new_jobs if j["relevance_score"] > 0]

    # ── Notify ──────────────────────────────────────────────────────────────
    if not new_jobs:
        print("\n  No new jobs today — no notification sent.\n")
    else:
        # Summary message
        summary = (
            f"📬 <b>Postdoc Hunt — {date.today().isoformat()}</b>\n\n"
            f"Found <b>{len(new_jobs)}</b> new listing(s) today.\n"
            f"• 🔬 High relevance (dimer/combinatorics): <b>{len(high_relevance)}</b>\n"
            f"• 📐 Any relevance: <b>{len(any_relevance)}</b>\n"
            f"• 📋 All new (broad math postdoc): <b>{len(new_jobs)}</b>"
        )
        send_telegram(summary)
        time.sleep(1)

        # Send top 10 by relevance
        to_notify = new_jobs[:10]
        for job in to_notify:
            send_telegram(format_job_message(job))
            mark_notified(conn, job["id"])
            time.sleep(1.2)   # Telegram rate limit: ~1 msg/sec

    # ── Local summary report ────────────────────────────────────────────────
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
