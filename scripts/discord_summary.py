import urllib.request, urllib.parse, json, os, re
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

# 1. Fetch PRs from GitHub

def gh_search(q):
    url = 'https://api.github.com/search/issues?q=' + urllib.parse.quote(q) + '&per_page=50'
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {GH_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'unicity-briefing'
    })
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())['items']

# Three project areas with their orgs and display names
AREAS = [
    ('astrid',        ['unicity-astrid'],  'Astrid'),
    ('sphere',        ['unicity-sphere'],  'Sphere'),
    ('unicitynetwork',['unicitynetwork'],  'Unicity Network'),
]

area_prs     = {}   # area_key -> list of PRs
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

total = len(all_prs)
print(f'Found {total} PRs merged on {date_str}')

if total == 0:
    print('No PRs merged — skipping Discord post.')
    exit(0)

# 2. Build per-area PR lists for Claude

def build_pr_text(prs):
    lines = []
    for pr in prs:
        repo = pr['repository_url'].split('/')[-1]
        body = (pr.get('body') or '')[:200].replace('\n', ' ')
        lines.append(f'- [{repo}] #{pr["number"]} "{pr["title"]}" by @{pr["user"]["login"]} | {body}')
    return '\n'.join(lines)

# 3. Call Anthropic API — one call, all areas in one prompt

area_sections = []
for area_key, orgs, label in AREAS:
    prs = area_prs[area_key]
    if prs:
        area_sections.append(f'=== {label} ({len(prs)} PRs) ===\n{build_pr_text(prs)}')

pr_text = '\n\n'.join(area_sections)

prompt = f"""You are writing the daily engineering executive summary for the Unicity project.
Date: {date_disp}
Total PRs merged: {total}
Releases: {', '.join(releases) if releases else 'none'}

PRs are grouped by project area below:

{pr_text}

Write a summary with one section per project area that had activity.
Each section has:
- "area": the project area name exactly as shown (Astrid, Sphere, or Unicity Network)
- "pr_count": number of PRs in that area
- "themes": array of 1-3 themes, each with:
  - "title": short punchy title, max 8 words, plain text
  - "repos": comma-separated list of repo names involved (e.g. "astrid, sdk-rust, capsule-memory")
  - "description": 2-3 plain English sentences explaining what changed and why it matters. Mention specific repo names.

Respond ONLY with a valid JSON array, no markdown fences, no preamble:
[
  {{
    "area": "Astrid",
    "pr_count": 34,
    "themes": [
      {{"title": "...", "repos": "astrid, sdk-rust", "description": "..."}}
    ]
  }},
  ...
]

Rules:
- Only include areas that have PRs
- Skip pure chore/bump PRs unless they represent a meaningful version milestone
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

# 4. Build Discord embeds — one per area

def cap(s, n):
    s = str(s).strip()
    return s if len(s) <= n else s[:n-1] + '...'

rel_str = f' | {", ".join(releases)}' if releases else ''
header  = f'{total} PRs merged{rel_str} | {len(contributors)} contributor{"s" if len(contributors)!=1 else ""}'

# Area colors: astrid=purple, sphere=teal, unicitynetwork=blue
AREA_COLORS = {
    'Astrid':           8353757,   # purple  0x7F77DD
    'Sphere':           1941621,   # teal    0x1D9E75
    'Unicity Network':  3639005,   # blue    0x378ADD
}

embeds = [{
    'title': cap('What was shipped yesterday', 256),
    'description': cap(f'{date_disp}\n\n{header}', 4096),
    'color': 1941621
}]

for area in areas_out:
    area_name  = area.get('area', 'Update')
    pr_count   = area.get('pr_count', '')
    themes     = area.get('themes', [])
    color      = AREA_COLORS.get(area_name, 6579300)

    # Build description: one block per theme
    theme_blocks = []
    for t in themes[:3]:
        title  = t.get('title', '')
        repos  = t.get('repos', '')
        desc   = t.get('description', '')
        block  = f'**{title}**'
        if repos:
            block += f'\n`{repos}`'
        if desc:
            block += f'\n{desc}'
        theme_blocks.append(block)

    description = '\n\n'.join(theme_blocks)

    embeds.append({
        'title': cap(f'{area_name} \u2014 {pr_count} PRs', 256),
        'description': cap(description, 4096),
        'color': color
    })

total_chars = sum(len(e.get('title','')) + len(e.get('description','')) for e in embeds)
print(f'Embed count: {len(embeds)}, total chars: {total_chars}')

discord_post({
    'username': 'Unicity Briefing',
    'embeds': embeds
})
print(f'Done - posted summary for {date_disp}')
