#!/usr/bin/env python3
"""LarpBuddy — posts new Canadian software internships into Discord.

How it fits with the collector
------------------------------
The collector runs on GitHub Actions and writes two files back into the repo:

    state/new.json   what appeared in the most recent run
    state/feed.json  everything currently open that matches the filter

Both are public raw JSON, so this bot needs no GitHub token and no webhook
from Actions — it just polls those two URLs. That keeps the two halves fully
decoupled: Actions never has to know the bot exists, and the bot never has to
be online for detection to work. If Railway is down for an hour, nothing is
lost; the next poll picks it up.

Required environment
--------------------
    DISCORD_TOKEN   bot token (set it in Railway's variables, never in git)
    CHANNEL_ID      numeric id of the channel to post into

Optional
--------
    GITHUB_REPO     default notjackl3/intern-radar
    GITHUB_BRANCH   default main
    POLL_SECONDS    default 300
    DATA_DIR        where to persist posted ids (default /data, then /tmp)
    GUILD_ID        sync slash commands to one guild instantly instead of
                    waiting up to an hour for the global sync
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sys

import aiohttp
import discord
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import commands, tasks

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("larpbuddy")

TOKEN = os.environ.get("DISCORD_TOKEN")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0") or 0)
REPO = os.environ.get("GITHUB_REPO", "notjackl3/intern-radar")
BRANCH = os.environ.get("GITHUB_BRANCH", "main")
POLL = int(os.environ.get("POLL_SECONDS", "300"))
GUILD_ID = os.environ.get("GUILD_ID")

RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/state"
NEW_URL = f"{RAW}/new.json"
FEED_URL = f"{RAW}/feed.json"

# Railway volumes mount at /data. Without one, fall back to /tmp — the only
# cost of losing this file is that a redeploy silently re-seeds (see below),
# so a restart never spams the channel.
DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR") or ("/data" if pathlib.Path("/data").is_dir() else "/tmp"))
POSTED = DATA_DIR / "larpbuddy_posted.json"

SOURCE_COLOR = {
    "workday": 0x0B5CAB,
    "greenhouse": 0x25A36F,
    "ashby": 0x6B5BD2,
    "lever": 0xE05C3E,
    "simplify": 0x8A8F98,
}

intents = discord.Intents.default()          # no privileged intents needed
bot = commands.Bot(command_prefix="!larp ", intents=intents)


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def load_posted() -> set[str]:
    try:
        return set(json.loads(POSTED.read_text()))
    except Exception:
        return set()


def save_posted(ids: set[str]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Keep the file bounded; 5000 ids is many months of postings.
        POSTED.write_text(json.dumps(sorted(ids)[-5000:]))
    except Exception as e:
        log.warning("could not persist posted ids: %s", e)


async def fetch_json(session: aiohttp.ClientSession, url: str):
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
        if r.status == 404:
            return None
        r.raise_for_status()
        return json.loads(await r.text())


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def age_hours(j: dict) -> float | None:
    """Hours since this role was posted, or None if the source publishes no date.

    This matters more than it looks: Workday — 30 of the ~95 open roles, and
    every bank on the watchlist — exposes only a localised display string
    ("Posted 3 Days Ago"), which the collector cannot parse, so posted_at is
    None for all of them. Any time filter must say so rather than silently
    hiding a third of the feed.
    """
    ts = j.get("posted_at")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None


def age_label(h: float | None) -> str:
    if h is None:
        return "date unknown"
    if h < 1:
        return "just posted"
    if h < 24:
        return f"{int(h)}h ago"
    days = h / 24
    if days < 30:
        return f"{int(days)}d ago"
    return f"{int(days / 30)}mo ago"


def embed_for(j: dict) -> discord.Embed:
    bits = [b for b in (j.get("location"), j.get("term"), age_label(age_hours(j))) if b]
    e = discord.Embed(
        title=(j.get("title") or "Untitled")[:250],
        url=j.get("url") or None,
        description=" · ".join(bits)[:400] or None,
        colour=SOURCE_COLOR.get(j.get("source"), 0x8A8F98),
    )
    if j.get("company"):
        e.set_author(name=j["company"][:250])
    e.set_footer(text=f"via {j.get('source', '?')}")
    return e


# --------------------------------------------------------------------------
# the poll loop
# --------------------------------------------------------------------------
@tasks.loop(seconds=POLL)
async def poll_new():
    posted = load_posted()
    first_boot = not POSTED.exists()
    try:
        async with aiohttp.ClientSession() as s:
            new = await fetch_json(s, NEW_URL)
    except Exception as e:
        log.warning("poll failed: %s", e)
        return
    if not new:
        return

    fresh = [j for j in new if j.get("id") and j["id"] not in posted]
    if not fresh:
        return

    # First boot with no saved state: record everything as posted WITHOUT
    # sending, so a fresh deploy never dumps a backlog into the channel.
    if first_boot:
        save_posted(posted | {j["id"] for j in fresh})
        log.info("first boot — seeded %d ids, posting starts next poll", len(fresh))
        return

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        log.error("CHANNEL_ID %s not visible to the bot — is it invited and "
                  "does it have View Channel + Send Messages there?", CHANNEL_ID)
        return

    n = len(fresh)
    await channel.send(f"**{n} new Canadian software internship{'s' if n != 1 else ''}**")
    for i in range(0, min(n, 30), 10):          # Discord caps 10 embeds/message
        await channel.send(embeds=[embed_for(j) for j in fresh[i:i + 10]])
    if n > 30:
        await channel.send(f"…and {n - 30} more — `/larp latest` to see them.")

    save_posted(posted | {j["id"] for j in fresh})
    log.info("posted %d new roles", n)


@poll_new.before_loop
async def before_poll():
    await bot.wait_until_ready()


# --------------------------------------------------------------------------
# slash commands — the actual reason this is a bot and not a webhook
# --------------------------------------------------------------------------
async def open_roles() -> list[dict]:
    async with aiohttp.ClientSession() as s:
        feed = await fetch_json(s, FEED_URL)
    return (feed or {}).get("jobs", [])


@bot.tree.command(name="latest", description="Most recently posted open internships")
@app_commands.describe(
    hours="Only roles posted in the last N hours (e.g. 24). Omit for all.",
    count="How many to show (1-10)")
async def latest(interaction: discord.Interaction, hours: int = None, count: int = 5):
    await interaction.response.defer()
    jobs = await open_roles()
    if not jobs:
        return await interaction.followup.send("Nothing open right now.")

    undated = 0
    if hours is not None:
        hours = max(1, hours)
        fresh = []
        for j in jobs:
            h = age_hours(j)
            if h is None:
                undated += 1
            elif h <= hours:
                fresh.append(j)
        jobs = fresh

    # Newest first; anything undated sorts last rather than pretending to be old.
    jobs.sort(key=lambda j: (j.get("posted_at") is not None, j.get("posted_at") or ""),
              reverse=True)
    total = len(jobs)
    jobs = jobs[:max(1, min(count, 10))]

    note = ""
    if hours is not None:
        note = f"**{total}** posted in the last **{hours}h**"
        if undated:
            note += (f"  ·  {undated} more hidden — Workday publishes no posting "
                     f"date, so they can't be filtered by age")
    if not jobs:
        return await interaction.followup.send(
            note or "Nothing open right now.")
    await interaction.followup.send(content=note or None,
                                    embeds=[embed_for(j) for j in jobs])


@bot.tree.command(name="search", description="Search open internships by title or company")
@app_commands.describe(query="e.g. machine learning, backend, RBC")
async def search(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    q = query.lower()
    jobs = [j for j in await open_roles()
            if q in (j.get("title", "") + " " + j.get("company", "")).lower()]
    if not jobs:
        return await interaction.followup.send(f"No open roles matching **{query}**.")
    head = jobs[:10]
    more = f"\n…and {len(jobs) - 10} more." if len(jobs) > 10 else ""
    await interaction.followup.send(
        content=f"**{len(jobs)}** open matching **{query}**{more}",
        embeds=[embed_for(j) for j in head])


@bot.tree.command(name="stats", description="What the radar is currently watching")
async def stats(interaction: discord.Interaction):
    await interaction.response.defer()
    import collections
    async with aiohttp.ClientSession() as s:
        feed = await fetch_json(s, FEED_URL)
    if not feed:
        return await interaction.followup.send("Couldn't read the feed.")
    jobs = feed.get("jobs", [])
    by_src = collections.Counter(j.get("source") for j in jobs)
    by_co = collections.Counter(j.get("company") for j in jobs).most_common(8)
    e = discord.Embed(title="Canada Intern Radar", colour=0xB23A2E,
                      description=f"**{len(jobs)}** open roles matching your filter")
    e.add_field(name="By source",
                value="\n".join(f"`{k or '?':<12}` {v}" for k, v in by_src.most_common()) or "—",
                inline=True)
    e.add_field(name="Top employers",
                value="\n".join(f"`{v:>3}` {k}" for k, v in by_co) or "—",
                inline=True)
    e.set_footer(text=f"updated {feed.get('generated_at', '?')[:16]} UTC")
    await interaction.followup.send(embed=e)


# --------------------------------------------------------------------------
@bot.event
async def on_ready():
    log.info("logged in as %s", bot.user)
    try:
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=g)
            synced = await bot.tree.sync(guild=g)
        else:
            synced = await bot.tree.sync()
        log.info("synced %d slash commands", len(synced))
    except Exception as e:
        log.warning("command sync failed: %s", e)
    if not poll_new.is_running():
        poll_new.start()


def main() -> int:
    if not TOKEN:
        log.error("DISCORD_TOKEN is not set — add it in Railway's Variables tab.")
        return 1
    if not CHANNEL_ID:
        log.error("CHANNEL_ID is not set.")
        return 1
    log.info("watching %s (branch %s), polling every %ss, state in %s",
             REPO, BRANCH, POLL, DATA_DIR)
    bot.run(TOKEN, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
