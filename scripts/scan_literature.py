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
EUROPEPMC_QUERIES = [
    'pyrenoid AND (ultrastructure OR "electron microscopy")',
    'pyrenoid AND (chloroplast OR plastid) AND algae',
]
OPENALEX_QUERIES = [
    "pyrenoid ultrastructure",
    "pyrenoid algae electron microscopy",
]

PAGE_SIZE = 100


def normalize_doi(doi):
    if not doi:
        return None
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I).strip().lower()


def fetch_europepmc(query):
    """Returns a list of dicts already normalized to
    {doi, title, authors, year, journal}."""
    params = {
        "query": query,
        "format": "json",
        "pageSize": PAGE_SIZE,
        "resultType": "core",
    }
    resp = requests.get(EUROPEPMC_URL, params=params, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("resultList", {}).get("result", [])
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


def fetch_openalex(query):
    """Returns a list of dicts already normalized to
    {doi, title, authors, year, journal}."""
    params = {
        "search": query,
        "per_page": PAGE_SIZE,
        "api_key": OPENALEX_API_KEY,
    }
    resp = requests.get(OPENALEX_URL, params=params, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results", [])
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
    so we don't re-suggest something already logged or already queued."""
    known = set()
    for table in ("submissions", "literature_queue"):
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=HEADERS,
            params={"select": "doi", "doi": "not.is.null"},
            timeout=30,
        )
        resp.raise_for_status()
        for row in resp.json():
            d = normalize_doi(row.get("doi"))
            if d:
                known.add(d)
    return known


def insert_candidate(citation, doi, source_name):
    payload = {
        "citation": citation,
        "doi": doi,
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

    for source_name, fetch_fn, queries in SOURCES:
        for query in queries:
            try:
                results = fetch_fn(query)
            except requests.RequestException as e:
                print(f"[{source_name}] query failed, skipping: {query!r} ({e})", file=sys.stderr)
                continue

            for r in results:
                doi = normalize_doi(r["doi"])
                # Skip anything with no DOI at all — without one, dedup is
                # unreliable and it's easy to flood the queue with repeats.
                if not doi or doi in known_dois:
                    continue

                citation = f"{r['authors']} ({r['year']}). {r['title']}. {r['journal']}."
                insert_candidate(citation, r["doi"], source_name)
                known_dois.add(doi)
                added += 1

    print(f"Added {added} new candidate reference(s) to literature_queue.")


if __name__ == "__main__":
    main()