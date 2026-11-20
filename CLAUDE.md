# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Āhara (`ahara-mcp`) is a single-file Python MCP server that fetches academic papers by DOI. All logic is in [server.py](server.py); it is the only module packaged into the wheel (see `[tool.hatch.build.targets.wheel]` in [pyproject.toml](pyproject.toml)). Built on `mcp==2.0.0`, using `mcp.server.mcpserver.MCPServer` (not the older `FastMCP` API).

## Commands

The project venv lives in `venv/` (Windows: `venv\Scripts\python.exe`). Āhara is registered with Claude Code at **user scope**, running from that venv: `claude mcp add --scope user ahara -- <venv python> <server.py>`. Check it with `claude mcp get ahara`. Don't also add it to the project `.mcp.json`; Claude Code warns about the same server defined in two scopes. **After changing dependencies, reinstall them into that venv** (`venv/Scripts/python -m pip install -r requirements.txt`). A stale venv makes the server crash on import, which Claude Code reports only as `CONNECTION_CLOSED`. Start a new session or use `/mcp` afterwards.

```bash
pip install -r requirements.txt          # runtime deps (keep in sync with pyproject.toml dependencies)
pip install pytest                       # or: pip install -e ".[dev]"
python -m pytest                         # offline tests (tests/, HTML fixtures in tests/fixtures/)
python -m pytest tests/test_parsers.py::test_scihub_robot_page   # a single test
python server.py                         # run server (stdio by default); also `ahara-mcp` when pip-installed
AHARA_TRANSPORT=streamable-http python server.py   # HTTP on AHARA_HOST:AHARA_PORT (default 127.0.0.1:8000); `sse` also supported
python test.py <DOI> [<DOI>...]          # live smoke test: what each source says about a DOI (no downloads, no browser)
python test.py                           # live Sci-Hub domain + Sci-Net status
python -m build                          # builds dist/ wheel + sdist (hatchling)
docker compose up --build                # streamable-http on :8000, robot-check page on 127.0.0.1:8765
```

When running live experiments, set `AHARA_TRANSPORT=streamable-http` to stop `webbrowser.open` from popping the robot-check page on the user's screen. Pass an `output_dir` to keep test downloads out of `sci-hub-downloads/`.

## Configuration

Read from `.env` next to `server.py` (loaded by absolute path, so cwd doesn't matter) into module-level constants at import time. See [.env.example](.env.example) and the README for the full list. The ones that change behavior most:
- `AHARA_SOURCES`: ordered subset of `oa,openalex_content,scinet,scihub`
- `AHARA_OPENALEX_API_KEY`: enables the `openalex_content` source
- `AHARA_ROBOT_WAIT`, `AHARA_RELAY_HOST`/`AHARA_RELAY_PORT`: robot-check relay
- `AHARA_STATE_DIR` (default `~/.ahara`): where `mirrors.json` lives

Tesseract-OCR must be installed on the system for the OCR fallback in `ahara_read_paper`.

## Architecture notes (server.py)

- **stdout is reserved for JSON-RPC** under stdio transport. All diagnostics go through the `logger` configured to stderr; never `print()` in server code.
- **Global DNS-over-HTTPS monkeypatch:** at import time `socket.getaddrinfo` is replaced so any hostname containing `sci-hub` is resolved via Cloudflare DoH (cached by TTL). Many ISPs return `::1` for Sci-Hub domains, so this is load-bearing. It affects every importer of `server.py`, including tests.
- **Fetch chain** (`fetch_paper`, used by `ahara_download_paper` and `ahara_bulk_download`): validated local file → sources in `SOURCES` order → `FetchOutcome` with status `ok`/`cached`/`needs_user`/`not_found` and an attempt log. Every download goes through `download_pdf`. It sniffs `%PDF-` in the first KB and writes via temp file + `os.replace`, so non-PDF responses are never saved. Results that need the user start with `ACTION NEEDED`, which the server `instructions` tell the client LLM to relay.
- **Two `requests` sessions:** `_shadow` (Sci-Hub + Sci-Net) must stay shared. Sci-Hub's robot-check clearance and DDoS-Guard cookies live in it, and its `/storage/…pdf` links only work inside it. `_web` is for open-access downloads. TLS verification is off only for `sci-hub` hosts.
- **Sci-Hub domains churn constantly.** Entry domains (`SCIHUB_SEED_MIRRORS`, Wikidata Q21980377) redirect to a single backend that changes often (`sci-hub.fr` in Nov 2026). `scihub_lookup` tries domains in this order: `order_mirror_candidates`, overrides, last good, recently seen, seeds, discovered. It stops at the first real Sci-Hub answer (`pdf`/`robot`/`not_found`); only network errors and non-Sci-Hub pages move on to the next domain. `remember_scihub_host` persists the final redirect target to `mirrors.json`. `discover_scihub_mirrors` (Wikidata P856 + links on sci-bot.ru) runs when nothing answers, or on `ahara_mirror_status(refresh=True)`.
- **Parsers are pure functions** (`parse_scihub_page`, `parse_scinet_page`, `parse_wikidata_sites`, `rebuild_abstract`, the `*_candidates` helpers) tested against real captured HTML in `tests/fixtures/`. Sci-Hub article pages also contain an `<altcha-widget>` (inside the hidden "report a problem" `<dialog>`). A robot check is only an ALTCHA widget *outside* a dialog with no PDF link. Never go back to substring matching on "captcha". When a layout changes, recapture the fixture first.
- **Robot-check relay** (`RobotCheckRelay`, `_RelayHandler`): a `ThreadingHTTPServer` on `RELAY_HOST:RELAY_PORT`, behind a random URL token. It serves a page with Sci-Hub's ALTCHA widget, and proxies the widget script, the challenge and the user's solution through `_shadow`. The user must tick the box themselves. Don't add `auto=` verification or solve the proof-of-work in code. On stdio, `ask_user_for_robot_check` opens the page and blocks up to `AHARA_ROBOT_WAIT`. Sync tools run in a worker thread (`anyio.to_thread`), so this doesn't stall the server.
- PubMed Central (bot-check page) and Europe PMC (HTTP 403) block scripted PDF downloads, even for open-access papers (checked Nov 2026). Don't add PMCID-derived PDF links back; OpenAlex's cached copy (`openalex_content`) is the scriptable route for those papers.
- Sci-Net (`sci-net.xyz/<DOI>`) is used anonymously and read-only. Unknown DOIs redirect to its homepage. Creating requests needs an account and SCI tokens; don't automate that.
- Metadata: `get_paper_metadata` = CrossRef, falling back to OpenAlex (needed for DataCite DOIs such as arXiv's). Successful lookups are cached per process in dicts; failures are not cached.
- Tools are plain sync functions decorated with `@mcp.tool()`, all prefixed `ahara_`. Their docstrings are the tool descriptions shown to the LLM. PDF reading uses PyMuPDF and OCRs any page with <50 chars of extractable text.
- `__version__` in `server.py` and `version` in `pyproject.toml` must be bumped together.
