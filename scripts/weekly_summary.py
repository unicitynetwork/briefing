import urllib.request, urllib.parse, urllib.error, json, os, re, sys, time, argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Weekly per-project summary, written for people who are not engineers.
#
# The daily briefing and the Discord summary group work by GitHub org. This groups it by
# PROJECT, which is what a reader outside engineering thinks in: one org can hold several
# products (Codewall lives in unicity-aos), one product can span orgs (Sphere's Nostr relays
# live in unicitynetwork), and one repo can serve two products (semanticd backs both SIF and
# Codewall, so it is sorted change by change in §7).
#
# Reads only. Writes one Markdown file and publishes nothing.
#
#   GH_TOKEN=... ANTHROPIC_API_KEY=... python3 scripts/weekly_summary.py [--week-of YYYY-MM-DD]
#
# --save-data / --load-data snapshot everything read from GitHub, so a prompt or model change
# can be compared on identical input without re-running the ~2 minute collection.

# Sonnet 5, chosen by comparison on 14-20 Sep 2026 data, Haiku 4.5 / Sonnet 5 / Opus 5, 2 runs each:
#   Haiku  - put the same 2 of 8 clear-cut semanticd PRs in the wrong product in both runs (machine
#            enrolment recovery and unenforced fallback policy went to SIF), wrote "production-ready"
#            with nothing in the record to support it, and used 3-5x the jargon (tenant, Docker, E2E).
#   Sonnet - 8/8 on the sort; every number and claim checked traced back to a PR; plainest prose.
#   Opus   - as accurate as Sonnet, 15-30% longer and denser, 2.5x the price, for no measured gain.
# About 29k tokens in and 3k out per week: Haiku $0.05, Sonnet $0.09, Opus $0.23. The comparison ran
# the script's exact prompts through Claude Code subagents (thinking on, no schema), so it measured
# judgement and writing, not parse reliability - output_config.format covers that.
SONNET = 'claude-sonnet-5'
HAIKU  = 'claude-haiku-4-5-20251001'

ap = argparse.ArgumentParser(description='Write the weekly per-project summary as Markdown.')
ap.add_argument('--week-of',   help='any date inside the week to report (default: last full week)')
ap.add_argument('--out',       help='output path (default: weekly-<monday>_<sunday>.md)')
ap.add_argument('--model',     default=SONNET, help=f'Claude model for every call (default {SONNET})')
ap.add_argument('--save-data', help='write the collected GitHub data to this JSON file')
ap.add_argument('--load-data', help='skip GitHub and read collected data from this JSON file')
args = ap.parse_args()

ANTHROPIC_KEY = os.environ['ANTHROPIC_API_KEY'].strip()
GH_TOKEN      = '' if args.load_data else os.environ['GH_TOKEN'].strip()

# ── 1. Window ─────────────────────────────────────────────────────────────────────────────────
# A full Monday-to-Sunday week, weekend included, in the team's own timezone so that work done
# late on Sunday or early on Monday lands in the week people remember doing it in.
TZ  = ZoneInfo('Europe/Tallinn')
now = datetime.now(TZ)
data = None
if args.load_data:
    with open(args.load_data) as f:
        data = json.load(f)
    ref = datetime.fromisoformat(data['week'][0]).date()
else:
    ref = date.fromisoformat(args.week_of) if args.week_of else now.date() - timedelta(days=7)
monday     = ref - timedelta(days=ref.weekday())
sunday     = monday + timedelta(days=6)
week_start = datetime(monday.year, monday.month, monday.day, tzinfo=TZ)
week_end   = week_start + timedelta(days=7)          # exclusive; wall-clock arithmetic, DST-safe

if monday.month == sunday.month:
    week_label = f'{monday.day}–{sunday.day} {sunday:%B %Y}'
else:
    week_label = f'{monday.day} {monday:%B} – {sunday.day} {sunday:%B %Y}'
out_path = args.out or f'weekly-{monday}_{sunday}.md'
print(f'Week: {week_start.isoformat()} .. {week_end.isoformat()} ({week_label})')

def ts(s):
    """GitHub timestamp -> aware datetime. Python 3.10 fromisoformat does not accept 'Z'."""
    return datetime.fromisoformat(s.replace('Z', '+00:00'))

def in_week(s):
    return bool(s) and week_start <= ts(s) < week_end

# ── 2. Projects ───────────────────────────────────────────────────────────────────────────────
# Which project a repo belongs to is decided here, in code, never by the model. A repo falls to
# its org's project unless overridden, so a newly created repo is reported, never dropped.
# Descriptions are shown to readers under each heading and given to the model as context.
PROJECTS = [
    ('Unicity Network',
     'The Unicity protocol itself: the consensus and aggregation layers that record and certify '
     'token state, the state-transition SDKs that apps use to create and move tokens, and the '
     'gateways and infrastructure that run the network.'),
    ('Sphere',
     'Sphere, the Unicity wallet and agent platform: the wallet app, the Sphere SDK for '
     'developers building autonomous agents, quests, the developer portal, and the Nostr '
     'messaging relays underneath.'),
    ('AOS',
     'Unicity AOS, the open agent operating system built on the Astrid runtime: the aos '
     'command-line tool and distributions, and the plug-in capsules that give agents tools, '
     'memory and model providers.'),
    ('Codewall',
     'Codewall, which governs AI coding agents such as Claude Code and Codex on company '
     'machines: each machine enrols with a central control plane, receives its policy, and '
     'enforces it locally.'),
    ('SIF',
     'SIF, the Semantic Firewall: a security gateway that inspects what goes into and comes out '
     'of AI models to stop prompt injection, jailbreaks and data leaks. Also covers the engine '
     'it shares with Codewall.'),
    ('Concierge',
     'Concierge, a private, voice-first AI personal assistant app that holds conversations, '
     'remembers what matters and runs tasks on its owner\'s behalf.'),
]
PROJECT_DESC = dict(PROJECTS)

ORGS        = ['unicitynetwork', 'unicity-sphere', 'unicity-aos', 'unicity-concierge']
EXTRA_REPOS = ['ristik/ndsmt-experiments']   # personal repo the README lists as tracked
ORG_PROJECT = {'unicitynetwork': 'Unicity Network', 'unicity-sphere': 'Sphere',
               'unicity-aos': 'AOS', 'unicity-concierge': 'Concierge', 'ristik': 'Unicity Network'}
REPO_PROJECT = {
    'unicitynetwork/semanticd':            'SIF',   # default only; changes are sorted one by one in §7
    'unicity-aos/codewall':                'Codewall',
    'unicity-aos/codewall-design':         'Codewall',
    'unicity-aos/codewall-dashboard':      'Codewall',
    'unicitynetwork/sif-docs':             'SIF',
    'unicitynetwork/astrid-capsule-sif':   'SIF',
    'unicitynetwork/srouter':              'SIF',   # model router; SIF gateway is its first consumer
    'unicitynetwork/router-spike':         'SIF',
    'unicitynetwork/ml-training':          'SIF',   # trains the firewall's detectors
    'unicitynetwork/unicity-relay':        'Sphere',
    'unicitynetwork/unicity-tokens-relay': 'Sphere',
    'unicitynetwork/nostr-js-sdk':         'Sphere',
    'unicitynetwork/nostr-sdk':            'Sphere',
    'unicitynetwork/nostr-sdk-rust':       'Sphere',
    'unicitynetwork/sphere-activity':      'Sphere',
    'unicitynetwork/sphere-activity-bot':  'Sphere',
    'unicity-sphere/astrid-site':          'AOS',
}
# semanticd is the backend both SIF and Codewall run on. Its changes are sorted one by one in
# §7; whatever is not specifically Codewall counts as SIF, shared engine work included.
SPLIT_REPO = 'unicitynetwork/semanticd'
# This repo's own bot pushes index.html to main every weekday; it is tooling, not a project.
IGNORED_REPOS = {'unicitynetwork/briefing'}

def project_of(repo):
    return REPO_PROJECT.get(repo) or ORG_PROJECT.get(repo.split('/')[0], 'Unicity Network')

def ignored(repo):
    return repo in IGNORED_REPOS or repo.endswith('/.github')

# ── 3. GitHub helpers ─────────────────────────────────────────────────────────────────────────
# Failure handling follows discord_summary.py: a source that cannot be read is recorded in
# `problems` and shown at the top of the file, never rendered as a quiet week. A 401 aborts
# outright, because a dead token makes EVERY source look empty (see CLAUDE.md, 2026-08-24).
problems = []

def gh_open(req):
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read()), r.headers.get('Link', '')
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit(f'GitHub rejected GH_TOKEN (401) at {req.full_url} - aborting rather than '
                     'writing a summary of an empty week.')
        raise

def gh_get(path):
    url = path if path.startswith('https://') else 'https://api.github.com' + path
    return gh_open(urllib.request.Request(url, headers={
        'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json',
        'User-Agent': 'unicity-briefing'}))

def gh_list(path, max_pages=10, stop_before=None):
    """Follow Link: rel=next. stop_before(item) -> True ends paging on a newest-first feed."""
    items, url = [], path
    for _ in range(max_pages):
        page, link = gh_get(url)
        items.extend(page)
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if not nxt or not page or (stop_before and stop_before(page[-1])):
            break
        url = nxt.group(1)
    return items

def gh_graphql(query, variables):
    req = urllib.request.Request('https://api.github.com/graphql',
        data=json.dumps({'query': query, 'variables': variables}).encode(),
        headers={'Authorization': f'bearer {GH_TOKEN}', 'Content-Type': 'application/json',
                 'User-Agent': 'unicity-briefing'})
    out, _ = gh_open(req)
    if out.get('errors'):
        raise RuntimeError(out['errors'][0].get('message', 'GraphQL error'))
    return out['data']

def gh_search(q):
    """Every result of one issue search, paginated. None means the search could not be read."""
    items, page = [], 1
    while True:
        try:
            data, _ = gh_get('/search/issues?' + urllib.parse.urlencode(
                {'q': q, 'per_page': 100, 'page': page}))
        except Exception as e:
            print(f'  SEARCH FAILED: {e} | {q[:90]}')
            return None
        items.extend(data['items'])
        # A week can pass the 100 a page holds, which the daily scripts never need to page past.
        # Search stops at 1000 results; say so rather than under-report in silence.
        if len(items) >= data['total_count'] or not data['items']:
            return items
        if page == 10:
            problems.append(f'{q.split()[0]}: {data["total_count"]} results, only the first '
                            f'{len(items)} are included')
            return items
        page += 1
        time.sleep(2)    # search allows ~30 requests a minute

def clean_text(text, limit):
    """PR bodies and commit messages carry templates the model does not need."""
    if not text:
        return ''
    t = re.sub(r'<!--.*?-->', ' ', text, flags=re.S)
    t = re.sub(r'```.*?```', ' ', t, flags=re.S)
    t = re.split(r'\n#{1,6}\s*(?:test plan|testing|how to test|checklist|screenshots?|'
                 r'verification)\b', t, flags=re.I)[0]
    keep = [l for l in t.splitlines()
            if not re.match(r'\s*[-*]\s*\[[ xX]\]', l)
            and not re.match(r'\s*(co-authored-by|signed-off-by):', l, re.I)
            and 'Generated with' not in l]
    t = ' '.join(' '.join(keep).split())
    return t if len(t) <= limit else t[:limit].rstrip() + '…'

def is_bot(login, name):
    return bool(re.search(r'\[bot\]$', login or '') or re.search(r'\bbot\b', name or '', re.I))

# ── 4. Merged PRs ─────────────────────────────────────────────────────────────────────────────
def collect_prs():
    prs = []
    span = f'{week_start.isoformat()}..{(week_end - timedelta(seconds=1)).isoformat()}'
    for org in ORGS + EXTRA_REPOS:
        scope = f'repo:{org}' if '/' in org else f'org:{org}'
        found = gh_search(f'{scope} is:pr is:merged merged:{span}')
        time.sleep(2)
        if found is None:
            problems.append(f'{org}: merged pull requests could not be read')
            continue
        for p in found:
            repo = '/'.join(p['repository_url'].split('/')[-2:])
            if ignored(repo):
                continue
            prs.append({'kind': 'pr', 'repo': repo, 'number': p['number'], 'title': p['title'],
                        'author': p['user']['login'], 'url': p['html_url'],
                        'body': clean_text(p.get('body'), 700),
                        'at': p['pull_request'].get('merged_at') or p['closed_at'],
                        'bot': p['user'].get('type') == 'Bot'})
        print(f'{org}: {len(found)} merged PRs')
    return prs

# ── 5. Direct pushes to the default branch ────────────────────────────────────────────────────
# The repository activity feed records each push by the time it was PUSHED, says who pushed it,
# and marks PR merges as `pr_merge`, so `push`/`force_push` on the default branch is exactly
# "reached main without a pull request". Commit dates cannot answer that: a commit written on
# Friday and pushed on Monday belongs to Monday's week.
COMMIT_PRS_Q = '''query($o: String!, $n: String!) { repository(owner: $o, name: $n) { %s } }'''

def active_repos():
    """Repos pushed to (any branch) since the week began; nothing else can hold a push in it."""
    repos = []
    for org in ORGS:
        try:
            listing = gh_list(f'/orgs/{org}/repos?type=all&sort=pushed&direction=desc&per_page=100',
                              stop_before=lambda r: not r.get('pushed_at') or ts(r['pushed_at']) < week_start)
        except Exception as e:
            problems.append(f'{org}: repository list could not be read, direct pushes not checked')
            print(f'  repo list failed for {org}: {e}')
            continue
        repos.extend(listing)
    for full in EXTRA_REPOS:
        try:
            repos.append(gh_get(f'/repos/{full}')[0])
        except Exception as e:
            problems.append(f'{full}: could not be read, direct pushes not checked')
            print(f'  {full} failed: {e}')
    # Archived repos stay in: one archived after the week still had its pushes in it, and PR search
    # and the release scan include archived repos too.
    return [r for r in repos
            if r.get('pushed_at') and ts(r['pushed_at']) >= week_start and not ignored(r['full_name'])]

def in_merged_pr(repo, branch, shas):
    """SHAs that belong to a PR merged into `branch` - someone merged locally and pushed."""
    owner, name = repo.split('/')
    hit = set()
    for i in range(0, len(shas), 50):
        chunk = shas[i:i + 50]
        fields = ' '.join(f'c{j}: object(oid: "{s}") {{ ... on Commit {{ associatedPullRequests'
                          f'(first: 5) {{ nodes {{ merged baseRefName }} }} }} }}'
                          for j, s in enumerate(chunk))
        data = gh_graphql(COMMIT_PRS_Q % fields, {'o': owner, 'n': name})['repository'] or {}
        for j, s in enumerate(chunk):
            prs = ((data.get(f'c{j}') or {}).get('associatedPullRequests') or {}).get('nodes', [])
            if any(p['merged'] and p['baseRefName'] == branch for p in prs):
                hit.add(s)
    return hit

# A new repo's first push arrives as `branch_creation` (never as a push with an all-zero `before`),
# with no previous head to compare against, so its history cannot be listed commit by commit. It is
# recorded as one event with its commit count and newest commits: a repo imported with 500 commits
# of local history would otherwise swamp the model's input for its project.
# `branch_creation` also covers a branch made from existing commits in the UI and then set as the
# default, where nothing was pushed at all. The two look the same in the feed, so only a creation
# within NEW_REPO_GRACE of the repo's own creation counts as a first push; any other is logged.
NEW_REPO_GRACE = timedelta(days=7)
CREATION_Q = '''query($o: String!, $n: String!, $oid: GitObjectID!) { repository(owner: $o, name: $n) {
  object(oid: $oid) { ... on Commit { history(first: 5) { totalCount
    nodes { oid messageHeadline url author { name user { login } } } } } } } }'''

def read_creation(repo, branch, a):
    owner, name = repo.split('/')
    h = gh_graphql(CREATION_Q, {'o': owner, 'n': name, 'oid': a['after']})['repository']['object']['history']
    return {'repo': repo, 'branch': branch, 'at': a['timestamp'], 'commits': h['totalCount'],
            'pusher': (a.get('actor') or {}).get('login') or 'unknown',
            'latest': [{'sha': c['oid'], 'title': c['messageHeadline'], 'url': c['url'],
                        'author': ((c.get('author') or {}).get('user') or {}).get('login')
                                  or (c.get('author') or {}).get('name') or 'unknown'}
                       for c in h['nodes']]}

def collect_direct(repos):
    """(commits pushed without a PR, default branches created by a push) for the week."""
    age = now - week_start
    period = 'month' if age < timedelta(days=28) else 'quarter' if age < timedelta(days=88) else 'year'
    if age > timedelta(days=360):
        problems.append('direct pushes are only kept for a year and were not checked for this week')
        return [], []
    commits, creations = [], []
    for r in repos:
        repo, branch = r['full_name'], r['default_branch']
        if r.get('created_at') and ts(r['created_at']) >= week_end:
            continue    # created after the week, so nothing in it was pushed during the week
        try:
            feed = gh_list(f'/repos/{repo}/activity?' + urllib.parse.urlencode(
                               {'ref': f'refs/heads/{branch}', 'time_period': period, 'per_page': 100}),
                           max_pages=50, stop_before=lambda a: ts(a['timestamp']) < week_start)
        except Exception as e:
            problems.append(f'{repo}: push history could not be read')
            print(f'  activity failed for {repo}: {e}')
            continue
        if len(feed) >= 50 * 100 and ts(feed[-1]['timestamp']) >= week_start:
            problems.append(f'{repo}: push history too long to reach this week, direct pushes may be missing')
        # Only the current default branch is read; which branch was default in a past week is not
        # recorded anywhere. If the current one was created after the week, it was not the default
        # then, and pushes to the branch that was are not checked - say so.
        if any(a['activity_type'] == 'branch_creation' and ts(a['timestamp']) >= week_end for a in feed):
            problems.append(f'{repo}: {branch} became the default branch after this week, so pushes '
                            'to the branch that was default then are not checked')
        week = [a for a in feed if in_week(a['timestamp'])]
        for a in week:
            if a['activity_type'] == 'branch_creation' or (
                    a['activity_type'] in ('push', 'force_push') and set(a['before']) == {'0'}):
                if not r.get('created_at') or ts(a['timestamp']) - ts(r['created_at']) > NEW_REPO_GRACE:
                    print(f'{repo}: {branch} created from existing history, not a first push - not listed')
                    continue
                try:
                    creations.append(read_creation(repo, branch, a))
                    print(f'{repo}: {branch} created by a push of {creations[-1]["commits"]} commit(s)')
                except Exception as e:
                    problems.append(f'{repo}: the push that created {branch} could not be read')
                    print(f'  creation read failed for {repo}: {e}')
        pushes = [a for a in week if a['activity_type'] in ('push', 'force_push') and set(a['before']) != {'0'}]
        found = []
        for a in pushes:
            pusher = (a.get('actor') or {}).get('login') or 'unknown'
            try:
                cmp = gh_get(f'/repos/{repo}/compare/{a["before"]}...{a["after"]}')[0]
                raw = cmp['commits']
                if cmp.get('total_commits', 0) > len(raw):
                    problems.append(f'{repo}: a push of {cmp["total_commits"]} commits, only '
                                    f'{len(raw)} are listed')
            except Exception as e:
                # A force push can leave `before` unreachable. List the new head, and say that any
                # other commits in the push are missing rather than letting the report look complete.
                print(f'  compare failed for {repo} {a["before"][:7]}..{a["after"][:7]}: {e}')
                try:
                    raw = [gh_get(f'/repos/{repo}/commits/{a["after"]}')[0]]
                    problems.append(f'{repo}: a push at {a["timestamp"]} could only be read up to its '
                                    'newest commit, earlier commits in it are not listed')
                except Exception as e:
                    problems.append(f'{repo}: a push at {a["timestamp"]} could not be read')
                    print(f'  commit {a["after"][:7]} failed: {e}')
                    continue
            for c in raw:
                if c['sha'] in {f['sha'] for f in found}:
                    continue    # re-pushed after a force push; count it once
                msg = c['commit']['message']
                login = (c.get('author') or {}).get('login')
                name  = c['commit']['author']['name']
                found.append({'kind': 'commit', 'repo': repo, 'sha': c['sha'],
                              'title': msg.split('\n', 1)[0], 'url': c['html_url'],
                              'body': clean_text(msg.split('\n', 1)[1] if '\n' in msg else '', 400),
                              'author': login or name, 'pusher': pusher, 'at': a['timestamp'],
                              'branch': branch,
                              'force': a['activity_type'] == 'force_push',
                              'bot': is_bot(login, name)})
        if found:
            try:
                merged = in_merged_pr(repo, branch, [c['sha'] for c in found])
            except Exception as e:
                # Keep them all as direct pushes, but say they may include merged-PR work.
                problems.append(f'{repo}: could not check whether pushed commits belong to pull '
                                'requests, so some listed as pushed without one may have been reviewed')
                print(f'  PR association check failed for {repo}: {e}')
                merged = set()
            found = [c for c in found if c['sha'] not in merged]
        if found:
            print(f'{repo}: {len(found)} commit(s) pushed straight to {branch}')
        commits.extend(found)
    return commits, creations

# ── 6. Releases ───────────────────────────────────────────────────────────────────────────────
# Every repo, not only those pushed this week: publishing a release from an existing tag is not a
# push, so pushed_at would hide it. And every release, not only recent ones: they can only be ordered
# by creation, and a draft can be published any time later. The orgs hold ~150 releases in all (41 in
# the largest repo), so reading them all costs a query or two and needs no cutoff.
RELEASE_PAGE = ('pageInfo { hasNextPage endCursor } '
                'nodes { tagName name url publishedAt isDraft isPrerelease }')
ORG_RELEASES_Q = '''query($org: String!, $cursor: String) { organization(login: $org) {
  repositories(first: 50, after: $cursor) { pageInfo { hasNextPage endCursor } nodes { nameWithOwner
    releases(first: 30, orderBy: {field: CREATED_AT, direction: DESC}) { %s } } } } }''' % RELEASE_PAGE
REPO_RELEASES_Q = '''query($o: String!, $n: String!, $cursor: String) { repository(owner: $o, name: $n) {
  releases(first: 100, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) { %s } } }''' % RELEASE_PAGE

def collect_releases():
    out = []
    def scan(repo, conn):
        owner, name = repo.split('/')
        while True:
            out.extend({'repo': repo, 'tag': rel['tagName'], 'url': rel['url'],
                        'name': rel.get('name') or rel['tagName'], 'prerelease': rel['isPrerelease'],
                        'notes': ''}
                       for rel in conn['nodes']
                       if not rel['isDraft'] and in_week(rel.get('publishedAt')))
            if not conn['pageInfo']['hasNextPage']:
                return
            conn = gh_graphql(REPO_RELEASES_Q, {'o': owner, 'n': name,
                                                'cursor': conn['pageInfo']['endCursor']})['repository']['releases']
    for org in ORGS:
        cursor = None
        try:
            while True:
                page = gh_graphql(ORG_RELEASES_Q, {'org': org, 'cursor': cursor})['organization']['repositories']
                for r in page['nodes']:
                    if not ignored(r['nameWithOwner']):
                        scan(r['nameWithOwner'], r['releases'])
                if not page['pageInfo']['hasNextPage']:
                    break
                cursor = page['pageInfo']['endCursor']
        except Exception as e:
            problems.append(f'{org}: releases could not be read')
            print(f'  releases failed for {org}: {e}')
    for full in EXTRA_REPOS:
        try:
            owner, name = full.split('/')
            scan(full, gh_graphql(REPO_RELEASES_Q, {'o': owner, 'n': name, 'cursor': None})['repository']['releases'])
        except Exception as e:
            problems.append(f'{full}: releases could not be read')
            print(f'  releases failed for {full}: {e}')
    # Notes only for the few releases kept; fetching them in the scan would pull every changelog.
    # They are context for the model, so a failure here is logged but the release is still reported.
    for r in out:
        try:
            r['notes'] = clean_text(gh_get(f'/repos/{r["repo"]}/releases/tags/'
                                           + urllib.parse.quote(r['tag'], safe=''))[0].get('body'), 500)
        except Exception as e:
            print(f'  release notes failed for {r["repo"]} {r["tag"]}: {e}')
    return out

def split_evidence(prs):
    """Changed-file evidence for each semanticd PR, for the SIF/Codewall sort in §7."""
    for p in prs:
        if p['repo'] != SPLIT_REPO:
            continue
        try:
            files = [f['filename'] for f in gh_list(
                f'/repos/{SPLIT_REPO}/pulls/{p["number"]}/files?per_page=100', max_pages=3)]
        except Exception as e:
            print(f'  files failed for semanticd #{p["number"]}: {e}')
            continue
        areas = Counter('/'.join(f.split('/')[:2]) if f.split('/')[0] in ('apps', 'crates', 'dashboard')
                        else f.split('/')[0] for f in files)
        cw  = sum('codewall' in f.lower() for f in files)
        sif = sum(bool(re.search(r'(^|/)(apps/sif|sif-dash)(/|$)', f)) for f in files)
        p['files'] = (f'{len(files)} files; {cw} under Codewall paths, {sif} under SIF-only paths; '
                      'areas: ' + ', '.join(f'{a} {n}' for a, n in areas.most_common(5)))

if data:
    problems.extend(data['problems'])
    print(f'Loaded collected data from {args.load_data}')
else:
    prs = collect_prs()
    repos = active_repos()
    print(f'Repos pushed to this week: {len(repos)}')
    direct, created = collect_direct(repos)
    releases = collect_releases()
    split_evidence(prs)
    data = {'week': [week_start.isoformat(), week_end.isoformat()], 'prs': prs,
            'direct': direct, 'created': created, 'releases': releases, 'problems': list(problems)}
    if args.save_data:
        with open(args.save_data, 'w') as f:
            json.dump(data, f, indent=1)
        print(f'Saved collected data to {args.save_data}')

prs, direct, releases = data['prs'], data['direct'], data['releases']
created = data.get('created', [])    # absent from data saved before creations were tracked


# ── 7. Claude ─────────────────────────────────────────────────────────────────────────────────
# Every call is schema-enforced (output_config.format): CLAUDE.md records the needs-attention
# card rendering empty for months because free-form JSON broke on quotes inside PR titles.
# Thinking is disabled for the same reason as in generate_briefing.py: max_tokens caps thinking
# plus output together, and these are summarisation calls.
usage = Counter()
models_used = Counter()

def claude(prompt, schema, max_tokens=4000):
    for model in (args.model, HAIKU if args.model != HAIKU else SONNET):
        body = {'model': model, 'max_tokens': max_tokens, 'thinking': {'type': 'disabled'},
                'messages': [{'role': 'user', 'content': prompt}],
                'output_config': {'format': {'type': 'json_schema', 'schema': schema}}}
        req = urllib.request.Request('https://api.anthropic.com/v1/messages',
            data=json.dumps(body).encode(),
            headers={'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01',
                     'content-type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                resp = json.loads(r.read())
            if resp.get('stop_reason') != 'end_turn':
                raise RuntimeError(f'stop_reason={resp.get("stop_reason")}')
            text = next(b['text'] for b in resp['content'] if b['type'] == 'text')
            out = json.loads(text)
            usage[f'{model} in']  += resp['usage']['input_tokens']
            usage[f'{model} out'] += resp['usage']['output_tokens']
            models_used[model] += 1
            print(f'  Claude OK ({model})')
            return out
        except urllib.error.HTTPError as e:
            print(f'  Claude error ({model}): {e.code} {e.read().decode("utf-8", "replace")[:300]}')
        except Exception as e:
            print(f'  Claude error ({model}): {e}')
    return None

SPLIT_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['items'],
    'properties': {'items': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['id', 'project', 'reason'],
        'properties': {'id': {'type': 'string'},
                       'project': {'type': 'string', 'enum': ['Codewall', 'SIF']},
                       'reason': {'type': 'string'}}}}}}

SECTION_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['overview', 'highlights'],
    'properties': {
        'overview': {'type': 'string'},
        'highlights': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['title', 'text'],
            'properties': {'title': {'type': 'string'}, 'text': {'type': 'string'}}}}}}

GLANCE_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['bullets'],
    'properties': {'bullets': {'type': 'array', 'items': {'type': 'string'}}}}

def item_id(it):
    return f'PR #{it["number"]}' if it['kind'] == 'pr' else f'commit {it["sha"][:7]}'

def id_key(s):
    """'PR #331', '#331' and 'pr 331' are the same answer."""
    m = re.search(r'#?\b(\d+|[0-9a-f]{7})\b', str(s).lower())
    return m.group(1) if m else str(s).strip().lower()

# SIF and Codewall share semanticd, and no single signal says which product a change serves:
# titles only sometimes carry a scope, and board links are rare and cross the line (a PR titled
# "... Codewall drafts" closes a SIF-board issue). So an explicit conventional-commit scope
# decides outright, and the model sorts the rest from title, description and changed paths.
# Anything it does not return defaults to SIF, which owns the shared engine.
def split_semanticd(items):
    todo = []
    for it in items:
        scope = re.match(r'\w+\((codewall|sif)\b[^)]*\)', it['title'], re.I)
        if scope:
            it['project'] = 'Codewall' if scope.group(1).lower() == 'codewall' else 'SIF'
            print(f'  semanticd {item_id(it)} -> {it["project"]} (title scope)')
        else:
            it['project'] = 'SIF'
            todo.append(it)
    if not todo:
        return
    lines = '\n'.join(f'- {item_id(it)}: "{it["title"]}" | {it["body"][:300]}'
                      + (f' | {it["files"]}' if it.get('files') else '') for it in todo)
    prompt = f"""semanticd is one codebase behind two products.

Codewall: {PROJECT_DESC['Codewall']}
SIF: {PROJECT_DESC['SIF']}

Sort each change below into the product it serves. Answer Codewall only when the change is
specifically for Codewall: enrolling machines, the fleet of enrolled endpoints, policy sent to
or enforced on those machines, the Codewall console. Everything else is SIF, including work on
the engine both products share (rules, policies, classifiers, database, CI, tests, docs,
deployment). Changed-file counts under Codewall or SIF-only paths are strong evidence.

Return every id exactly as written, with a reason of at most 12 words.

{lines}"""
    out = claude(prompt, SPLIT_SCHEMA, max_tokens=3000)
    if out is None:
        problems.append('semanticd changes could not be sorted between SIF and Codewall; all counted as SIF')
        return
    verdict = {id_key(v.get('id', '')): v for v in out.get('items', []) if isinstance(v, dict)}
    for it in todo:
        v = verdict.get(id_key(item_id(it)))
        if v and v.get('project') in ('Codewall', 'SIF'):
            it['project'] = v['project']
            print(f'  semanticd {item_id(it)} -> {it["project"]} ({v.get("reason", "")})')
        else:
            print(f'  semanticd {item_id(it)} -> SIF (not returned by the model)')

split_semanticd([it for it in prs + direct if it['repo'] == SPLIT_REPO])

# Not every repo publishes a GitHub Release: sphere-sdk tags versions from a CI-pushed
# "chore: release v0.17.3" commit and nothing else. Same title rule as the daily scripts. Runs after
# the semanticd sort so a release inferred from a change lands in that change's project.
def version_key(repo, tag):
    return (repo, tag.lstrip('v'))

seen = {version_key(r['repo'], r['tag']) for r in releases}
for it in prs + direct:
    if not re.search(r'^chore(\([^)]*\))?!?:\s*release\b|\brelease\s+v\d', it['title'], re.I):
        continue
    m = re.search(r'\bv?\d+\.\d+\.\d+(?:-[\w.]+)?', it['title'])
    if m and version_key(it['repo'], m.group()) not in seen:
        seen.add(version_key(it['repo'], m.group()))
        releases.append({'repo': it['repo'], 'tag': m.group(), 'url': it['url'], 'name': it['title'],
                         'prerelease': '-' in m.group(), 'notes': it['body'],
                         'project': it.get('project')})
print(f'Merged PRs: {len(prs)}, direct commits: {len(direct)}, releases: {len(releases)}')

buckets = {name: {'prs': [], 'direct': [], 'releases': [], 'created': []} for name, _ in PROJECTS}
for p in prs:
    buckets[p.get('project') or project_of(p['repo'])]['prs'].append(p)
for c in direct:
    buckets[c.get('project') or project_of(c['repo'])]['direct'].append(c)
for r in releases:
    buckets[r.get('project') or project_of(r['repo'])]['releases'].append(r)
for c in created:
    buckets[project_of(c['repo'])]['created'].append(c)

def short(repo):
    return repo.split('/')[1]

def plural(n, word):
    return f'{n} {word}{"" if n == 1 else "s"}'

def section_prompt(name, b):
    lines = [f'- Release of {short(r["repo"])} {r["tag"]}'
             + (' (pre-release)' if r['prerelease'] else '') + f': {r["name"]} | {r["notes"]}'
             for r in b['releases']]
    lines += [f'- Merged in {short(p["repo"])}: "{p["title"]}" | {p["body"]}' for p in b['prs']]
    lines += [f'- Pushed straight to {short(c["repo"])}: "{c["title"]}" | {c["body"]}'
              for c in b['direct']]
    lines += [f'- New repository {short(c["repo"])}, first pushed with {plural(c["commits"], "commit")}'
              + '; newest: ' + '; '.join(f'"{l["title"]}"' for l in c['latest'][:3])
              for c in b['created']]
    record = '\n'.join(lines)
    return f"""You are writing one section of the weekly update on the Unicity project. The readers are
not engineers: leadership, partners, investors and community members. They want to know what is new
and what changed in a big way this week, in plain language they could repeat to someone else.

Project: {name}
What it is: {PROJECT_DESC[name]}
Week: {week_label}

This week's engineering record for {name} (written by engineers, often technical):
{record}

Write:
- "overview": 1-2 sentences on what this week brought for {name}.
- "highlights": up to 4 new features or major changes, most important first. Each has
  - "title": at most 8 plain words
  - "text": 1-2 sentences: what is new or different now, and why it matters to someone who uses
    or depends on {name}.

What belongs:
- New features, new apps or products, new things people can do, and changes big enough that users
  or partners would notice: a launch, a new way of working, a significant change in how the
  product behaves.
- Leave out bug fixes, polish, speed-ups, documentation, tests, build and release tooling,
  refactors, dependency updates and internal clean-up. Mention a fix only if the problem was big
  enough that people outside engineering would have noticed it, and then briefly.
- Group related changes into one highlight.
- If nothing new or major happened, say that in one plain sentence and return no highlights.
  Do not pad.

How to write:
- Plain words for someone who does not work in software. No repo, file, package, function or
  endpoint names, no version numbers, PR numbers, usernames or commit hashes. If a technical idea
  is essential, explain it in everyday terms.
- Say only what the record supports. Do not invent motives, customers, dates or impact.
- Do not count PRs, commits or contributors."""

sections = {}
for name, _ in PROJECTS:
    b = buckets[name]
    if not (b['prs'] or b['direct'] or b['releases'] or b['created']):
        continue
    print(f'Writing {name} ({len(b["prs"])} PRs, {len(b["direct"])} direct commits, '
          f'{len(b["releases"])} releases, {len(b["created"])} new repos)')
    out = claude(section_prompt(name, b), SECTION_SCHEMA)
    if out is None:
        problems.append(f'{name}: the summary could not be written')
    sections[name] = out

glance = None
written = {n: s for n, s in sections.items() if s}
if written:
    digest = '\n\n'.join(f'{n}: {s["overview"]}\n' + '\n'.join(f'- {h["title"]}: {h["text"]}'
                                                             for h in s['highlights'])
                         for n, s in written.items())
    out = claude(f"""Below are this week's per-project sections of the Unicity weekly update, written for
readers who are not engineers.

{digest}

Write up to 5 bullets for the top of the update: the most important new features and major changes
across all projects, one plain sentence each, each starting with the project name and a colon.
Use only what the sections say. Leave out projects with nothing new, give no project more than two
bullets, and write fewer bullets rather than padding.""", GLANCE_SCHEMA, max_tokens=1500)
    glance = out['bullets'] if out else None

# ── 8. Render ─────────────────────────────────────────────────────────────────────────────────
# The report is for people outside engineering: what is new and what changed, nothing else. No PR,
# commit or contributor counts, no repo names, no change lists - the repo owner removed all of those
# as noise for this audience. Direct pushes, new repos and releases still reach the model above.
def md(s):
    """Escape text that comes from the model."""
    s = ' '.join(str(s).split())
    s = s.replace('\\', '\\\\').replace('<', '&lt;').replace('>', '&gt;')
    return re.sub(r'([*_`\[\]|])', r'\\\1', s)

L = ['# Unicity weekly update', f'**{week_label}**', '']
if week_end > now:
    L += ['> **Week in progress** \u2014 this covers the days so far, not the full week.', '']
if problems:
    L += ['> \u26a0\ufe0f **Incomplete:** ' + '; '.join(md(p) for p in problems) + '.', '']

if glance:
    L += ['## This week at a glance', '']
    for b in glance[:5]:
        m = re.match(r'([^:]{1,30}):\s*(.+)', b)
        L += [f'- **{md(m.group(1))}:** {md(m.group(2))}' if m else f'- {md(b)}']
    L += ['']

for name, desc in PROJECTS:
    L += [f'## {name}', f'*{desc}*', '']
    if name not in sections:
        L += ['Nothing new this week.', '']
    elif sections[name] is None:
        L += ['*The summary for this project could not be written this week.*', '']
    else:
        s = sections[name]
        L += [md(s['overview']), '']
        if s['highlights']:
            L += [f'- **{md(h["title"])}.** {md(h["text"])}' for h in s['highlights'][:4]] + ['']

model_note = ', '.join(sorted(models_used)) or 'none'
L += ['---', f'<sub>Generated {now:%-d %B %Y} from the team\'s GitHub activity \u00b7 {model_note}</sub>', '']

with open(out_path, 'w') as f:
    f.write('\n'.join(L))
print(f'Wrote {out_path}')
if usage:
    print('Token usage: ' + ', '.join(f'{k} {v}' for k, v in sorted(usage.items())))
if problems:
    print('INCOMPLETE: ' + '; '.join(problems))
    sys.exit(1)
