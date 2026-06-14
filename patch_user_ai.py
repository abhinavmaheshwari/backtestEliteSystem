import re

filename = "app/user_dashboard.html"
with open(filename, 'r') as f:
    content = f.read()

html_patch = """            </div>
          </div>
          <div style="grid-column: 1 / -1; padding-top: 16px; border-top: 1px dashed rgba(0,212,161,.15);">
              <div class="detail-label" style="color:#b266ff; display:flex; justify-content:space-between; align-items:center;">
                  <span style="font-size:10px; font-weight:700;">🤖 AI Concall Analyzer</span>
                  <button onclick="generateAI('${t.symbol}', 'ai-${id}', this)" style="background:rgba(178,102,255,0.15); border:1px solid #b266ff; color:#b266ff; padding:4px 10px; border-radius:4px; font-size:9px; font-weight:700; cursor:pointer; transition:0.2s;">Generate Insights ✨</button>
              </div>
              <div class="detail-val" style="margin-top:8px;" id="ai-${id}">
                  <div style="font-size:11px; color:var(--muted); font-family:var(--font-body); font-weight:normal;">Click Generate to extract insights from the latest earnings call or investor presentation.</div>
              </div>
          </div>
          ${t.signals ? `<div class="detail-signals">⚡ ${t.signals}</div>` : ''}"""

js_patch = """
async function generateAI(symbol, containerId, btnEl) {
    const el = document.getElementById(containerId);
    if(!el) return;
    
    // UI Loading state
    btnEl.disabled = true;
    btnEl.innerText = "Analyzing PDF... ⏳";
    btnEl.style.opacity = "0.7";
    el.innerHTML = '<div style="font-size:11px; color:#b266ff; font-family:var(--font-mono);">Downloading and reading latest concall transcript/presentation. This takes ~15 seconds...</div>';
    
    try {
        const res = await fetch('/api/concall_ai/' + symbol);
        const data = await res.json();
        
        btnEl.innerText = "Analyzed ✅";
        
        if(data.error) {
            el.innerHTML = `<div style="font-size:11px; color:var(--warn);">${data.error}</div>`;
            return;
        }
        
        // Render JSON nicely
        let html = '<div style="display:grid; grid-template-columns: 1fr 1fr; gap:16px;">';
        
        const renderItem = (label, val, highlight=false) => {
            if(!val || val === "Not Mentioned") return "";
            return `
            <div style="background:var(--card2); padding:8px 12px; border-radius:4px; border-left:2px solid ${highlight ? '#b266ff' : 'rgba(178,102,255,0.3)'};">
                <div style="font-size:9px; color:var(--muted); text-transform:uppercase; margin-bottom:4px;">${label}</div>
                <div style="font-size:11px; color:var(--text); line-height:1.4; font-family:var(--font-body); font-weight:${highlight ? '700' : 'normal'}; ${highlight && label==='Management Confidence' ? 'color:#b266ff; font-size:14px;' : ''}">${val}</div>
            </div>`;
        };
        
        let risksHtml = "";
        if(data.key_risks && Array.isArray(data.key_risks)) {
            risksHtml = `
            <div style="grid-column: 1 / -1; background:var(--card2); padding:8px 12px; border-radius:4px; border-left:2px solid var(--warn);">
                <div style="font-size:9px; color:var(--warn); text-transform:uppercase; margin-bottom:4px;">Key Risks</div>
                <ul style="margin:0; padding-left:16px; font-size:11px; color:var(--text); line-height:1.4; font-family:var(--font-body);">
                    ${data.key_risks.map(r => `<li>${r}</li>`).join('')}
                </ul>
            </div>`;
        }
        
        html += renderItem("Management Confidence", data.management_confidence ? data.management_confidence + " / 10" : null, true);
        html += renderItem("Growth Outlook", data.growth_outlook);
        html += renderItem("Margin Outlook", data.margin_outlook);
        html += renderItem("Capex Plans", data.capex_plans);
        html += renderItem("Debt Reduction", data.debt_reduction);
        html += risksHtml;
        html += '</div>';
        
        el.innerHTML = html;
        
    } catch(e) {
        btnEl.innerText = "Error ❌";
        btnEl.disabled = false;
        el.innerHTML = `<div style="font-size:11px; color:var(--danger);">Network error while reaching AI endpoint.</div>`;
    }
}
"""

if "🤖 AI Concall Analyzer" not in content:
    content = content.replace("            </div>\n          </div>\n          ${t.signals ? `<div class=\"detail-signals\">⚡ ${t.signals}</div>` : ''}", html_patch)
    content = content.replace("async function fetchNotices", js_patch + "\nasync function fetchNotices")
    with open(filename, 'w') as f:
        f.write(content)
    print("Patched user_dashboard.html")
else:
    print("Already patched!")
