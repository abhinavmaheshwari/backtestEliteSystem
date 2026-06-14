import re

filename = "app/dashboard_server.py"
with open(filename, 'r') as f:
    content = f.read()

new_endpoint = """
import subprocess
import json

@app.route("/api/notices/<symbol>")
def api_notices(symbol):
    \"\"\"Fetch recent corporate announcements from NSE via curl to bypass WAF.\"\"\"
    yf_symbol = symbol.replace('.NS', '')
    url = f"https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={yf_symbol}"
    cmd = [
        "curl", "-s", url,
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ]
    try:
        output = subprocess.check_output(cmd, timeout=5).decode('utf-8')
        if not output.strip():
            return jsonify([])
            
        data = json.loads(output)
        notices = []
        for n in data[:4]:
            desc = str(n.get("desc", ""))
            # Truncate overly long descriptions
            if len(desc) > 40:
                desc = desc[:37] + "..."
                
            notices.append({
                "date": n.get("an_dt", "").split(" ")[0],
                "desc": desc,
                "link": n.get("attchmntFile", "")
            })
        return jsonify(notices)
    except Exception as e:
        logger.error(f"Failed to fetch notices for {symbol}: {e}")
        return jsonify([])

# ── Scanner DOWN helpers
"""

content = content.replace("# ── Scanner DOWN helpers", new_endpoint.strip())

with open(filename, 'w') as f:
    f.write(content)

print("Patched dashboard_server.py")
