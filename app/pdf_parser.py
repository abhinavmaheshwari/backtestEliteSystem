import requests
import tempfile
import os
import logging
from PyPDF2 import PdfReader

logger = logging.getLogger(__name__)

def extract_text_from_nse_pdf(pdf_url: str) -> str:
    """
    Downloads a PDF from NSE archives into memory/temp file, 
    extracts the text using PyPDF2, and returns it.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Referer': 'https://www.nseindia.com/'
    }
    
    try:
        s = requests.Session()
        s.get('https://www.nseindia.com', headers=headers, timeout=5)
        
        response = s.get(pdf_url, headers=headers, stream=True, timeout=15)
        response.raise_for_status()
        
        # Write to a temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    tmp_file.write(chunk)
            tmp_path = tmp_file.name
            
        # Parse PDF
        text = ""
        try:
            reader = PdfReader(tmp_path)
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        finally:
            os.remove(tmp_path)
            
        return text.strip()
    except Exception as e:
        logger.error(f"Failed to extract text from {pdf_url}: {e}")
        return ""

if __name__ == "__main__":
    # Test
    url = "https://nsearchives.nseindia.com/corporate/PARAS_10062026152832_InvestormeetKotakLondon.pdf"
    print("Testing PDF Extractor...")
    text = extract_text_from_nse_pdf(url)
    print(f"Extracted {len(text)} characters.")
    print(text[:500])
