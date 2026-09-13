# intern-radar

Polls Canadian internship postings **at the origin** — the applicant tracking
systems the postings are born in — and opens a GitHub issue when something new
appears. Runs on GitHub Actions every ~20 minutes, free.

## Why the ATS and not LinkedIn

Every posting is published into a company's ATS first, then propagates outward.
LinkedIn and Indeed sit one to seven days downstream. Greenhouse, Lever, Ashby
and SmartRecruiters all serve their boards from public, unauthenticated JSON,
and Workday has an undocumented-but-open JSON endpoint. Reading those directly
is the only approach that is genuinely earlier than everyone else.

Measured on the 449 live Canadian intern listings on 12 Sep 2026:

| Platform        | Share of Canadian intern roles |
|-----------------|-------------------------------:|
| Workday         | 60% |
| Custom / in-house | 14% |
| Greenhouse      | 7% |
| Ashby           | 5% |
| iCIMS           | 4% |
| Lever           | 3% |
| SmartRecruiters | 3% |

Workday dominating is the opposite of the US picture, where Greenhouse and
Lever lead. Canadian employers are banks, insurers, telcos and industrials —
Workday's core market. A pipeline that only speaks Greenhouse and Lever misses
three roles in five.

## How it runs

Nothing runs on your laptop. GitHub Actions runs the collector on its own
machines, on a schedule, whether or not you're online.

Two cadences, because the sources cost very different amounts:

| Tier | What it polls | Requests | Cadence |
|------|---------------|---------:|---------|
| `fast` | 18 boards where one GET returns the whole board (Greenhouse, Lever, Ashby) + RBC Early Talent, BMO Campus, CIBC Campus | ~26 | every 20 min |
| `full` | everything — all 20 Workday tenants (20 results per page, so they must be paged), SmartRecruiters, the Simplify dataset, hiring.cafe | ~80 | every 2 hours |

Adapters run concurrently **across** hosts but never against the same host:
`adapters.py` holds a per-host lock and a 1.2s per-host delay, so a run
finishes in well under a minute while no employer ever sees parallel requests.

### Cost

GitHub Actions is **unlimited and free on public repositories**, and 2,000
minutes/month on private ones. At the cadence above this uses roughly 2,900
minutes/month — over the private allowance, about $5/month in overage.

**So: make the repo public.** Nothing in it is sensitive; it's a list of job
boards. If you want it private, drop the fast tier to `*/30` and the full tier
to every 4 hours, which lands under 2,000.

### Detection

There are no webhooks — none of these platforms offer one — so this is polling
with a diff. Each job gets a stable `id` (a hash of its ATS + board token +
the platform's own job id), `state/seen.json` holds every id ever seen, and a
run reports the set difference. Nothing is re-reported, and a job that closes
and reopens under a new id shows up again, correctly.

Ids are derived, never sequential, which matters for Workday: its `postedOn`
field is a localised display string ("Posted 3 Days Ago"), useless as a date,
so Workday is diffed on `externalPath` instead.

### Notification

New roles open a **GitHub issue** grouped by source. GitHub emails you when one
opens (watch the repo → Custom → Issues), and the GitHub mobile app turns that
into a phone push. No extra service, no API key, free.

Add a `DISCORD_WEBHOOK` repo secret to also push to Discord.

A run with nothing new is silent — it just commits state.

## What is actually verified

Built 12 Sep 2026. Verification status, honestly:

| Source | Status | Evidence |
|---|---|---|
| Simplify | **verified** | 12.5 MB downloaded and parsed — 16,675 rows, 449 live Canadian |
| Greenhouse | **verified live** | returns a `jobs` array; `absolute_url`/`location`/`updated_at`/`id` all present as coded |
| Ashby | **verified live** | returns `jobs` with `descriptionPlain`, `isListed`, `publishedAt` as coded |
| Lever | **verified live** | returns an array; `createdAt` came back as `1757698781791` — epoch **milliseconds**, confirming the `/1000` conversion |
| Workday | **partially verified** | the tenant/site paths resolve (no 404), but the CXS endpoint needs a POST and could not be exercised. This is 60% of the market and the only undocumented, community-derived endpoint here — **run `doctor` first.** |
| SmartRecruiters | **off by default** | `api.smartrecruiters.com` serves a blanket-disallow robots.txt despite documenting a public API. Commented out in the watchlist; re-enable deliberately or not at all. |
| hiring.cafe | **unverified** | undocumented API. Wrapped in try/except — failures are logged, never fatal. |

Before trusting any of it:

```
python -m collector.doctor
```

Or in Actions: Run workflow → tier `doctor`. It hits every board once and
prints ok / EMPTY / DEAD per employer. **EMPTY is the one to read carefully** —
a bad token and a quiet board look identical on SmartRecruiters and some
Workday sites, so an empty board means "go re-read the identifier off the
careers page", not "nothing is hiring".

## Setup

1. Create a **private** repo and push this directory to it.
2. Settings → Actions → General → Workflow permissions → **Read and write**.
3. Actions → `collect` → **Run workflow**, with `seed` checked. This records
   everything currently open as already-seen so your first real run isn't 400
   notifications.
4. Watch the repo (Custom → Issues) so GitHub emails you when one opens.

Optional: add a `DISCORD_WEBHOOK` repo secret to also push to Discord.

## Tuning

Everything you'd want to change lives in two files:

- `config.yml` — terms, title keywords, which broad sweeps are on.
- `watchlist.yml` — which employers get polled directly.

After a big filter change, re-run with `seed` checked.

### Adding an employer

**Workday** — read their careers URL:

```
https://rbc.wd3.myworkdayjobs.com/en-US/RBCEARLYTALENT1/job/...
       ^tenant ^shard              ^drop  ^site
```

**Greenhouse / Lever / Ashby / SmartRecruiters** — the token is the first path
segment of any job URL on their board.

## Output

- `state/new.json` — what appeared in the last run
- `state/feed.json` — everything currently open that matches your filter
- `state/seen.json` — the dedupe ledger
- Git history — a free audit log of exactly when each role appeared

## Gotchas worth knowing

- **Workday caps `limit` at 20.** Ask for 21 and you get an empty array with
  HTTP 200 and no error. Its `postedOn` is a display string ("Posted 3 Days
  Ago"), so we diff on `externalPath` instead.
- **Simplify's data is on the `dev` branch.** `main` 404s.
- **SmartRecruiters returns HTTP 200 with zero results** for a bad company
  token, so a typo silently watches nothing. The collector logs a warning.
- **hiring.cafe's API is undocumented** and may break. Failures are logged and
  never fatal.

## Manners

One request per host at a time with a ~1.2s delay, a real User-Agent, no
authentication, no rate-limit evasion. `boards.greenhouse.io/embed/*` and
Workday's `/refreshFacet/` are disallowed in robots.txt and are not touched —
both have permitted JSON alternatives, which is what this uses.

Reading is the easy half. Note that most ATS terms of service require a human
applicant, so this stops at telling you what exists.
