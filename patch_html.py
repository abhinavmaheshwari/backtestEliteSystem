import re

def patch(filename):
    with open(filename, 'r') as f:
        content = f.read()

    # 1. Update the toggleDetail call to include symbol
    content = content.replace("onclick=\"toggleDetail('${id}')\"", "onclick=\"toggleDetail('${id}', '${t.symbol}')\"")

    # 2. Split the bottom row into 2 columns
    old_news_block = """                    <div class="detail-item" style="grid-column: 1 / -1; padding-top: 16px; border-top: 1px dashed rgba(0,212,161,.15);">
            <div class="detail-label" style="color:var(--accent); display:flex; justify-content:space-between; align-items:center;">
              <span>Live Catalysts</span>
              <a href="https://www.nseindia.com/get-quotes/equity?symbol=${t.symbol}" target="_blank" style="color:var(--accent);text-decoration:none;font-size:9px;border:1px solid rgba(0,212,161,.3);padding:2px 6px;border-radius:4px;transition:0.2s;">Official NSE Notices ↗</a>
            </div>
            <div class="detail-val" style="margin-top:4px; max-height:150px; overflow-y:auto; padding-right:8px;">
              ${(window.NEWS_CACHE && window.NEWS_CACHE[t.symbol]) ? window.NEWS_CACHE[t.symbol] : `
                <div id="news-${id}" style="font-size:11px; font-weight:normal; color:var(--text); font-family:var(--font-body); line-height:1.4;">Waiting...</div>
                <button id="btn-news-${id}" onclick="fetchNews('${t.symbol}', 'news-${id}', 'btn-news-${id}')" style="background:var(--card2); border:1px solid var(--border); color:var(--text); padding:4px 8px; border-radius:4px; font-size:10px; cursor:pointer; margin-top:6px;">Fetch Latest News</button>
              `}
            </div>
          </div>"""

    new_news_block = """          <div style="grid-column: 1 / -1; padding-top: 16px; border-top: 1px dashed rgba(0,212,161,.15); display:grid; grid-template-columns: 1fr 1fr; gap:24px;">
            <div class="detail-item">
              <div class="detail-label" style="color:var(--accent);">Live Catalysts</div>
              <div class="detail-val" style="margin-top:4px; max-height:150px; overflow-y:auto; padding-right:8px;">
                ${(window.NEWS_CACHE && window.NEWS_CACHE[t.symbol]) ? window.NEWS_CACHE[t.symbol] : `
                  <div id="news-${id}" style="font-size:11px; font-weight:normal; color:var(--text); font-family:var(--font-body); line-height:1.4;">Waiting...</div>
                  <button id="btn-news-${id}" onclick="fetchNews('${t.symbol}', 'news-${id}', 'btn-news-${id}')" style="background:var(--card2); border:1px solid var(--border); color:var(--text); padding:4px 8px; border-radius:4px; font-size:10px; cursor:pointer; margin-top:6px;">Fetch Latest News</button>
                `}
              </div>
            </div>
            <div class="detail-item">
              <div class="detail-label" style="color:var(--accent);">Official NSE Notices</div>
              <div class="detail-val" style="margin-top:4px; max-height:150px; overflow-y:auto; padding-right:8px;" id="notices-${id}">
                ${(window.NOTICES_CACHE && window.NOTICES_CACHE[t.symbol]) ? window.NOTICES_CACHE[t.symbol] : `
                  <div style="font-size:11px; font-weight:normal; color:var(--muted); font-family:var(--font-body); line-height:1.4;">Fetching...</div>
                `}
              </div>
            </div>
          </div>"""
    
    if old_news_block in content:
        content = content.replace(old_news_block, new_news_block)
    else:
        print("COULD NOT FIND NEWS BLOCK IN", filename)

    # 3. Add toggleDetail update & fetchNotices function
    old_toggle = "function toggleDetail(id) {"
    new_toggle = "window.NOTICES_CACHE = window.NOTICES_CACHE || {};\n\nfunction toggleDetail(id, symbol) {"
    content = content.replace(old_toggle, new_toggle)

    old_btn_click = """    const btn = document.getElementById('btn-news-' + id);
    if (btn) {
      btn.click();
    }
  }
}"""
    new_btn_click = """    const btn = document.getElementById('btn-news-' + id);
    if (btn) {
      btn.click();
    }
    if (symbol && !window.NOTICES_CACHE[symbol]) {
        fetchNotices(symbol, 'notices-' + id);
    }
  }
}

async function fetchNotices(symbol, containerId) {
    const el = document.getElementById(containerId);
    if(!el) return;
    try {
        const res = await fetch('/api/notices/' + symbol);
        const data = await res.json();
        if(!data || data.length === 0) {
            const html = '<div style="font-size:11px; color:var(--muted);">No recent notices.</div>';
            el.innerHTML = html;
            window.NOTICES_CACHE[symbol] = html;
            return;
        }
        
        let html = '<div style="display:flex; flex-direction:column; gap:8px;">';
        data.forEach(n => {
            html += `
              <div style="background:var(--card2); padding:6px 10px; border-radius:4px; border-left:2px solid var(--accent);">
                <div style="font-size:9px; color:var(--muted); margin-bottom:2px;">${n.date}</div>
                <a href="${n.link}" target="_blank" style="color:var(--text); text-decoration:none; font-size:11px; font-weight:600; line-height:1.3; display:block;">
                  ${n.desc} ↗
                </a>
              </div>
            `;
        });
        html += '</div>';
        el.innerHTML = html;
        window.NOTICES_CACHE[symbol] = html;
    } catch(e) {
        el.innerHTML = '<div style="font-size:11px; color:var(--danger);">Error fetching notices.</div>';
    }
}
"""
    if old_btn_click in content:
        content = content.replace(old_btn_click, new_btn_click)
    else:
        print("COULD NOT FIND BTN CLICK IN", filename)

    with open(filename, 'w') as f:
        f.write(content)
    print("Patched", filename)

patch("app/admin_dashboard.html")
patch("app/user_dashboard.html")
