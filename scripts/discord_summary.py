import urllib.request, urllib.parse, json, os, re, time
from datetime import datetime, timedelta, timezone

GH_TOKEN        = os.environ['GH_TOKEN'].strip()
DISCORD_WEBHOOK = os.environ['DISCORD_WEBHOOK'].strip()
ANTHROPIC_KEY   = os.environ['ANTHROPIC_API_KEY'].strip()

print(f'Webhook URL length: {len(DISCORD_WEBHOOK)}')
print(f'Anthropic key length: {len(ANTHROPIC_KEY)} (first 8 chars: {ANTHROPIC_KEY[:8]})')

now       = datetime.now(timezone.utc)
yesterday = now - timedelta(days=1)
date_str  = yesterday.strftime('%Y-%m-%d')
date_disp = yesterday.strftime('%A, %-d %B %Y')

def discord_post(payload_dict):
    data = json.dumps(payload_dict).encode()
    print(f'Posting {len(data)} bytes to Discord...')
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=data,
        headers={
            'Content-Type': 'application/json',
            'User-Agent': 'DiscordBot (https://github.com/unicitynetwork/briefing, 1.0)'
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req) as r:
            print(f'Discord OK: {r.status}')
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        print(f'DISCORD ERROR {e.code}: {body}')
        raise

def gh_search(q):
    url = 'https://api.github.com/search/issues?q=' + urllib.parse.quote(q) + '&per_page=50'
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {GH_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'unicity-briefing'
    })
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())['items']

def gh_search_commits(q, per_page=100):
    """Search commits via indexed commit search API (Accept: cloak-preview).
    Works for private repos with repo-scoped token. Returns [] on any error.
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

# Three project areas with their orgs and display names
AREAS = [
    ('astrid',         ['unicity-astrid'],  'Astrid'),
    ('sphere',         ['unicity-sphere'],  'Sphere'),
    ('unicitynetwork', ['unicitynetwork'],  'Unicity Network'),
]

# ── 1. Fetch merged PRs ───────────────────────────────────────────────────────
area_prs     = {}
all_prs      = []
releases     = []
contributors = set()

for area_key, orgs, _ in AREAS:
    area_prs[area_key] = []
    for org in orgs:
        prs = gh_search(f'org:{org} is:pr is:merged merged:{date_str}')
        area_prs[area_key].extend(prs)
        all_prs.extend(prs)
        for pr in prs:
            contributors.add(pr['user']['login'])
            m = re.search(r'v\d+\.\d+\.\d+', pr['title'])
            t = pr['title'].lower()
            if m and ('release' in t or 'chore: release' in t):
                repo = pr['repository_url'].split('/')[-1]
                releases.append(f'{repo} {m.group()}')

total_prs = len(all_prs)
print(f'Found {total_prs} PRs merged on {date_str}')

# ── 2. Fetch direct commits per org (Search Commits API) ─────────────────────
area_commits = {}
total_commits = 0

for area_key, orgs, _ in AREAS:
    area_commits[area_key] = []
    for org in orgs:
        items = gh_search_commits(f'org:{org} author-date:{date_str}', per_page=100)
        time.sleep(1)
        for c in items:
            author_login = (c.get('author') or {}).get('login', '') or \
                           (c.get('commit', {}).get('author', {}) or {}).get('name', '')
            repo = c.get('repository', {}).get('name', '')
            msg  = (c.get('commit', {}).get('message', '') or '').split('\n')[0][:120]
            area_commits[area_key].append({'repo': repo, 'author': author_login, 'msg': msg})
            if author_login: contributors.add(author_login)
    total_commits += len(area_commits[area_key])
    if area_commits[area_key]:
        print(f'  {area_key}: {len(area_commits[area_key])} direct commits')

print(f'Total direct commits: {total_commits}')

# ── 3. Check for activity ─────────────────────────────────────────────────────
if total_prs == 0 and total_commits == 0:
    discord_post({'content': f'No activity on {date_disp}.', 'username': 'Unicity Briefing'})
    exit(0)

# ── 4. Build per-area text for Claude ────────────────────────────────────────
def build_area_text(area_key, label):
    parts = []
    prs = area_prs[area_key]
    if prs:
        pr_lines = []
        for pr in prs:
            repo = pr['repository_url'].split('/')[-1]
            body = (pr.get('body') or '')[:150].replace('\n', ' ')
            pr_lines.append(f'- [{repo}] #{pr["number"]} "{pr["title"]}" by @{pr["user"]["login"]} | {body}')
        parts.append(f'Merged PRs ({len(prs)}):\n' + '\n'.join(pr_lines))
    commits = area_commits[area_key]
    if commits:
        # Group by repo for conciseness
        by_repo = {}
        for c in commits:
            by_repo.setdefault(c['repo'], []).append(f'{c["msg"]} (by {c["author"]})')
        commit_lines = []
        for repo, msgs in list(by_repo.items())[:10]:
            n = len(msgs)
            commit_lines.append(f'- [{repo}] {n} commit{"s" if n!=1 else ""}: {msgs[0]}' +
                                 (f' (+{n-1} more)' if n > 1 else ''))
        parts.append(f'Direct commits to branches ({len(commits)} total):\n' + '\n'.join(commit_lines))
    return '\n\n'.join(parts) if parts else 'No activity'

area_sections = []
for area_key, orgs, label in AREAS:
    text = build_area_text(area_key, label)
    if text != 'No activity':
        n_prs     = len(area_prs[area_key])
        n_commits = len(area_commits[area_key])
        area_sections.append(f'=== {label} ({n_prs} PRs, {n_commits} direct commits) ===\n{text}')

activity_text = '\n\n'.join(area_sections)

# ── 5. Call Anthropic API ─────────────────────────────────────────────────────
header_parts = []
if total_prs:    header_parts.append(f'{total_prs} PR{"s" if total_prs!=1 else ""} merged')
if total_commits: header_parts.append(f'{total_commits} direct commit{"s" if total_commits!=1 else ""}')
if releases:     header_parts.append(', '.join(releases))
header_parts.append(f'{len(contributors)} contributor{"s" if len(contributors)!=1 else ""}')

prompt = f"""You are writing the daily engineering executive summary for the Unicity project.
Date: {date_disp}
Activity: {' | '.join(header_parts)}

Activity is grouped by project area. Each area may have merged PRs and/or direct commits to branches (work not yet in a PR).

{activity_text}

Write a summary with one section per project area that had any activity (PRs or direct commits).
Each section has:
- "area": the project area name exactly as shown (Astrid, Sphere, or Unicity Network)
- "pr_count": number of merged PRs (0 if none)
- "themes": array of 1-3 themes covering ALL activity, each with:
  - "title": short punchy title, max 8 words, plain text
  - "repos": comma-separated repo names involved
  - "description": 2-3 plain English sentences. Cover both merged PRs and notable direct commit work. Mention repo names and what the commits/PRs actually do.

Respond ONLY with a valid JSON array, no markdown fences, no preamble:
[
  {{
    "area": "Astrid",
    "pr_count": 0,
    "themes": [
      {{"title": "...", "repos": "capsule-system, sdk-rust", "description": "..."}}
    ]
  }},
  ...
]

Rules:
- Include areas with direct commits even if pr_count is 0
- Skip pure chore/bump commits unless they represent a meaningful milestone
- Max 3 themes per area
- Title: max 60 chars, no special characters
- Description: max 300 chars, plain text, no backticks, no asterisks"""

print('Calling Anthropic API...')
payload = json.dumps({
    'model': 'claude-haiku-4-5-20251001',
    'max_tokens': 1500,
    'messages': [{'role': 'user', 'content': prompt}]
}).encode()

req = urllib.request.Request(
    'https://api.anthropic.com/v1/messages',
    data=payload,
    headers={
        'x-api-key': ANTHROPIC_KEY,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json'
    }
)
try:
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read())
        print('Anthropic API call succeeded')
except urllib.error.HTTPError as e:
    body = e.read().decode('utf-8', errors='replace')
    print(f'ANTHROPIC ERROR {e.code}: {body}')
    raise

raw = resp['content'][0]['text'].strip()
raw = re.sub(r'^```[a-z]*\n?', '', raw).rstrip('`').strip()
print(f'Claude raw output (first 300): {raw[:300]}')
areas_out = json.loads(raw)
print(f'Areas: {len(areas_out)}')

# ── 6. Build Discord embeds ───────────────────────────────────────────────────
def cap(s, n):
    s = str(s).strip()
    return s if len(s) <= n else s[:n-1] + '...'

AREA_COLORS = {
    'Astrid':          8353757,  # purple  0x7F77DD
    'Sphere':          1941621,  # teal    0x1D9E75
    'Unicity Network': 3639005,  # blue    0x378ADD
}

header_str = ' | '.join(header_parts)
embeds = [{
    'title': cap('What happened yesterday', 256),
    'description': cap(f'{date_disp}\n\n{header_str}', 4096),
    'color': 1941621
}]

for area in areas_out:
    area_name = area.get('area', 'Update')
    pr_count  = area.get('pr_count', 0)
    themes    = area.get('themes', [])
    color     = AREA_COLORS.get(area_name, 6579300)

    # Build subtitle
    ak = next((k for k, _, l in AREAS if l == area_name), None)
    sub_parts = []
    if ak:
        if area_prs.get(ak):    sub_parts.append(f'{len(area_prs[ak])} PRs merged')
        if area_commits.get(ak): sub_parts.append(f'{len(area_commits[ak])} direct commits')
    subtitle = ' \u00b7 '.join(sub_parts)

    theme_blocks = []
    for t in themes[:3]:
        block = f'**{t.get("title", "")}**'
        if t.get('repos'):       block += f'\n`{t["repos"]}`'
        if t.get('description'): block += f'\n{t["description"]}'
        theme_blocks.append(block)

    description = (f'*{subtitle}*\n\n' if subtitle else '') + '\n\n'.join(theme_blocks)

    embeds.append({
        'title': cap(area_name, 256),
        'description': cap(description, 4096),
        'color': color
    })

total_chars = sum(len(e.get('title','')) + len(e.get('description','')) for e in embeds)
print(f'Embed count: {len(embeds)}, total chars: {total_chars}')

discord_post({'username': 'Unicity Briefing', 'embeds': embeds})
print(f'Done - posted summary for {date_disp}')
