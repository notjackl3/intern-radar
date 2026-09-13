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
   `bot`. Nixpacks picks up `requirements.txt` and `railway.json`.
4. **Variables:**

   | name | value |
   |---|---|
   | `DISCORD_TOKEN` | from step 1 |
   | `CHANNEL_ID` | right-click the channel → Copy Channel ID |
   | `GUILD_ID` | right-click the server → Copy Server ID (optional; makes slash commands appear instantly instead of taking up to an hour) |
   | `GITHUB_REPO` | `notjackl3/intern-radar` |
   | `POLL_SECONDS` | `300` |

   (Copy IDs needs Discord → Settings → Advanced → Developer Mode on.)

5. Optionally attach a **Volume at `/data`** so the posted-ids file survives
   redeploys. Without one it falls back to `/tmp`; see below for why that's
   safe.

## Slash commands

| command | does |
|---|---|
| `/latest [count]` | most recently posted open roles |
| `/search <query>` | search open roles by title or company |
| `/stats` | how many are open, by source and employer |

These are the reason this is a bot rather than a webhook — a webhook can post,
but it can't answer.

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
