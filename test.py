"""
Manual smoke-test script for Ahara's core lookups (CrossRef, Unpaywall, Sci-Hub
mirrors). Reuses server.py's implementations directly instead of duplicating
the DoH patch and scraping logic here.

Usage: python test.py <DOI>
"""
import sys

from server import (
    SCI_HUB_URLS,
    get_crossref_metadata,
    get_unpaywall_pdf_url,
    get_scihub_pdf_url,
)


def test_crossref(doi: str) -> None:
    print(f"\n[CrossRef] Fetching metadata for {doi}...")
    metadata = get_crossref_metadata(doi)
    if metadata:
        print(f"Title: {metadata.get('title')}")
        print(f"Authors: {', '.join(metadata.get('authors', []))}")
        print(f"Year: {metadata.get('year')}")
    else:
        print("No metadata found.")


def test_unpaywall(doi: str) -> None:
    print(f"\n[Unpaywall] Checking Open Access for {doi}...")
    pdf_url = get_unpaywall_pdf_url(doi)
    if pdf_url:
        print(f"Is Open Access: True")
        print(f"Best PDF URL: {pdf_url}")
    else:
        print("Is Open Access: False (or no OA PDF found)")


def test_scihub_mirrors() -> None:
    import requests

    print("\n[Sci-Hub] Checking mirrors...")
    for m in SCI_HUB_URLS:
        try:
            resp = requests.head(m, timeout=5, verify=False)
            print(f"{m}: {'ONLINE' if resp.status_code < 400 else f'ERROR ({resp.status_code})'}")
        except Exception:
            print(f"{m}: OFFLINE")


def test_scihub_scrape(doi: str) -> None:
    print(f"\n[Sci-Hub Scraper] Trying mirrors for {doi}...")
    try:
        pdf_url = get_scihub_pdf_url(doi)
        print(f"Found PDF URL: {pdf_url}")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        doi = sys.argv[1]
        print(f"=== TESTING DOI: {doi} ===")

        test_crossref(doi)
        test_unpaywall(doi)
        test_scihub_mirrors()
        test_scihub_scrape(doi)
    else:
        print("Usage: python test.py <DOI>")
        test_scihub_mirrors()
