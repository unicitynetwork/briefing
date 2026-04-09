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
# ISO window for event filtering
window_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
window_end   = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)

def discord_post(payload_dict):
    data = json.dumps(payload_dict).encode()
    print(f'Posting {len(data)} bytes to Discord...')
    req = urllib.request.Request(
        DISCORD_WEBHOOK, data=data,
        headers={
            'Content-Type': 'application/json',
            'User-Agent': 'DiscordBot (https://github.com/unicitynetwork/briefing, 1.0)'
        }, method='POST'
    )
    try:
        with urllib.request.urlopen(req) as r:
            print(f'Discord OK: {r.status}')
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        print(f'DISCORD ERROR {e.code}: {body}')
        raise

def gh_get(url):
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {GH_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'unicity-briefing'
    })
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f'  gh_get error {url[:80]}: {e}')
        return []

def gh_search(q):
    url = 'https://api.github.com/search/issues?q=' + urllib.parse.quote(q) + '&per_page=50'
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {GH_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'unicity-briefing'
    })
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())['items']

# ── Fetch org events (push + branch creation) ─────────────────────────────────
def fetch_org_events(org):
    """Fetch yesterday's PushEvent and CreateEvent from the org events stream."""
    pushes = []   # {repo, branch, author, commits: [{sha, message}]}
    branches = [] # {repo, branch, author}

    for page in range(1, 5):  # up to 4 pages = 120 events
        url = f'https://api.github.com/orgs/{org}/events?per_page=30&page={page}'
        events = gh_get(url)
        if not events or not isinstance(events, list):
            break

        found_any_yesterday = False
        for ev in events:
            created = ev.get('created_at', '')
            try:
                ev_time = datetime.fromisoformat(created.replace('Z', '+00:00'))
            except Exception:
                continue

            if ev_time > window_end.replace(tzinfo=timezone.utc):
                continue  # too recent
            if ev_time < window_start.replace(tzinfo=timezone.utc):
                break     # too old, events are in reverse-chron order
            found_any_yesterday = True

            ev_type = ev.get('type', '')
            repo_name = ev.get('repo', {}).get('name', '').split('/')[-1]
            actor = ev.get('actor', {}).get('login', '')
            payload = ev.get('payload', {})

            if ev_type == 'PushEvent':
                ref = payload.get('ref', '')
                branch = ref.replace('refs/heads/', '') if ref.startswith('refs/heads/') else ref
                commits = [
                    {'sha': c.get('id', '')[:7], 'message': c.get('message', '').split('\n')[0][:100]}
                    for c in payload.get('commits', [])
                    if c.get('distinct', True)
                ]
                if commits:
                    pushes.append({'repo': repo_name, 'branch': branch, 'author': actor, 'commits': commits})

            elif ev_type == 'CreateEvent' and payload.get('ref_type') == 'branch':
                branch = payload.get('ref', '')
                branches.append({'repo': repo_name, 'branch': branch, 'author': actor})

        if not found_any_yesterday:
            break
        time.sleep(0.5)

    return pushes, branches

# ── Three project areas ────────────────────────────────────────────────────────
AREAS = [
    ('astrid',         ['unicity-astrid'],  'Astrid'),
    ('sphere',         ['unicity-sphere'],  'Sphere'),
    ('unicitynetwork', ['unicitynetwork'],  'Unicity Network'),
]

area_prs      = {}
area_pushes   = {}
area_branches = {}
all_prs       = []
releases      = []
contributors  = set()

for area_key, orgs, _ in AREAS:
    area_prs[area_key]      = []
    area_pushes[area_key]   = []
    area_branches[area_key] = []
    for org in orgs:
        # Merged PRs
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

        # Push events and branch creations
        pushes, branches = fetch_org_events(org)
        area_pushes[area_key].extend(pushes)
        area_branches[area_key].extend(branches)
        for p in pushes:
            contributors.add(p['author'])
        for b in branches:
            contributors.add(b['author'])

total_prs = len(all_prs)
total_pushes   = sum(len(v) for v in area_pushes.values())
total_branches = sum(len(v) for v in area_branches.values())
print(f'PRs merged: {total_prs} | Push events: {total_pushes} | New branches: {total_branches}')

# Check if there's anything to report at all
has_activity = total_prs > 0 or total_pushes > 0 or total_branches > 0
if not has_activity:
    discord_post({'content': f'No activity on {date_disp}.', 'username': 'Unicity Briefing'})
    exit(0)

# ── Build per-area text for Claude ────────────────────────────────────────────
def build_area_text(area_key, label):
    lines = []
    prs = area_prs[area_key]
    if prs:
        lines.append(f'--- Merged PRs ({len(prs)}) ---')
        for pr in prs:
            repo = pr['repository_url'].split('/')[-1]
            lines.append(f'- [{repo}] #{pr["number"]} "{pr["title"]}" by @{pr["user"]["login"]}')

    pushes = area_pushes[area_key]
    if pushes:
        lines.append(f'--- Direct pushes to branches ({len(pushes)} push events) ---')
        for p in pushes:
            n = len(p['commits'])
            msgs = '; '.join(c['message'] for c in p['commits'][:3])
            suffix = f' (+{n-3} more)' if n > 3 else ''
            lines.append(f'- [{p["repo"]}:{p["branch"]}] {n} commit(s) by @{p["author"]}: {msgs}{suffix}')

    branches = area_branches[area_key]
    if branches:
        lines.append(f'--- New branches created ({len(branches)}) ---')
        for b in branches:
            lines.append(f'- [{b["repo"]}] New branch: {b["branch"]} by @{b["author"]}')

    return '\n'.join(lines) if lines else 'No activity'

area_sections = []
for area_key, orgs, label in AREAS:
    text = build_area_text(area_key, label)
    if text != 'No activity':
        n_prs = len(area_prs[area_key])
        n_pushes = len(area_pushes[area_key])
        n_branches = len(area_branches[area_key])
        area_sections.append(f'=== {label} ({n_prs} PRs, {n_pushes} push events, {n_branches} new branches) ===\n{text}')

activity_text = '\n\n'.join(area_sections)

# ── Claude prompt ─────────────────────────────────────────────────────────────
prompt = f"""You are writing the daily engineering executive summary for the Unicity project.
Date: {date_disp}
Total PRs merged: {total_prs} | Direct push events: {total_pushes} | New branches: {total_branches}
Releases: {', '.join(releases) if releases else 'none'}

Activity is grouped by project area below (merged PRs, direct branch pushes, new branches):

{activity_text}

Write a summary with one section per project area that had activity.
Each section has:
- "area": the project area name exactly as shown (Astrid, Sphere, or Unicity Network)
- "pr_count": number of merged PRs in that area
- "themes": array of 1-3 themes covering ALL activity (PRs merged, direct commits, new branches), each with:
  - "title": short punchy title, max 8 words, plain text
  - "repos": comma-separated list of repo names involved
  - "description": 2-3 plain English sentences. Cover merged PRs AND notable direct commits/branch work. Mention specific repo names and branch names where relevant.

Respond ONLY with a valid JSON array, no markdown fences, no preamble:
[
  {{
    "area": "Astrid",
    "pr_count": 3,
    "themes": [
      {{"title": "...", "repos": "astrid, sdk-rust", "description": "..."}}
    ]
  }},
  ...
]

Rules:
- Only include areas that have any activity (PRs, pushes, or new branches)
- Skip pure chore/bump commits unless they represent a meaningful version milestone
- Max 3 themes per area
- Title: max 60 chars, no special characters
- Repos: just the short repo name(s), comma separated
- Description: max 300 chars, plain text, no backticks, no asterisks"""

print('Calling Anthropic API...')
payload = json.dumps({
    'model': 'claude-haiku-4-5-20251001',
    'max_tokens': 1500,
    'messages': [{'role': 'user', 'content': prompt}]
}).encode()

req = urllib.request.Request(
    'https://api.anthropic.com/v1/messages', data=payload,
    headers={'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'}
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

# ── Build Discord embeds ───────────────────────────────────────────────────────
def cap(s, n):
    s = str(s).strip()
    return s if len(s) <= n else s[:n-1] + '...'

rel_str = f' | {", ".join(releases)}' if releases else ''
header_parts = []
if total_prs:      header_parts.append(f'{total_prs} PR{"s" if total_prs!=1 else ""} merged')
if total_pushes:   header_parts.append(f'{total_pushes} branch push{"es" if total_pushes!=1 else ""}')
if total_branches: header_parts.append(f'{total_branches} new branch{"es" if total_branches!=1 else ""}')
header_parts.append(f'{len(contributors)} contributor{"s" if len(contributors)!=1 else ""}')
header = ' | '.join(header_parts) + rel_str

AREA_COLORS = {
    'Astrid':          8353757,  # purple  0x7F77DD
    'Sphere':          1941621,  # teal    0x1D9E75
    'Unicity Network': 3639005,  # blue    0x378ADD
}

embeds = [{
    'title': cap('What happened yesterday', 256),
    'description': cap(f'{date_disp}\n\n{header}', 4096),
    'color': 1941621
}]

for area in areas_out:
    area_name = area.get('area', 'Update')
    pr_count  = area.get('pr_count', '')
    themes    = area.get('themes', [])
    color     = AREA_COLORS.get(area_name, 6579300)

    # Subtitle line showing what types of activity
    ak = next((k for k, _, l in AREAS if l == area_name), None)
    sub_parts = []
    if ak:
        if area_prs.get(ak):      sub_parts.append(f'{len(area_prs[ak])} PRs merged')
        if area_pushes.get(ak):   sub_parts.append(f'{len(area_pushes[ak])} branch pushes')
        if area_branches.get(ak): sub_parts.append(f'{len(area_branches[ak])} new branches')
    subtitle = ' · '.join(sub_parts)

    theme_blocks = []
    for t in themes[:3]:
        block = f'**{t.get("title", "")}**'
        if t.get('repos'): block += f'\n`{t["repos"]}`'
        if t.get('description'): block += f'\n{t["description"]}'
        theme_blocks.append(block)

    description = f'*{subtitle}*\n\n' + '\n\n'.join(theme_blocks) if subtitle else '\n\n'.join(theme_blocks)

    embeds.append({
        'title': cap(area_name, 256),
        'description': cap(description, 4096),
        'color': color
    })

total_chars = sum(len(e.get('title','')) + len(e.get('description','')) for e in embeds)
print(f'Embed count: {len(embeds)}, total chars: {total_chars}')

discord_post({'username': 'Unicity Briefing', 'embeds': embeds})
print(f'Done - posted summary for {date_disp}')
