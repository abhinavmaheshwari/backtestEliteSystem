import re

filename = "app/dashboard_server.py"
with open(filename, 'r') as f:
    content = f.read()

ai_endpoint = """
@app.route("/api/concall_ai/<symbol>")
def api_concall_ai(symbol):
    \"\"\"Fetches the latest Concall transcript from NSE, parses the PDF, and uses AI to summarize it.\"\"\"
    yf_symbol = symbol.replace('.NS', '')
    url = f"https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={yf_symbol}"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*'
    }
    
    try:
        import requests
        s = requests.Session()
        s.get('https://www.nseindia.com', headers=headers, timeout=5)
        r = s.get(url, headers=headers, timeout=5)
        
        if r.status_code != 200:
            return jsonify({"error": "Failed to fetch NSE announcements."}), 500
            
        data = r.json()
        target_pdf = None
        for n in data:
            desc = str(n.get("desc", ""))
            # Look for Concall transcripts, Investor Meets, or Earnings Presentations
            if "Con. Call" in desc or "Investor Meet" in desc or "Transcript" in desc or "Earnings" in desc:
                target_pdf = n.get("attchmntFile")
                break
                
        if not target_pdf:
            return jsonify({"error": "No recent concall transcripts or investor presentations found on NSE."}), 404
            
        # Parse the PDF
        from pdf_parser import extract_text_from_nse_pdf
        text = extract_text_from_nse_pdf(target_pdf)
        
        if not text:
            return jsonify({"error": "Could not extract text from the PDF document."}), 500
            
        # Analyze with AI
        from ai_analyzer import analyze_concall_text
        ai_data = analyze_concall_text(text)
        
        if "error" in ai_data:
            return jsonify(ai_data), 500
            
        return jsonify(ai_data)
        
    except Exception as e:
        logger.error(f"AI Concall failed for {symbol}: {e}")
        return jsonify({"error": str(e)}), 500

# ── Scanner DOWN helpers
"""

if "# ── Scanner DOWN helpers" in content:
    content = content.replace("# ── Scanner DOWN helpers", ai_endpoint.strip() + "\n\n# ── Scanner DOWN helpers")
    with open(filename, 'w') as f:
        f.write(content)
    print("Patched dashboard_server.py with AI endpoint")
else:
    print("Could not find insertion point!")
