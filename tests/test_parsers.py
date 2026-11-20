"""Offline tests for Āhara's parsers and helpers.

The HTML fixtures in tests/fixtures/ were captured from the live sites (November 2026).
When Sci-Hub or Sci-Net change their layout, capture a fresh page, replace the
fixture, and adjust parse_scihub_page / parse_scinet_page until these pass again.
"""

from pathlib import Path

import pytest

import server

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- Sci-Hub pages -------------------------------------------------------------


def test_scihub_article_page_gives_pdf_link():
    page = server.parse_scihub_page(load("scihub_article.html"), "https://sci-hub.fr/10.1126/science.1157996")
    assert page.kind == "pdf"
    assert page.pdf_url == "https://sci-hub.fr/storage/2024/1116/56ebf93d7d8ff0844e69aeaa79500880/lee2008.pdf"


def test_scihub_article_page_is_not_mistaken_for_robot_check():
    # Article pages embed an ALTCHA widget in their hidden "report a problem" dialog,
    # and the word "captcha" appears in their scripts.
    html = load("scihub_article.html")
    assert "altcha-widget" in html and "captcha" in html
    assert server.parse_scihub_page(html, "https://sci-hub.fr/x").kind == "pdf"


def test_scihub_robot_page():
    page = server.parse_scihub_page(load("scihub_robot.html"), "https://sci-hub.fr/10.1126/science.1157996")
    assert page.kind == "robot"
    assert page.pdf_url is None
    assert page.challenge_url == "https://sci-hub.fr/captcha/challenge/25536286"
    assert page.solution_url == "https://sci-hub.fr/captcha/solution/25536286"
    assert page.script_url == "https://sci-hub.fr/scripts/altcha.min.js"


def test_scihub_not_found_page_lists_similar_papers():
    page = server.parse_scihub_page(load("scihub_notfound.html"), "https://sci-hub.fr/10.1016/j.cell.2026.01.001")
    assert page.kind == "not_found"
    assert "10.1007/s10555-023-10095-1" in page.similar
    assert len(page.similar) <= 5


def test_page_that_is_not_scihub_is_unknown():
    html = "<html><head><title>This domain is for sale</title></head><body></body></html>"
    assert server.parse_scihub_page(html, "https://sci-hub.example/10.1/x").kind == "unknown"


def test_legacy_iframe_layout_still_parses():
    html = '<html><body><iframe id="pdf" src="//moscow.sci-hub.example/1234/abc.pdf#view=FitH"></iframe></body></html>'
    page = server.parse_scihub_page(html, "https://sci-hub.example/10.1/x")
    assert page.kind == "pdf"
    assert page.pdf_url == "https://moscow.sci-hub.example/1234/abc.pdf"


def test_relay_page_states():
    page = server.parse_scihub_page(load("scihub_robot.html"), "https://sci-hub.fr/10.1126/science.1157996")
    relay = server.RobotCheckRelay()
    assert "Nothing to check" in relay.render()

    relay.arm("10.1126/science.1157996", "https://sci-hub.fr/10.1126/science.1157996", page)
    relay._refresh = lambda: None  # stay offline: keep the fixture's challenge
    pending = relay.render()
    assert "<altcha-widget" in pending and "10.1126/science.1157996" in pending
    assert "auto=" not in pending  # the user has to tick the box themselves

    relay.solved.set()
    assert "<h1>Done</h1>" in relay.render()


# --- Sci-Net pages ---------------------------------------------------------------


def test_scinet_paper_page_gives_pdf_link():
    url = server.parse_scinet_page(load("scinet_found.html"), "https://sci-net.xyz/10.1038/s41586-024-07421-0")
    assert url.startswith("https://sci-net.xyz/storage/2024/1715398/")
    assert url.endswith(".pdf")  # the "#view=FitH" fragment is dropped


def test_scinet_sends_unknown_dois_to_its_homepage():
    assert server.parse_scinet_page(load("scinet_home.html"), "https://sci-net.xyz/") is None


# --- Mirror bookkeeping --------------------------------------------------------------


def test_mirror_order_prefers_overrides_then_last_good_then_recent():
    order = server.order_mirror_candidates(
        env=["sci-hub.custom"],
        last_good="https://sci-hub.fr/",
        seen={
            "https://sci-hub.old/": "2026-01-01T00:00:00+00:00",
            "https://sci-hub.fr/": "2026-11-20T00:00:00+00:00",
            "https://sci-hub.mid/": "2026-06-01T00:00:00+00:00",
        },
        seeds=["https://sci-hub.ru/"],
        discovered=["https://sci-hub.ru/", "https://sci-hub.su/"],
    )
    assert order == [
        "https://sci-hub.custom/",
        "https://sci-hub.fr/",
        "https://sci-hub.mid/",
        "https://sci-hub.old/",
        "https://sci-hub.ru/",
        "https://sci-hub.su/",
    ]


def _site(url, rank="normal", ended=None):
    claim = {"rank": rank, "mainsnak": {"datavalue": {"value": url}}}
    if ended:
        claim["qualifiers"] = {"P582": [{"snaktype": ended}]}
    return claim


def test_wikidata_sites_skip_ended_deprecated_and_onion():
    entity = {
        "claims": {
            "P856": [
                _site("https://sci-hub.se", ended="value"),
                _site("https://sci-hub.mu", ended="somevalue"),
                _site("https://sci-hub.red"),
                _site("http://scihub22266oqcxt.onion"),
                _site("https://sci-hub.ru", rank="preferred"),
                _site("https://sci-hub.old", rank="deprecated"),
                _site("https://sci-hub.st", rank="preferred", ended="novalue"),
            ]
        }
    }
    assert server.parse_wikidata_sites(entity) == [
        "https://sci-hub.ru/",
        "https://sci-hub.st/",
        "https://sci-hub.red/",
    ]


# --- Open-access candidates ------------------------------------------------------------


def test_unpaywall_candidates_cover_every_location():
    record = {
        "best_oa_location": {"url_for_pdf": "https://a.example/best.pdf"},
        "oa_locations": [
            {"url_for_pdf": "https://a.example/best.pdf"},
            {"url_for_pdf": None, "url": "https://repo.example/paper.pdf?download=1"},
            {"url_for_pdf": None, "url": "https://repo.example/landing-page"},
            {"url_for_pdf": "https://b.example/second.pdf"},
        ],
    }
    assert server.unpaywall_pdf_candidates(record) == [
        "https://a.example/best.pdf",
        "https://repo.example/paper.pdf?download=1",
        "https://b.example/second.pdf",
    ]


def test_openalex_candidates_use_listed_pdf_links():
    work = {
        "best_oa_location": {"pdf_url": "https://best.example/paper.pdf"},
        "locations": [
            {"pdf_url": "https://ora.example/file"},
            {"pdf_url": None},
            {"pdf_url": "https://best.example/paper.pdf"},
        ],
        "ids": {"pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/11186750"},
    }
    # Europe PMC links are not invented from the PMCID: they 403 scripted requests.
    assert server.openalex_pdf_candidates(work) == ["https://best.example/paper.pdf", "https://ora.example/file"]


def test_arxiv_dois_go_straight_to_arxiv():
    assert server.arxiv_pdf_url("10.48550/arXiv.1706.03762") == "https://arxiv.org/pdf/1706.03762"
    assert server.arxiv_pdf_url("10.1038/nature12373") is None


# --- Small helpers -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("10.1038/nature12373", "10.1038/nature12373"),
        ("  https://doi.org/10.1038/nature12373 ", "10.1038/nature12373"),
        ("http://dx.doi.org/10.1002%2Fleap.1254", "10.1002/leap.1254"),
        ("doi:10.1126/science.1157996", "10.1126/science.1157996"),
    ],
)
def test_normalize_doi(raw, expected):
    assert server.normalize_doi(raw) == expected


def test_require_doi_rejects_non_dois():
    with pytest.raises(ValueError):
        server._require_doi("Attention is all you need")


def test_pdf_sniff():
    assert server.looks_like_pdf(b"%PDF-1.4\n%\xe2\xe3")
    assert server.looks_like_pdf(b"\xef\xbb\xbf  %PDF-1.7")  # leading junk is allowed
    assert not server.looks_like_pdf(b"<!doctype html><title>Are you a robot?</title>")


def test_rebuild_abstract_restores_word_order():
    inverted = {"Graphene": [0], "is": [1, 4], "strong": [2], "and": [3], "light": [5]}
    assert server.rebuild_abstract(inverted) == "Graphene is strong and is light"
    assert server.rebuild_abstract(None) == ""


def test_manual_options_follow_enabled_sources(monkeypatch):
    monkeypatch.setattr(server, "SOURCES", ("oa",))
    monkeypatch.setattr(server, "LIBRARY_PROXY", "")
    text = server._manual_options("10.1/x", None)
    assert "sci-hub" not in text.lower() and "sci-net" not in text.lower()
    assert "https://doi.org/10.1/x" in text


def test_library_link(monkeypatch):
    monkeypatch.setattr(server, "LIBRARY_PROXY", "https://login.proxy.example/login?url=")
    assert server.library_link("10.1/x") == "https://login.proxy.example/login?url=https://doi.org/10.1/x"
    monkeypatch.setattr(server, "LIBRARY_PROXY", "https://proxy.example/{url}?via=ahara")
    assert server.library_link("10.1/x") == "https://proxy.example/https://doi.org/10.1/x?via=ahara"
