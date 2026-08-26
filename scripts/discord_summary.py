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
#
# Losing access to an org (rename, transfer, permissions, SSO, outage) must NEVER
# fail the job. gh_search returns None to mean "could not read" and [] to mean
# "read fine, nothing merged" — the two are deliberately distinguishable so a
# degraded run reports itself instead of silently claiming zero activity.

failed_sources    = []   # orgs we could not read this run
truncated_sources = []   # orgs whose result set did not fit in one page

def gh_search(q):
    url = 'https://api.github.com/search/issues?q=' + urllib.parse.quote(q) + '&per_page=100'
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {GH_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'unicity-briefing'
    })
    try:
        with urllib.request.urlopen(req) as r:
            data  = json.loads(r.read())
            items = data['items']
            # One page only. Say so rather than under-reporting in silence - the whole
            # point of this summary is that nothing merged goes unmentioned.
            if data.get('total_count', 0) > len(items):
                print(f'  TRUNCATED: {data["total_count"]} results, showing {len(items)} | {q[:90]}')
                truncated_sources.append((q, data['total_count'], len(items)))
            return items
    except Exception as e:
        # 401/403 (token), 404/422 (org renamed, deleted, or not visible), 5xx, timeouts
        print(f'  SEARCH FAILED: {e} | {q[:90]}')
        return None

# Three project areas with their orgs and display names
AREAS = [
    ('astrid',        ['unicity-aos'],  'Astrid'),
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
        if prs is None:
            failed_sources.append(org)
            continue
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
if failed_sources:
    print(f'WARNING: could not read {len(failed_sources)} source(s): {", ".join(failed_sources)}')

warn_line = ''
if failed_sources:
    warn_line = ('\u26a0\ufe0f Could not read: ' + ', '.join(failed_sources) +
                 '\nThese are missing from this summary (org renamed, moved, or access changed).')
if truncated_sources:
    dropped = sum(t - n for _, t, n in truncated_sources)
    warn_line += (('\n\n' if warn_line else '') +
                  f'\u26a0\ufe0f {dropped} more merged PR(s) did not fit in one page of search '
                  'results and are not counted here.')

# Nothing merged anywhere.
if total == 0:
    if failed_sources:
        # Don't go quiet on a broken run — a silent "no activity" hides the breakage.
        discord_post({
            'username': 'Unicity Briefing',
            'embeds': [{
                'title': 'Daily summary unavailable',
                'description': f'{date_disp}\n\n{warn_line}',
                'color': 15158332   # red 0xE74C3C
            }]
        })
        print('Posted degraded-run warning.')
        exit(0)
    print('No PRs merged - skipping Discord post.')
    exit(0)

# 2. Build per-area PR lists for Claude

def repo_counts(prs):
    counts = {}
    for pr in prs:
        counts.setdefault(pr['repository_url'].split('/')[-1], []).append(pr)
    return counts

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
        breakdown = ', '.join(f'{r} {len(v)}' for r, v in sorted(repo_counts(prs).items()))
        area_sections.append(f'=== {label} ({len(prs)} PRs across {breakdown}) ===\n{build_pr_text(prs)}')

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
- "themes": array of 1-6 themes, each with:
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
- Max 6 themes per area. Use as many as the area's repos need.
- An area is a GitHub org, not a product. One org holds unrelated projects: aggregator-go
  is blockchain infrastructure, semanticd is agent security. They share an area and nothing
  else, so they never share a theme.
- A theme may span several repos only when those repos are one system changed together -
  sphere, sphere-sdk and wallet-api are one wallet product, so a single theme covering them
  is right. Merging unrelated repos to save a theme slot is wrong.
- Prefer giving each repo its own theme. If a repo has no coherent theme worth writing,
  leave it out entirely rather than bolting it onto an unrelated one - it gets listed
  separately by the caller. A misleading grouping is worse than an omission.
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

summary_desc = f'{date_disp}\n\n{header}'
if warn_line:
    summary_desc += f'\n\n{warn_line}'

embeds = [{
    'title': cap('What was shipped yesterday', 256),
    'description': cap(summary_desc, 4096),
    'color': 15158332 if failed_sources else 1941621   # red if degraded, else teal
}]

# The areas and the PR counts come from AREAS and the search results, never from the
# model: an area that merged PRs always gets an embed even if Claude omitted it, and
# the count in the title is the one we counted. Claude only supplies the prose.
by_area = {}
for a in (areas_out if isinstance(areas_out, list) else []):
    if isinstance(a, dict):
        by_area[str(a.get('area', '')).strip().lower()] = a

for area_key, orgs, label in AREAS:
    prs = area_prs[area_key]
    if not prs:
        continue
    themes = by_area.get(label.lower(), {}).get('themes') or []
    color  = AREA_COLORS.get(label, 6579300)

    # Build description: one block per theme
    theme_blocks = []
    for t in themes[:6]:
        if not isinstance(t, dict):
            continue
        title  = t.get('title', '')
        repos  = t.get('repos', '')
        desc   = t.get('description', '')
        block  = f'**{title}**'
        if repos:
            block += f'\n`{repos}`'
        if desc:
            block += f'\n{desc}'
        theme_blocks.append(block)

    # Coverage backstop. The prompt deliberately lets Claude drop a repo it cannot
    # theme coherently, because the alternative is what it did on the first attempt at
    # this fix: a single "Request timeouts and operator notifications" theme welding
    # aggregator-go to semanticd purely to satisfy a coverage rule. Anything skipped is
    # listed verbatim here, so omission is cheap and grab-bag themes are unnecessary.
    by_repo = repo_counts(prs)
    blob    = ' '.join(theme_blocks).lower()
    missing = [r for r in sorted(by_repo)
               if not re.search(r'(?<![\w-])' + re.escape(r.lower()) + r'(?![\w-])', blob)]
    if missing:
        print(f'  {label}: themes omitted {", ".join(missing)} - listing explicitly')
        lines_ = [f'[{r} #{pr["number"]}]({pr["html_url"]}) {cap(pr["title"], 90)} — @{pr["user"]["login"]}'
                  for r in missing for pr in by_repo[r]]
        shown = lines_[:12]
        if len(lines_) > len(shown):
            shown.append(f'...and {len(lines_) - len(shown)} more')
        theme_blocks.append('**Also merged**\n' + '\n'.join(shown))

    embeds.append({
        'title': cap(f'{label} — {len(prs)} PRs', 256),
        'description': cap('\n\n'.join(theme_blocks), 4096),
        'color': color
    })

# Discord rejects a payload whose embeds exceed 6000 characters in total. Trim the
# longest description first so one busy area cannot silence the others.
def embed_chars():
    return sum(len(e.get('title', '')) + len(e.get('description', '')) for e in embeds)

while embed_chars() > 5800:
    biggest = max(embeds, key=lambda e: len(e.get('description', '')))
    if len(biggest['description']) <= 300:
        break
    biggest['description'] = biggest['description'][:-220].rstrip().rstrip('.').rstrip() + '\n...'

print(f'Embed count: {len(embeds)}, total chars: {embed_chars()}')

discord_post({
    'username': 'Unicity Briefing',
    'embeds': embeds
})
print(f'Done - posted summary for {date_disp}')
