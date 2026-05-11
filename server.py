import os
import re
import urllib.parse
import tempfile
import requests
import urllib3
import socket
import webbrowser
import json
from datetime import datetime
from bs4 import BeautifulSoup
import fitz  # PyMuPDF
from PIL import Image
import pytesseract
from habanero import CrossRef
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

# Load environment variables (e.g., AHARA_EMAIL for CrossRef/Unpaywall)
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(env_path)
AHARA_EMAIL = os.getenv("AHARA_EMAIL", "ahara-mcp@example.com")

# Disable insecure request warnings since Sci-Hub certs can sometimes be weird
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# DNS over HTTPS (DoH) patch to bypass ISP blocking
_orig_getaddrinfo = socket.getaddrinfo

def _doh_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if "sci-hub" in host:
        try:
            resp = requests.get(f"https://cloudflare-dns.com/dns-query?name={host}&type=A", headers={"accept": "application/dns-json"}, timeout=5, verify=False)
            if resp.status_code == 200:
                data = resp.json()
                if "Answer" in data:
                    ip = data["Answer"][0]["data"]
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]
        except Exception:
            pass # Fall back to original
    return _orig_getaddrinfo(host, port, family, type, proto, flags)

socket.getaddrinfo = _doh_getaddrinfo

# Create the MCP server
mcp = FastMCP(
    "Ahara",
    description="Academic paper fetcher with Sci-Hub, CrossRef, and Unpaywall integration. Part of the Vashira (https://vashira.org/) research ecosystem. Site web concept par **\"Le Rafael\"** 😎 @ [Ram0nes.com](https://ram0nes.com)"
)

# Known Sci-Hub domains
SCI_HUB_URLS = [
    "https://sci-hub.se/",
    "https://sci-hub.st/",
    "https://sci-hub.ru/",
    "https://sci-hub.it/",
    "https://sci-hub.ee/",
    "https://sci-hub.shop/"
]

def get_crossref_metadata(doi: str) -> dict:
    """Fetches metadata for a DOI from CrossRef."""
    try:
        cr = CrossRef(mailto=AHARA_EMAIL)
        res = cr.works(ids=doi)
        if res and 'message' in res:
            msg = res['message']
            return {
                "title": msg.get("title", ["Unknown"])[0],
                "authors": [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in msg.get("author", [])],
                "publisher": msg.get("publisher", "Unknown"),
                "journal": msg.get("container-title", ["Unknown"])[0],
                "year": msg.get("published-print", msg.get("published-online", {"date-parts": [[None]]}))["date-parts"][0][0],
                "doi": doi,
                "url": msg.get("URL", f"https://doi.org/{doi}")
            }
    except Exception:
        pass
    return {}

def get_unpaywall_pdf_url(doi: str) -> str:
    """Attempts to find a legal Open Access PDF via Unpaywall."""
    try:
        email = AHARA_EMAIL
        url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("is_oa"):
                oa_locations = data.get("oa_locations", [])
                for loc in oa_locations:
                    if loc.get("url_for_pdf"):
                        return loc.get("url_for_pdf")
    except Exception:
        pass
    return None

def get_scihub_pdf_url(doi: str) -> str:
    """Attempts to find the direct PDF link on Sci-Hub for a given DOI."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    errors = []
    
    for base_url in SCI_HUB_URLS:
        try:
            search_url = urllib.parse.urljoin(base_url, doi)
            response = requests.get(search_url, headers=headers, timeout=15, verify=False)
            
            # Check for direct PDF response
            if response.headers.get('Content-Type') == 'application/pdf':
                return search_url
            
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # Check for Captcha
                if 'captcha' in response.text.lower() or soup.find(id='captcha'):
                    webbrowser.open(search_url)
                    raise Exception(f"Captcha detected on {base_url}. The page has been opened in your browser. Please solve the captcha and try the AI request again.")
                
                # Sci-Hub puts the PDF in an iframe, embed, or specific button
                pdf_element = soup.find('iframe', id='pdf') or soup.find('embed', id='pdf')
                if not pdf_element:
                    # Look for save button
                    pdf_element = soup.find('a', onclick=lambda x: x and 'location.href' in x)
                
                if pdf_element:
                    src = pdf_element.get('src') or pdf_element.get('href')
                    if not src or src == '#':
                        onclick_attr = pdf_element.get('onclick', '')
                        match = re.search(r"location\.href='([^']+)'", onclick_attr)
                        if match:
                            src = match.group(1)
                            
                    if src:
                        # Fix protocol relative URLs
                        if src.startswith('//'):
                            src = 'https:' + src
                        elif src.startswith('/'):
                            src = urllib.parse.urljoin(base_url, src)
                        return src
                        
        except requests.exceptions.ConnectionError as e:
            errors.append(f"{base_url}: Connection Error (ISP DNS Block likely bypassed by DoH but network still failed)")
            continue
        except Exception as e:
            if "Captcha detected" in str(e):
                raise
            errors.append(f"{base_url}: {str(e)}")
            continue
            
    error_msg = "\n".join(errors)
    raise Exception(f"Could not find PDF for DOI: {doi} on any Sci-Hub mirror.\nErrors encountered:\n{error_msg}")

@mcp.tool()
def ahara_download_paper(doi: str, output_dir: str = None) -> str:
    """
    Downloads an academic paper using its DOI.
    Tries Unpaywall (Open Access) first, then falls back to Sci-Hub.
    
    Args:
        doi: The Digital Object Identifier (e.g., '10.1038/s41586-020-2649-2')
        output_dir: Optional. Directory to save the PDF. Defaults to 'sci-hub-downloads'.
        
    Returns:
        A message with the path and extracted metadata.
    """
    if not output_dir:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(base_dir, "sci-hub-downloads")
        
    os.makedirs(output_dir, exist_ok=True)
    
    # Sanitize DOI for filename
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', doi) + ".pdf"
    file_path = os.path.join(output_dir, safe_name)
    
    metadata = get_crossref_metadata(doi)
    metadata_str = json.dumps(metadata, indent=2) if metadata else "No metadata found."
    
    if os.path.exists(file_path):
        return f"File already exists at: {file_path}\n\nMetadata:\n{metadata_str}"
    
    # 1. Try Unpaywall
    pdf_url = get_unpaywall_pdf_url(doi)
    source = "Unpaywall (Open Access)"
    
    # 2. Try Sci-Hub
    if not pdf_url:
        pdf_url = get_scihub_pdf_url(doi)
        source = "Sci-Hub"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    # Download the actual PDF
    response = requests.get(pdf_url, headers=headers, stream=True, verify=False, timeout=30)
    if response.status_code == 200:
        with open(file_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return f"Successfully downloaded PDF from {source} to: {file_path}\n\nMetadata:\n{metadata_str}"
    else:
        raise Exception(f"Failed to download PDF from {pdf_url}. Status code: {response.status_code}")

@mcp.tool()
def ahara_search_papers(query: str, limit: int = 5) -> str:
    """
    Searches for academic papers via CrossRef and returns their DOIs and titles.
    
    Args:
        query: Search keywords (e.g., 'agricultural metrics El Ejido')
        limit: Max number of results.
    """
    try:
        cr = CrossRef(mailto=AHARA_EMAIL)
        res = cr.works(query=query, limit=limit)
        items = res.get('message', {}).get('items', [])
        
        results = []
        for item in items:
            title = item.get('title', ['No Title'])[0]
            doi = item.get('DOI', 'No DOI')
            year = item.get('created', {}).get('date-parts', [[None]])[0][0]
            results.append(f"- **{title}** ({year})\n  DOI: {doi}")
            
        return "\n".join(results) if results else "No results found."
    except Exception as e:
        return f"Search failed: {str(e)}"

@mcp.tool()
def ahara_mirror_status() -> str:
    """Checks the availability of known Sci-Hub mirrors."""
    results = []
    for url in SCI_HUB_URLS:
        try:
            start = datetime.now()
            resp = requests.head(url, timeout=5, verify=False)
            latency = (datetime.now() - start).total_seconds()
            status = "✅ ONLINE" if resp.status_code < 400 else f"⚠️ ERROR ({resp.status_code})"
            results.append(f"{url}: {status} ({latency:.2f}s)")
        except Exception:
            results.append(f"{url}: ❌ OFFLINE")
            
    return "\n".join(results)

@mcp.tool()
def ahara_bulk_download(dois: list[str], output_dir: str = None) -> str:
    """
    Downloads multiple papers in bulk.
    
    Args:
        dois: A list of DOI strings.
        output_dir: Optional output directory.
    """
    results = []
    for doi in dois:
        try:
            msg = ahara_download_paper(doi, output_dir)
            results.append(f"DOI {doi}: SUCCESS\n{msg}")
        except Exception as e:
            results.append(f"DOI {doi}: FAILED - {str(e)}")
            
    return "\n\n---\n\n".join(results)

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
                    text_content += f"[OCR Failed: {str(ocr_err)}. Is Tesseract installed?]"
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

if __name__ == "__main__":
    # Run the server using stdio
    mcp.run()
