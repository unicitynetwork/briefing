import urllib.request, urllib.parse, json, os, re, base64
from datetime import datetime, timedelta, timezone

GH_TOKEN      = os.environ['GH_TOKEN'].strip()
ANTHROPIC_KEY = os.environ['ANTHROPIC_API_KEY'].strip()

# ── 1. Calculate window based on day of week ─────────────────────────────────
# Monday  → cover Fri + Sat + Sun (3 days back)
# Tue–Fri → cover previous day only

now     = datetime.now(timezone.utc)
weekday = now.weekday()  # 0=Mon, 1=Tue ... 6=Sun

if weekday == 0:  # Monday
    window_start = (now - timedelta(days=3)).strftime('%Y-%m-%d')
    window_label = 'Friday \u2013 Sunday'
    days_back    = 3
else:             # Tue-Fri
    window_start = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    window_label = (now - timedelta(days=1)).strftime('%A, %-d %B %Y')
    days_back    = 1

window_end  = (now - timedelta(days=1)).strftime('%Y-%m-%d')
date_range  = f'{window_start}..{window_end}'
report_date = now.strftime('%A, %-d %B %Y')

print(f'Window: {date_range} ({window_label})')

# ── 2. GitHub search helpers ─────────────────────────────────────────────────

def gh_search(q, per_page=50):
    url = 'https://api.github.com/search/issues?q=' + urllib.parse.quote(q) + f'&per_page={per_page}'
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {GH_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'unicity-briefing'
    })
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read())
    return data.get('items', [])

ORGS    = ['unicity-astrid', 'unicity-sphere', 'unicitynetwork']
MEMBERS = [
    'joshuajbouw','MastaP','igmahl','KruGoL','ristik',
    'martti007','jvsteiner','ahtotruu','b3y0urs3lf',
    'jait91','lploom','vrogojin','0xt1mo'
]

# Per-org PR sweep
org_prs  = {}
all_prs  = []
releases = []
contributors = set()

for org in ORGS:
    prs = gh_search(f'org:{org} is:pr is:merged merged:{date_range}')
    org_prs[org] = prs
    all_prs.extend(prs)
    for pr in prs:
        contributors.add(pr['user']['login'])
        m = re.search(r'v\d+\.\d+\.\d+', pr['title'])
        t = pr['title'].lower()
        if m and ('release' in t or 'chore: release' in t):
            repo = pr['repository_url'].split('/')[-1]
            releases.append(f'{repo} {m.group()}')

total_merged = len(all_prs)
print(f'Merged PRs: {total_merged}')

# involves sweep — catches reviews, comments, assignments
involves_seen = set()
involves_prs  = []
for member in MEMBERS:
    items = gh_search(f'involves:{member} updated:{date_range} is:pr org:unicity-astrid OR org:unicity-sphere OR org:unicitynetwork')
    for item in items:
        if item['number'] not in involves_seen:
            involves_seen.add(item['number'])
            involves_prs.append(item)

# Long-standing open PRs (>7 days)
cutoff = (now - timedelta(days=7)).strftime('%Y-%m-%d')
long_prs = []
for org in ORGS:
    items = gh_search(f'org:{org} is:pr is:open created:<{cutoff}', per_page=50)
    long_prs.extend(items)
long_prs.sort(key=lambda p: p['created_at'])

print(f'Long-standing open PRs: {len(long_prs)}')

# ── 3. Build PR text for Claude ───────────────────────────────────────────────

def pr_lines(prs, limit=60):
    lines = []
    for pr in prs[:limit]:
        repo = pr['repository_url'].split('/')[-1]
        body = (pr.get('body') or '')[:200].replace('\n', ' ')
        lines.append(f'- [{repo}] #{pr["number"]} "{pr["title"]}" by @{pr["user"]["login"]} | {body}')
    return '\n'.join(lines)

pr_text_astrid  = pr_lines(org_prs.get('unicity-astrid', []))
pr_text_sphere  = pr_lines(org_prs.get('unicity-sphere', []))
pr_text_network = pr_lines(org_prs.get('unicitynetwork', []))

# ── 4. Call Claude to generate thematic summaries ────────────────────────────

prompt = f"""You are generating the daily engineering briefing for the Unicity project.
Period: {window_label}
Date range: {date_range}
Total PRs merged: {total_merged}
Releases: {', '.join(releases) if releases else 'none'}

=== unicity-astrid ({len(org_prs.get('unicity-astrid',[]))} PRs) ===
{pr_text_astrid or 'No activity'}

=== unicity-sphere ({len(org_prs.get('unicity-sphere',[]))} PRs) ===
{pr_text_sphere or 'No activity'}

=== unicitynetwork ({len(org_prs.get('unicitynetwork',[]))} PRs) ===
{pr_text_network or 'No activity'}

For each org that had activity, produce 1-4 themes.
Each theme: title (max 10 words), repos (comma-separated short names), description (2-4 plain English sentences, max 400 chars, mention repo names).

Respond ONLY with valid JSON, no fences:
{{
  "astrid":  [{{"title":"...","repos":"...","description":"..."}}],
  "sphere":  [{{"title":"...","repos":"...","description":"..."}}],
  "network": [{{"title":"...","repos":"...","description":"..."}}]
}}"""

payload = json.dumps({
    'model': 'claude-haiku-4-5-20251001',
    'max_tokens': 2000,
    'messages': [{'role': 'user', 'content': prompt}]
}).encode()

req = urllib.request.Request(
    'https://api.anthropic.com/v1/messages',
    data=payload,
    headers={'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'}
)
try:
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read())
    raw = resp['content'][0]['text'].strip()
    raw = re.sub(r'^```[a-z]*\n?', '', raw).rstrip('`').strip()
    themes = json.loads(raw)
    print('Claude themes generated')
except Exception as e:
    print(f'Claude error: {e}')
    themes = {'astrid': [], 'sphere': [], 'network': []}

# ── 5. Build HTML ─────────────────────────────────────────────────────────────

def age_class(created_at):
    days = (now - datetime.fromisoformat(created_at.replace('Z','+00:00'))).days
    if days >= 90: return 'age-critical'
    if days >= 30: return 'age-high'
    if days >= 14: return 'age-medium'
    return 'age-low'

def age_days(created_at):
    return (now - datetime.fromisoformat(created_at.replace('Z','+00:00'))).days

def theme_cards(theme_list, dot_color):
    if not theme_list:
        return '<p style="font-size:13px;color:var(--color-text-secondary);padding:4px 0">No activity this period.</p>'
    html = ''
    for t in theme_list:
        html += f'''
    <div class="event-row">
      <div class="event-dot" style="background:{dot_color}"></div>
      <div class="event-body">
        <div class="event-title">{t.get("title","")}</div>
        <div class="event-detail">{t.get("description","")}</div>
        <div class="event-meta"><code>{t.get("repos","")}</code></div>
      </div>
    </div>'''
    return html

def long_pr_rows(prs):
    if not prs:
        return '<tr><td colspan="5" style="padding:12px;font-size:13px;color:#888;text-align:center">No open PRs older than 7 days.</td></tr>'
    rows = ''
    for pr in prs[:20]:
        repo  = pr['repository_url'].split('/')[-1]
        days  = age_days(pr['created_at'])
        cls   = age_class(pr['created_at'])
        draft = '<span class="draft-tag">Draft</span>' if pr.get('draft') else ''
        rows += f'''<tr>
      <td><span class="age-pill {cls}">{days} days</span></td>
      <td><a href="{pr["html_url"]}" class="pr-link">{pr["title"]}</a>{draft}<div class="pr-repo">{repo} #{pr["number"]}</div></td>
      <td class="pr-author">@{pr["user"]["login"]}</td>
    </tr>'''
    return rows

rel_str   = ' &middot; '.join(f'<span class="badge badge-release">{r}</span>' for r in releases)
n_astrid  = len(org_prs.get('unicity-astrid',[]))
n_sphere  = len(org_prs.get('unicity-sphere',[]))
n_network = len(org_prs.get('unicitynetwork',[]))

HTML = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Unicity Briefing &mdash; {report_date}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1a1a18;background:#f5f4f0;padding:1.5rem}}
.card{{background:#fff;border:0.5px solid rgba(0,0,0,0.12);border-radius:12px;padding:1rem 1.25rem;margin-bottom:12px}}
.badge{{display:inline-block;font-size:11px;font-weight:500;padding:2px 8px;border-radius:6px}}
.badge-purple{{background:#EEEDFE;color:#3C3489}}.badge-teal{{background:#E1F5EE;color:#085041}}
.badge-blue{{background:#E6F1FB;color:#0C447C}}.badge-amber{{background:#FAEEDA;color:#633806}}
.badge-green{{background:#EAF3DE;color:#27500A}}.badge-release{{background:#FDF3C7;color:#92400E}}
.metric-grid{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:16px}}
.metric{{background:#f5f4f0;border-radius:8px;padding:12px}}
.metric-label{{font-size:12px;color:#666;margin-bottom:4px}}
.metric-val{{font-size:22px;font-weight:500}}
.metric-val.hi{{color:#1D9E75}}.metric-val.pu{{color:#7F77DD}}.metric-val.am{{color:#D97706}}.metric-val.re{{color:#E24B4A}}
.section-title{{font-size:11px;font-weight:500;color:#666;text-transform:uppercase;letter-spacing:.05em;margin:14px 0 6px}}
.event-row{{display:flex;gap:10px;align-items:flex-start;padding:9px 0;border-bottom:0.5px solid rgba(0,0,0,0.08)}}
.event-row:last-child{{border-bottom:none}}
.event-dot{{width:7px;height:7px;border-radius:50%;flex-shrink:0;margin-top:6px}}
.event-body{{flex:1;min-width:0}}
.event-title{{font-size:13px;font-weight:500;line-height:1.5;margin-bottom:3px}}
.event-detail{{font-size:12px;color:#666;line-height:1.6}}
.event-meta{{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin-top:5px}}
code{{font-family:'SF Mono',Monaco,monospace;font-size:11.5px;background:#f5f4f0;padding:1px 5px;border-radius:4px}}
.header{{margin-bottom:20px;padding-bottom:14px;border-bottom:0.5px solid rgba(0,0,0,0.1)}}
.header h2{{font-size:18px;font-weight:500}}
.header p{{font-size:13px;color:#666;margin-top:3px}}
.window-badge{{display:inline-block;font-size:11px;font-weight:500;padding:2px 8px;border-radius:6px;background:#E6F1FB;color:#0C447C;margin-top:5px}}
.org-header{{display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap}}
.pr-table{{width:100%;border-collapse:collapse;font-size:12px}}
.pr-table th{{font-size:10.5px;font-weight:600;color:#666;text-transform:uppercase;letter-spacing:.04em;padding:6px 10px;background:#f5f4f0;border-bottom:0.5px solid rgba(0,0,0,0.1);text-align:left}}
.pr-table td{{padding:8px 10px;border-bottom:0.5px solid rgba(0,0,0,0.06);vertical-align:top}}
.pr-table tr:last-child td{{border-bottom:none}}
.age-pill{{display:inline-block;font-size:10.5px;font-weight:600;padding:1px 7px;border-radius:10px;white-space:nowrap}}
.age-critical{{background:#FCEBEB;color:#791F1F}}.age-high{{background:#FAEEDA;color:#633806}}
.age-medium{{background:#FDF3C7;color:#92400E}}.age-low{{background:#F1EFE8;color:#444}}
.pr-link{{color:#1a1a18;font-weight:500;line-height:1.4;display:block;text-decoration:none}}
.pr-link:hover{{text-decoration:underline}}
.pr-repo{{font-size:11px;font-family:'SF Mono',Monaco,monospace;color:#888;margin-top:1px}}
.pr-author{{font-size:11px;font-family:'SF Mono',Monaco,monospace;color:#666}}
.draft-tag{{display:inline-block;font-size:10px;padding:1px 5px;border-radius:3px;background:#F1EFE8;color:#888;margin-left:4px;font-style:italic}}
.generated-note{{font-size:11px;color:#aaa;margin-top:16px;text-align:center}}
</style>
</head>
<body>
<div style="max-width:960px;margin:0 auto;padding:1.25rem 0">

<div class="header">
  <h2>Unicity project &mdash; daily brief</h2>
  <p>{report_date}</p>
  <span class="window-badge">Coverage: {window_label} &middot; GitHub API (author + involves sweep)</span>
  {f'<div style="margin-top:6px">{rel_str}</div>' if releases else ''}
</div>

<div class="metric-grid">
  <div class="metric"><div class="metric-label">PRs merged</div><div class="metric-val hi">{total_merged}</div></div>
  <div class="metric"><div class="metric-label">Releases</div><div class="metric-val am">{len(releases)}</div></div>
  <div class="metric"><div class="metric-label">Open PRs &gt;7 days</div><div class="metric-val re">{len(long_prs)}</div></div>
  <div class="metric"><div class="metric-label">Contributors</div><div class="metric-val pu">{len(contributors)}</div></div>
  <div class="metric"><div class="metric-label">Astrid / Sphere / Network</div><div class="metric-val hi" style="font-size:16px">{n_astrid} / {n_sphere} / {n_network}</div></div>
</div>

<div class="card" style="border-color:#7F77DD">
  <div class="org-header"><span class="badge badge-purple">unicity-astrid</span><span style="font-size:13px;color:#666">{n_astrid} PRs merged</span></div>
  {theme_cards(themes.get('astrid',[]), '#7F77DD')}
</div>

<div class="card" style="border-color:#1D9E75">
  <div class="org-header"><span class="badge badge-teal">unicity-sphere</span><span style="font-size:13px;color:#666">{n_sphere} PRs merged</span></div>
  {theme_cards(themes.get('sphere',[]), '#1D9E75')}
</div>

<div class="card" style="border-color:#378ADD">
  <div class="org-header"><span class="badge badge-blue">unicitynetwork</span><span style="font-size:13px;color:#666">{n_network} PRs merged</span></div>
  {theme_cards(themes.get('network',[]), '#378ADD')}
</div>

<div class="card" style="border-color:#E24B4A">
  <div class="org-header"><span class="badge" style="background:#FCEBEB;color:#791F1F">Long-standing open PRs</span><span style="font-size:12px;color:#666">All open PRs older than 7 days &mdash; sorted oldest first</span></div>
  <div style="overflow-x:auto">
  <table class="pr-table">
    <thead><tr><th>Age</th><th>PR</th><th>Author</th></tr></thead>
    <tbody>{long_pr_rows(long_prs)}</tbody>
  </table>
  </div>
</div>

<p class="generated-note">Auto-generated {now.strftime('%Y-%m-%d %H:%M')} UTC &mdash; board comparison and ristik/ndsmt-experiments commits require manual check</p>

</div>
</body>
</html>'''

print(f'HTML built: {len(HTML)} chars')

# ── 6. Push index.html to briefing repo ──────────────────────────────────────

# Get current SHA of index.html
sha_url = 'https://api.github.com/repos/unicitynetwork/briefing/contents/index.html'
req = urllib.request.Request(sha_url, headers={
    'Authorization': f'token {GH_TOKEN}',
    'Accept': 'application/vnd.github.v3+json',
    'User-Agent': 'unicity-briefing'
})
try:
    with urllib.request.urlopen(req) as r:
        existing = json.loads(r.read())
    current_sha = existing['sha']
    print(f'Current index.html SHA: {current_sha}')
except Exception as e:
    current_sha = None
    print(f'No existing index.html: {e}')

# Push updated index.html
push_payload = {
    'message': f'briefing: auto-report {report_date} ({window_label})',
    'content': base64.b64encode(HTML.encode()).decode(),
    'branch': 'main'
}
if current_sha:
    push_payload['sha'] = current_sha

push_data = json.dumps(push_payload).encode()
req = urllib.request.Request(
    sha_url,
    data=push_data,
    headers={
        'Authorization': f'token {GH_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
        'User-Agent': 'unicity-briefing'
    },
    method='PUT'
)
with urllib.request.urlopen(req) as r:
    result = json.loads(r.read())
    print(f'Pushed index.html: {result["commit"]["sha"]}')

print(f'Done. https://unicitynetwork.github.io/briefing/')
