"""
Live smoke test for Āhara's sources: shows what each one says about a DOI without
downloading anything or opening a browser. (Sci-Hub domain learning may update
~/.ahara/mirrors.json, the same way the server does.)

Usage:
    python test.py <DOI> [<DOI> ...]
    python test.py              # Sci-Hub / Sci-Net reachability only
"""
import sys

import server


def check(doi: str) -> None:
    doi = server.normalize_doi(doi)
    print(f"\n=== {doi} ===")

    meta = server.get_crossref_metadata(doi)
    print(f"[CrossRef]       {meta.get('title', 'no metadata')} ({meta.get('year')})")

    work = server.get_openalex_work(doi)
    oa = server._dedupe(
        [server.arxiv_pdf_url(doi)]
        + server.unpaywall_pdf_candidates(server.get_unpaywall_record(doi))
        + server.openalex_pdf_candidates(work)
    )
    print(f"[Open access]    {len(oa)} candidate link(s)")
    for url in oa[:5]:
        print(f"                 - {url}")
    cached = server.openalex_content_url(work)
    print(f"[OpenAlex cache] {'available' if cached else 'not available (or no AHARA_OPENALEX_API_KEY)'}")

    try:
        pdf, _ = server.scinet_lookup(doi)
        print(f"[Sci-Net]        {pdf or 'not uploaded'}")
    except Exception as e:
        print(f"[Sci-Net]        error: {e}")

    result = server.scihub_lookup(doi)
    print(f"[Sci-Hub]        {result.kind} (via {result.page_url or 'no domain'})")
    if result.pdf_url:
        print(f"                 PDF: {result.pdf_url}")
    if result.page and result.page.similar:
        print(f"                 similar: {', '.join(result.page.similar)}")
    for err in result.errors:
        print(f"                 skipped {err}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            check(arg)
    else:
        print(__doc__)
        print(server.ahara_mirror_status())
