import urllib.request, urllib.parse, json, os, re, base64, time
from datetime import datetime, timedelta, timezone

GH_TOKEN      = os.environ['GH_TOKEN'].strip()
ANTHROPIC_KEY = os.environ['ANTHROPIC_API_KEY'].strip()

# ── 1. Window ─────────────────────────────────────────────────────────────────
now     = datetime.now(timezone.utc)
weekday = now.weekday()  # 0=Mon

if weekday == 0:
    window_start = (now - timedelta(days=3)).strftime('%Y-%m-%d')
    window_label = 'Friday \u2013 Sunday'
else:
    window_start = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    window_label = (now - timedelta(days=1)).strftime('%A, %-d %B %Y')

window_end   = (now - timedelta(days=1)).strftime('%Y-%m-%d')
date_range   = f'{window_start}..{window_end}'
report_date  = now.strftime('%A, %-d %B %Y')
generated_at = now.strftime('%-d %B %Y, %H:%M UTC')
print(f'Window: {date_range} ({window_label})')

# ── 2. Helpers ────────────────────────────────────────────────────────────────
def gh_search(q, per_page=50):
    url = 'https://api.github.com/search/issues?q=' + urllib.parse.quote(q) + f'&per_page={per_page}'
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {GH_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'unicity-briefing'
    })
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read()).get('items', [])
    except Exception as e:
        print(f'Search error ({q[:60]}): {e}')
        return []

def gh_graphql(query, variables=None):
    payload = json.dumps({'query': query, 'variables': variables or {}}).encode()
    req = urllib.request.Request(
        'https://api.github.com/graphql',
        data=payload,
        headers={
            'Authorization': f'bearer {GH_TOKEN}',
            'Content-Type': 'application/json',
            'User-Agent': 'unicity-briefing'
        }
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f'GraphQL error: {e}')
        return {}

ORGS = ['unicity-astrid', 'unicity-sphere', 'unicitynetwork']
ORG_LABELS = {
    'unicity-astrid': 'Astrid',
    'unicity-sphere': 'Sphere',
    'unicitynetwork': 'Unicity Network'
}
MEMBERS = [
    'joshuajbouw','MastaP','igmahl','KruGoL','ristik',
    'martti007','jvsteiner','ahtotruu','b3y0urs3lf',
    'jait91','lploom','vrogojin','0xt1mo'
]
MEMBER_NAMES = {
    'joshuajbouw':'Joshua J. Bouw','MastaP':'Pavel Grigorenko',
    'igmahl':'Igor Mahlinovski','KruGoL':'Alexander Khrushkov',
    'ristik':'Risto Laanoja','martti007':'Martti Marran',
    'jvsteiner':'Jamie Steiner',
}

# ── 3. Per-org merged PR sweep ────────────────────────────────────────────────
org_prs      = {}
all_prs      = []
releases     = []
contributors = set()
merged_keys  = set()

for org in ORGS:
    prs = gh_search(f'org:{org} is:pr is:merged merged:{date_range}')
    org_prs[org] = prs
    all_prs.extend(prs)
    for pr in prs:
        contributors.add(pr['user']['login'])
        repo = pr['repository_url'].split('/')[-1]
        merged_keys.add((repo, pr['number']))
        m = re.search(r'v\d+\.\d+\.\d+', pr['title'])
        t = pr['title'].lower()
        if m and ('release' in t or 'chore: release' in t):
            releases.append(f'{repo} {m.group()}')

total_merged = len(all_prs)
print(f'Merged PRs: {total_merged}')

# ── 4. involves sweep ─────────────────────────────────────────────────────────
member_data     = {m: {'authored_merged':[], 'authored_open':[], 'involved':[]} for m in MEMBERS}
seen_per_member = {m: set() for m in MEMBERS}

for member in MEMBERS:
    for org in ORGS:
        for kind in ('pr', 'issue'):
            items = gh_search(f'involves:{member} updated:{date_range} is:{kind} org:{org}')
            for item in items:
                uid = (item['number'], item['repository_url'])
                if uid in seen_per_member[member]:
                    continue
                seen_per_member[member].add(uid)
                is_author = item['user']['login'].lower() == member.lower()
                is_merged = bool(item.get('pull_request', {}).get('merged_at'))
                is_open   = item['state'] == 'open'
                if is_author and is_merged:
                    member_data[member]['authored_merged'].append(item)
                elif is_author and is_open:
                    member_data[member]['authored_open'].append(item)
                else:
                    member_data[member]['involved'].append(item)
        time.sleep(0.5)

# ── 5. Long-standing open PRs (>7 days) ──────────────────────────────────────
cutoff   = (now - timedelta(days=7)).strftime('%Y-%m-%d')
long_prs = []
for org in ORGS:
    long_prs.extend(gh_search(f'org:{org} is:pr is:open created:<{cutoff}', per_page=50))
long_prs.sort(key=lambda p: p['created_at'])
print(f'Long-standing open PRs: {len(long_prs)}')

# ── 6. Board fetch via GraphQL ────────────────────────────────────────────────
BOARD_Q = '''
query($org: String!, $num: Int!) {
  organization(login: $org) {
    projectV2(number: $num) {
      items(first: 100) {
        totalCount
        nodes {
          fieldValues(first: 10) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
          content {
            ... on PullRequest {
              number title url state mergedAt isDraft
              repository { name }
            }
            ... on Issue {
              number title url state closedAt
              repository { name }
            }
          }
        }
      }
    }
  }
}'''

boards     = {}
board_keys = set()

for org in ORGS:
    result    = gh_graphql(BOARD_Q, {'org': org, 'num': 1})
    items_out = []
    try:
        nodes = result['data']['organization']['projectV2']['items']['nodes']
        total = result['data']['organization']['projectV2']['items']['totalCount']
        if total > 100:
            print(f'  Warning: {org} board has {total} items, only first 100 fetched')
        for node in nodes:
            status = None
            for fv in node.get('fieldValues', {}).get('nodes', []):
                if isinstance(fv.get('field'), dict) and fv['field'].get('name') == 'Status':
                    status = fv.get('name')
                    break
            c = node.get('content')
            if not c:
                continue
            repo   = c.get('repository', {}).get('name', '')
            number = c.get('number')
            is_pr  = 'isDraft' in c or 'mergedAt' in c
            item   = {
                'status':    status or 'No Status',
                'type':      'pr' if is_pr else 'issue',
                'number':    number,
                'repo':      repo,
                'title':     c.get('title', ''),
                'url':       c.get('url', ''),
                'state':     c.get('state', ''),
                'merged_at': c.get('mergedAt'),
                'is_draft':  c.get('isDraft', False),
            }
            items_out.append(item)
            if repo and number:
                board_keys.add((repo, number))
        boards[org] = items_out
        print(f'  Board {org}: {len(items_out)} items')
    except Exception as e:
        print(f'  Board fetch failed for {org}: {e}')
        boards[org] = []

# ── 7. Board comparison ───────────────────────────────────────────────────────
IN_DEV_STATUSES = {'In Dev','In Development','In Progress','In Review','Review','Test','Testing'}
board_issues    = []

for org, items in boards.items():
    label = ORG_LABELS.get(org, org)
    for item in items:
        repo, num, status, title, url = (
            item['repo'], item['number'], item['status'],
            item['title'], item['url']
        )
        if item['type'] == 'pr' and status in IN_DEV_STATUSES:
            if item['state'] == 'MERGED' or item.get('merged_at'):
                board_issues.append({'org': label, 'sev': 'stale',
                    'msg': f'Stuck in \u201c{status}\u201d \u2014 PR already merged',
                    'title': title, 'url': url, 'ref': f'{repo} #{num}'})
        if status == 'No Status':
            board_issues.append({'org': label, 'sev': 'nostatus',
                'msg': 'No Status assigned',
                'title': title, 'url': url, 'ref': f'{repo} #{num}'})

for pr in long_prs:
    repo = pr['repository_url'].split('/')[-1]
    if (repo, pr['number']) not in board_keys:
        board_issues.append({'org': ORG_LABELS.get(
            next((o for o in ORGS if pr['repository_url'].startswith(f'https://api.github.com/repos/{o}')), ''), 'Unknown'),
            'sev': 'missing',
            'msg': 'Open PR not tracked on any board',
            'title': pr['title'], 'url': pr['html_url'],
            'ref': f'{repo} #{pr["number"]}'})

print(f'Board issues: {len(board_issues)}')

# ── 8. Claude themes ──────────────────────────────────────────────────────────
def pr_lines(prs, limit=60):
    lines = []
    for pr in prs[:limit]:
        repo = pr['repository_url'].split('/')[-1]
        body = (pr.get('body') or '')[:200].replace('\n', ' ')
        lines.append(f'- [{repo}] #{pr["number"]} "{pr["title"]}" by @{pr["user"]["login"]} | {body}')
    return '\n'.join(lines)

prompt = f"""You are generating the daily engineering briefing for the Unicity project.
Period: {window_label}  |  Date range: {date_range}  |  Total PRs merged: {total_merged}
Releases: {', '.join(releases) if releases else 'none'}

=== unicity-astrid ({len(org_prs.get('unicity-astrid',[]))} PRs) ===
{pr_lines(org_prs.get('unicity-astrid',[])) or 'No activity'}

=== unicity-sphere ({len(org_prs.get('unicity-sphere',[]))} PRs) ===
{pr_lines(org_prs.get('unicity-sphere',[])) or 'No activity'}

=== unicitynetwork ({len(org_prs.get('unicitynetwork',[]))} PRs) ===
{pr_lines(org_prs.get('unicitynetwork',[])) or 'No activity'}

For each org with activity produce 1-4 themes.
Each theme: title (max 10 words), repos (comma-separated short names), description (2-4 plain English sentences max 400 chars mentioning repo names).

Respond ONLY with valid JSON no fences:
{{"astrid":[{{"title":"...","repos":"...","description":"..."}}],"sphere":[...],"network":[...]}}"""

try:
    payload = json.dumps({'model':'claude-haiku-4-5-20251001','max_tokens':2000,
        'messages':[{'role':'user','content':prompt}]}).encode()
    req = urllib.request.Request('https://api.anthropic.com/v1/messages', data=payload,
        headers={'x-api-key':ANTHROPIC_KEY,'anthropic-version':'2023-06-01','content-type':'application/json'})
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read())
    raw    = re.sub(r'^```[a-z]*\n?','',resp['content'][0]['text'].strip()).rstrip('`').strip()
    themes = json.loads(raw)
    print('Claude themes OK')
except Exception as e:
    print(f'Claude error: {e}')
    themes = {'astrid':[],'sphere':[],'network':[]}

# ── 9. HTML helpers ───────────────────────────────────────────────────────────
def esc(s): return str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

def age_days(ts):
    return (now - datetime.fromisoformat(ts.replace('Z','+00:00'))).days

def age_class(ts):
    d = age_days(ts)
    if d >= 90: return 'age-critical'
    if d >= 30: return 'age-high'
    if d >= 14: return 'age-medium'
    return 'age-low'

def theme_cards(tlist, color):
    if not tlist:
        return '<p style="font-size:13px;color:#888;padding:4px 0">No activity this period.</p>'
    out = ''
    for t in tlist:
        out += f'''<div class="event-row">
  <div class="event-dot" style="background:{color}"></div>
  <div class="event-body">
    <div class="event-title">{esc(t.get("title",""))}</div>
    <div class="event-detail">{esc(t.get("description",""))}</div>
    <div class="event-meta"><code>{esc(t.get("repos",""))}</code></div>
  </div></div>'''
    return out

def long_pr_rows(prs):
    if not prs:
        return '<tr><td colspan="3" style="padding:12px;font-size:13px;color:#888;text-align:center">No open PRs older than 7 days.</td></tr>'
    rows = ''
    for pr in prs[:20]:
        repo  = pr['repository_url'].split('/')[-1]
        days  = age_days(pr['created_at'])
        draft = '<span class="draft-tag">Draft</span>' if pr.get('draft') else ''
        rows += f'''<tr>
  <td><span class="age-pill {age_class(pr['created_at'])}">{days}d</span></td>
  <td><a href="{esc(pr['html_url'])}" class="pr-link">{esc(pr['title'])}</a>{draft}
    <div class="pr-repo">{esc(repo)} #{pr['number']}</div></td>
  <td class="pr-author">@{esc(pr['user']['login'])}</td></tr>'''
    return rows

def board_rows(issues):
    if not issues:
        return '<p style="font-size:13px;color:#888;padding:8px 0">No board issues detected.</p>'
    sev_chip = {
        'stale':    '<span class="chip chip-stale">STALE STATUS</span>',
        'nostatus': '<span class="chip chip-nostatus">NO STATUS</span>',
        'missing':  '<span class="chip chip-miss">NOT ON BOARD</span>',
    }
    out = ''
    for issue in issues[:25]:
        chip = sev_chip.get(issue['sev'], '<span class="chip">?</span>')
        out += f'''<div class="board-row">
  {chip}
  <div class="board-body">
    <div class="board-title"><a href="{esc(issue['url'])}" class="pr-link">{esc(issue['title'])}</a></div>
    <div class="board-detail"><code>{esc(issue['ref'])}</code> &middot; {esc(issue['org'])} &middot; {esc(issue['msg'])}</div>
  </div></div>'''
    return out

def member_cards():
    active, inactive = [], []
    for member in MEMBERS:
        d = member_data[member]
        if len(d['authored_merged']) + len(d['authored_open']) + len(d['involved']) > 0:
            active.append((member, d))
        else:
            inactive.append(member)

    if not active and not inactive:
        return '<p style="font-size:13px;color:#888">No member activity data.</p>'

    out = '<div class="member-grid">'
    for member, d in active:
        name    = MEMBER_NAMES.get(member, '')
        n_merge = len(d['authored_merged'])
        n_open  = len(d['authored_open'])
        n_inv   = len(d['involved'])
        repos_touched = set()
        for item in d['authored_merged'] + d['authored_open'] + d['involved']:
            r = item.get('repository_url','').split('/')[-1]
            if r: repos_touched.add(r)
        details = []
        if n_merge: details.append(f'{n_merge} PR{"s" if n_merge!=1 else ""} merged')
        if n_open:  details.append(f'{n_open} open PR{"s" if n_open!=1 else ""}')
        if n_inv:   details.append(f'involved in {n_inv} item{"s" if n_inv!=1 else ""}')
        repos_html = ''.join(f'<span class="tag">{esc(r)}</span>' for r in sorted(repos_touched)[:5])
        out += f'''<div class="member-card">
  <div class="mc-name">{esc(name) if name else ""} <span class="mc-handle">@{esc(member)}</span></div>
  <div class="mc-detail">{esc(", ".join(details))}</div>
  <div style="margin-top:6px">{repos_html}</div>
</div>'''
    out += '</div>'
    if inactive:
        quiet = ', '.join(f'@{m}' for m in inactive)
        out += f'<p style="font-size:11.5px;color:#888;margin-top:10px;line-height:1.6"><strong>No activity this window:</strong> {esc(quiet)}</p>'
    out += '''<div class="method-note"><strong>Sweep method (permanent):</strong> Each report runs
<code>involves:USERNAME</code> for every team member in addition to org-level PR/issue sweeps.
Catches closes, reviews, comments, and assignments \u2014 not just authored items.</div>'''
    return out

# ── 10. Build HTML ────────────────────────────────────────────────────────────
rel_str   = ' &middot; '.join(f'<span class="badge badge-release">{esc(r)}</span>' for r in releases)
n_astrid  = len(org_prs.get('unicity-astrid',[]))
n_sphere  = len(org_prs.get('unicity-sphere',[]))
n_network = len(org_prs.get('unicitynetwork',[]))
boards_ok = any(boards.values())

CSS = '''*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1a1a18;background:#f5f4f0;padding:1.5rem}
.card{background:#fff;border:0.5px solid rgba(0,0,0,0.12);border-radius:12px;padding:1rem 1.25rem;margin-bottom:12px}
.badge{display:inline-block;font-size:11px;font-weight:500;padding:2px 8px;border-radius:6px}
.badge-purple{background:#EEEDFE;color:#3C3489}.badge-teal{background:#E1F5EE;color:#085041}
.badge-blue{background:#E6F1FB;color:#0C447C}.badge-release{background:#FDF3C7;color:#92400E}
.metric-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:16px}
.metric{background:#f5f4f0;border-radius:8px;padding:12px}
.metric-label{font-size:12px;color:#666;margin-bottom:4px}
.metric-val{font-size:22px;font-weight:500}
.metric-val.hi{color:#1D9E75}.metric-val.pu{color:#7F77DD}.metric-val.am{color:#D97706}.metric-val.re{color:#E24B4A}
.event-row{display:flex;gap:10px;align-items:flex-start;padding:9px 0;border-bottom:0.5px solid rgba(0,0,0,0.08)}
.event-row:last-child{border-bottom:none}
.event-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;margin-top:6px}
.event-body{flex:1;min-width:0}
.event-title{font-size:13px;font-weight:500;line-height:1.5;margin-bottom:3px}
.event-detail{font-size:12px;color:#666;line-height:1.6}
.event-meta{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin-top:5px}
code{font-family:'SF Mono',Monaco,monospace;font-size:11.5px;background:#f5f4f0;padding:1px 5px;border-radius:4px}
.header{margin-bottom:20px;padding-bottom:14px;border-bottom:0.5px solid rgba(0,0,0,0.1)}
.header h2{font-size:18px;font-weight:500}
.header p{font-size:13px;color:#666;margin-top:3px}
.header-meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:6px}
.window-badge{display:inline-block;font-size:11px;font-weight:500;padding:2px 8px;border-radius:6px;background:#E6F1FB;color:#0C447C}
.updated-badge{display:inline-block;font-size:11px;font-weight:500;padding:2px 8px;border-radius:6px;background:#F1EFE8;color:#444}
.org-header{display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.pr-table{width:100%;border-collapse:collapse;font-size:12px}
.pr-table th{font-size:10.5px;font-weight:600;color:#666;text-transform:uppercase;letter-spacing:.04em;padding:6px 10px;background:#f5f4f0;border-bottom:0.5px solid rgba(0,0,0,0.1);text-align:left}
.pr-table td{padding:8px 10px;border-bottom:0.5px solid rgba(0,0,0,0.06);vertical-align:top}
.pr-table tr:last-child td{border-bottom:none}
.age-pill{display:inline-block;font-size:10.5px;font-weight:600;padding:1px 7px;border-radius:10px;white-space:nowrap}
.age-critical{background:#FCEBEB;color:#791F1F}.age-high{background:#FAEEDA;color:#633806}
.age-medium{background:#FDF3C7;color:#92400E}.age-low{background:#F1EFE8;color:#444}
.pr-link{color:#1a1a18;font-weight:500;line-height:1.4;display:block;text-decoration:none}
.pr-link:hover{text-decoration:underline}
.pr-repo{font-size:11px;font-family:'SF Mono',Monaco,monospace;color:#888;margin-top:1px}
.pr-author{font-size:11px;font-family:'SF Mono',Monaco,monospace;color:#666}
.draft-tag{display:inline-block;font-size:10px;padding:1px 5px;border-radius:3px;background:#F1EFE8;color:#888;margin-left:4px;font-style:italic}
.board-row{display:flex;gap:10px;align-items:flex-start;padding:8px 0;border-bottom:0.5px solid rgba(0,0,0,0.06);font-size:12px}
.board-row:last-child{border-bottom:none}
.chip{display:inline-block;font-size:10px;font-weight:600;padding:2px 7px;border-radius:4px;flex-shrink:0;min-width:80px;text-align:center;margin-top:1px}
.chip-stale{background:#FAEEDA;color:#633806}
.chip-nostatus{background:#EEEDFE;color:#3C3489}
.chip-miss{background:#FCEBEB;color:#791F1F}
.board-body{flex:1}
.board-title{font-size:12px;color:#1a1a18;line-height:1.4}
.board-detail{font-size:11.5px;color:#666;margin-top:2px}
.member-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}
.member-card{background:#f5f4f0;border-radius:8px;padding:10px 12px;border-left:3px solid #1D9E75}
.mc-name{font-size:13px;font-weight:500}
.mc-handle{font-family:'SF Mono',Monaco,monospace;font-size:11px;color:#888;margin-left:5px}
.mc-detail{font-size:12px;color:#666;margin-top:5px;line-height:1.5}
.tag{font-size:11px;color:#666;background:#fff;padding:1px 6px;border-radius:4px;display:inline-block;margin:2px 2px 0 0}
.method-note{font-size:11px;color:#666;background:#f5f4f0;border-radius:8px;padding:8px 12px;margin-top:12px;border-left:3px solid #7F77DD;line-height:1.5}
.footer-note{font-size:11px;color:#aaa;margin-top:16px;text-align:center;padding-bottom:8px}'''

HTML = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Unicity Briefing &mdash; {esc(report_date)}</title>
<style>{CSS}</style>
</head>
<body>
<div style="max-width:960px;margin:0 auto;padding:1.25rem 0">

<div class="header">
  <h2>Unicity project &mdash; daily brief</h2>
  <p>{esc(report_date)}</p>
  <div class="header-meta">
    <span class="window-badge">Coverage: {esc(window_label)} &middot; GitHub API (author + involves sweep)</span>
    <span class="updated-badge">Updated: {esc(generated_at)}</span>
    {rel_str}
  </div>
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
  <div class="org-header">
    <span class="badge" style="background:#FCEBEB;color:#791F1F">Project board comparison</span>
    <span style="font-size:12px;color:#666">Stale statuses &middot; missing items &middot; untracked PRs{"" if boards_ok else " &mdash; board fetch failed (token may need read:project scope)"}</span>
  </div>
  {board_rows(board_issues)}
</div>

<div class="card" style="border-color:#1D9E75">
  <div class="org-header"><span class="badge badge-teal">Team activity</span><span style="font-size:12px;color:#666">All members &mdash; author + involves sweep</span></div>
  {member_cards()}
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

<p class="footer-note">ristik/ndsmt-experiments commits require manual check</p>

</div>
</body>
</html>'''

print(f'HTML built: {len(HTML)} chars')

# ── 11. Push index.html ───────────────────────────────────────────────────────
sha_url = 'https://api.github.com/repos/unicitynetwork/briefing/contents/index.html'
req = urllib.request.Request(sha_url, headers={
    'Authorization': f'token {GH_TOKEN}',
    'Accept': 'application/vnd.github.v3+json',
    'User-Agent': 'unicity-briefing'
})
try:
    with urllib.request.urlopen(req) as r:
        current_sha = json.loads(r.read())['sha']
    print(f'Current SHA: {current_sha}')
except Exception:
    current_sha = None

push_body = {'message': f'briefing: auto-report {report_date} ({window_label})',
             'content': base64.b64encode(HTML.encode()).decode(), 'branch': 'main'}
if current_sha:
    push_body['sha'] = current_sha

req = urllib.request.Request(sha_url, data=json.dumps(push_body).encode(),
    headers={'Authorization': f'token {GH_TOKEN}',
             'Accept': 'application/vnd.github.v3+json',
             'Content-Type': 'application/json',
             'User-Agent': 'unicity-briefing'}, method='PUT')
with urllib.request.urlopen(req) as r:
    result = json.loads(r.read())
    print(f'Pushed: {result["commit"]["sha"]}')

print('Done. https://unicitynetwork.github.io/briefing/')
