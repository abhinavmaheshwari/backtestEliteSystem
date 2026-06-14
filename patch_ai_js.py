import re

filename = "app/admin_dashboard.html"
with open(filename, 'r') as f:
    content = f.read()

ai_js = """
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

if "async function generateAI" not in content:
    content = content.replace("async function fetchNotices", ai_js + "\nasync function fetchNotices")
    with open(filename, 'w') as f:
        f.write(content)
    print("Patched admin_dashboard.html with JS")
else:
    print("Already patched!")
