import sys
import requests
import urllib3
import socket
from bs4 import BeautifulSoup
import urllib.parse
import re
import json

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

def test_crossref(doi):
    print(f"\n[CrossRef] Fetching metadata for {doi}...")
    try:
        resp = requests.get(f"https://api.crossref.org/works/{doi}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            title = data['message'].get('title', ['Unknown'])[0]
            print(f"Title: {title}")
            return True
    except Exception as e:
        print(f"CrossRef Error: {e}")
    return False

def test_unpaywall(doi):
    print(f"\n[Unpaywall] Checking Open Access for {doi}...")
    try:
        import os
        from dotenv import load_dotenv
        load_dotenv()
        email = os.getenv("AHARA_EMAIL", "ahara-mcp@example.com")
        
        resp = requests.get(f"https://api.unpaywall.org/v2/{doi}?email={email}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            is_oa = data.get('is_oa', False)
            print(f"Is Open Access: {is_oa}")
            if is_oa:
                pdf_url = data.get('best_oa_location', {}).get('url_for_pdf')
                print(f"Best PDF URL: {pdf_url}")
            return True
    except Exception as e:
        print(f"Unpaywall Error: {e}")
    return False

def test_scihub_mirrors():
    mirrors = ["https://sci-hub.se/", "https://sci-hub.st/", "https://sci-hub.ru/", "https://sci-hub.it/", "https://sci-hub.shop/"]
    print("\n[Sci-Hub] Checking mirrors...")
    for m in mirrors:
        try:
            resp = requests.head(m, timeout=5, verify=False)
            print(f"{m}: {'ONLINE' if resp.status_code < 400 else f'ERROR ({resp.status_code})'}")
        except Exception:
            print(f"{m}: OFFLINE")

if len(sys.argv) > 1:
    doi = sys.argv[1]
    print(f"=== TESTING DOI: {doi} ===")
    
    test_crossref(doi)
    test_unpaywall(doi)
    test_scihub_mirrors()
    
    print(f"\n[Sci-Hub Scraper] Trying mirrors for {doi}...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    for base_url in ["https://sci-hub.se/", "https://sci-hub.st/", "https://sci-hub.ru/"]:
        print(f"Trying {base_url}...")
        try:
            search_url = urllib.parse.urljoin(base_url, doi)
            response = requests.get(search_url, headers=headers, timeout=10, verify=False)
            print(f"Status code: {response.status_code}")
            
            if response.headers.get('Content-Type') == 'application/pdf':
                print("Found direct PDF response.")
                continue

            if response.status_code == 200:
                if 'captcha' in response.text.lower() or BeautifulSoup(response.text, 'html.parser').find(id='captcha'):
                    print("Captcha detected.")
                    continue
                    
                soup = BeautifulSoup(response.text, 'html.parser')
                pdf_element = soup.find('iframe', id='pdf') or soup.find('embed', id='pdf')
                if not pdf_element:
                    pdf_element = soup.find('a', onclick=lambda x: x and 'location.href' in x)
                    
                if pdf_element:
                    src = pdf_element.get('src') or pdf_element.get('href')
                    print(f"Found PDF URL: {src}")
                else:
                    print(f"No PDF element found.")
            
        except Exception as e:
            print(f"Error: {e}")
else:
    print("Usage: python test.py <DOI>")
    test_scihub_mirrors()
