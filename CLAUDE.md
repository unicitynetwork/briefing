# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A self-updating GitHub Pages site (https://unicitynetwork.github.io/briefing/) that publishes a daily
engineering briefing for the Unicity project, plus a daily Discord summary. There is no application
here — just two standalone Python generators and the GitHub Actions cron jobs that run them.

`index.html` is a **build artifact**, not source. It is regenerated wholesale by
`scripts/generate_briefing.py` and pushed straight to `main`. Never hand-edit it — edit the generator's
CSS/render functions instead, or the change is silently overwritten on the next 04:17 EET run.

## Commands

There is no build, no package manager, no dependency file, no test suite, and no linter. Both scripts are
pure Python 3 stdlib (`urllib.request`, `json`, `re`, `base64`) — the workflows run `python3 script.py`
with no `pip install` step. Keep it that way; adding a third-party import breaks CI.

```bash
# Run either generator locally (see the warning below before doing this)
GH_TOKEN=... ANTHROPIC_API_KEY=... python3 scripts/generate_briefing.py
GH_TOKEN=... ANTHROPIC_API_KEY=... DISCORD_WEBHOOK=... python3 scripts/discord_summary.py

# Trigger a real run in CI (both workflows have workflow_dispatch)
gh workflow run generate-briefing.yml
gh workflow run discord-summary.yml
gh run list --workflow=generate-briefing.yml
```

### Running `generate_briefing.py` locally overwrites the live site

Section 12 (the last block) PUTs the generated HTML to `unicitynetwork/briefing/contents/index.html` on
branch `main` via the GitHub Contents API, then explicitly requests a Pages build. A local run with a
write-capable token publishes immediately — there is no dry-run flag and no confirmation.

The script also **never writes `index.html` to disk**; the API push is the only output path. So to iterate
on layout locally, temporarily replace the push block with `open('index.html','w').write(HTML)` and open
the file in a browser. Revert before committing.

Similarly, `discord_summary.py` posts to the webhook unconditionally on any run where PRs were merged.

Because the bot pushes `index.html` directly to `main`, a local clone goes stale after every scheduled
run. `git pull` before touching anything.

## Architecture

### Two independent pipelines, deliberately not sharing code

| | `generate_briefing.py` (1176 lines) | `discord_summary.py` (266 lines) |
|---|---|---|
| Schedule | `17 2 * * 1-5` — weekdays only | `47 1 * * *` — every day |
| Window | Yesterday; **Mon looks back 3 days** (Fri–Sun) | Always exactly yesterday |
| Data | Merged PRs + open PRs + 3 ProjectV2 boards + `involves:` sweep | Merged PRs only |
| Claude | 4 calls, `claude-sonnet-5` (cross-fallback to haiku) | 1 call, `claude-haiku-4-5`, no fallback |
| Output | Full HTML page pushed to `main` | Discord embeds via webhook |

They duplicate the org list, member handles, area labels and color values. **A change to the tracked orgs
or team roster must be applied to both files.**

### `generate_briefing.py` flow

Numbered sections in the file map 1:1 to the pipeline; keep the numbering when adding stages.

1. **Window** — `window_start`/`window_label`, Monday special-case.
2. **Helpers** — `gh_search` (REST search), `gh_graphql`, `claude` (model-fallback loop that strips
   ``` fences before returning).
3–4. **Merged PRs** per org, then **open PRs older than 7 days**.
4c. **Release tracking** — ProjectV2 #1 of `unicitynetwork`, filtered to the `Release` field value.
5. **Board fetch** — paginated ProjectV2 #1 for each of the three orgs, plus SIF (`unicitynetwork` #4)
   and Concierge (`unicity-concierge` #1). Items whose status is in `DONE_STATUSES` are dropped.
6. **Board issues / blocked items** — cross-checks: merged PR still sitting in an in-dev column, items
   with `No Status`, open PRs tracked on no board.
7. **`involves:` sweep** — the signature feature. For every member × org × {pr, issue} it runs
   `involves:USERNAME`, which catches reviews, comments, closes and assignments, not just authorship.
8–9c. **Four Claude calls** — themes, per-member narratives, "needs attention", release sentiment.
10–11. **Render** — plain f-string HTML; the whole stylesheet lives in the `CSS` literal at line ~1018.
12. **Push.**

### Ordering and rate limits are load-bearing

The GitHub search API allows ~30 requests/minute. The sweep issues 13 members × 3 orgs × 2 kinds = 78
searches with a 2.5s sleep each (~3 min) and would exhaust the budget for anything after it — which is
why section 4 (long-standing open PRs) is explicitly placed *before* the sweep. Do not reorder these
sections or drop the `time.sleep` calls.

### Failure handling is asymmetric between the two scripts — this is intentional

`discord_summary.py` distinguishes `None` (could not read an org: token, rename, SSO, outage) from `[]`
(read fine, nothing merged). A degraded run posts a red "Daily summary unavailable" embed rather than
silently claiming zero activity. Preserve that distinction when touching its `gh_search`.

`generate_briefing.py` takes the opposite approach *within* the report: every fetch and every Claude
call degrades to an empty list/dict and the page still renders with that section missing or empty
(`boards_ok`, `needs_html`, `apr26_html`, `blocked_html` are all conditionally emitted). Any new
AI-backed section must follow the same pattern — parse in a `try`, fall back to a neutral default.

But §12 gates the publish. `note_fetch_failure` counts `401`s, and the push aborts with a non-zero exit
if there was any `401`, or if the PR sweep *and* every board came back empty. Rationale: on 2026-08-24 an
empty `GH_PAT` made every call 401, each section degraded to empty exactly as designed, and the run
published "0 PRs, no activity" over a good page and exited 0. A stale correct page plus a red run beats a
fresh empty one. `403`/`429` are deliberately *not* fatal — they are usually rate limiting from the
~78-request sweep, and must not block an otherwise sound report.

### Claude call configuration

- **`thinking` is explicitly disabled on every call.** Sonnet 5 turns adaptive thinking on when the
  field is omitted (Sonnet 4.6 did not), and `max_tokens` caps thinking *plus* output — measured, the
  themes call hits its 3000 cap and returns truncated JSON. Do not drop the `thinking` key.
- **The "needs attention" call is schema-enforced** via `output_config.format` (`ATTENTION_SCHEMA`),
  and returns `{"items": [...]}` rather than a bare array. That card rendered empty on every run for
  months. The prompt renders PR titles wrapped in quotes, and one long-standing PR — `bft-core #11`,
  whose title is literally `EVM` — comes through as `"EVM"`; the model then echoed those quotes
  unescaped inside its own JSON string, breaking `json.loads`. Measured on real data, 6 trials each:

  | config | parse failures |
  |---|---|
  | `claude-sonnet-4-6`, prompt-only | 6/6 — deterministic, hence "broken every run" |
  | `claude-haiku-4-5`, prompt-only | 2/6 — intermittent, which is why swapping models looked like a fix |
  | `claude-sonnet-5`, prompt-only | 0/6 |
  | either model, schema-enforced | 0/6 |

  Sonnet 5 alone appears to fix it, but only the schema makes it structurally impossible. Keep both.
  Any new call whose output must parse should take the same route.
- Model is a per-call argument (`claude(..., model=)`), not a global, so one call can move without
  moving the others.

### HTML safety

`esc()` (line 555) is the only escaping in the codebase. Every value interpolated into HTML — especially
model-generated text and PR titles — must go through it.

## Hardcoded configuration

There is no config file. These constants are edited in place:

- `ORGS` / `ORG_LABELS` — `unicity-aos` (labeled **Astrid**), `unicity-sphere`, `unicitynetwork`.
  Note the README says `unicity-astrid`; the code has always used `unicity-aos`.
- `MEMBERS` (13 handles) and `MEMBER_NAMES` (display names, only some filled in).
- Project board numbers and `BOARD_URLS` / `ORG_CFG` board links inside `render_standup_card`.
- The release milestone: `if release != 'June'` and `deadline = datetime(2026, 6, 30, ...)`, with the
  section still named `apr26_*` from an earlier milestone. Rolling to a new release means updating the
  field-value string, the deadline, and the "June release" badge text — the variable prefix is cosmetic.
- Status vocabularies: `DONE_STATUSES`, `IN_DEV`, `BLOCKED_STATUSES`, `STANDUP_DEV_L`, `STANDUP_TEST_L`.
  These must match the literal column names configured on the GitHub project boards.
- `ristik/ndsmt-experiments` is listed in the README as tracked but is not covered by either script; the
  page carries a footer note saying its commits need a manual check.

## Secrets and deployment

Repo secrets: `GH_PAT`, `ANTHROPIC_API_KEY`, `DISCORD_WEBHOOK_URL`. Pages serves the repo root from
`main` (`build_type: legacy`); `.nojekyll` keeps Jekyll from touching it.

### Two credentials, on purpose — do not collapse them back into one

- **`GH_PAT` — reads only.** A human's classic PAT. Needs `repo` (private repos across
  `unicitynetwork`, `unicity-aos`, `unicity-sphere`, `unicity-concierge`), `read:org`, and
  `read:project` (Projects v2 GraphQL — the SIF and Concierge boards are private projects and fetching
  them fails without it). Authorize it for each org if SSO is enforced.
- **`GH_PUSH_TOKEN` — the write.** Supplied by the workflow as `${{ github.token }}`, repo-scoped and
  owned by no person. `generate_briefing.py` falls back to `GH_TOKEN` when it is unset, so local runs
  and any non-Actions caller still work.

This split exists because the site went stale for ten days (17–24 Aug 2026) when the PAT owner left the
org: reads kept working against public repos, so the run looked healthy for five minutes and then died
on a bare `HTTP 404` at the final PUT. Putting the write on a personal token re-creates that failure.

**Degradation is invisible on the read side.** When a PAT loses org access it does not error — private
repos and private project boards simply vanish from results, and `gh_search` returns `[]`. The
`failed_sources` machinery in `discord_summary.py` catches API *failures*, not permission-scoped
*filtering*, so a quietly under-reporting run still looks green. If board counts or PR counts drop
sharply for no reason, suspect the PAT before suspecting the team.
