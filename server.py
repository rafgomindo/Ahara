"""Āhara: an MCP server that fetches academic papers by DOI.

Fetch chain (order and membership set by AHARA_SOURCES):
  oa                legal open-access copies: arXiv, Unpaywall, OpenAlex
  openalex_content  OpenAlex's cached PDF (needs AHARA_OPENALEX_API_KEY)
  scinet            Sci-Net community uploads (sci-net.xyz), read-only and anonymous
  scihub            Sci-Hub, through whichever domain currently answers

Sci-Hub's domains change often, so Āhara learns the current one from redirects and
remembers it (see "Sci-Hub mirror state"). When Sci-Hub asks "are you a robot?", the
user answers it themselves on a small local page; Āhara only relays that answer so
the check clears for the session that downloads the PDF (see "Robot-check relay").
"""

import contextlib
import html
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pymupdf as fitz
import pytesseract
import requests
import urllib3
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from habanero import Crossref
from mcp.server.mcpserver import MCPServer
from PIL import Image

__version__ = "2.1.1"

# --- Logging -----------------------------------------------------------
# MCP stdio transport uses stdout for the JSON-RPC stream, so all
# diagnostic output MUST go to stderr instead.
logging.basicConfig(
    level=os.getenv("AHARA_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("ahara")
# habanero's HTTP client logs every request at INFO; keep that out of the log.
for _noisy in ("httpx", "httpx2"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# --- Configuration -------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# pip installs put server.py in site-packages, which is no place for settings or papers.
INSTALLED = "site-packages" in BASE_DIR.replace("\\", "/").split("/")
# Settings: .env next to server.py (repo checkout), then ~/.ahara/.env (any install).
# Neither overrides variables that are already set.
load_dotenv(os.path.join(BASE_DIR, ".env"))
load_dotenv(os.path.join(os.path.expanduser("~"), ".ahara", ".env"))


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a whole number; using {default}.")
        return default


ALL_SOURCES = ("oa", "openalex_content", "scinet", "scihub")


def _parse_sources(raw: str | None) -> tuple[str, ...]:
    if not raw or not raw.strip():
        return ALL_SOURCES
    chosen = []
    for name in (part.strip().lower() for part in raw.split(",")):
        if name in ALL_SOURCES and name not in chosen:
            chosen.append(name)
        elif name:
            logger.warning(f"Ignoring unknown source {name!r} in AHARA_SOURCES (valid: {', '.join(ALL_SOURCES)}).")
    return tuple(chosen) or ALL_SOURCES


AHARA_EMAIL = os.getenv("AHARA_EMAIL", "ahara-mcp@example.com")
TRANSPORT = os.getenv("AHARA_TRANSPORT", "stdio")
SOURCES = _parse_sources(os.getenv("AHARA_SOURCES"))
OPENALEX_API_KEY = os.getenv("AHARA_OPENALEX_API_KEY", "").strip()
LIBRARY_PROXY = os.getenv("AHARA_LIBRARY_PROXY", "").strip()
ROBOT_WAIT_SECONDS = _env_int("AHARA_ROBOT_WAIT", 90)
RELAY_HOST = os.getenv("AHARA_RELAY_HOST", "127.0.0.1")
RELAY_PORT = _env_int("AHARA_RELAY_PORT", 0)
STATE_DIR = os.path.expanduser(os.getenv("AHARA_STATE_DIR", "~/.ahara"))
DOWNLOADS_DIR = os.path.expanduser(os.getenv("AHARA_DOWNLOADS_DIR", "~/Downloads"))
DEFAULT_OUTPUT_DIR = os.path.expanduser(
    os.getenv("AHARA_OUTPUT_DIR")
    or ("~/sci-hub-downloads" if INSTALLED else os.path.join(BASE_DIR, "sci-hub-downloads"))
)
IMPORT_WINDOW_MINUTES = 15

# Long-lived Sci-Hub entry domains (Wikidata Q21980377, "official website"). The
# backend they redirect to changes often, so the one that actually answers is
# learned at runtime and remembered. AHARA_SCIHUB_MIRRORS entries are tried first.
SCIHUB_SEED_MIRRORS = (
    "https://sci-hub.ru/",
    "https://sci-hub.st/",
    "https://sci-hub.su/",
    "https://sci-hub.red/",
    "https://sci-hub.box/",
)
ENV_MIRRORS = [m.strip() for m in os.getenv("AHARA_SCIHUB_MIRRORS", "").split(",") if m.strip()]
SCIHUB_WIKIDATA_ID = "Q21980377"
SCINET_URL = "https://sci-net.xyz/"
SCIBOT_URL = "https://sci-bot.ru/"
OPENALEX_API = "https://api.openalex.org"

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
API_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; academic-paper-fetcher)"}

# Sci-Hub certificates are sometimes odd, so its requests skip TLS verification.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- DNS over HTTPS (DoH) patch to bypass ISP blocking of Sci-Hub --------
_orig_getaddrinfo = socket.getaddrinfo
_doh_cache: dict[str, tuple[str, float]] = {}


def _doh_resolve(host: str) -> str | None:
    cached = _doh_cache.get(host)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    resp = requests.get(
        "https://cloudflare-dns.com/dns-query",
        params={"name": host, "type": "A"},
        headers={"accept": "application/dns-json"},
        timeout=5,
    )
    if resp.status_code != 200:
        return None
    for answer in resp.json().get("Answer", []):
        if answer.get("type") == 1:  # A record (skips CNAMEs)
            ttl = max(60, int(answer.get("TTL") or 300))
            _doh_cache[host] = (answer["data"], time.monotonic() + ttl)
            return answer["data"]
    return None


def _doh_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if isinstance(host, str) and "sci-hub" in host:
        try:
            ip = _doh_resolve(host)
            if ip:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        except Exception as e:
            logger.debug(f"DoH lookup failed for {host}: {e}")
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _doh_getaddrinfo

# --- HTTP ------------------------------------------------------------------
# One cookie-keeping session for Sci-Hub and Sci-Net: their robot-check clearance
# and DDoS-Guard cookies live in it, and Sci-Hub's PDF link only works inside it.
_shadow = requests.Session()
_shadow.headers.update(REQUEST_HEADERS)
# Connection pooling for publisher and repository downloads.
_web = requests.Session()
_web.headers.update(REQUEST_HEADERS)


def request_with_retry(
    method: str,
    url: str,
    retries: int = 2,
    backoff: float = 1.5,
    *,
    session: requests.Session | None = None,
    **kwargs,
) -> requests.Response:
    """Performs an HTTP request, retrying on transient network errors."""
    send = session.request if session is not None else requests.request
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return send(method, url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exc = e
            if attempt < retries:
                wait = backoff**attempt
                logger.warning(f"Request to {url} failed ({e}), retrying in {wait:.1f}s...")
                time.sleep(wait)
    assert last_exc is not None
    raise last_exc


# --- Small helpers ----------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dedupe(items) -> list:
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


def _short(url: str, limit: int = 70) -> str:
    """host + path, trimmed, for compact attempt logs (drops the query string)."""
    parsed = urllib.parse.urlparse(url)
    text = parsed.netloc + parsed.path
    return text if len(text) <= limit else text[: limit - 1] + "…"


_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
_DOI_SHAPE = re.compile(r"^10\.\d{4,9}(?:\.\d+)*/\S+$")


def normalize_doi(doi: str) -> str:
    """Strips doi.org / 'doi:' prefixes and whitespace."""
    doi = (doi or "").strip()
    stripped = _DOI_PREFIX.sub("", doi)
    if stripped != doi:
        stripped = urllib.parse.unquote(stripped)
    return stripped.strip()


def _require_doi(doi: str) -> str:
    clean = normalize_doi(doi)
    if not _DOI_SHAPE.match(clean):
        raise ValueError(f"'{doi}' doesn't look like a DOI (expected something like 10.1038/nature12373).")
    return clean


def _quote_doi(doi: str) -> str:
    return urllib.parse.quote(doi, safe="/:;()")


def paper_path(doi: str, output_dir: str | None = None) -> str:
    directory = os.path.expanduser(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    return os.path.join(directory, re.sub(r"[^a-zA-Z0-9_\-]", "_", doi) + ".pdf")


def _metadata_text(metadata: dict) -> str:
    return json.dumps(metadata, indent=2, ensure_ascii=False) if metadata else "No metadata found."


# --- PDF download -------------------------------------------------------------
PDF_MAGIC = b"%PDF-"


def looks_like_pdf(head: bytes) -> bool:
    """True if the first KB holds the PDF signature (the spec allows leading junk)."""
    return PDF_MAGIC in head[:1024]


def _is_pdf_file(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return looks_like_pdf(f.read(1024))
    except OSError:
        return False


def download_pdf(
    url: str,
    dest_path: str,
    *,
    session: requests.Session | None = None,
    referer: str | None = None,
    timeout: int = 60,
) -> tuple[bool, str]:
    """Saves url to dest_path only if it really is a PDF. Returns (ok, reason).

    Writes to a temporary file and renames it at the end, so an HTML error or
    captcha page is never left behind as '<doi>.pdf'.
    """
    headers = {"Referer": referer} if referer else None
    directory = os.path.dirname(dest_path) or "."
    try:
        resp = request_with_retry(
            "GET",
            url,
            session=session or _web,
            stream=True,
            timeout=timeout,
            verify="sci-hub" not in _host(url),
            headers=headers,
        )
        with resp:
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}"
            chunks = resp.iter_content(chunk_size=65536)
            head = b""
            for chunk in chunks:
                head += chunk
                if len(head) >= 1024:
                    break
            if not looks_like_pdf(head):
                ctype = (resp.headers.get("Content-Type") or "unknown type").split(";")[0]
                return False, f"not a PDF ({ctype})"
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(suffix=".part", dir=directory)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(head)
                    for chunk in chunks:
                        f.write(chunk)
                os.replace(tmp_path, dest_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.remove(tmp_path)
                raise
    except requests.RequestException as e:
        return False, type(e).__name__
    return True, "ok"


# --- Page parsers (pure: HTML in, result out; tested against tests/fixtures) ---


@dataclass
class SciHubPage:
    """What a Sci-Hub page turned out to be."""

    kind: str  # "pdf", "robot", "not_found" or "unknown"
    pdf_url: str | None = None
    challenge_url: str | None = None
    solution_url: str | None = None
    script_url: str | None = None
    similar: list[str] = field(default_factory=list)


def _resolve_link(link: str, page_url: str) -> str:
    link = link.strip().split("#", 1)[0]
    if link.startswith("//"):
        link = "https:" + link
    return urllib.parse.urljoin(page_url, link)


def _pdf_links(soup: BeautifulSoup) -> list[str]:
    """Candidate PDF links on a Sci-Hub page, most reliable first."""
    links = []
    meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if meta and meta.get("content"):
        links.append(meta["content"])
    for tag in soup.find_all("object", attrs={"data": True}):
        if tag.get("type") == "application/pdf" or ".pdf" in tag["data"].lower():
            links.append(tag["data"])
    for tag in soup.find_all(["iframe", "embed"], attrs={"src": True}):
        src = tag["src"]
        if tag.get("id") == "pdf" or "/storage/" in src or ".pdf" in src.lower():
            links.append(src)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/storage/" in href or href.lower().split("#", 1)[0].endswith(".pdf"):
            links.append(href)
    for el in soup.find_all(attrs={"onclick": True}):
        match = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", el["onclick"])
        if match:
            links.append(match.group(1))
    return [link for link in links if link.strip() and not link.strip().startswith(("#", "javascript:"))]


def parse_scihub_page(html_text: str, page_url: str) -> SciHubPage:
    """Classifies a Sci-Hub DOI page and extracts what Āhara needs from it."""
    soup = BeautifulSoup(html_text, "html.parser")

    links = _pdf_links(soup)
    if links:
        return SciHubPage("pdf", pdf_url=_resolve_link(links[0], page_url))

    # The robot check is an ALTCHA widget in the page body. Article pages embed
    # one too, but only inside the hidden "report a problem" <dialog>.
    widget = soup.find("altcha-widget")
    if widget is not None and widget.find_parent("dialog") is None:
        challenge = widget.get("challengeurl")
        match = re.search(r"""fetch\(\s*['"]([^'"]*/captcha/solution/[^'"]*)['"]""", html_text)
        if match:
            solution = match.group(1)
        elif challenge and "/captcha/challenge" in challenge:
            solution = challenge.replace("/captcha/challenge", "/captcha/solution", 1)
        else:
            solution = None
        script = soup.find("script", src=re.compile("altcha", re.I))
        return SciHubPage(
            "robot",
            challenge_url=_resolve_link(challenge, page_url) if challenge else None,
            solution_url=_resolve_link(solution, page_url) if solution else None,
            script_url=_resolve_link(script["src"] if script else "/scripts/altcha.min.js", page_url),
        )
    if soup.find(id="captcha") is not None:  # older image-captcha layout
        return SciHubPage("robot")

    title = soup.title.get_text(strip=True) if soup.title else ""
    if (
        soup.find("a", href=re.compile(r"sci-net\.xyz"))
        or soup.find("div", class_="similar")
        or title.lower().startswith("sci-hub")
    ):
        similar = [
            a["href"].lstrip("/")
            for a in soup.select("div.similar a[href]")
            if a["href"].lstrip("/").startswith("10.")
        ]
        return SciHubPage("not_found", similar=similar[:5])
    return SciHubPage("unknown")


def parse_scinet_page(html_text: str, page_url: str) -> str | None:
    """The PDF link on a Sci-Net paper page, or None.

    Sci-Net redirects DOIs it doesn't hold to its homepage.
    """
    if urllib.parse.urlparse(page_url).path.strip("/") == "":
        return None
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup.select("div.pdf iframe[src], iframe[src], embed[src]"):
        return _resolve_link(tag["src"], page_url)
    obj = soup.find("object", attrs={"data": True})
    if obj:
        return _resolve_link(obj["data"], page_url)
    meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if meta and meta.get("content"):
        return _resolve_link(meta["content"], page_url)
    return None


def rebuild_abstract(inverted_index: dict | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; this puts them back in order."""
    if not inverted_index:
        return ""
    positioned = [(pos, word) for word, positions in inverted_index.items() for pos in positions]
    return " ".join(word for _, word in sorted(positioned))


# --- Sci-Hub mirror state ---------------------------------------------------------
# ~/.ahara/mirrors.json remembers which Sci-Hub domain answered last, the domains
# seen recently (mostly redirect targets) and what discovery found.
_STATE_FILE = os.path.join(STATE_DIR, "mirrors.json")
_state_lock = threading.Lock()
MAX_REMEMBERED_HOSTS = 10
DISCOVERY_MAX_AGE_SECONDS = 24 * 3600


def _normalize_base(url: str | None) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
        return None
    return f"{parsed.scheme or 'https'}://{parsed.netloc.lower()}/"


def _load_state() -> dict:
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp_path = _STATE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, _STATE_FILE)
    except OSError as e:
        logger.warning(f"Could not save mirror state to {_STATE_FILE}: {e}")


def order_mirror_candidates(
    env: list[str], last_good: str | None, seen: dict[str, str], seeds, discovered: list[str]
) -> list[str]:
    """Order to try Sci-Hub domains: user overrides, last that answered, recently
    seen, built-in seeds, then discovered."""
    recent = [host for host, _ in sorted(seen.items(), key=lambda kv: kv[1], reverse=True)]
    return _dedupe(_normalize_base(u) for u in [*env, last_good, *recent, *seeds, *discovered])


def scihub_candidates() -> list[str]:
    state = _load_state()
    return order_mirror_candidates(
        ENV_MIRRORS,
        state.get("last_good"),
        state.get("seen") or {},
        SCIHUB_SEED_MIRRORS,
        state.get("discovered") or [],
    )


def remember_scihub_host(url: str) -> None:
    """Records the domain that just answered as Sci-Hub as the one to try first."""
    base = _normalize_base(url)
    if not base:
        return
    with _state_lock:
        state = _load_state()
        seen = dict(state.get("seen") or {})
        seen[base] = _now_iso()
        state["seen"] = dict(sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:MAX_REMEMBERED_HOSTS])
        state["last_good"] = base
        _save_state(state)


def parse_wikidata_sites(entity: dict) -> list[str]:
    """Sci-Hub domains from a Wikidata entity's 'official website' (P856) claims
    that are not deprecated and have no end time (P582); preferred ones first."""
    preferred, normal = [], []
    for claim in (entity.get("claims") or {}).get("P856", []):
        if claim.get("rank") == "deprecated":
            continue
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if not isinstance(value, str) or "sci-hub" not in value or ".onion" in value:
            continue
        end_times = (claim.get("qualifiers") or {}).get("P582", [])
        if any(q.get("snaktype") in ("value", "somevalue") for q in end_times):
            continue
        (preferred if claim.get("rank") == "preferred" else normal).append(_normalize_base(value))
    return _dedupe(preferred + normal)


def discover_scihub_mirrors(force: bool = False) -> list[str]:
    """Current Sci-Hub domains from Wikidata and Sci-Hub's sister site Sci-Bot.

    Cached in the mirror state for a day unless force=True.
    """
    state = _load_state()
    last_run = state.get("discovered_at")
    if not force and last_run:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_run)).total_seconds()
            if age < DISCOVERY_MAX_AGE_SECONDS:
                return state.get("discovered") or []
        except ValueError:
            pass

    found: list[str] = []
    try:
        resp = request_with_retry(
            "GET",
            f"https://www.wikidata.org/wiki/Special:EntityData/{SCIHUB_WIKIDATA_ID}.json",
            headers=API_HEADERS,
            timeout=15,
        )
        found += parse_wikidata_sites(resp.json()["entities"][SCIHUB_WIKIDATA_ID])
    except Exception as e:
        logger.warning(f"Wikidata mirror discovery failed: {e}")
    try:
        resp = request_with_retry("GET", SCIBOT_URL, headers=REQUEST_HEADERS, timeout=15)
        found += [_normalize_base(u) for u in re.findall(r"https?://(?:www\.)?sci-hub\.[a-z]{2,}", resp.text)]
    except Exception as e:
        logger.warning(f"Sci-Bot mirror discovery failed: {e}")

    found = _dedupe(found)
    with _state_lock:
        state = _load_state()
        if found:
            state["discovered"] = found
        state["discovered_at"] = _now_iso()
        _save_state(state)
    return found or (state.get("discovered") or [])


# --- Sources ----------------------------------------------------------------------
# Successful lookups are cached per process; failures are not, so a network hiccup
# doesn't stick for the whole session.
_crossref_cache: dict[str, dict] = {}
_unpaywall_cache: dict[str, dict] = {}
_openalex_cache: dict[str, dict] = {}


def get_crossref_metadata(doi: str) -> dict:
    """Fetches metadata for a DOI from CrossRef."""
    if doi in _crossref_cache:
        return _crossref_cache[doi]
    try:
        cr = Crossref(mailto=AHARA_EMAIL)
        res = cr.works(ids=doi)
        if res and "message" in res:
            msg = res["message"]
            dated = msg.get("published-print") or msg.get("published-online") or msg.get("issued") or {}
            metadata = {
                "title": (msg.get("title") or ["Unknown"])[0],
                "authors": [
                    f"{a.get('given', '')} {a.get('family', '')}".strip() for a in msg.get("author", [])
                ],
                "publisher": msg.get("publisher", "Unknown"),
                "journal": (msg.get("container-title") or ["Unknown"])[0],
                "year": (dated.get("date-parts") or [[None]])[0][0],
                "doi": doi,
                "url": msg.get("URL", f"https://doi.org/{doi}"),
            }
            _crossref_cache[doi] = metadata
            return metadata
    except Exception as e:
        # 404 is normal for DOIs registered elsewhere (arXiv's are DataCite DOIs).
        log = logger.debug if "404" in str(e) else logger.warning
        log(f"CrossRef lookup failed for {doi}: {e}")
    return {}


def get_unpaywall_record(doi: str) -> dict:
    if doi in _unpaywall_cache:
        return _unpaywall_cache[doi]
    try:
        resp = request_with_retry(
            "GET", f"https://api.unpaywall.org/v2/{_quote_doi(doi)}", params={"email": AHARA_EMAIL}, timeout=15
        )
        if resp.status_code == 200:
            _unpaywall_cache[doi] = resp.json()
            return _unpaywall_cache[doi]
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"Unpaywall lookup failed for {doi}: {e}")
    return {}


def unpaywall_pdf_candidates(record: dict) -> list[str]:
    """Every PDF link Unpaywall lists for a DOI, best location first."""
    urls = []
    for loc in [record.get("best_oa_location"), *(record.get("oa_locations") or [])]:
        if not loc:
            continue
        if loc.get("url_for_pdf"):
            urls.append(loc["url_for_pdf"])
        elif (loc.get("url") or "").lower().split("?", 1)[0].endswith(".pdf"):
            urls.append(loc["url"])
    return _dedupe(urls)


def get_openalex_work(doi: str) -> dict:
    if doi in _openalex_cache:
        return _openalex_cache[doi]
    try:
        params = {"api_key": OPENALEX_API_KEY} if OPENALEX_API_KEY else None
        resp = request_with_retry("GET", f"{OPENALEX_API}/works/doi:{_quote_doi(doi)}", params=params, timeout=15)
        if resp.status_code == 200:
            _openalex_cache[doi] = resp.json()
            return _openalex_cache[doi]
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"OpenAlex lookup failed for {doi}: {e}")
    return {}


def openalex_metadata(work: dict, doi: str) -> dict:
    """Same shape as get_crossref_metadata, built from an OpenAlex work (covers
    DataCite DOIs such as arXiv's, which CrossRef doesn't know)."""
    if not work:
        return {}
    source = ((work.get("primary_location") or {}).get("source") or {})
    return {
        "title": work.get("display_name") or "Unknown",
        "authors": [
            a.get("author", {}).get("display_name")
            for a in (work.get("authorships") or [])
            if a.get("author", {}).get("display_name")
        ],
        "publisher": source.get("host_organization_name") or "Unknown",
        "journal": source.get("display_name") or "Unknown",
        "year": work.get("publication_year"),
        "doi": doi,
        "url": f"https://doi.org/{doi}",
    }


def get_paper_metadata(doi: str) -> dict:
    """CrossRef metadata, falling back to OpenAlex for DOIs CrossRef doesn't have."""
    return get_crossref_metadata(doi) or openalex_metadata(get_openalex_work(doi), doi)


def openalex_pdf_candidates(work: dict) -> list[str]:
    """PDF links OpenAlex knows for a work, best location first.

    PubMed Central and Europe PMC answer scripted PDF requests with a bot check or
    HTTP 403 (checked Nov 2026), so their links usually fail the PDF sniff; for
    those papers OpenAlex's cached copy (openalex_content) is the scriptable route.
    """
    urls = []
    best = work.get("best_oa_location") or {}
    if best.get("pdf_url"):
        urls.append(best["pdf_url"])
    urls += [loc["pdf_url"] for loc in (work.get("locations") or []) if loc and loc.get("pdf_url")]
    return _dedupe(urls)


def openalex_content_url(work: dict) -> str | None:
    """OpenAlex's cached copy of the PDF ($0.01 each; a free key covers about 100 a day)."""
    if not OPENALEX_API_KEY or not (work.get("has_content") or {}).get("pdf"):
        return None
    url = (work.get("content_urls") or {}).get("pdf")
    if not url:
        work_id = (work.get("id") or "").rsplit("/", 1)[-1]
        if not work_id:
            return None
        url = f"https://content.openalex.org/works/{work_id}.pdf"
    return url + ("&" if "?" in url else "?") + urllib.parse.urlencode({"api_key": OPENALEX_API_KEY})


def arxiv_pdf_url(doi: str) -> str | None:
    match = re.match(r"10\.48550/arxiv\.(.+)$", doi, re.I)
    return f"https://arxiv.org/pdf/{match.group(1)}" if match else None


def scinet_lookup(doi: str) -> tuple[str | None, str]:
    """(PDF link or None, page URL) for a DOI on Sci-Net. Anonymous and read-only."""
    resp = request_with_retry("GET", SCINET_URL + _quote_doi(doi), session=_shadow, timeout=20)
    if resp.status_code != 200:
        return None, resp.url
    return parse_scinet_page(resp.text, resp.url), resp.url


@dataclass
class SciHubResult:
    kind: str  # "pdf", "robot", "not_found" or "unreachable"
    page_url: str | None = None
    page: SciHubPage | None = None
    pdf_url: str | None = None
    errors: list[str] = field(default_factory=list)


def scihub_lookup(doi: str) -> SciHubResult:
    """Asks Sci-Hub about a DOI, trying domains until one answers.

    Every working domain currently redirects to the same backend, so the first real
    answer (PDF, robot check or "not available") is final; only network failures and
    pages that aren't Sci-Hub move on to the next domain. Discovery runs if none of
    the known domains answer.
    """
    errors: list[str] = []
    tried: set[str] = set()

    def attempt(bases: list[str]) -> SciHubResult | None:
        for base in bases:
            if not base or base in tried:
                continue
            tried.add(base)
            try:
                resp = request_with_retry("GET", base + _quote_doi(doi), session=_shadow, timeout=20, verify=False)
            except requests.RequestException as e:
                errors.append(f"{_host(base)}: {type(e).__name__}")
                continue
            if resp.status_code != 200:
                errors.append(f"{_host(base)}: HTTP {resp.status_code}")
                continue
            if _host(resp.url).endswith("sci-net.xyz"):  # Sci-Hub sometimes hands DOIs to Sci-Net
                pdf = parse_scinet_page(resp.text, resp.url)
                kind = "pdf" if pdf else "not_found"
                return SciHubResult(kind, page_url=resp.url, pdf_url=pdf, errors=errors)
            page = parse_scihub_page(resp.text, resp.url)
            if page.kind == "unknown":
                errors.append(f"{_host(base)}: not a Sci-Hub page")
                continue
            remember_scihub_host(resp.url)
            return SciHubResult(page.kind, page_url=resp.url, page=page, pdf_url=page.pdf_url, errors=errors)
        return None

    result = attempt(scihub_candidates()) or attempt(discover_scihub_mirrors())
    return result or SciHubResult("unreachable", errors=errors)


# --- Robot-check relay ------------------------------------------------------------
# Sci-Hub's "are you a robot?" check is an ALTCHA proof-of-work widget, and the
# clearance it grants belongs to the session that answered it. Solving it in your
# own browser therefore doesn't help Āhara. Instead Āhara serves a small page on
# localhost with the same widget: the user ticks the box in their browser and Āhara
# forwards the challenge and the user's answer through its own Sci-Hub session.
# Nothing is answered automatically; without the user's click the check stays.

_PAGE_STYLE = (
    "body{font:16px/1.5 system-ui,sans-serif;max-width:36rem;margin:4rem auto;padding:0 1rem;color:#1d1d1f}"
    "h1{font-size:1.5rem}code{background:#f1f1f1;padding:.1rem .3rem;border-radius:4px}"
    "#status{margin-top:1.5rem;font-weight:600}"
)


def _relay_page(title: str, body: str) -> str:
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width"><title>{html.escape(title)}</title>'
        f"<style>{_PAGE_STYLE}</style></head><body>{body}</body></html>"
    )


class RobotCheckRelay:
    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(16)
        self.solved = threading.Event()
        self._lock = threading.Lock()
        self._pending: dict | None = None
        self._server: ThreadingHTTPServer | None = None
        self._script: bytes | None = None

    @property
    def url(self) -> str:
        host = "localhost" if RELAY_HOST in ("0.0.0.0", "::", "") else RELAY_HOST
        port = self._server.server_address[1] if self._server else RELAY_PORT
        return f"http://{host}:{port}/{self.token}/"

    def arm(self, doi: str, page_url: str, page: SciHubPage) -> None:
        with self._lock:
            self._pending = {
                "doi": doi,
                "page_url": page_url,
                "challenge_url": page.challenge_url,
                "solution_url": page.solution_url,
                "script_url": page.script_url,
            }
        self.solved.clear()

    def start(self) -> None:
        if self._server is not None:
            return
        handler = type("RelayHandler", (_RelayHandler,), {"relay": self})
        self._server = ThreadingHTTPServer((RELAY_HOST, RELAY_PORT), handler)
        threading.Thread(target=self._server.serve_forever, name="ahara-robot-relay", daemon=True).start()
        logger.info(f"Robot-check page listening on {self.url}")

    def _snapshot(self) -> dict:
        with self._lock:
            return dict(self._pending or {})

    def _refresh(self) -> None:
        """Re-reads the DOI page so the widget gets a fresh challenge (or notices the
        session is already clear)."""
        pending = self._snapshot()
        if not pending:
            return
        try:
            resp = request_with_retry("GET", pending["page_url"], session=_shadow, timeout=20, verify=False)
        except requests.RequestException as e:
            logger.warning(f"Robot-check refresh failed: {e}")
            return
        page = parse_scihub_page(resp.text, resp.url)
        if page.kind == "robot" and page.challenge_url and page.solution_url:
            with self._lock:
                if self._pending:
                    self._pending.update(
                        page_url=resp.url,
                        challenge_url=page.challenge_url,
                        solution_url=page.solution_url,
                        script_url=page.script_url,
                    )
        elif page.kind in ("pdf", "not_found"):
            self.solved.set()

    def render(self) -> str:
        if not self._snapshot():
            return _relay_page("Āhara", "<h1>Nothing to check</h1><p>Āhara has no pending Sci-Hub robot check.</p>")
        if not self.solved.is_set():
            self._refresh()
        if self.solved.is_set():
            return _relay_page(
                "Āhara: done",
                "<h1>Done</h1><p>Sci-Hub accepted your answer. You can close this tab and go back to Claude.</p>",
            )
        doi = html.escape(self._snapshot().get("doi", ""))
        return _relay_page(
            "Āhara: Sci-Hub robot check",
            '<script type="module" src="altcha.js"></script>'
            "<h1>Sci-Hub is asking: are you a robot?</h1>"
            f"<p>Āhara hit Sci-Hub's robot check while fetching <code>{doi}</code>. "
            "Tick the box below to answer it yourself. Āhara only passes your answer on "
            "to Sci-Hub, so the check clears for the session that downloads the paper.</p>"
            '<altcha-widget challengeurl="challenge"></altcha-widget>'
            '<p id="status"></p>'
            "<script>"
            "document.querySelector('altcha-widget').addEventListener('statechange', async (ev) => {"
            "  if (ev.detail.state !== 'verified') return;"
            "  const status = document.getElementById('status');"
            "  status.textContent = 'Sending your answer to Sci-Hub…';"
            "  try {"
            "    const r = await fetch('solution', {method: 'POST', headers: {'Content-Type': 'application/json'},"
            "      body: JSON.stringify({captcha: ev.detail.payload})});"
            "    const data = await r.json().catch(() => ({}));"
            "    status.textContent = data.success"
            "      ? 'Done. You can close this tab and go back to Claude.'"
            "      : 'Sci-Hub did not accept the answer. Reload this page to try again.';"
            "  } catch (e) { status.textContent = 'Could not reach Āhara: ' + e; }"
            "});"
            "</script>",
        )

    def fetch_script(self) -> tuple[int, bytes, str]:
        if self._script is None:
            pending = self._snapshot()
            if not pending.get("script_url"):
                return 404, b"", "text/plain"
            resp = request_with_retry(
                "GET", pending["script_url"], session=_shadow, timeout=20, verify=False,
                headers={"Referer": pending["page_url"]},
            )
            if resp.status_code != 200:
                return 502, b"", "text/plain"
            self._script = resp.content
        return 200, self._script, "text/javascript; charset=utf-8"

    def fetch_challenge(self) -> tuple[int, bytes, str]:
        pending = self._snapshot()
        if not pending.get("challenge_url"):
            return 404, b'{"error": "no pending robot check"}', "application/json"
        resp = request_with_retry(
            "GET", pending["challenge_url"], session=_shadow, timeout=20, verify=False,
            headers={"Referer": pending["page_url"], "Accept": "application/json"},
        )
        return resp.status_code, resp.content, resp.headers.get("Content-Type", "application/json")

    def submit_solution(self, body: bytes) -> tuple[int, bytes, str]:
        pending = self._snapshot()
        if not pending.get("solution_url"):
            return 404, b'{"success": false}', "application/json"
        page = urllib.parse.urlparse(pending["page_url"])
        resp = request_with_retry(
            "POST", pending["solution_url"], session=_shadow, data=body, timeout=20, verify=False,
            headers={
                "Content-Type": "application/json",
                "Referer": pending["page_url"],
                "Origin": f"{page.scheme}://{page.netloc}",
            },
        )
        try:
            data = resp.json()
            accepted = resp.ok and isinstance(data, dict) and bool(data.get("success"))
        except ValueError:
            accepted = False
        if accepted:
            logger.info("The user answered Sci-Hub's robot check.")
            self.solved.set()
        return resp.status_code, resp.content, resp.headers.get("Content-Type", "application/json")


class _RelayHandler(BaseHTTPRequestHandler):
    relay: RobotCheckRelay

    def log_message(self, format, *args):  # keep request logs out of stderr noise
        logger.debug("relay: " + format % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _route(self) -> str | None:
        prefix = f"/{self.relay.token}/"
        path = urllib.parse.urlparse(self.path).path
        return path[len(prefix):] if path.startswith(prefix) else None

    def do_GET(self):
        route = self._route()
        try:
            if route == "":
                return self._send(200, self.relay.render().encode("utf-8"), "text/html; charset=utf-8")
            if route == "altcha.js":
                return self._send(*self.relay.fetch_script())
            if route == "challenge":
                return self._send(*self.relay.fetch_challenge())
        except requests.RequestException as e:
            return self._send(502, f"Could not reach Sci-Hub: {e}".encode(), "text/plain; charset=utf-8")
        self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        if self._route() != "solution":
            return self._send(404, b"Not found", "text/plain")
        length = min(int(self.headers.get("Content-Length") or 0), 65536)
        try:
            self._send(*self.relay.submit_solution(self.rfile.read(length)))
        except requests.RequestException as e:
            self._send(502, json.dumps({"success": False, "error": str(e)}).encode(), "application/json")


_relay = RobotCheckRelay()


def ask_user_for_robot_check(doi: str, result: SciHubResult, wait: bool = True) -> tuple[bool, str | None]:
    """Puts the robot check in front of the user. Returns (answered, relay_url).

    On stdio (Āhara running on the user's machine) the page opens in their browser
    and this waits up to AHARA_ROBOT_WAIT seconds for them to tick the box.
    """
    page = result.page
    if not (page and page.challenge_url and page.solution_url and result.page_url):
        return False, None  # unfamiliar check layout: only the manual options apply
    _relay.arm(doi, result.page_url, page)
    try:
        _relay.start()
    except OSError as e:
        logger.warning(f"Could not start the robot-check page on {RELAY_HOST}:{RELAY_PORT}: {e}")
        return False, None
    url = _relay.url
    opened = False
    if TRANSPORT == "stdio":
        try:
            opened = webbrowser.open(url)
        except Exception as e:
            logger.debug(f"Could not open a browser: {e}")
    if not (wait and opened and ROBOT_WAIT_SECONDS > 0):
        return False, url
    logger.info(f"Waiting up to {ROBOT_WAIT_SECONDS}s for the user to answer Sci-Hub's robot check at {url}")
    return _relay.solved.wait(ROBOT_WAIT_SECONDS), url


# --- Fetch chain ------------------------------------------------------------------


@dataclass
class FetchOutcome:
    doi: str
    status: str = "not_found"  # "ok", "cached", "needs_user" or "not_found"
    path: str | None = None
    source: str | None = None
    attempts: list[str] = field(default_factory=list)
    scihub_page: str | None = None
    relay_url: str | None = None
    similar: list[str] = field(default_factory=list)


def _try_downloads(label: str, urls: list[str], dest: str, out: FetchOutcome, **kwargs) -> bool:
    for url in urls:
        ok, reason = download_pdf(url, dest, **kwargs)
        if ok:
            out.status, out.source = "ok", f"{label} ({_host(url)})"
            return True
        out.attempts.append(f"{label}: {_short(url)} → {reason}")
    return False


def fetch_paper(doi: str, output_dir: str | None = None, *, use_scihub: bool = True, wait: bool = True) -> FetchOutcome:
    """Walks the source chain (AHARA_SOURCES) until one yields a real PDF."""
    dest = paper_path(doi, output_dir)
    out = FetchOutcome(doi, path=dest)
    if os.path.exists(dest):
        if _is_pdf_file(dest):
            out.status, out.source = "cached", "already downloaded"
            return out
        logger.warning(f"Removing {dest}: it is not a PDF (left over from an older Āhara version?)")
        os.remove(dest)

    robot_pending = False
    for source in SOURCES:
        if source == "oa":
            urls = _dedupe(
                [arxiv_pdf_url(doi)]
                + unpaywall_pdf_candidates(get_unpaywall_record(doi))
                + openalex_pdf_candidates(get_openalex_work(doi))
            )
            if not urls:
                out.attempts.append("open access: no open-access copy listed")
            elif _try_downloads("open access", urls, dest, out):
                return out

        elif source == "openalex_content":
            url = openalex_content_url(get_openalex_work(doi))
            if url and _try_downloads("OpenAlex cache", [url], dest, out):
                return out
            if not url:
                out.attempts.append(
                    "OpenAlex cache: " + ("no cached PDF" if OPENALEX_API_KEY else "skipped (no AHARA_OPENALEX_API_KEY)")
                )

        elif source == "scinet":
            try:
                pdf_url, page_url = scinet_lookup(doi)
            except requests.RequestException as e:
                out.attempts.append(f"Sci-Net: {type(e).__name__}")
                continue
            if not pdf_url:
                out.attempts.append("Sci-Net: not uploaded there")
            elif _try_downloads("Sci-Net", [pdf_url], dest, out, session=_shadow, referer=page_url):
                return out

        elif source == "scihub":
            if not use_scihub:
                out.attempts.append("Sci-Hub: skipped (its robot check is still unanswered)")
                robot_pending = True
                continue
            result = scihub_lookup(doi)
            if result.kind == "robot":
                out.scihub_page = result.page_url
                answered, out.relay_url = ask_user_for_robot_check(doi, result, wait=wait)
                if answered:
                    result = scihub_lookup(doi)  # same session, now cleared
            if result.kind == "robot":
                out.attempts.append(f"Sci-Hub ({_host(result.page_url or '')}): robot check")
                robot_pending = True
            elif result.kind == "pdf" and result.pdf_url:
                if _try_downloads("Sci-Hub", [result.pdf_url], dest, out, session=_shadow, referer=result.page_url):
                    return out
            elif result.kind == "not_found":
                out.attempts.append(f"Sci-Hub ({_host(result.page_url or '')}): not in its database")
                out.scihub_page = result.page_url
                if result.page:
                    out.similar = result.page.similar
            else:
                detail = "; ".join(result.errors[:4]) or "no domains to try"
                out.attempts.append(f"Sci-Hub: no domain answered ({detail}). Try ahara_mirror_status(refresh=True).")

    if robot_pending:
        out.status = "needs_user"
    return out


def library_link(doi: str) -> str | None:
    """The DOI through the user's library proxy (EZproxy style), if configured."""
    if not LIBRARY_PROXY:
        return None
    target = f"https://doi.org/{doi}"
    if "{url}" in LIBRARY_PROXY:
        return LIBRARY_PROXY.replace("{url}", target)
    return LIBRARY_PROXY + target


def _manual_options(doi: str, scihub_page: str | None) -> str:
    """Links for getting the paper by hand. Only mentions sources enabled in AHARA_SOURCES."""
    lines = ["Manual options (download it yourself, then call ahara_import_pdf with this DOI):"]
    if "scihub" in SOURCES:
        scihub = scihub_page or (scihub_candidates() or list(SCIHUB_SEED_MIRRORS))[0] + _quote_doi(doi)
        lines.append(f"- Sci-Hub page: {scihub}")
    if "scinet" in SOURCES:
        lines.append(
            f"- Sci-Net: {SCINET_URL}{doi} (if it isn't there, it can be requested on sci-net.xyz;"
            " that needs a Sci-Net account and an SCI-token bounty)"
        )
    lines.append(f"- Publisher: https://doi.org/{doi}")
    proxied = library_link(doi)
    if proxied:
        lines.append(f"- Through your library: {proxied}")
    return "\n".join(lines)


def _robot_check_request(out: FetchOutcome) -> str:
    if out.relay_url:
        return (
            "ACTION NEEDED: Sci-Hub is asking whether you're a robot.\n"
            f"Ask the user to open {out.relay_url} and tick the box. That page passes their answer "
            "to Sci-Hub inside Āhara's own session. When they say it's done, retry the download."
        )
    return (
        "ACTION NEEDED: Sci-Hub is showing a robot check that Āhara can't relay. "
        "Ask the user to use one of the manual options below."
    )


# Create the MCP server
mcp = MCPServer(
    "Ahara",
    version=__version__,
    instructions=(
        "Academic paper fetcher. Downloads try legal open-access copies first (arXiv, Unpaywall, "
        "OpenAlex), then Sci-Net, then Sci-Hub. When a result starts with 'ACTION NEEDED', "
        "pass the request on to the user and wait for them before retrying. Fully standalone (does not "
        "require Gefyra or Vashira), but part of the broader Vashira (https://vashira.org/) research "
        "ecosystem. Concept & Développement par **\"Le Rafael\"** 😎 @ [Ram0nes.com](https://ram0nes.com)"
    ),
)


@mcp.tool()
def ahara_download_paper(doi: str, output_dir: str | None = None) -> str:
    """
    Downloads an academic paper by DOI. Tries legal open-access copies first
    (arXiv, Unpaywall, OpenAlex), then Sci-Net, then Sci-Hub.
    If Sci-Hub asks for a robot check, the result starts with ACTION NEEDED:
    relay it to the user, then call this again.

    Args:
        doi: The Digital Object Identifier (e.g., '10.1038/s41586-020-2649-2'); doi.org links work too.
        output_dir: Optional. Directory to save the PDF. Defaults to 'sci-hub-downloads'.

    Returns:
        A message with the path and extracted metadata.
    """
    doi = _require_doi(doi)
    out = fetch_paper(doi, output_dir)
    metadata = get_paper_metadata(doi)

    if out.status == "cached":
        return f"File already exists at: {out.path}\n\nMetadata:\n{_metadata_text(metadata)}"
    if out.status == "ok":
        return f"Successfully downloaded PDF from {out.source} to: {out.path}\n\nMetadata:\n{_metadata_text(metadata)}"

    attempts = "\n".join(f"- {a}" for a in out.attempts)
    if out.status == "needs_user":
        return f"{_robot_check_request(out)}\n\nAttempts:\n{attempts}\n\n{_manual_options(doi, out.scihub_page)}"

    message = f"Could not find a PDF for {doi}.\n\nAttempts:\n{attempts}\n\n{_manual_options(doi, out.scihub_page)}"
    if out.similar:
        message += "\n\nSci-Hub has these related papers: " + ", ".join(out.similar)
    raise Exception(message)


@mcp.tool()
def ahara_bulk_download(dois: list[str], output_dir: str | None = None) -> str:
    """
    Downloads multiple papers by DOI, using the same source chain as ahara_download_paper.
    If Sci-Hub asks for a robot check that isn't answered, Sci-Hub is skipped for the
    rest of the batch and those DOIs are listed at the end.

    Args:
        dois: A list of DOI strings.
        output_dir: Optional output directory.
    """
    lines, waiting, missing = [], [], []
    scihub_open, relay_url = True, None
    for i, raw in enumerate(dois):
        try:
            doi = _require_doi(raw)
        except ValueError as e:
            lines.append(f"{raw}: ❌ {e}")
            continue
        out = fetch_paper(doi, output_dir, use_scihub=scihub_open)
        if out.status in ("ok", "cached"):
            lines.append(f"{doi}: ✅ {out.source} → {out.path}")
        elif out.status == "needs_user":
            # The check went unanswered: stop knocking on Sci-Hub for the rest of the batch.
            scihub_open = False
            relay_url = out.relay_url or relay_url
            waiting.append(doi)
            lines.append(f"{doi}: ⏸ waiting on Sci-Hub's robot check")
        else:
            missing.append(doi)
            lines.append(f"{doi}: ❌ not found ({'; '.join(out.attempts[-2:])})")
        # Be polite to the services between papers.
        if i < len(dois) - 1:
            time.sleep(1)

    report = "\n".join(lines)
    if waiting:
        how = (
            f"Ask the user to open {relay_url} and tick the box, then"
            if relay_url
            else "Ask the user to answer Sci-Hub's robot check in their browser, then"
        )
        report += f"\n\nACTION NEEDED: Sci-Hub is asking whether you're a robot. {how} run ahara_bulk_download again for: {', '.join(waiting)}"
    if missing:
        report += (
            f"\n\nNot found anywhere: {', '.join(missing)}. Call ahara_download_paper on one to see the manual "
            "options (library, Sci-Net request), then file the PDF with ahara_import_pdf."
        )
    return report


@mcp.tool()
def ahara_import_pdf(doi: str, file_path: str | None = None, output_dir: str | None = None) -> str:
    """
    Files a PDF the user downloaded themselves (for example after a robot check, or
    through their library) into Āhara's download folder under its DOI.

    Args:
        doi: The paper's DOI.
        file_path: Optional. Path to the PDF. If omitted, uses the newest PDF in the user's
            Downloads folder from the last 15 minutes.
        output_dir: Optional. Folder to file it in. Defaults to 'sci-hub-downloads'.
    """
    doi = _require_doi(doi)
    if not file_path:
        file_path = _newest_download(DOWNLOADS_DIR, IMPORT_WINDOW_MINUTES)
        if not file_path:
            raise FileNotFoundError(
                f"No PDF was downloaded to {DOWNLOADS_DIR} in the last {IMPORT_WINDOW_MINUTES} minutes. "
                "Pass file_path explicitly."
            )
    file_path = os.path.expanduser(file_path)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Cannot find file at {file_path}")
    if not _is_pdf_file(file_path):
        raise ValueError(f"{file_path} is not a PDF.")

    dest = paper_path(doi, output_dir)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(file_path, dest)
    message = f"Imported {file_path}\n→ {dest}"
    if not _pdf_mentions(dest, doi):
        message += (
            f"\n\n⚠️ The DOI {doi} doesn't appear on the first pages of this PDF. "
            "Check with the user that it's the right paper."
        )
    return f"{message}\n\nMetadata:\n{_metadata_text(get_paper_metadata(doi))}"


def _newest_download(directory: str, minutes: int) -> str | None:
    cutoff = time.time() - minutes * 60
    newest, newest_mtime = None, cutoff
    try:
        for entry in os.scandir(directory):
            if entry.is_file() and entry.name.lower().endswith(".pdf"):
                mtime = entry.stat().st_mtime
                if mtime >= newest_mtime:
                    newest, newest_mtime = entry.path, mtime
    except OSError:
        return None
    return newest


def _pdf_mentions(path: str, doi: str, pages: int = 2) -> bool:
    try:
        with fitz.open(path) as doc:
            text = " ".join(doc.load_page(i).get_text("text") for i in range(min(pages, len(doc))))
    except Exception:
        return True  # can't tell, so don't warn
    return doi.lower() in text.lower()


@mcp.tool()
def ahara_search_papers(query: str, limit: int = 5) -> str:
    """
    Searches for academic papers (OpenAlex, falling back to CrossRef). Each result has
    the DOI, year, venue, open-access status, citation count and the start of the
    abstract, so you can choose what to download.

    Args:
        query: Search keywords (e.g., 'agricultural metrics El Ejido')
        limit: Max number of results (1-25).
    """
    limit = max(1, min(int(limit), 25))
    try:
        params = {
            "search": query,
            "per_page": limit,
            "select": "doi,display_name,publication_year,primary_location,open_access,"
            "cited_by_count,abstract_inverted_index,authorships,has_content",
        }
        if OPENALEX_API_KEY:
            params["api_key"] = OPENALEX_API_KEY
        resp = request_with_retry("GET", f"{OPENALEX_API}/works", params=params, timeout=20)
        resp.raise_for_status()
        works = resp.json().get("results", [])
        return "\n\n".join(_format_search_hit(i, w) for i, w in enumerate(works, 1)) or "No results found."
    except Exception as e:
        logger.warning(f"OpenAlex search failed for '{query}', falling back to CrossRef: {e}")
    try:
        cr = Crossref(mailto=AHARA_EMAIL)
        res = cr.works(query=query, limit=limit)
        results = []
        for item in res.get("message", {}).get("items", []):
            title = (item.get("title") or ["No Title"])[0]
            year = item.get("created", {}).get("date-parts", [[None]])[0][0]
            results.append(f"- **{title}** ({year})\n  DOI: {item.get('DOI', 'No DOI')}")
        return "\n".join(results) if results else "No results found."
    except Exception as e:
        logger.warning(f"CrossRef search failed for '{query}': {e}")
        return f"Search failed: {str(e)}"


def _format_search_hit(n: int, work: dict) -> str:
    doi = normalize_doi(work.get("doi") or "") or "no DOI"
    venue = ((work.get("primary_location") or {}).get("source") or {}).get("display_name") or "unknown venue"
    oa = work.get("open_access") or {}
    pdf_likely = oa.get("is_oa") or (work.get("has_content") or {}).get("pdf")
    access = f"open access ({oa.get('oa_status')})" if oa.get("is_oa") else "closed"
    authors = [a.get("author", {}).get("display_name") for a in (work.get("authorships") or [])]
    authors = [a for a in authors if a]
    byline = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
    abstract = rebuild_abstract(work.get("abstract_inverted_index"))
    if len(abstract) > 400:
        abstract = abstract[:399].rsplit(" ", 1)[0] + "…"
    lines = [
        f"{n}. **{work.get('display_name') or 'Untitled'}** ({work.get('publication_year')}), {venue}",
        f"   DOI: {doi} | {access}{' · PDF likely available' if pdf_likely else ''} | cited by {work.get('cited_by_count', 0)}",
    ]
    if byline:
        lines.append(f"   Authors: {byline}")
    if abstract:
        lines.append(f"   Abstract: {abstract}")
    return "\n".join(lines)


@mcp.tool()
def ahara_get_metadata(doi: str) -> str:
    """
    Fetches structured metadata (title, authors, journal, year) for a DOI without
    downloading the paper. Uses CrossRef, or OpenAlex for DOIs CrossRef doesn't have
    (such as arXiv's).

    Args:
        doi: The Digital Object Identifier.
    """
    doi = normalize_doi(doi)
    metadata = get_paper_metadata(doi)
    if not metadata:
        return f"No metadata found for DOI: {doi} (checked CrossRef and OpenAlex)"
    return _metadata_text(metadata)


def _format_bibtex(doi: str, metadata: dict) -> str:
    """Builds a BibTeX entry from CrossRef (or OpenAlex) metadata."""
    if not metadata:
        raise Exception(f"No metadata available for DOI: {doi} (checked CrossRef and OpenAlex)")

    authors = metadata.get("authors") or []
    author_field = " and ".join(a for a in authors if a) or "Unknown"
    first_author_key = (
        re.sub(r"[^a-zA-Z]", "", authors[0].split()[-1]) if authors and authors[0] else "unknown"
    )
    year = metadata.get("year") or "n.d."
    cite_key = f"{first_author_key}{year}"

    fields = {
        "title": metadata.get("title", "Unknown"),
        "author": author_field,
        "journal": metadata.get("journal", "Unknown"),
        "year": str(year),
        "publisher": metadata.get("publisher", "Unknown"),
        "doi": doi,
        "url": metadata.get("url", f"https://doi.org/{doi}"),
    }
    body = ",\n".join(f'  {k} = {{{v}}}' for k, v in fields.items())
    return f"@article{{{cite_key},\n{body}\n}}"


@mcp.tool()
def ahara_export_bibtex(doi: str) -> str:
    """
    Generates a BibTeX citation entry for a DOI using CrossRef (or OpenAlex) metadata.

    Args:
        doi: The Digital Object Identifier.
    """
    doi = normalize_doi(doi)
    return _format_bibtex(doi, get_paper_metadata(doi))


@mcp.tool()
def ahara_mirror_status(refresh: bool = False) -> str:
    """
    Checks which Sci-Hub domains answer right now, where they redirect (Sci-Hub's
    backend changes often), and whether Sci-Net is up. The domain that answers is
    remembered and tried first next time.

    Args:
        refresh: Also look up current Sci-Hub domains on Wikidata and Sci-Bot.
    """
    if refresh:
        discover_scihub_mirrors(force=True)
    lines = []
    for base in scihub_candidates():
        start = time.monotonic()
        try:
            resp = request_with_retry("GET", base, retries=0, session=_shadow, timeout=10, verify=False)
        except requests.RequestException as e:
            lines.append(f"❌ {_host(base)}: unreachable ({type(e).__name__})")
            continue
        latency = time.monotonic() - start
        final = _host(resp.url)
        hop = f" → {final}" if final != _host(base) else ""
        page = parse_scihub_page(resp.text, resp.url) if resp.status_code == 200 else None
        if page is None:
            lines.append(f"⚠️ {_host(base)}{hop}: HTTP {resp.status_code} ({latency:.1f}s)")
        elif page.kind == "unknown":
            lines.append(f"⚠️ {_host(base)}{hop}: answers, but not with Sci-Hub ({latency:.1f}s)")
        else:
            remember_scihub_host(resp.url)
            note = ", asking for a robot check" if page.kind == "robot" else ""
            lines.append(f"✅ {_host(base)}{hop}: online{note} ({latency:.1f}s)")
    try:
        resp = request_with_retry("GET", SCINET_URL, retries=0, session=_shadow, timeout=10)
        lines.append(f"{'✅' if resp.ok else '⚠️'} Sci-Net ({_host(SCINET_URL)}): HTTP {resp.status_code}")
    except requests.RequestException as e:
        lines.append(f"❌ Sci-Net: unreachable ({type(e).__name__})")
    state = _load_state()
    lines.append(f"\nTried first next time: {state.get('last_good') or 'nothing learned yet'}")
    if state.get("discovered_at"):
        lines.append(f"Last discovery run: {state['discovered_at']}")
    return "\n".join(lines)


@mcp.tool()
def ahara_read_paper(file_path: str, start_page: int = 1, end_page: int = 15) -> str:
    """
    Extracts text from a downloaded PDF so the AI can read it.
    Features OCR fallback for scanned images (requires Tesseract-OCR installed).

    Args:
        file_path: The absolute path to the local PDF file.
        start_page: The first page to read (1-indexed).
        end_page: The last page to read (1-indexed).

    Returns:
        The extracted text content of the paper.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Cannot find file at {file_path}")

    try:
        doc = fitz.open(file_path)
        text_content = f"--- START OF DOCUMENT: {os.path.basename(file_path)} ---\n"
        text_content += f"Total Pages: {len(doc)}\n\n"

        start_idx = max(0, start_page - 1)
        end_idx = min(len(doc), end_page)

        for i in range(start_idx, end_idx):
            page = doc.load_page(i)
            page_text = page.get_text("text").strip()

            # OCR Fallback for scanned pages
            if len(page_text) < 50:
                text_content += f"\n\n[PAGE {i+1}] (OCR Applied)\n"
                try:
                    pix = page.get_pixmap(dpi=150)
                    mode = "RGBA" if pix.alpha else "RGB"
                    img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
                    ocr_text = pytesseract.image_to_string(img)
                    text_content += ocr_text
                except Exception as ocr_err:
                    text_content += f"[OCR Failed: {str(ocr_err).rstrip('.')}. Is Tesseract installed?]"
            else:
                text_content += f"\n\n[PAGE {i+1}]\n"
                text_content += page_text

        doc.close()
        return text_content
    except Exception as e:
        return f"Error extracting text: {str(e)}"


@mcp.tool()
def ahara_extract_abstract(file_path: str) -> str:
    """
    Quickly extracts the abstract or first page of a paper to summarize it.

    Args:
        file_path: The absolute path to the local PDF file.

    Returns:
        The text of the first page.
    """
    return ahara_read_paper(file_path, start_page=1, end_page=1)


def main() -> None:
    """Entry point used both by `python server.py` and the packaged `ahara-mcp` console script."""
    logger.info(
        f"Starting Ahara MCP server v{__version__} ({TRANSPORT}); sources: {', '.join(SOURCES)}."
    )
    if TRANSPORT == "stdio":
        mcp.run(transport="stdio")
    elif TRANSPORT in ("streamable-http", "sse"):
        host = os.getenv("AHARA_HOST", "127.0.0.1")
        port = int(os.getenv("AHARA_PORT", "8000"))
        mcp.run(transport=TRANSPORT, host=host, port=port)
    else:
        raise ValueError(f"Unknown AHARA_TRANSPORT: {TRANSPORT}")


if __name__ == "__main__":
    main()
