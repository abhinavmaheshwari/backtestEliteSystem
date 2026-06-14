import re

def process_file(filename):
    with open(filename, 'r') as f:
        content = f.read()

    # 1. Fix the equityChart DOM element bug
    content = content.replace('if (window.equityChart) {', 'if (window.equityChart instanceof Chart) {')

    # 2. Fix the ticker color (hardcode white/slate for dark background)
    # The container:
    content = content.replace(
        'style="background:#0b0e14; border-bottom:1px solid var(--border); padding:6px 16px; font-size:12px; font-family:var(--font-mono); color:var(--muted); display:flex; gap:20px; align-items:center; overflow-x:auto; white-space:nowrap;"',
        'style="background:#0b0e14; border-bottom:1px solid var(--border); padding:6px 16px; font-size:12px; font-family:var(--font-mono); color:#94a3b8; display:flex; gap:20px; align-items:center; overflow-x:auto; white-space:nowrap;"'
    )
    
    # The LIVE MARKETS title:
    content = content.replace(
        'style="font-weight:700; color:var(--text); letter-spacing:1px; margin-right:10px;">LIVE MARKETS</div>',
        'style="font-weight:700; color:#ffffff; letter-spacing:1px; margin-right:10px;">LIVE MARKETS</div>'
    )
    
    # The JS injected ticker items:
    content = content.replace('<span style="color:var(--text);">${name}</span>', '<span style="color:#ffffff;">${name}</span>')

    with open(filename, 'w') as f:
        f.write(content)
    print(f"Fixed bugs in {filename}")

process_file('app/admin_dashboard.html')
process_file('app/user_dashboard.html')
