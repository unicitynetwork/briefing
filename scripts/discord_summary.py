import urllib.request, urllib.parse, json, os, re
from datetime import datetime, timedelta, timezone

GH_TOKEN        = os.environ['GH_TOKEN']
DISCORD_WEBHOOK = os.environ['DISCORD_WEBHOOK']
ANTHROPIC_KEY   = os.environ['ANTHROPIC_API_KEY']

now       = datetime.now(timezone.utc)
yesterday = now - timedelta(days=1)
date_str  = yesterday.strftime('%Y-%m-%d')
date_disp = yesterday.strftime('%A, %-d %B %Y')

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

ORGS = ['unicity-astrid', 'unicity-sphere', 'unicitynetwork']

all_prs      = []
releases     = []
contributors = set()

for org in ORGS:
    prs = gh_search(f'org:{org} is:pr is:merged merged:{date_str}')
    all_prs.extend(prs)
    for pr in prs:
        contributors.add(pr['user']['login'])
        m = re.search(r'v\d+\.\d+\.\d+', pr['title'])
        t = pr['title'].lower()
        if m and ('release' in t or 'chore: release' in t):
            repo = pr['repository_url'].split('/')[-1]
            releases.append(f'{repo} {m.group()}')

total = len(all_prs)
if total == 0:
    print(f'No PRs merged on {date_str} - skipping Discord post.')
    exit(0)

# 2. Build PR list for Claude

pr_lines = []
for pr in all_prs:
    repo = pr['repository_url'].split('/')[-1]
    body = (pr.get('body') or '')[:400].replace('\n', ' ')
    pr_lines.append(f'- [{repo}] #{pr["number"]} "{pr["title"]}" by @{pr["user"]["login"]} | {body}')

pr_text = '\n'.join(pr_lines)

prompt = f"""You are writing the daily engineering executive summary for the Unicity project.
Date: {date_disp}
Total PRs merged: {total}
Releases: {', '.join(releases) if releases else 'none'}

Here are all the merged PRs:
{pr_text}

Write a concise executive summary grouped by THEME (not by repo or org).
Each theme should have:
1. A short title (max 8 words)
2. Two to three sentences explaining what changed and why it matters. Plain English, no jargon.

Respond ONLY with a valid JSON array, no markdown fences, no preamble. Format:
[
  {{"title": "Theme title", "description": "Two to three sentence explanation."}},
  ...
]

Rules:
- Group related PRs into one theme
- Skip pure chore/bump PRs unless they represent a meaningful version milestone
- Max 5 themes
- Title max 80 characters
- Description max 280 characters
- No markdown formatting inside the JSON strings (no **, no `)"""

# 3. Call Anthropic API

payload = json.dumps({
    'model': 'claude-sonnet-4-20250514',
    'max_tokens': 1000,
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
with urllib.request.urlopen(req) as r:
    resp = json.loads(r.read())

raw = resp['content'][0]['text'].strip()
raw = re.sub(r'^```[a-z]*\n?', '', raw).rstrip('`').strip()
themes = json.loads(raw)
print(f'Claude generated {len(themes)} themes')

# 4. Build Discord embeds
# Discord limits: title <= 256, description <= 4096, total all embeds <= 6000

def cap(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n-1] + '\u2026'

rel_str = f' | {", ".join(releases)}' if releases else ''
header  = f'{total} PRs merged{rel_str} | {len(contributors)} contributor{"s" if len(contributors)!=1 else ""}'

THEME_COLORS = [1941621, 8353757, 3639005, 15704871, 14177840, 6529314]
# decimal equivalents of 0x1D9E75, 0x7F77DD, 0x378ADD, 0xEF9F27, 0xD85A30, 0x639922

embeds = []

embeds.append({
    'title': cap('Unicity \u2014 what shipped', 256),
    'description': cap(f'{date_disp}\n\n{header}', 4096),
    'color': 1941621,
    'url': 'https://unicitynetwork.github.io/briefing/'
})

for i, theme in enumerate(themes[:5]):
    embeds.append({
        'title': cap(theme.get('title', 'Update'), 256),
        'description': cap(theme.get('description', ''), 4096),
        'color': THEME_COLORS[i % len(THEME_COLORS)]
    })

embeds.append({
    'description': 'View full daily briefing: https://unicitynetwork.github.io/briefing/',
    'color': 4473921
})

# Verify total character count stays under 6000
total_chars = sum(
    len(e.get('title', '')) + len(e.get('description', ''))
    for e in embeds
)
print(f'Total embed chars: {total_chars}')

discord_payload = json.dumps({
    'username': 'Unicity Briefing',
    'embeds': embeds
}).encode()

print(f'Payload size: {len(discord_payload)} bytes')

req = urllib.request.Request(
    DISCORD_WEBHOOK,
    data=discord_payload,
    headers={'Content-Type': 'application/json'},
    method='POST'
)
try:
    with urllib.request.urlopen(req) as r:
        print(f'Discord response: {r.status}')
        print(f'Posted {len(embeds)} embeds for {date_disp}')
except urllib.error.HTTPError as e:
    body = e.read().decode('utf-8', errors='replace')
    print(f'Discord error {e.code}: {body}')
    raise
