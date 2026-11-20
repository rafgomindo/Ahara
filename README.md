# Āhara (आहर)

**Āhara** (Sanskrit for "the Bringer" or "Fetcher") is an MCP (Model Context Protocol) server designed to act as a bridge between AI assistants and the scholarly literature: open-access repositories, Sci-Net and Sci-Hub. In computational terms, it maps perfectly to a "GET" request or a fetch operation. It acts as the primary data-fetcher, retrieving full-text academic papers, abstracts, and metadata based on DOI (Digital Object Identifier) or URL. 

Retrieval-focused and great for a RAG-style tool, it facilitates Retrieval-Augmented Generation by indexing retrieved papers, allowing the LLM to perform evidence-based reasoning rather than relying on its internal (and potentially outdated) training data. Beyond the PDF itself, it parses citations, publication dates, and author affiliations to provide the LLM with a structured "identity" for every document it fetches. Specifically for your doctoral research, it automates the "investigator" phase—scanning for agricultural metrics, labor history, and economic data within the El Ejido dataset.

## Features

- **Open Access First:** Tries legal copies before anything else: arXiv, every location **Unpaywall** lists, **OpenAlex**, and optionally OpenAlex's own cached PDFs. The cached PDFs help with PubMed Central papers, because PMC and Europe PMC block scripted downloads.
- **Sci-Net:** Fetches papers uploaded to [Sci-Net](https://sci-net.xyz/), Sci-Hub's community platform. It covers many recent papers that Sci-Hub's main archive (which mostly stops around 2021–22) doesn't have.
- **Sci-Hub that keeps up with domain changes:** Sci-Hub's domains change often. Āhara follows redirects to whichever backend currently answers, remembers it, and rediscovers current domains from Wikidata and Sci-Bot when the known ones stop working.
- **You answer the robot check:** When Sci-Hub asks "are you a robot?", Āhara opens a small local page where you tick the box yourself. See [Robot checks](#robot-checks).
- **Only real PDFs are saved:** Every download is checked for a PDF signature, so error and captcha pages never end up in your library.
- **Import what you downloaded yourself:** `ahara_import_pdf` files a PDF you got through your browser or your library under its DOI.
- **Research-friendly search:** `ahara_search_papers` uses OpenAlex and returns open-access status, citation counts and the start of each abstract (CrossRef as fallback).
- **Rich Metadata:** Structured paper identities (Title, Authors, Journal, Year) via `ahara_get_metadata`, from CrossRef or, for DOIs CrossRef doesn't know (such as arXiv's), OpenAlex.
- **BibTeX Export:** Generate a citation entry for any DOI with `ahara_export_bibtex`.
- **DoH (DNS over HTTPS):** Automatically bypasses ISP-level DNS blocking of Sci-Hub domains using Cloudflare's DoH API.
- **Mirror Health Monitor:** `ahara_mirror_status` shows which Sci-Hub domains answer, where they redirect, and whether Sci-Net is up.
- **Bulk Downloading:** Retrieve many papers at once (with a polite delay between requests).
- **Automatic Retries:** Transient network failures are retried with backoff.
- **Advanced PDF Reading:** Supports paginated reading (`start_page` / `end_page`) to manage token context efficiently.
- **OCR Fallback:** Uses `pytesseract` to automatically perform OCR on scanned PDFs.

## How a download works

`ahara_download_paper` tries each source in turn and stops at the first real PDF:

1. **Open access** (`oa`): arXiv, Unpaywall, OpenAlex.
2. **OpenAlex cache** (`openalex_content`): OpenAlex's stored copy of open-access papers. Needs a free `AHARA_OPENALEX_API_KEY`; costs $0.01 per PDF, and the free daily allowance covers about 100.
3. **Sci-Net** (`scinet`): papers the Sci-Net community has uploaded. Read-only; Āhara never posts requests or spends tokens.
4. **Sci-Hub** (`scihub`): through whichever domain currently answers.

If nothing works, the result lists the manual options: the Sci-Hub and Sci-Net pages, the publisher link, and your library proxy if you set one. Download the paper yourself, then ask for it to be imported with `ahara_import_pdf`.

Set `AHARA_SOURCES` to change the order or leave sources out. For example, `AHARA_SOURCES=oa,openalex_content` only uses legal open-access sources.

## Robot checks

Sci-Hub sometimes answers with "are you a robot?" (an [ALTCHA](https://altcha.org/) check). Its clearance belongs to the session that answered it, so solving it in your own browser doesn't help Āhara.

Instead, Āhara opens a small page on `localhost` with the same check. You tick the box; Āhara only passes your answer on to Sci-Hub, so the check clears for the session that downloads the paper. Āhara never answers the check by itself.

- When Āhara runs on your machine (stdio), the page opens in your browser and the download waits up to `AHARA_ROBOT_WAIT` seconds (default 90) for you.
- Otherwise the result starts with **ACTION NEEDED** and includes the link; your assistant asks you to open it, then retries.
- In Docker, the page is served on port 8765 (see `docker-compose.yml`).

## About Sci-Bot

[Sci-Bot](https://sci-bot.ru/) is Sci-Hub's AI assistant (launched April 2026). It answers questions from Sci-Hub's collection and cites the papers by DOI, but it doesn't deliver PDFs. Its API is only available to logged-in users and isn't publicly documented, so Āhara doesn't use it yet. Āhara does read the Sci-Hub domains Sci-Bot links to when looking for working mirrors.

## The Ecosystem: Gefyra & Vashira

Āhara is a fully **standalone** tool and does not require any other software to function. However, it is designed to be a "fetcher" in a broader AI academic ecosystem. 
Once Āhara downloads a paper (defaulting to the `sci-hub-downloads` folder), it can be used alongside:

- **[Gefyra](https://github.com/rafgomindo/Gefyra):** A Zotero MCP server. Gefyra can take the files downloaded by Āhara and automatically organize them into your Zotero library, managing references and citations.
- **[Vashira](https://vashira.org/):** A comprehensive research management system. If you prefer alternatives to Zotero, Vashira can handle your sources and libraries for your thesis and research.

---
Concept & Développement par **"Le Rafael"** 😎 @ [Ram0nes.com](https://ram0nes.com) .

## Prerequisites

1. **Python 3.10+**
2. **Tesseract-OCR:** Required for the OCR fallback feature on scanned PDFs.
   - *Windows:* Download from [UB-Mannheim](https://github.com/UB-Mannheim/tesseract/wiki).
   - *macOS:* `brew install tesseract`
   - *Linux:* `sudo apt install tesseract-ocr`

## Installation

The quickest way is to install it with pip, which gives you an `ahara-mcp` command:

```bash
pip install git+https://github.com/rafgomindo/Ahara.git
```

Or clone the repository and install the Python dependencies:

```bash
git clone https://github.com/rafgomindo/Ahara.git
cd Ahara
pip install -r requirements.txt
```

## Configuration

Create a `.env` file in the root directory and add your email to enable CrossRef and Unpaywall features. If you installed Āhara with pip, put it in `~/.ahara/.env` instead.

```env
AHARA_EMAIL=your.email@example.com
```

Optional settings:

```env
# Sources to try, in order. Default: oa,openalex_content,scinet,scihub
AHARA_SOURCES=oa,openalex_content,scinet,scihub

# Free key from openalex.org/settings/api. Enables OpenAlex's cached PDFs.
AHARA_OPENALEX_API_KEY=

# Your library's proxy prefix, used for a manual link when nothing else works.
# Either a prefix (the DOI link is appended) or a template containing {url}.
AHARA_LIBRARY_PROXY=https://login.ezproxy.example.edu/login?url=

# Sci-Hub domains to try before the learned and built-in ones (comma-separated).
AHARA_SCIHUB_MIRRORS=https://sci-hub.ru/

# Seconds to wait for you to answer a Sci-Hub robot check (0 = don't wait).
AHARA_ROBOT_WAIT=90

# Where the robot-check page listens. Port 0 picks a free port.
AHARA_RELAY_HOST=127.0.0.1
AHARA_RELAY_PORT=0

# Where papers are saved. Default: sci-hub-downloads/ in the repo, or ~/sci-hub-downloads for pip installs.
AHARA_OUTPUT_DIR=~/sci-hub-downloads

# Where Āhara remembers working Sci-Hub domains. Default: ~/.ahara
AHARA_STATE_DIR=~/.ahara

# Folder ahara_import_pdf checks when no file path is given. Default: ~/Downloads
AHARA_DOWNLOADS_DIR=~/Downloads

# Logging verbosity (DEBUG, INFO, WARNING, ERROR). Defaults to INFO. Always written to stderr.
AHARA_LOG_LEVEL=INFO
```

## Running the Server

To start the MCP server, run:

```bash
python server.py
```

If you installed the package with pip, the `ahara-mcp` command does the same. Connect it to your preferred MCP client following their respective documentation. If you change dependencies, reinstall them into the environment your MCP client launches (for example, the project's `venv`).

To run it over HTTP instead, set `AHARA_TRANSPORT=streamable-http` (or `sse`), plus `AHARA_HOST` and `AHARA_PORT`. `docker compose up --build` does this on port 8000.

## Add it to Claude

**Claude Code** (terminal or IDE extension). Register Āhara once for all your projects:

```bash
claude mcp add --scope user ahara -- /path/to/Ahara/venv/bin/python /path/to/Ahara/server.py
```

On Windows, the Python path is `C:\path\to\Ahara\venv\Scripts\python.exe`. If you installed with pip, it's simply `claude mcp add --scope user ahara -- ahara-mcp`. Check it with `claude mcp get ahara`, which should report `Connected`. New Claude Code sessions then have the `ahara_*` tools; `/mcp` shows their status.

If `claude` isn't on your PATH because you only use an IDE extension, the extension ships the same program in its `resources/native-binary` folder.

**Claude Desktop**. Open Settings → Developer → Edit Config, add the server, and restart the app:

```json
{
  "mcpServers": {
    "ahara": {
      "command": "/path/to/Ahara/venv/bin/python",
      "args": ["/path/to/Ahara/server.py"]
    }
  }
}
```

**claude.ai (web and mobile)**. Connectors there must be remote servers reachable over HTTPS. Āhara is meant to run on your own computer: it saves PDFs there, and its robot-check page is local. Use Claude Code or Claude Desktop instead.

## Development

```bash
pip install -e ".[dev]"
python -m pytest            # offline tests against saved Sci-Hub and Sci-Net pages
python test.py <DOI>        # live check of what each source says about a DOI
python test.py              # live check of Sci-Hub domains and Sci-Net
```

When Sci-Hub or Sci-Net change their page layout, save a fresh copy of the page in `tests/fixtures/` and adjust `parse_scihub_page` or `parse_scinet_page` until the tests pass again.
