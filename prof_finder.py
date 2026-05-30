#!/usr/bin/env python3
"""
Professor Finder — Academic Outreach List Builder
==================================================
Finds researchers publishing on topics relevant to the monopole-dimer thesis
(combinatorics / statistical mechanics / algebraic combinatorics / random tilings),
checks their publications against Q1 journals and top conferences, extracts
current designation + affiliation + location, and produces a ranked outreach list.

Sources (all completely free, no API key needed):
  arXiv API      — paper & author discovery in her exact research area
  OpenAlex API   — author profiles, h-index, Q1 journal check, institution/country
  Semantic Scholar API — designation / position inference (free tier)

Outputs:
  profs.db                 — SQLite (persists across runs; only queries new names)
  professors_ranked.csv    — ready-to-use ranked outreach spreadsheet

Scoring weights (total 100):
  Research overlap    30 pts  — keyword match with her thesis area (PRIMARY criterion)
  Publication quality 35 pts  — Q1 paper count + top-conf papers + h-index
  Collaboration fit   15 pts  — career stage (assistant/junior prof ideal)
  Location            20 pts  — salary + lifestyle index by country

Run:
  pip install requests
  python prof_finder.py

  # Or with optional Telegram summary:
  TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy python prof_finder.py
"""

import csv
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from urllib.parse import urlencode, quote_plus

import requests

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

# Add your email so OpenAlex routes you to the polite pool (100k req/day free)
CONTACT_EMAIL      = os.getenv("CONTACT_EMAIL", "postdoc.search@example.com")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
DB_PATH            = os.getenv("PROFS_DB_PATH", "profs.db")
CSV_PATH           = "professors_ranked.csv"

HEADERS = {
    "User-Agent": "PostdocResearchBot/1.0 (academic outreach; mailto:" + CONTACT_EMAIL + ")"
}
POLITE_DELAY = 1.0  # seconds between API calls

# ─────────────────────────────────────────────────────────────────────────────
# RESEARCH AREA — tuned to the monopole-dimer thesis
# ─────────────────────────────────────────────────────────────────────────────

# Primary: directly overlap with her thesis
PRIMARY_RESEARCH_KW = [
    "dimer model", "dimer", "monopole-dimer",
    "domino tiling", "lozenge tiling", "random tiling",
    "arctic circle", "arctic curve",
    "kasteleyn", "pfaffian", "pfaffian formula",
    "height function", "height function lattice",
    "transfer matrix", "exactly solvable",
    "cylindrical lattice", "toroidal lattice",
    "möbius", "klein bottle",
    "matching polynomial", "matching theory",
]

# Secondary: broader field she can publish in
SECONDARY_RESEARCH_KW = [
    "algebraic combinatorics",
    "enumerative combinatorics",
    "symmetric functions",
    "young tableaux", "plane partitions",
    "cluster algebra",
    "statistical mechanics",
    "partition function",
    "integrable systems",
    "representation theory combinatorics",
    "discrete mathematics",
    "random matrix",
    "determinantal point process",
    "schur polynomial", "hook length",
    "catalan", "ballot problem",
    "generating function",
]

# arXiv search queries (math.CO = combinatorics, math-ph = math physics, math.PR = probability)
ARXIV_QUERIES = [
    "ti:dimer AND (cat:math.CO OR cat:math-ph)",
    "ti:monopole-dimer",
    "ti:lozenge tiling AND cat:math.CO",
    "ti:domino tiling AND cat:math.CO",
    "ti:arctic circle AND cat:math.CO",
    "ti:kasteleyn AND cat:math.CO",
    "ti:pfaffian AND ti:matching AND cat:math.CO",
    "ti:algebraic combinatorics AND ti:lattice",
    "abs:dimer model AND abs:cylindrical AND cat:math.CO",
    "ti:random tiling AND (cat:math.CO OR cat:math-ph)",
]

# ─────────────────────────────────────────────────────────────────────────────
# Q1 JOURNALS — combinatorics, algebra, probability, mathematical physics
# Sourced from Scimago SJR Q1 lists + field consensus
# ─────────────────────────────────────────────────────────────────────────────

Q1_JOURNALS: set[str] = {
    # ── Top-tier general math ────────────────────────────────────────────────
    "annals of mathematics",
    "journal of the american mathematical society",
    "inventiones mathematicae",
    "acta mathematica",
    "duke mathematical journal",
    "advances in mathematics",
    "forum of mathematics sigma",
    "forum of mathematics pi",
    "american journal of mathematics",
    "mathematische annalen",
    "journal für die reine und angewandte mathematik",
    "crelle's journal",
    "international mathematics research notices",
    "proceedings of the london mathematical society",
    "journal of the london mathematical society",
    # ── Combinatorics ────────────────────────────────────────────────────────
    "journal of combinatorial theory series a",
    "journal of combinatorial theory series b",
    "combinatorica",
    "siam journal on discrete mathematics",
    "european journal of combinatorics",
    "advances in combinatorics",
    "algebraic combinatorics",
    "journal of algebraic combinatorics",
    "electronic journal of combinatorics",
    "discrete mathematics",
    "journal of graph theory",
    "random structures and algorithms",
    # ── Algebra ──────────────────────────────────────────────────────────────
    "journal of algebra",
    "transformation groups",
    "selecta mathematica",
    "algebra and number theory",
    "communications in algebra",
    # ── Probability / statistical mechanics ──────────────────────────────────
    "annals of probability",
    "probability theory and related fields",
    "annals of applied probability",
    "electronic journal of probability",
    "stochastic processes and their applications",
    "journal of statistical physics",
    "communications in mathematical physics",
    "annales de l'institut henri poincaré probabilités et statistiques",
    "bernoulli",
    # ── Mathematical physics / discrete geometry ──────────────────────────────
    "letters in mathematical physics",
    "nuclear physics b",
    "journal of physics a mathematical and theoretical",
    "annales henri poincaré",
    # ── AMS / SIAM journals ───────────────────────────────────────────────────
    "transactions of the american mathematical society",
    "proceedings of the american mathematical society",
    "mathematics of computation",
    "siam journal on applied mathematics",
}

# Top conferences relevant to her area
TOP_CONFERENCES: set[str] = {
    "fpsac",          # Formal Power Series and Algebraic Combinatorics — primary conf
    "soda",           # ACM-SIAM Symposium on Discrete Algorithms
    "stoc",           # Symposium on Theory of Computing
    "focs",           # Foundations of Computer Science
    "eurocomb",       # European Conference on Combinatorics
    "slc",            # Séminaire Lotharingien de Combinatoire
    "lattice",        # International Symposium on Lattice Field Theory
    "combinatorics",
    "discrete mathematics",
}

# ─────────────────────────────────────────────────────────────────────────────
# LOCATION SCORING TABLE
# (lifestyle_score/100, approx_annual_postdoc_usd)
# ─────────────────────────────────────────────────────────────────────────────

LOCATION_DATA: dict[str, tuple[int, int]] = {
    "Switzerland":    (95, 88000),
    "Denmark":        (90, 62000),
    "Norway":         (88, 56000),
    "Sweden":         (86, 42000),
    "Finland":        (84, 52000),
    "Netherlands":    (83, 45000),
    "Germany":        (82, 56000),
    "Austria":        (81, 46000),
    "Belgium":        (80, 50000),
    "Australia":      (84, 82000),
    "Singapore":      (82, 70000),
    "Canada":         (80, 56000),
    "United States":  (79, 72000),
    "United Kingdom": (77, 44000),
    "France":         (75, 38000),
    "Israel":         (73, 46000),
    "Spain":          (70, 30000),
    "Italy":          (68, 28000),
    "Portugal":       (66, 26000),
    "Japan":          (70, 38000),
    "South Korea":    (71, 40000),
    "India":          (55, 12000),
}

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────

def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS professors (
            id                   TEXT PRIMARY KEY,  -- OpenAlex author ID
            name                 TEXT NOT NULL,
            designation          TEXT,   -- assistant/associate/full professor, postdoc …
            affiliation          TEXT,   -- institution name
            city                 TEXT,
            country              TEXT,
            openalex_url         TEXT,
            works_count          INTEGER DEFAULT 0,
            cited_by_count       INTEGER DEFAULT 0,
            h_index              INTEGER DEFAULT 0,
            q1_paper_count       INTEGER DEFAULT 0,
            top_conf_count       INTEGER DEFAULT 0,
            research_overlap_score  INTEGER DEFAULT 0,
            publication_quality_score INTEGER DEFAULT 0,
            collab_fit_score     INTEGER DEFAULT 0,
            location_score       INTEGER DEFAULT 0,
            total_score          INTEGER DEFAULT 0,
            relevant_papers_json TEXT,  -- JSON array of {title, venue, year, citations}
            last_updated         TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_arxiv_papers (
            arxiv_id TEXT PRIMARY KEY
        )
    """)
    conn.commit()
    return conn


def prof_exists(conn: sqlite3.Connection, openalex_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM professors WHERE id=?", (openalex_id,)
    ).fetchone() is not None


def upsert_professor(conn: sqlite3.Connection, p: dict) -> None:
    conn.execute("""
        INSERT INTO professors
          (id, name, designation, affiliation, city, country, openalex_url,
           works_count, cited_by_count, h_index, q1_paper_count, top_conf_count,
           research_overlap_score, publication_quality_score, collab_fit_score,
           location_score, total_score, relevant_papers_json, last_updated)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          designation=excluded.designation,
          affiliation=excluded.affiliation,
          city=excluded.city,
          country=excluded.country,
          works_count=excluded.works_count,
          cited_by_count=excluded.cited_by_count,
          h_index=excluded.h_index,
          q1_paper_count=excluded.q1_paper_count,
          top_conf_count=excluded.top_conf_count,
          research_overlap_score=excluded.research_overlap_score,
          publication_quality_score=excluded.publication_quality_score,
          collab_fit_score=excluded.collab_fit_score,
          location_score=excluded.location_score,
          total_score=excluded.total_score,
          relevant_papers_json=excluded.relevant_papers_json,
          last_updated=excluded.last_updated
    """, (
        p["id"], p["name"], p["designation"], p["affiliation"],
        p["city"], p["country"], p["openalex_url"],
        p["works_count"], p["cited_by_count"], p["h_index"],
        p["q1_paper_count"], p["top_conf_count"],
        p["research_overlap_score"], p["publication_quality_score"],
        p["collab_fit_score"], p["location_score"], p["total_score"],
        json.dumps(p["relevant_papers"]),
        date.today().isoformat(),
    ))
    conn.commit()


def mark_arxiv_seen(conn: sqlite3.Connection, arxiv_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen_arxiv_papers VALUES (?)", (arxiv_id,)
    )
    conn.commit()


def is_arxiv_seen(conn: sqlite3.Connection, arxiv_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM seen_arxiv_papers WHERE arxiv_id=?", (arxiv_id,)
    ).fetchone() is not None


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None, timeout: int = 20) -> dict | str | None:
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "")
        if "json" in ct:
            return r.json()
        return r.text
    except Exception as e:
        print(f"    ⚠ GET error {url}: {e}")
        return None


def normalise_venue(name: str) -> str:
    """Lowercase + strip punctuation for fuzzy journal matching."""
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def is_q1(venue_name: str) -> bool:
    if not venue_name:
        return False
    norm = normalise_venue(venue_name)
    for j in Q1_JOURNALS:
        if normalise_venue(j) in norm or norm in normalise_venue(j):
            return True
    return False


def is_top_conf(venue_name: str) -> bool:
    if not venue_name:
        return False
    norm = venue_name.lower()
    return any(c in norm for c in TOP_CONFERENCES)


# ─────────────────────────────────────────────────────────────────────────────
# arXiv: PAPER & AUTHOR DISCOVERY
# ─────────────────────────────────────────────────────────────────────────────

def fetch_arxiv_authors(query: str, max_results: int = 50) -> list[dict]:
    """
    Query arXiv and return a list of
    {arxiv_id, title, authors: [{name, arxiv_id}], categories, year}
    """
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    xml_text = _get("http://export.arxiv.org/api/query", params=params)
    if not xml_text or not isinstance(xml_text, str):
        return []

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    papers = []
    for entry in root.findall("atom:entry", ns):
        arxiv_id = ""
        id_el = entry.find("atom:id", ns)
        if id_el is not None and id_el.text:
            arxiv_id = id_el.text.split("/abs/")[-1].strip()

        title_el = entry.find("atom:title", ns)
        title = title_el.text.strip().replace("\n", " ") if title_el is not None else ""

        published_el = entry.find("atom:published", ns)
        year = int(published_el.text[:4]) if published_el is not None else 0

        authors = []
        for a in entry.findall("atom:author", ns):
            name_el = a.find("atom:name", ns)
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())

        papers.append({
            "arxiv_id": arxiv_id,
            "title": title,
            "authors": authors,
            "year": year,
        })

    return papers


# ─────────────────────────────────────────────────────────────────────────────
# OpenAlex: AUTHOR PROFILE + PUBLICATION HISTORY
# ─────────────────────────────────────────────────────────────────────────────

def openalex_search_author(name: str) -> dict | None:
    """Find the best-matching OpenAlex author profile for a given name."""
    params = {
        "search": name,
        "per_page": 3,
        "mailto": CONTACT_EMAIL,
    }
    data = _get("https://api.openalex.org/authors", params=params)
    if not data or not isinstance(data, dict):
        return None
    results = data.get("results", [])
    if not results:
        return None
    # Pick the result whose display_name best matches; prefer most-cited
    for r in results:
        if r.get("display_name", "").lower() == name.lower():
            return r
    return results[0]   # fallback: most cited match


def openalex_get_works(author_id: str, max_results: int = 100) -> list[dict]:
    """
    Fetch recent works for an OpenAlex author.
    Returns list of {title, venue, year, citations, is_q1, is_top_conf, keywords}.
    """
    params = {
        "filter": f"authorships.author.id:{author_id}",
        "per_page": min(max_results, 200),
        "sort": "cited_by_count:desc",
        "select": "title,primary_location,publication_year,cited_by_count,concepts",
        "mailto": CONTACT_EMAIL,
    }
    data = _get("https://api.openalex.org/works", params=params)
    if not data or not isinstance(data, dict):
        return []

    works = []
    for w in data.get("results", []):
        venue = ""
        loc = w.get("primary_location") or {}
        source = loc.get("source") or {}
        venue = source.get("display_name", "") or ""

        concepts = [c.get("display_name", "").lower() for c in w.get("concepts", [])]
        keywords = " ".join(concepts)

        works.append({
            "title": w.get("title", ""),
            "venue": venue,
            "year": w.get("publication_year", 0),
            "citations": w.get("cited_by_count", 0),
            "is_q1": is_q1(venue),
            "is_top_conf": is_top_conf(venue),
            "keywords": keywords,
        })

    return works


def extract_institution(author_data: dict) -> tuple[str, str, str]:
    """
    Returns (institution_name, city, country) from an OpenAlex author object.
    """
    inst = (author_data.get("last_known_institution") or
            author_data.get("last_known_affiliations", [{}])[0] if
            author_data.get("last_known_affiliations") else {})

    if not inst:
        return "", "", ""

    name    = inst.get("display_name", "")
    country = inst.get("country_code", "")

    # Convert ISO country code to full name
    country = ISO_TO_COUNTRY.get(country.upper(), country)

    # City is not directly in OpenAlex author object; extract from institution geo
    # We can get it via the institution endpoint if needed
    city = inst.get("city", "")

    return name, city, country


def infer_designation(author_data: dict, works: list[dict]) -> str:
    """
    Infer career stage from works count + citation count + first publication year.
    OpenAlex doesn't expose job title directly, so we estimate.
    """
    first_pub = author_data.get("works_count", 0)
    h_index   = author_data.get("summary_stats", {}).get("h_index", 0)
    citations = author_data.get("cited_by_count", 0)
    years_active = 0

    if works:
        years = [w["year"] for w in works if w["year"] and w["year"] > 1980]
        if years:
            years_active = date.today().year - min(years)

    # Heuristic tiers (rough but useful for filtering)
    if years_active <= 3 and h_index <= 5:
        return "Postdoc / Early Career"
    elif years_active <= 7 and h_index <= 12:
        return "Assistant Professor"
    elif years_active <= 15 and h_index <= 25:
        return "Associate Professor"
    else:
        return "Full Professor"


# ─────────────────────────────────────────────────────────────────────────────
# ISO COUNTRY CODE → FULL NAME (subset)
# ─────────────────────────────────────────────────────────────────────────────

ISO_TO_COUNTRY = {
    "US": "United States", "GB": "United Kingdom", "DE": "Germany",
    "FR": "France", "CH": "Switzerland", "NL": "Netherlands",
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland",
    "AT": "Austria", "BE": "Belgium", "AU": "Australia", "CA": "Canada",
    "IL": "Israel", "IT": "Italy", "ES": "Spain", "PT": "Portugal",
    "JP": "Japan", "CN": "China", "SG": "Singapore", "KR": "South Korea",
    "IN": "India", "BR": "Brazil", "NZ": "New Zealand", "IE": "Ireland",
    "CZ": "Czech Republic", "PL": "Poland", "HU": "Hungary",
    "RU": "Russia", "ZA": "South Africa",
}


# ─────────────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────────────

def score_research_overlap(works: list[dict], author_concepts: list[str]) -> int:
    """
    0–30 pts.
    Counts how many of her primary/secondary keywords appear across
    the author's paper titles, concepts, and venues.
    """
    all_text = " ".join(
        f"{w['title']} {w['venue']} {w['keywords']}" for w in works
    ).lower()
    all_text += " " + " ".join(author_concepts).lower()

    score = 0
    for kw in PRIMARY_RESEARCH_KW:
        if kw.lower() in all_text:
            score += 3
    for kw in SECONDARY_RESEARCH_KW:
        if kw.lower() in all_text:
            score += 1

    return min(score, 30)


def score_publication_quality(
    works: list[dict],
    h_index: int,
    q1_count: int,
    top_conf_count: int,
) -> int:
    """
    0–35 pts.
    h-index (capped), Q1 paper count (capped), top-conference count.
    """
    h_score    = min(h_index, 10)          # max 10 pts from h-index
    q1_score   = min(q1_count * 2, 15)     # max 15 pts: 8+ Q1 papers → full marks
    conf_score = min(top_conf_count * 2, 10) # max 10 pts: 5+ top-conf papers
    return h_score + q1_score + conf_score


def score_collaboration_fit(designation: str) -> int:
    """
    0–15 pts.
    Assistant professor / junior faculty is the ideal host:
    they have stable positions, need to build groups, and benefit from
    productive postdocs. Full professors have less bandwidth.
    """
    d = designation.lower()
    if "assistant" in d:
        return 15
    elif "associate" in d:
        return 12
    elif "junior" in d or "early career" in d:
        return 11
    elif "postdoc" in d:
        return 6   # not stable enough to host; still worth knowing
    elif "full" in d or "professor" in d:
        return 8
    return 8


def score_location(country: str) -> int:
    """0–20 pts. Blends lifestyle index + salary into a single score."""
    if country not in LOCATION_DATA:
        return 5   # unknown country
    lifestyle, salary = LOCATION_DATA[country]
    # Normalise salary: $88k (Switzerland max) → 20 pts
    salary_norm = min(salary / 88000, 1.0) * 10
    lifestyle_norm = (lifestyle / 100) * 10
    return round(salary_norm + lifestyle_norm)


# ─────────────────────────────────────────────────────────────────────────────
# CSV EXPORT
# ─────────────────────────────────────────────────────────────────────────────

CSV_COLS = [
    "Rank", "Name", "Designation", "Institution", "City", "Country",
    "H-Index", "Citations", "Q1 Papers", "Top Conf Papers",
    "Research Overlap /30", "Publication Quality /35",
    "Collab Fit /15", "Location /20", "Total Score /100",
    "Top Relevant Papers",
    "OpenAlex Profile",
]


def export_csv(conn: sqlite3.Connection, path: str) -> int:
    rows = conn.execute("""
        SELECT name, designation, affiliation, city, country,
               h_index, cited_by_count, q1_paper_count, top_conf_count,
               research_overlap_score, publication_quality_score,
               collab_fit_score, location_score, total_score,
               relevant_papers_json, openalex_url
        FROM professors
        ORDER BY total_score DESC
    """).fetchall()

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLS)
        for rank, row in enumerate(rows, 1):
            (name, desig, affil, city, country,
             h, cites, q1, tconf,
             overlap, pubq, collab, loc, total,
             papers_json, oa_url) = row

            # Format top 3 relevant papers
            papers = json.loads(papers_json or "[]")[:3]
            papers_str = " | ".join(
                f"{p['title'][:60]} ({p['venue'][:30]}, {p['year']}, {p['citations']} cites)"
                for p in papers
            )
            writer.writerow([
                rank, name, desig, affil, city, country,
                h, cites, q1, tconf,
                overlap, pubq, collab, loc, total,
                papers_str, oa_url,
            ])

    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM (optional summary)
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram error: {e}")


def format_prof_summary(profs: list[tuple]) -> str:
    """Top-10 Telegram summary."""
    lines = [f"🔬 <b>Prof Finder — {date.today().isoformat()}</b>\n"
             f"Top researchers for outreach:\n"]
    for rank, (name, desig, affil, country, total, overlap) in enumerate(profs[:10], 1):
        lines.append(
            f"{rank}. <b>{name}</b> — {affil} ({country})\n"
            f"   {desig} · Score: {total}/100 · Overlap: {overlap}/30\n"
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run() -> None:
    print(f"\n{'='*60}")
    print(f"  Professor Finder — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    conn = init_db(DB_PATH)

    # ── Step 1: Discover authors from arXiv ─────────────────────────────────
    print("  Phase 1: Discovering authors from arXiv ...")
    all_authors: set[str] = set()

    for query in ARXIV_QUERIES:
        print(f"    Query: {query}")
        papers = fetch_arxiv_authors(query, max_results=30)
        for paper in papers:
            if is_arxiv_seen(conn, paper["arxiv_id"]):
                continue
            mark_arxiv_seen(conn, paper["arxiv_id"])
            for author in paper["authors"]:
                all_authors.add(author)
        time.sleep(POLITE_DELAY)

    print(f"  → Unique authors found: {len(all_authors)}\n")

    # ── Step 2: Enrich each author via OpenAlex ──────────────────────────────
    print("  Phase 2: Fetching OpenAlex profiles ...")
    processed = 0
    skipped   = 0

    for author_name in sorted(all_authors):
        print(f"    Looking up: {author_name}")

        # Find OpenAlex profile
        oa_author = openalex_search_author(author_name)
        if not oa_author:
            skipped += 1
            time.sleep(POLITE_DELAY)
            continue

        oa_id = oa_author.get("id", "").split("/")[-1]  # e.g. A12345678

        if not oa_id:
            skipped += 1
            continue

        # Fetch their works
        works = openalex_get_works(oa_id, max_results=100)
        time.sleep(POLITE_DELAY)

        # Institutional data
        institution, city, country = extract_institution(oa_author)

        # Publication counts
        q1_works      = [w for w in works if w["is_q1"]]
        top_conf_works = [w for w in works if w["is_top_conf"]]

        # h-index (OpenAlex provides this directly)
        stats   = oa_author.get("summary_stats") or {}
        h_index = stats.get("h_index", 0) or 0
        cites   = oa_author.get("cited_by_count", 0) or 0

        # Infer designation
        designation = infer_designation(oa_author, works)

        # Research overlap
        concepts = [c.get("display_name", "") for c in oa_author.get("x_concepts", [])]
        overlap_score = score_research_overlap(works, concepts)

        # Skip if zero overlap AND zero Q1 papers — unrelated researcher
        if overlap_score == 0 and len(q1_works) == 0:
            print(f"      → Skipping (no relevance): {author_name}")
            skipped += 1
            continue

        # Score components
        pub_score    = score_publication_quality(works, h_index, len(q1_works), len(top_conf_works))
        collab_score = score_collaboration_fit(designation)
        loc_score    = score_location(country)
        total_score  = overlap_score + pub_score + collab_score + loc_score

        # Top relevant papers (Q1 or high overlap, sorted by citations)
        relevant = sorted(
            [w for w in works if w["is_q1"] or w["is_top_conf"] or
             any(k.lower() in w["title"].lower() for k in PRIMARY_RESEARCH_KW)],
            key=lambda w: w["citations"],
            reverse=True,
        )[:5]

        prof = {
            "id":              oa_id,
            "name":            oa_author.get("display_name", author_name),
            "designation":     designation,
            "affiliation":     institution,
            "city":            city,
            "country":         country,
            "openalex_url":    oa_author.get("id", ""),
            "works_count":     oa_author.get("works_count", 0),
            "cited_by_count":  cites,
            "h_index":         h_index,
            "q1_paper_count":  len(q1_works),
            "top_conf_count":  len(top_conf_works),
            "research_overlap_score":      overlap_score,
            "publication_quality_score":   pub_score,
            "collab_fit_score":            collab_score,
            "location_score":              loc_score,
            "total_score":                 total_score,
            "relevant_papers":             relevant,
        }

        upsert_professor(conn, prof)
        processed += 1
        print(
            f"      ✓ {prof['name']} | {institution} ({country}) | "
            f"Score: {total_score}/100 | H: {h_index} | Q1: {len(q1_works)}"
        )
        time.sleep(POLITE_DELAY)

    print(f"\n  Processed: {processed}  |  Skipped (no relevance): {skipped}")

    # ── Step 3: Export ranked CSV ────────────────────────────────────────────
    print(f"\n  Phase 3: Exporting ranked list to {CSV_PATH} ...")
    n = export_csv(conn, CSV_PATH)
    print(f"  → {n} professors written to {CSV_PATH}")

    # ── Step 4: Telegram summary (optional) ─────────────────────────────────
    top_profs = conn.execute("""
        SELECT name, designation, affiliation, country,
               total_score, research_overlap_score
        FROM professors
        ORDER BY total_score DESC
        LIMIT 10
    """).fetchall()

    if TELEGRAM_BOT_TOKEN:
        send_telegram(format_prof_summary(top_profs))

    # ── Console top-10 ───────────────────────────────────────────────────────
    print(f"\n  {'─'*58}")
    print(f"  {'RANK':<5} {'NAME':<25} {'INSTITUTION':<25} {'SCORE':>5}")
    print(f"  {'─'*58}")
    for rank, row in enumerate(top_profs, 1):
        name, desig, affil, country, total, overlap = row
        print(f"  {rank:<5} {name[:24]:<25} {(affil or '')[:24]:<25} {total:>5}")
    print(f"  {'─'*58}\n")
    print(f"  Full list saved to: {CSV_PATH}")
    print(f"  Database:           {DB_PATH}\n")

    conn.close()


if __name__ == "__main__":
    run()
