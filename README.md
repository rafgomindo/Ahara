# Āhara (आहर)

**Āhara** (Sanskrit for "the Bringer" or "Fetcher") is an MCP (Model Context Protocol) server designed to act as a bridge between AI assistants and Sci-Hub. In computational terms, it maps perfectly to a "GET" request or a fetch operation. It acts as the primary data-fetcher, retrieving full-text academic papers, abstracts, and metadata from the Sci-Hub repository based on DOI (Digital Object Identifier) or URL. 

Retrieval-focused and great for a RAG-style tool, it facilitates Retrieval-Augmented Generation by indexing retrieved papers, allowing the LLM to perform evidence-based reasoning rather than relying on its internal (and potentially outdated) training data. Beyond the PDF itself, it parses citations, publication dates, and author affiliations to provide the LLM with a structured "identity" for every document it fetches. Specifically for your doctoral research, it automates the "investigator" phase—scanning for agricultural metrics, labor history, and economic data within the El Ejido dataset.

## Features

- **Robust Scraping:** Intelligently parses various Sci-Hub mirror HTML structures, locating direct PDF links.
- **Open Access First:** Integrates with the **Unpaywall API** to prioritize legal, fast, and captcha-free downloads.
- **Rich Metadata:** Uses the **CrossRef API** to fetch structured paper identities (Title, Authors, Journal, Year).
- **Search Capability:** Find DOIs and papers using keywords/titles with the `ahara_search_papers` tool.
- **DoH (DNS over HTTPS):** Automatically bypasses ISP-level DNS blocking of Sci-Hub domains using Cloudflare's DoH API.
- **Captcha Handling:** Detects when Sci-Hub serves a captcha, pausing the AI and opening the page in your browser.
- **Mirror Health Monitor:** A tool to check which Sci-Hub mirrors are currently active.
- **Bulk Downloading:** Automate the retrieval of multiple papers at once.
- **Advanced PDF Reading:** Supports paginated reading (`start_page` / `end_page`) to manage token context efficiently.
- **OCR Fallback:** Uses `pytesseract` to automatically perform OCR on scanned PDFs.

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

Clone the repository and install the Python dependencies:

```bash
git clone https://github.com/rafgomindo/Ahara.git
cd Ahara
pip install -r requirements.txt
```

## Configuration

Create a `.env` file in the root directory and add your email to enable CrossRef and Unpaywall features:

```env
AHARA_EMAIL=your.email@example.com
```

## Running the Server

To start the MCP server, run:

```bash
mcp run server.py
```

Or connect it to your preferred MCP client following their respective documentation.
