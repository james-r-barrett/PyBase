"""
Scans Europe PMC and OpenAlex for new pyrenoid-related literature and adds
anything not already known (in submissions or literature_queue) to the
review queue.

Requires environment variables, provided as GitHub Actions secrets:
  SUPABASE_URL               e.g. https://iplxcpyzefjbtnfacejd.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  the *secret* key (sb_secret_...) — never the
                              publishable key, and never committed to the repo.
  OPENALEX_API_KEY           free key from openalex.org/settings/api
"""

import os
import re
import sys
import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENALEX_API_KEY = os.environ["OPENALEX_API_KEY"]

HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

EUROPEPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
OPENALEX_URL = "https://api.openalex.org/works"

# Add more queries here over time as you learn what phrasing works / doesn't.
# "fine structure" is deliberately included — it's the period-typical phrase
# 1960s-80s EM papers use instead of "ultrastructure".
EUROPEPMC_QUERIES = [
    'pyrenoid AND (ultrastructure OR "electron microscopy" OR "fine structure")',
    'pyrenoid AND (chloroplast OR plastid) AND algae',
    'pyrenoid AND (taxonomy OR morphology) AND algae',
]
OPENALEX_QUERIES = [
    "pyrenoid ultrastructure",
    "pyrenoid fine structure algae",
    "pyrenoid electron microscopy",
]

# How many results to pull per query, per source, via pagination. Given the
# free daily OpenAlex credit covers ~100,000 search results, this is nowhere
# near the ceiling — raise it further if the queue still looks thin.
MAX_RESULTS_PER_QUERY = 1000


def normalize_doi(doi):
    if not doi:
        return None
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I).strip().lower()


def fetch_europepmc(query, max_results=MAX_RESULTS_PER_QUERY):
    """Paginates via Europe PMC's cursorMark mechanism. Returns a list of
    dicts normalized to {doi, title, authors, year, journal}."""
    results = []
    cursor_mark = "*"
    while len(results) < max_results:
        params = {
            "query": query,
            "format": "json",
            "pageSize": min(100, max_results - len(results)),
            "resultType": "core",
            "cursorMark": cursor_mark,
        }
        resp = requests.get(EUROPEPMC_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("resultList", {}).get("result", [])
        if not batch:
            break
        results.extend(batch)
        next_cursor = data.get("nextCursorMark")
        if not next_cursor or next_cursor == cursor_mark:
            break
        cursor_mark = next_cursor

    return [
        {
            "doi": r.get("doi"),
            "title": (r.get("title") or "").rstrip("."),
            "authors": r.get("authorString") or "",
            "year": r.get("pubYear") or "",
            "journal": r.get("journalTitle") or "",
        }
        for r in results
    ]


def fetch_openalex(query, max_results=MAX_RESULTS_PER_QUERY):
    """Paginates via OpenAlex's cursor mechanism. Returns a list of dicts
    normalized to {doi, title, authors, year, journal}."""
    results = []
    cursor = "*"
    while len(results) < max_results:
        params = {
            "search": query,
            "per_page": min(200, max_results - len(results)),
            "cursor": cursor,
            "api_key": OPENALEX_API_KEY,
        }
        resp = requests.get(OPENALEX_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("results", [])
        if not batch:
            break
        results.extend(batch)
        next_cursor = (data.get("meta") or {}).get("next_cursor")
        if not next_cursor:
            break
        cursor = next_cursor

    normalized = []
    for r in results:
        authorships = r.get("authorships") or []
        authors = ", ".join(
            a.get("author", {}).get("display_name", "")
            for a in authorships
            if a.get("author", {}).get("display_name")
        )
        primary_location = r.get("primary_location") or {}
        source = primary_location.get("source") or {}
        normalized.append(
            {
                "doi": r.get("doi"),
                "title": (r.get("title") or "").rstrip("."),
                "authors": authors,
                "year": r.get("publication_year") or "",
                "journal": source.get("display_name") or "",
            }
        )
    return normalized


SOURCES = [
    ("Europe PMC", fetch_europepmc, EUROPEPMC_QUERIES),
    ("OpenAlex", fetch_openalex, OPENALEX_QUERIES),
]


def fetch_known_dois():
    """Pull every DOI already present in submissions or literature_queue,
    so we don't re-suggest something already logged or already queued.
    Paginated via PostgREST's Range header — the default 1000-row cap would
    otherwise silently miss entries once either table grows past that."""
    known = set()
    for table in ("submissions", "literature_queue"):
        start = 0
        page_size = 1000
        while True:
            resp = requests.get(
                f"{SUPABASE_URL}/rest/v1/{table}",
                headers={**HEADERS, "Range": f"{start}-{start + page_size - 1}"},
                params={"select": "doi", "doi": "not.is.null"},
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json()
            for row in batch:
                d = normalize_doi(row.get("doi"))
                if d:
                    known.add(d)
            if len(batch) < page_size:
                break
            start += page_size
    return known


def safe_year(y):
    try:
        return int(y)
    except (TypeError, ValueError):
        return None


def insert_candidate(citation, doi, source_name, pub_year=None):
    payload = {
        "citation": citation,
        "doi": doi,
        "pub_year": pub_year,
        "notes": f"auto-added from {source_name} scan",
    }
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/literature_queue",
        headers=HEADERS,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()


def main():
    known_dois = fetch_known_dois()
    added = 0
    per_source_counts = {}

    for source_name, fetch_fn, queries in SOURCES:
        per_source_counts[source_name] = 0
        for query in queries:
            try:
                results = fetch_fn(query)
            except requests.RequestException as e:
                print(f"[{source_name}] query failed, skipping: {query!r} ({e})", file=sys.stderr)
                continue

            print(f"[{source_name}] {query!r} -> {len(results)} result(s) fetched")

            for r in results:
                doi = normalize_doi(r["doi"])
                if not doi or doi in known_dois:
                    continue

                citation = f"{r['authors']} ({r['year']}). {r['title']}. {r['journal']}."
                insert_candidate(citation, r["doi"], source_name, pub_year=safe_year(r["year"]))
                known_dois.add(doi)
                added += 1
                per_source_counts[source_name] += 1

    print(f"Added {added} new candidate reference(s) to literature_queue.")
    for name, count in per_source_counts.items():
        print(f"  {name}: {count}")


if __name__ == "__main__":
    main()