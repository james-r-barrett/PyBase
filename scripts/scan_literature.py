"""
Scans Europe PMC for new pyrenoid-related literature and adds anything not
already known (in submissions or literature_queue) to the review queue.

Requires two environment variables, provided as GitHub Actions secrets:
  SUPABASE_URL               e.g. https://iplxcpyzefjbtnfacejd.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  the *secret* key (sb_secret_...) — never the
                              publishable key, and never committed to the repo.
"""

import os
import re
import sys
import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

EUROPEPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# Add more queries here over time as you learn what phrasing works / doesn't.
QUERIES = [
    'pyrenoid AND (ultrastructure OR "electron microscopy")',
    'pyrenoid AND (chloroplast OR plastid) AND algae',
]

PAGE_SIZE = 100


def normalize_doi(doi):
    if not doi:
        return None
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I).strip().lower()


def fetch_europepmc(query):
    params = {
        "query": query,
        "format": "json",
        "pageSize": PAGE_SIZE,
        "resultType": "core",
    }
    resp = requests.get(EUROPEPMC_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("resultList", {}).get("result", [])


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


def insert_candidate(citation, doi):
    payload = {
        "citation": citation,
        "doi": doi,
        "notes": "auto-added from Europe PMC scan",
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

    for query in QUERIES:
        try:
            results = fetch_europepmc(query)
        except requests.RequestException as e:
            print(f"Query failed, skipping: {query!r} ({e})", file=sys.stderr)
            continue

        for r in results:
            doi = normalize_doi(r.get("doi"))
            # Skip anything with no DOI at all — without one, dedup is
            # unreliable and it's easy to flood the queue with repeats.
            if not doi or doi in known_dois:
                continue

            title = (r.get("title") or "").rstrip(".")
            authors = r.get("authorString") or ""
            year = r.get("pubYear") or ""
            journal = r.get("journalTitle") or ""
            citation = f"{authors} ({year}). {title}. {journal}."

            insert_candidate(citation, r.get("doi"))
            known_dois.add(doi)
            added += 1

    print(f"Added {added} new candidate reference(s) to literature_queue.")


if __name__ == "__main__":
    main()