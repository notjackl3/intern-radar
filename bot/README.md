# LarpBuddy

Posts new Canadian software internships into Discord, and answers questions
about what's currently open.

## How it fits with the collector

The collector runs on **GitHub Actions** and writes two files back into this
repo after every run:

| file | contents |
|---|---|
| `state/new.json` | what appeared in the most recent run |
| `state/feed.json` | everything currently open that matches the filter |

Both are public raw JSON. So this bot needs **no GitHub token and no webhook
from Actions** — it just polls those two URLs.

```
GitHub Actions (every 20 min)  ──writes──>  state/*.json in the repo
                                                   │
                                            LarpBuddy polls raw.githubusercontent
                                                   │
                                            posts to #your-channel
```

That decoupling is deliberate:

- Actions never has to know the bot exists — no secrets shared between them.
- The bot never has to be online for detection to work. If Railway is down for
  an hour, nothing is missed; the next poll picks it up.
- Email alerts keep working regardless, as a second channel.

## Deploy on Railway

1. **Create the Discord app** — dev portal → New Application → `LarpBuddy` →
   Bot → Reset Token → copy it. That token is a password for the bot; put it
   only in Railway's Variables tab, never in git.
2. **Invite it** — OAuth2 → URL Generator → scopes `bot` + `applications.commands`,
   bot permissions **Send Messages**, **Embed Links**, **View Channel**. That
   is the whole permission set it needs; it never reads message content, so no
   privileged intents are required.
3. **New Railway service** → deploy from this repo → set **Root Directory** to
   `bot`. Railpack (same builder your `liftbuddy` service uses) picks up `requirements.txt`; `railway.json` sets the start command.
4. **Variables:**

   | name | value |
   |---|---|
   | `DISCORD_TOKEN` | from step 1 |
   | `CHANNEL_ID` | right-click the channel → Copy Channel ID |
   | `GUILD_ID` | right-click the server → Copy Server ID (optional; makes slash commands appear instantly instead of taking up to an hour) |
   | `GITHUB_REPO` | `notjackl3/intern-radar` |
   | `POLL_SECONDS` | `300` |
| `GITHUB_TOKEN` | **only needed for `/watch`** — see below. Leave unset and every other command still works. |
   | `DATA_DIR` | `/data` if you attach a volume (liftbuddy's `DB_PATH` equivalent) |

   (Copy IDs needs Discord → Settings → Advanced → Developer Mode on.)

5. Optionally attach a **Volume at `/data`** so the posted-ids file survives
   redeploys. Without one it falls back to `/tmp`; see below for why that's
   safe.

## Slash commands

| command | does |
|---|---|
| `/latest [hours] [level] [count]` | most recently posted open roles |
| `/search <query>` | search open roles by title or company |
| `/stats` | how many are open, by source and employer |
| `/watching [query]` | every employer the radar polls |
| `/watch <company or board URL>` | start monitoring a new employer |
| `/unwatch <company>` | stop monitoring one |

These are the reason this is a bot rather than a webhook — a webhook can post,
but it can't answer.

## `/watch` — subscribing to an employer from Discord

```
/watch company: Jobber
/watch company: https://jobs.ashbyhq.com/jobber
/watch company: https://bb.wd3.myworkdayjobs.com/en-US/QNX/job/Ottawa/Dev_R-1
```

It resolves the board, **calls the API to prove it exists**, shows you real job
titles off it, and only writes to `watchlist.yml` after you press the button.
The next collector run picks the new employer up — there is nothing to
redeploy and no bot restart.

### Why there's no LLM in it

The four systems we poll are self-describing: a board URL contains its token,
and the API answers definitively. Specifically —

> a **wrong** identifier 404s on all four systems.
> a **right** identifier with no open roles returns 200 and an empty list.

So a 200 is proof, and only a 200 is ever written. A language model guessing
`Jane` → `jane` / `janeapp` / `janestreet` would be strictly worse than one
HTTP request plus a human glancing at three job titles. The one genuinely
ambiguous question — *is this board the company you meant* — is the one a
person answers instantly and a model can't answer at all. That's the confirm
button, and it's why it's there.

Guessing from a plain name is still best-effort: it tries `cohere`,
`neofinancial`, `top-hat`, `arcteryx.com` and similar shapes against
Greenhouse, Lever and Ashby. **Workday can't be guessed at all** — tenant,
shard and site are three independent unknowns — so for those, paste the URL.

An **empty board is a valid thing to watch**, not an error. PlayStation's
Waterloo co-op board sits empty between cycles; refusing it would drop one of
the best Canadian intern boards there is.

### The token

`/watch` commits to `watchlist.yml`, so it needs a GitHub token:

1. GitHub → Settings → Developer settings → **Fine-grained personal access
   tokens** → Generate new token.
2. Repository access: **Only select repositories** → `intern-radar`.
3. Permissions → Repository permissions → **Contents: Read and write**. Nothing
   else — not Actions, not Issues, not metadata beyond the default.
4. Paste it into Railway as `GITHUB_TOKEN`.

Without the token `/watch` still resolves and validates the board; it just
prints the YAML line for you to paste instead of committing it.

### What it won't do

- **Won't overwrite the file's comments.** `watchlist.yml` is two thirds
  explanation — which ATS each employer is on, why Qualcomm was dropped, which
  boards are quiet rather than broken. The edit inserts a line and leaves every
  other byte alone; a YAML round-trip would erase all of that on the first
  `/watch`.
- **Won't commit a file that doesn't parse.** The collector reads this file
  every run, so a broken commit would stop the pipeline silently. The result is
  validated before the PUT, and rejected if an entry is malformed or duplicated.
- **Won't delete a hand-written multi-line entry.** `/unwatch` removes
  single-line entries only; RBC and friends are written as blocks with their own
  comments, and rewriting those from Discord would throw away why they're there.
- **Anyone in the server can run it.** Each new employer is extra polling for
  everyone, and the entry records who added it. If the server grows beyond
  people you trust, gate `/watch` on a role before that becomes a problem.

## Behaviour worth knowing

- **A fresh deploy never spams.** On first boot with no saved state, it records
  everything currently in `new.json` as already-posted *without sending*, and
  starts posting from the next poll. So losing `/tmp` on a redeploy costs you
  at most one batch of alerts — never a wall of backlog.
- **Discord caps 10 embeds per message**, so a big drop is split across
  messages, with anything past 30 summarised.
- **Five-minute poll** means the bot adds up to 5 minutes on top of the
  collector's ~20. If you want it tighter, lower `POLL_SECONDS` — raw
  GitHub is fine with it, it's a static CDN file.
- If the bot logs `CHANNEL_ID ... not visible`, it was invited but lacks
  **View Channel** or **Send Messages** in that specific channel.
