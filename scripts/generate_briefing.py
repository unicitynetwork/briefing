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
        print(f'  search error: {e} | {q[:80]}')
        return []

def gh_search_commits(q, per_page=100):
    """Search commits via the indexed commit search API.
    Requires Accept: application/vnd.github.cloak-preview.
    Works for private repos when authenticated with repo scope.
    Returns [] gracefully on any error so callers degrade cleanly.
    """
    url = 'https://api.github.com/search/commits?q=' + urllib.parse.quote(q) + f'&per_page={per_page}'
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {GH_TOKEN}',
        'Accept': 'application/vnd.github.cloak-preview',
        'User-Agent': 'unicity-briefing'
    })
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read()).get('items', [])
    except Exception as e:
        print(f'  commit search error: {e} | {q[:80]}')
        return []

def gh_graphql(query, variables=None):
    payload = json.dumps({'query': query, 'variables': variables or {}}).encode()
    req = urllib.request.Request('https://api.github.com/graphql', data=payload,
        headers={'Authorization': f'bearer {GH_TOKEN}', 'Content-Type': 'application/json',
                 'User-Agent': 'unicity-briefing'})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f'  graphql error: {e}')
        return {}

def claude(prompt, max_tokens=3000):
    for model in ('claude-sonnet-4-6', 'claude-haiku-4-5-20251001'):
        try:
            payload = json.dumps({'model': model, 'max_tokens': max_tokens,
                'messages': [{'role': 'user', 'content': prompt}]}).encode()
            req = urllib.request.Request('https://api.anthropic.com/v1/messages', data=payload,
                headers={'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01',
                         'content-type': 'application/json'})
            with urllib.request.urlopen(req) as r:
                resp = json.loads(r.read())
            raw = re.sub(r'^```[a-z]*\n?', '', resp['content'][0]['text'].strip()).rstrip('`').strip()
            print(f'Claude OK ({model})')
            return raw
        except Exception as e:
            print(f'Claude error ({model}): {e}')
    return None

ORGS = ['unicity-astrid', 'unicity-sphere', 'unicitynetwork']
ORG_LABELS = {'unicity-astrid': 'Astrid', 'unicity-sphere': 'Sphere', 'unicitynetwork': 'Unicity Network'}
MEMBERS = ['joshuajbouw','MastaP','igmahl','KruGoL','ristik','martti007','jvsteiner',
           'ahtotruu','b3y0urs3lf','jait91','lploom','vrogojin','0xt1mo']
MEMBER_NAMES = {
    'joshuajbouw':'Joshua J. Bouw', 'MastaP':'Pavel Grigorenko',
    'igmahl':'Igor Mahlinovski',    'KruGoL':'Alexander Khrushkov',
    'ristik':'Risto Laanoja',       'martti007':'Martti Marran',
    'jvsteiner':'Jamie Steiner',
}

# ── 3. Merged PRs ─────────────────────────────────────────────────────────────
org_prs = {}; all_prs = []; releases = []; contributors = set(); merged_keys = set()

for org in ORGS:
    prs = gh_search(f'org:{org} is:pr is:merged merged:{date_range}', per_page=100)
    time.sleep(2)
    org_prs[org] = prs; all_prs.extend(prs)
    for pr in prs:
        contributors.add(pr['user']['login'])
        repo = pr['repository_url'].split('/')[-1]
        merged_keys.add((repo, pr['number']))
        m = re.search(r'v\d+\.\d+\.\d+', pr['title'])
        if m and re.search(r'chore:\s*release|release\s+v', pr['title'].lower()):
            releases.append(f'{repo} {m.group()}')

total_merged = len(all_prs)
print(f'Merged PRs: {total_merged}')

# ── 4. Long-standing open PRs — BEFORE involves sweep to avoid rate limit
cutoff = (now - timedelta(days=7)).strftime('%Y-%m-%d')
long_prs = []
for org in ORGS:
    long_prs.extend(gh_search(f'org:{org} is:pr is:open created:<{cutoff}', per_page=100))
    time.sleep(2)
long_prs.sort(key=lambda p: p['created_at'])
print(f'Long-standing open PRs: {len(long_prs)}')

# ── 5. Board fetch — paginated, board_keys = ALL items, boards = non-Done only
BOARD_Q = '''
query($org: String!, $num: Int!, $cursor: String) {
  organization(login: $org) {
    projectV2(number: $num) {
      title
      items(first: 100, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          fieldValues(first: 15) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
          content {
            ... on PullRequest { number title url state mergedAt isDraft repository { name } }
            ... on Issue        { number title url state closedAt          repository { name } }
          }
        }
      }
    }
  }
}'''

boards = {}; board_keys = set(); board_counts = {}
DONE_STATUSES = {'done','closed','complete','completed','shipped'}

for org in ORGS:
    all_nodes = []; cursor = None; page = 0; status_counts = {}
    while True:
        page += 1
        result = gh_graphql(BOARD_Q, {'org': org, 'num': 1, 'cursor': cursor})
        try:
            proj = result['data']['organization']['projectV2']
            pd   = proj['items']
            if page == 1: print(f'  Board {org} "{proj.get("title","")}" : {pd["totalCount"]} items')
            all_nodes.extend(pd['nodes'])
            if not pd['pageInfo']['hasNextPage']: break
            cursor = pd['pageInfo']['endCursor']
        except Exception as e:
            print(f'  Board {org} page {page} failed: {e}'); break

    items_out = []
    for node in all_nodes:
        status = None
        for fv in node.get('fieldValues',{}).get('nodes',[]):
            if fv and 'name' in fv and isinstance(fv.get('field'), dict):
                if 'status' in fv['field'].get('name','').lower():
                    status = fv.get('name'); break
        if status is None:
            for fv in node.get('fieldValues',{}).get('nodes',[]):
                if fv and 'name' in fv and isinstance(fv.get('field'), dict):
                    status = fv.get('name'); break
        c = node.get('content')
        if not c: continue
        repo = c.get('repository',{}).get('name',''); number = c.get('number')
        is_pr = 'isDraft' in c or 'mergedAt' in c
        status = status or 'No Status'
        status_counts[status] = status_counts.get(status, 0) + 1
        if repo and number: board_keys.add((repo, number))
        if status.lower() not in DONE_STATUSES:
            items_out.append({
                'status':    status,
                'type':      'pr' if is_pr else 'issue',
                'number':    number, 'repo': repo,
                'title':     c.get('title',''), 'url': c.get('url',''),
                'state':     c.get('state',''), 'merged_at': c.get('mergedAt'),
                'is_draft':  c.get('isDraft', False),
            })
    boards[org] = items_out; board_counts[org] = status_counts
    print(f'  Board {org}: {len(items_out)} non-Done | {dict(sorted(status_counts.items()))}')

# ── 6. Board issues ───────────────────────────────────────────────────────────
IN_DEV = {'In Dev','In Development','In Progress','In Review','Review','Test','Testing','Blocked','In Prod','Ready','Todo','Backlog'}
board_issues = []

for org, items in boards.items():
    label = ORG_LABELS.get(org, org)
    for item in items:
        repo, num, status, title, url = item['repo'], item['number'], item['status'], item['title'], item['url']
        if item['type'] == 'pr' and status in IN_DEV and (item['state'] == 'MERGED' or item.get('merged_at')):
            board_issues.append({'org': label, 'sev': 'stale',
                'msg': f'PR merged, still \u201c{status}\u201d on board',
                'title': title, 'url': url, 'ref': f'{repo} #{num}'})
        if status == 'No Status':
            board_issues.append({'org': label, 'sev': 'nostatus',
                'msg': 'No Status assigned', 'title': title, 'url': url, 'ref': f'{repo} #{num}'})

for pr in long_prs:
    repo = pr['repository_url'].split('/')[-1]
    if (repo, pr['number']) not in board_keys:
        pr_org = next((o for o in ORGS if f'/{o}/' in pr['repository_url']), '')
        board_issues.append({'org': ORG_LABELS.get(pr_org,'Unknown'), 'sev': 'missing',
            'msg': 'Open PR not tracked on any board',
            'title': pr['title'], 'url': pr['html_url'], 'ref': f'{repo} #{pr["number"]}'})

print(f'Board issues: {len(board_issues)}')

# ── 6b. Blocked items — simple list from board status column only
BLOCKED_STATUSES = {'blocked', 'blocking'}
BOARD_URLS = {
    'unicity-astrid':  'https://github.com/orgs/unicity-astrid/projects/1/views/1',
    'unicity-sphere':  'https://github.com/orgs/unicity-sphere/projects/1/views/1',
    'unicitynetwork':  'https://github.com/orgs/unicitynetwork/projects/1/views/17',
}
blocked_items = []
for org, items in boards.items():
    label = ORG_LABELS.get(org, org)
    for item in items:
        if item['status'].lower() in BLOCKED_STATUSES:
            blocked_items.append({
                'org':       label,
                'board_url': BOARD_URLS.get(org, ''),
                'type':      item['type'],
                'repo':      item['repo'],
                'number':    item['number'],
                'title':     item['title'],
                'url':       item['url'],
                'is_draft':  item.get('is_draft', False),
            })

print(f'Blocked items: {len(blocked_items)}')

# ── 7. involves sweep — 2s sleep per call stays under 30/min
# member_data['commits'] is populated in section 7b below via Search Commits API
member_data = {m: {'authored_merged':[], 'authored_open':[], 'involved':[], 'commits':[]} for m in MEMBERS}
seen_per_member = {m: set() for m in MEMBERS}

for member in MEMBERS:
    for org in ORGS:
        for kind in ('pr', 'issue'):
            items = gh_search(f'involves:{member} updated:>={window_start} is:{kind} org:{org}')
            time.sleep(2)
            for item in items:
                uid = (item['number'], item['repository_url'])
                if uid in seen_per_member[member]: continue
                seen_per_member[member].add(uid)
                is_author = item['user']['login'].lower() == member.lower()
                is_merged = bool(item.get('pull_request',{}).get('merged_at'))
                is_open   = item['state'] == 'open'
                if is_author and is_merged:   member_data[member]['authored_merged'].append(item)
                elif is_author and is_open:   member_data[member]['authored_open'].append(item)
                else:                         member_data[member]['involved'].append(item)

print('Involves sweep done')

# ── 7b. Commit search — direct commits per member via Search Commits API ──────
# Uses /search/commits (Accept: application/vnd.github.cloak-preview).
# This is a proper indexed search that works for private repos with token scope.
# One query per member per org (3 orgs × 13 members = 39 calls at 1s each).
# Degrades cleanly: if search returns nothing the member card is unchanged.
print('Commit search...')
for member in MEMBERS:
    for org in ORGS:
        items = gh_search_commits(
            f'author:{member} org:{org} author-date:{window_start}..{window_end}',
            per_page=100
        )
        time.sleep(1)
        for c in items:
            repo = c.get('repository', {}).get('name', '')
            sha  = (c.get('sha') or '')[:7]
            msg  = (c.get('commit', {}).get('message', '') or '').split('\n')[0][:120]
            member_data[member]['commits'].append({'repo': repo, 'sha': sha, 'msg': msg, 'org': org})
    n = len(member_data[member]['commits'])
    if n:
        contributors.add(member)
        print(f'  {member}: {n} commits')
print('Commit search done')

# ── 8. Claude call 1 — thematic summaries ─────────────────────────────────────
def pr_lines(prs, limit=60):
    return '\n'.join(
        f'- [{pr["repository_url"].split("/")[-1]}] #{pr["number"]} "{pr["title"]}" by @{pr["user"]["login"]}'
        for pr in prs[:limit]
    )

def commit_lines_for_org(org, limit=30):
    """Aggregate commit lines for an org from member_data commits."""
    lines = []
    for member in MEMBERS:
        for c in member_data[member]['commits']:
            if c.get('org') == org:
                lines.append(f'- [{c["repo"]}] {c["sha"]} "{c["msg"]}" by @{member}')
    return '\n'.join(lines[:limit]) if lines else ''

def org_block(org):
    parts = []
    prs = org_prs.get(org, [])
    if prs: parts.append(f'Merged PRs ({len(prs)}):\n{pr_lines(prs)}')
    clines = commit_lines_for_org(org)
    if clines: parts.append(f'Direct commits (no PR):\n{clines}')
    return '\n\n'.join(parts) if parts else 'No activity'

theme_prompt = f"""You are writing the daily engineering briefing for the Unicity project.
Period: {window_label} | PRs merged: {total_merged} | Releases: {', '.join(releases) or 'none'}

Activity includes merged PRs AND direct commits to branches (commits not part of a PR).

=== unicity-astrid ===
{org_block('unicity-astrid')}

=== unicity-sphere ===
{org_block('unicity-sphere')}

=== unicitynetwork (includes website, ssl-manager, uniquake and all other repos) ===
{org_block('unicitynetwork')}

For each org with any activity (PRs or direct commits), group into 1-4 meaningful themes.
Each theme: title (punchy, max 10 words, name actual capability), repos (comma-sep), description (3-5 sentences, specific: name PR numbers, commit SHAs or messages, branch work, what changed, why it matters).

Respond ONLY with valid JSON no fences:
{{"astrid":[{{"title":"...","repos":"...","description":"..."}}],"sphere":[...],"network":[...]}}"""

raw = claude(theme_prompt)
try:    themes = json.loads(raw)
except: themes = {'astrid':[], 'sphere':[], 'network':[]}

# ── 9. Claude call 2 — member narratives + needs attention ────────────────────
def member_summary_lines():
    lines = []
    for member in MEMBERS:
        d = member_data[member]
        merged  = d['authored_merged']
        opened  = d['authored_open']
        inv     = d['involved']
        commits = d['commits']
        if not any([merged, opened, inv, commits]): continue
        merged_items = ', '.join(f'{pr["repository_url"].split("/")[-1]} #{pr["number"]} "{pr["title"]}"' for pr in merged[:15])
        open_items   = ', '.join(f'{pr["repository_url"].split("/")[-1]} #{pr["number"]} "{pr["title"]}"' for pr in opened[:6])
        inv_items    = ', '.join(f'{it["repository_url"].split("/")[-1]} #{it["number"]} "{it["title"]}"' for it in inv[:6])
        lines.append(f'@{member} ({MEMBER_NAMES.get(member, "")}):')
        if merged_items: lines.append(f'  merged: {merged_items}')
        if open_items:   lines.append(f'  open PRs: {open_items}')
        if inv_items:    lines.append(f'  involved: {inv_items}')
        # Commits: group by repo, show first message + count
        if commits:
            by_repo = {}
            for c in commits:
                by_repo.setdefault(c['repo'], []).append(c['msg'])
            commit_parts = []
            for repo, msgs in list(by_repo.items())[:5]:
                n = len(msgs)
                commit_parts.append(
                    f'[{repo}] {n} commit{"s" if n!=1 else ""}: {msgs[0]}' +
                    (f' (+{n-1} more)' if n > 1 else '')
                )
            lines.append(f'  direct commits: {"; ".join(commit_parts)}')
    return '\n'.join(lines)

def long_pr_summary():
    return '\n'.join(
        f'- {pr["repository_url"].split("/")[-1]} #{pr["number"]} "{pr["title"]}" by @{pr["user"]["login"]}'
        f' — open {(now - datetime.fromisoformat(pr["created_at"].replace("Z","+00:00"))).days} days'
        for pr in long_prs[:15]
    )

narrative_prompt = f"""You are writing the daily engineering briefing for the Unicity project. Period: {window_label}.

TEAM ACTIVITY THIS WINDOW (PRs + direct commits to branches):
{member_summary_lines() or 'No activity'}

LONG-STANDING OPEN PRs (>7 days):
{long_pr_summary() or 'None'}

BOARD ISSUES:
{chr(10).join(f'- [{i["sev"].upper()}] {i["ref"]} ({i["org"]}): {i["title"]} — {i["msg"]}' for i in board_issues[:20]) or 'None'}

Task 1 — For each active team member write a narrative (2-3 sentences max, specific: mention PR numbers, repo names, what they merged, what's open, what they reviewed, AND any notable direct commits to branches). Also write 2-4 short tags.

Example when member has both PRs and commits: "Merged sphere-sdk #99 (Nostr reconnect fix) and pushed 13 commits to website covering homepage redesign spec and responsive layout. Has sphere-quest #23 open."
Example when member has only commits: "Pushed 22 commits across sdk-rust and capsule-system migrating all capsule repos to SDK 0.6.0 (wasmtime Component Model). No open PRs this window."
Example tags: ["13 commits", "website redesign", "sdk migration"]

Task 2 — Write 3-6 "Needs attention" items from long PRs and board issues.
Badge options: "review needed", "decision needed", "close or revive", "assign reviewer", "unblock", "critical"
Badge colors: "purple", "amber", "blue", "red", "green"

Respond ONLY with valid JSON no fences:
{{
  "member_narratives": {{"username": {{"detail": "...", "tags": ["tag1", "tag2"]}}}},
  "needs_attention": [{{"title": "...", "badge": "...", "badge_color": "amber", "detail": ""}}]
}}"""

raw2 = claude(narrative_prompt, max_tokens=4000)
try:
    enriched = json.loads(raw2)
    member_narratives = enriched.get('member_narratives', {})
    needs_attention   = enriched.get('needs_attention', [])
    print(f'Enrichment OK: {len(member_narratives)} narratives, {len(needs_attention)} attention items')
except Exception as e:
    print(f'Enrichment parse error: {e}')
    member_narratives = {}; needs_attention = []

# ── 10. HTML helpers ──────────────────────────────────────────────────────────
def esc(s):
    return str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

def age_days(ts):
    return (now - datetime.fromisoformat(ts.replace('Z','+00:00'))).days

def age_class(ts):
    d = age_days(ts)
    if d >= 90: return 'age-critical'
    if d >= 30: return 'age-high'
    if d >= 14: return 'age-medium'
    return 'age-low'

def pr_dot_class(title):
    t = title.lower()
    if re.search(r'chore:\s*release|release\s+v\d', t): return 'tl-rel'
    if t.startswith('fix') or t.startswith('revert'):   return 'tl-fix'
    if t.startswith('feat') or t.startswith('add '):    return 'tl-feat'
    if t.startswith('chore') or t.startswith('ci') or t.startswith('bump'): return 'tl-chore'
    if t.startswith('refactor'): return 'tl-refactor'
    return 'tl-done'

def fmt_time(ts):
    try:
        dt = datetime.fromisoformat(ts.replace('Z','+00:00')) + timedelta(hours=2)
        return dt.strftime('%a %H:%M')
    except: return ''

def build_timeline(prs):
    if not prs: return ''
    def get_ts(pr):
        return pr.get('pull_request',{}).get('merged_at') or pr.get('closed_at') or pr.get('updated_at') or ''
    sorted_prs = sorted(prs, key=get_ts)
    groups, grp, cur = [], [], None
    for pr in sorted_prs:
        ts_str = get_ts(pr)
        if not ts_str: continue
        try: ts = datetime.fromisoformat(ts_str.replace('Z','+00:00'))
        except: continue
        if cur is None or (ts - cur).total_seconds() <= 300:
            grp.append((ts, pr)); cur = cur or ts
        else:
            if grp: groups.append(grp)
            grp, cur = [(ts, pr)], ts
    if grp: groups.append(grp)
    out = '<div class="timeline">'
    for group in groups:
        times = [t for t,_ in group]
        t_start = fmt_time(times[0].strftime('%Y-%m-%dT%H:%M:%SZ'))
        if len(times) > 1:
            t_end = fmt_time(times[-1].strftime('%Y-%m-%dT%H:%M:%SZ'))
            t_label = t_start if t_start == t_end else f'{t_start.split(" ")[0]} {t_start.split(" ")[1]}\u2013{t_end.split(" ")[1]}'
        else:
            t_label = t_start
        prs_g = [pr for _,pr in group]
        if len(prs_g) == 1:
            pr = prs_g[0]; repo = pr['repository_url'].split('/')[-1]
            label = f'<a href="{esc(pr["html_url"])}" class="tl-link">{esc(repo)} #{pr["number"]}</a> \u2014 {esc(pr["title"])}'
            out += f'<div class="tl-item {pr_dot_class(pr["title"])}"><span class="tl-time">{esc(t_label)}</span><span class="tl-label">{label}</span></div>'
        else:
            parts = [f'<a href="{esc(pr["html_url"])}" class="tl-link">{esc(pr["repository_url"].split("/")[-1])} #{pr["number"]}</a> ({esc(pr["title"])})' for pr in prs_g]
            out += f'<div class="tl-item {pr_dot_class(prs_g[0]["title"])}"><span class="tl-time">{esc(t_label)}</span><span class="tl-label">{" &middot; ".join(parts)}</span></div>'
    return out + '</div>'

def theme_cards(tlist, color):
    if not tlist:
        return '<p style="font-size:13px;color:#888;padding:4px 0">No activity this period.</p>'
    out = ''
    for t in tlist:
        out += f'''<div class="event-row"><div class="event-dot" style="background:{color}"></div>
  <div class="event-body">
    <div class="event-title">{esc(t.get("title",""))}</div>
    <div class="event-detail">{esc(t.get("description",""))}</div>
    <div class="event-meta"><code>{esc(t.get("repos",""))}</code></div>
  </div></div>'''
    return out

def org_card(org_key, badge_class, border_color, dot_color, theme_key, n_prs):
    prs   = org_prs.get(org_key, [])
    tlist = themes.get(theme_key, [])
    label = ORG_LABELS.get(org_key, org_key)
    # Count direct commits for this org
    n_commits = sum(len(member_data[m]['commits']) for m in MEMBERS
                    if any(c.get('org') == org_key for c in member_data[m]['commits']))
    sub = f'{n_prs} PRs merged'
    if n_commits: sub += f' \u00b7 {n_commits} direct commit{"s" if n_commits!=1 else ""}'
    html  = f'<div class="card" style="border-color:{border_color}">'
    html += f'<div class="org-header"><span class="badge {badge_class}">{esc(label)}</span>'
    html += f'<span style="font-size:13px;color:#666">{sub}</span></div>'
    html += theme_cards(tlist, dot_color)
    if len(prs) >= 5:
        html += f'<p class="section-title" style="margin-top:14px">Timeline \u2014 {esc(window_label)}</p>'
        html += build_timeline(prs)
    return html + '</div>'

def board_status_line(org):
    counts = board_counts.get(org, {})
    if not counts: return ''
    done_key = next((k for k in counts if k.lower() in DONE_STATUSES), None)
    done_val = counts.get(done_key, 0) if done_key else 0
    parts = [f'{k} {v}' for k,v in sorted(counts.items()) if k.lower() not in DONE_STATUSES]
    if done_val: parts.append(f'Done {done_val}')
    return ' \u00b7 '.join(parts)

def render_board_section():
    if not board_issues and not any(board_counts.values()):
        return '<p style="font-size:13px;color:#888;padding:8px 0">No board issues detected \u2014 all active items correctly tracked.</p>'
    SEV_LABELS = {
        'stale':    ('\u26a0 STALE STATUS', '#D97706'),
        'nostatus': ('\u2298 NO STATUS',    '#3C3489'),
        'missing':  ('\u2717 NOT ON BOARD', '#791F1F'),
    }
    CHIP_CLASS = {'stale': 'chip-stale', 'nostatus': 'chip-nostatus', 'missing': 'chip-miss'}
    org_order = ['Astrid', 'Sphere', 'Unicity Network']
    by_org = {}
    for issue in board_issues[:40]:
        by_org.setdefault(issue['org'], []).append(issue)
    org_keys  = {'Astrid': 'unicity-astrid', 'Sphere': 'unicity-sphere', 'Unicity Network': 'unicitynetwork'}
    board_urls = {
        'Astrid':          'https://github.com/orgs/unicity-astrid/projects/1/views/1',
        'Sphere':          'https://github.com/orgs/unicity-sphere/projects/1/views/1',
        'Unicity Network': 'https://github.com/orgs/unicitynetwork/projects/1/views/17',
    }
    out = ''
    for org_label in org_order:
        org_key     = org_keys.get(org_label, '')
        status_line = board_status_line(org_key)
        board_url   = board_urls.get(org_label, '')
        org_issues  = by_org.get(org_label, [])
        link_html   = f' <a href="{board_url}" style="font-size:11px;color:#378ADD;font-family:\'SF Mono\',monospace;text-decoration:none">board \u2197</a>' if board_url else ''
        status_html = f'<span style="font-size:11px;color:#888;margin-left:8px">{status_line}</span>' if status_line else ''
        out += f'<p class="section-title">{esc(org_label)}{link_html}{status_html}</p>'
        if not org_issues:
            out += '<p style="font-size:12px;color:#888;margin-bottom:10px">\u2713 No issues detected.</p>'
            continue
        by_sev = {}
        for issue in org_issues:
            by_sev.setdefault(issue['sev'], []).append(issue)
        for sev in ('stale', 'nostatus', 'missing'):
            items = by_sev.get(sev, [])
            if not items: continue
            label_text, label_color = SEV_LABELS[sev]
            chip_cls = CHIP_CLASS[sev]
            out += f'<div class="board-wrap"><div class="board-head"><span style="font-size:11px;font-weight:700;color:{label_color}">{label_text}</span></div>'
            for issue in items:
                out += f'''<div class="board-row">
  <span class="chip {chip_cls}">{sev.upper()}</span>
  <div class="board-body">
    <div class="board-title"><a href="{esc(issue['url'])}" class="pr-link">{esc(issue['title'])}</a></div>
    <div class="board-detail"><code>{esc(issue['ref'])}</code> \u00b7 {esc(issue['msg'])}</div>
  </div></div>'''
            out += '</div>'
    return out

def render_blocked_items():
    if not blocked_items: return ''
    org_order = ['Astrid', 'Sphere', 'Unicity Network']
    by_org = {}
    for item in blocked_items:
        by_org.setdefault(item['org'], []).append(item)
    out = ''
    for org_label in org_order:
        items = by_org.get(org_label, [])
        if not items: continue
        board_url = items[0]['board_url']
        link_html = f' <a href="{board_url}" style="font-size:11px;color:#378ADD;font-family:\'SF Mono\',monospace;text-decoration:none">board \u2197</a>' if board_url else ''
        out += f'<p class="section-title">{esc(org_label)}{link_html}</p>'
        out += '<div class="board-wrap"><div class="board-head"><span style="font-size:11px;font-weight:700;color:#791F1F">\u26d4 BLOCKED</span></div>'
        for item in items:
            kind  = 'PR' if item['type'] == 'pr' else 'Issue'
            draft = ' <span class="draft-tag">Draft</span>' if item.get('is_draft') else ''
            out += f'''<div class="board-row">
  <span class="chip chip-blocked">{kind}</span>
  <div class="board-body">
    <div class="board-title"><a href="{esc(item['url'])}" class="pr-link">{esc(item['title'])}</a>{draft}</div>
    <div class="board-detail"><code>{esc(item['repo'])} #{item['number']}</code></div>
  </div></div>'''
        out += '</div>'
    return out

def render_needs_attention():
    if not needs_attention: return ''
    BADGE_COLORS = {
        'purple': 'background:#EEEDFE;color:#3C3489', 'amber': 'background:#FAEEDA;color:#633806',
        'blue':   'background:#E6F1FB;color:#0C447C', 'red':   'background:#FCEBEB;color:#791F1F',
        'green':  'background:#E1F5EE;color:#085041',
    }
    DOT_COLORS = {'purple':'#7F77DD','amber':'#EF9F27','blue':'#378ADD','red':'#E24B4A','green':'#1D9E75'}
    out = ''
    for item in needs_attention[:6]:
        color     = item.get('badge_color', 'amber')
        badge_css = BADGE_COLORS.get(color, BADGE_COLORS['amber'])
        dot_color = DOT_COLORS.get(color, '#EF9F27')
        detail    = esc(item.get('detail',''))
        out += f'''<div class="event-row">
  <div class="event-dot" style="background:{dot_color}"></div>
  <div class="event-body">
    <div class="event-title">{esc(item.get("title",""))}</div>
    {f'<div class="event-detail">{detail}</div>' if detail else ''}
    <div class="event-meta"><span class="badge" style="{badge_css}">{esc(item.get("badge",""))}</span></div>
  </div></div>'''
    return out

def render_member_cards():
    active, inactive = [], []
    for member in MEMBERS:
        d = member_data[member]
        # Active if any PR activity OR any direct commits
        if any([d['authored_merged'], d['authored_open'], d['involved'], d['commits']]):
            active.append(member)
        else:
            inactive.append(member)
    out = '<div class="member-grid">'
    for member in active:
        d    = member_data[member]; name = MEMBER_NAMES.get(member, '')
        narr = member_narratives.get(member, {})
        detail = narr.get('detail', ''); tags = narr.get('tags', [])
        # Fallback if Claude didn't produce a narrative
        if not detail:
            parts = []
            n_m = len(d['authored_merged']); n_o = len(d['authored_open']); n_i = len(d['involved'])
            n_c = len(d['commits'])
            if n_m: parts.append(f'{n_m} PR{"s" if n_m!=1 else ""} merged')
            if n_o: parts.append(f'{n_o} open PR{"s" if n_o!=1 else ""}')
            if n_i: parts.append(f'involved in {n_i} item{"s" if n_i!=1 else ""}')
            if n_c: parts.append(f'{n_c} direct commit{"s" if n_c!=1 else ""}')
            detail = ', '.join(parts)
        # Fallback tags: collect repos from all activity types
        if not tags:
            repos = set()
            for item in d['authored_merged'] + d['authored_open'] + d['involved']:
                r = item.get('repository_url','').split('/')[-1]
                if r: repos.add(r)
            for c in d['commits']:
                if c.get('repo'): repos.add(c['repo'])
            tags = sorted(repos)[:4]
        tags_html = ''.join(f'<span class="tag">{esc(t)}</span>' for t in tags[:5])
        out += f'''<div class="member-card">
  <div class="mc-name">{esc(name) + " " if name else ""}<span class="mc-handle">@{esc(member)}</span></div>
  <div class="mc-detail">{esc(detail)}</div>
  <div style="margin-top:6px">{tags_html}</div>
</div>'''
    out += '</div>'
    if inactive:
        quiet = ', '.join(f'@{m}' for m in inactive)
        out += f'<div style="margin-top:12px;padding-top:10px;border-top:0.5px solid rgba(0,0,0,0.08)"><p style="font-size:11px;font-weight:500;color:#666;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px">No activity this window</p><p style="font-size:11.5px;color:#888;line-height:1.7">{esc(quiet)}</p></div>'
    out += '''<div class="method-note"><strong>Sweep method (permanent):</strong> Each report runs
<code>involves:USERNAME</code> for every team member (PRs, issues, reviews, comments)
plus <code>/search/commits</code> per member per org to catch direct branch commits without a PR.</div>'''
    return out

def long_pr_rows(prs):
    if not prs:
        return '<tr><td colspan="3" style="padding:12px;font-size:13px;color:#888;text-align:center">No open PRs older than 7 days.</td></tr>'
    rows = ''
    for pr in prs[:25]:
        repo  = pr['repository_url'].split('/')[-1]
        days  = age_days(pr['created_at'])
        draft = '<span class="draft-tag">Draft</span>' if pr.get('draft') else ''
        rows += f'''<tr>
  <td><span class="age-pill {age_class(pr["created_at"])}">{days}d</span></td>
  <td><a href="{esc(pr["html_url"])}" class="pr-link">{esc(pr["title"])}</a>{draft}
    <div class="pr-repo">{esc(repo)} #{pr["number"]}</div></td>
  <td class="pr-author">@{esc(pr["user"]["login"])}</td></tr>'''
    return rows

# ── 11. Build HTML ────────────────────────────────────────────────────────────
n_astrid  = len(org_prs.get('unicity-astrid', []))
n_sphere  = len(org_prs.get('unicity-sphere', []))
n_network = len(org_prs.get('unicitynetwork', []))
rel_str   = ' &middot; '.join(f'<span class="badge badge-release">{esc(r)}</span>' for r in releases)
boards_ok = any(board_counts.values())
needs_html   = render_needs_attention()
blocked_html = render_blocked_items()

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
.event-detail{font-size:12px;color:#666;line-height:1.6;margin-bottom:3px}
.event-meta{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin-top:4px}
code{font-family:'SF Mono',Monaco,monospace;font-size:11.5px;background:#f5f4f0;padding:1px 5px;border-radius:4px}
.header{margin-bottom:20px;padding-bottom:14px;border-bottom:0.5px solid rgba(0,0,0,0.1)}
.header h2{font-size:18px;font-weight:500}
.header p{font-size:13px;color:#666;margin-top:3px}
.header-meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:6px}
.window-badge{display:inline-block;font-size:11px;font-weight:500;padding:2px 8px;border-radius:6px;background:#E6F1FB;color:#0C447C}
.updated-badge{display:inline-block;font-size:11px;font-weight:500;padding:2px 8px;border-radius:6px;background:#F1EFE8;color:#444}
.org-header{display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.section-title{font-size:11px;font-weight:500;color:#666;text-transform:uppercase;letter-spacing:.05em;margin:14px 0 6px}
.section-title:first-child{margin-top:0}
.timeline{position:relative;padding-left:14px}
.timeline::before{content:'';position:absolute;left:3px;top:6px;bottom:6px;width:1px;background:rgba(0,0,0,0.1)}
.tl-item{position:relative;padding:3px 0 4px 12px;font-size:12px;color:#444;line-height:1.55}
.tl-item::before{content:'';position:absolute;left:-3px;top:9px;width:7px;height:7px;border-radius:50%;background:#ccc}
.tl-done::before,.tl-feat::before{background:#1D9E75}
.tl-fix::before{background:#E24B4A}.tl-rel::before{background:#D97706}
.tl-chore::before{background:#888}.tl-refactor::before{background:#378ADD}
.tl-time{font-family:'SF Mono',Monaco,monospace;font-size:10.5px;color:#aaa;margin-right:8px;min-width:80px;display:inline-block}
.tl-label{color:#1a1a18}
.tl-link{color:#1a1a18;text-decoration:none;font-weight:500}
.tl-link:hover{text-decoration:underline}
.board-wrap{border:0.5px solid rgba(0,0,0,0.1);border-radius:8px;overflow:hidden;margin-bottom:8px}
.board-head{background:#f5f4f0;padding:8px 12px;border-bottom:0.5px solid rgba(0,0,0,0.08)}
.board-row{display:flex;gap:10px;align-items:flex-start;padding:8px 12px;border-bottom:0.5px solid rgba(0,0,0,0.06);font-size:12px}
.board-row:last-child{border-bottom:none}
.chip{display:inline-block;font-size:10px;font-weight:600;padding:2px 7px;border-radius:4px;flex-shrink:0;min-width:72px;text-align:center;margin-top:1px}
.chip-stale{background:#FAEEDA;color:#633806}.chip-nostatus{background:#EEEDFE;color:#3C3489}
.chip-miss{background:#FCEBEB;color:#791F1F}.chip-blocked{background:#FCEBEB;color:#791F1F}
.board-body{flex:1}
.board-title{font-size:12px;color:#1a1a18;line-height:1.4}
.board-detail{font-size:11.5px;color:#666;margin-top:2px}
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
.member-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}
.member-card{background:#f5f4f0;border-radius:8px;padding:10px 12px;border-left:3px solid #1D9E75}
.mc-name{font-size:13px;font-weight:500}
.mc-handle{font-family:'SF Mono',Monaco,monospace;font-size:11px;color:#888;margin-left:4px}
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
    <span class="window-badge">Coverage: {esc(window_label)} &middot; PRs + direct commits (involves + commit search)</span>
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

{org_card('unicity-astrid','badge-purple','#7F77DD','#7F77DD','astrid', n_astrid)}
{org_card('unicity-sphere','badge-teal',  '#1D9E75','#1D9E75','sphere', n_sphere)}
{org_card('unicitynetwork','badge-blue',  '#378ADD','#378ADD','network',n_network)}

<div class="card" style="border-color:#E24B4A">
  <div class="org-header">
    <span class="badge" style="background:#FCEBEB;color:#791F1F">Project board comparison</span>
    <span style="font-size:12px;color:#666">Stale statuses &middot; No Status &middot; open PRs not tracked{"" if boards_ok else " &mdash; board fetch failed"}</span>
  </div>
  {render_board_section()}
</div>

{"<div class='card' style='border-color:#E24B4A'><div class='org-header'><span class='badge' style='background:#FCEBEB;color:#791F1F'>&#x26D4; Blocked items</span><span style='font-size:12px;color:#666'>All items in Blocked column across all project boards</span></div>" + blocked_html + "</div>" if blocked_html else ""}

{"<div class='card' style='border-color:#EF9F27'><div class='org-header'><span class='badge' style='background:#FAEEDA;color:#633806'>Needs attention</span></div>" + needs_html + "</div>" if needs_html else ""}

<div class="card" style="border-color:#1D9E75">
  <div class="org-header"><span class="badge badge-teal">Team activity</span><span style="font-size:12px;color:#666">All members &mdash; PRs, reviews, comments + direct commits</span></div>
  {render_member_cards()}
</div>

<div class="card" style="border-color:#E24B4A">
  <div class="org-header"><span class="badge" style="background:#FCEBEB;color:#791F1F">Long-standing open PRs</span><span style="font-size:12px;color:#666">All open PRs older than 7 days &mdash; sorted oldest first</span></div>
  <div style="overflow-x:auto"><table class="pr-table">
    <thead><tr><th>Age</th><th>PR</th><th>Author</th></tr></thead>
    <tbody>{long_pr_rows(long_prs)}</tbody>
  </table></div>
</div>

<p class="footer-note">ristik/ndsmt-experiments commits require manual check</p>
</div></body></html>'''

print(f'HTML built: {len(HTML)} chars')

# ── 12. Push index.html ───────────────────────────────────────────────────────
sha_url = 'https://api.github.com/repos/unicitynetwork/briefing/contents/index.html'
req = urllib.request.Request(sha_url, headers={'Authorization': f'token {GH_TOKEN}',
    'Accept': 'application/vnd.github.v3+json', 'User-Agent': 'unicity-briefing'})
try:
    with urllib.request.urlopen(req) as r:
        current_sha = json.loads(r.read())['sha']
    print(f'Current SHA: {current_sha}')
except: current_sha = None

push_body = {'message': f'briefing: auto-report {report_date} ({window_label})',
             'content': base64.b64encode(HTML.encode()).decode(), 'branch': 'main'}
if current_sha: push_body['sha'] = current_sha

req = urllib.request.Request(sha_url, data=json.dumps(push_body).encode(),
    headers={'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github.v3+json',
             'Content-Type': 'application/json', 'User-Agent': 'unicity-briefing'}, method='PUT')
with urllib.request.urlopen(req) as r:
    result = json.loads(r.read())
    print(f'Pushed: {result["commit"]["sha"]}')

print('Done. https://unicitynetwork.github.io/briefing/')
