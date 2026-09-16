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

import prefs as P
import repo as repo_mod
import resolve as R
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
    "simplify-newgrad": 0xB8860B,
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
    lvl = "new grad" if j.get("level") == "newgrad" else "intern"
    e.set_footer(text=f"{lvl} · via {j.get('source', '?')}")
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


LEVEL_CHOICES = [
    app_commands.Choice(name="internships / co-ops", value="intern"),
    app_commands.Choice(name="new grad (full-time)", value="newgrad"),
    app_commands.Choice(name="all", value="all"),
]
COUNTRY_CHOICES = [
    app_commands.Choice(name="Canada", value="ca"),
    app_commands.Choice(name="United States", value="us"),
    app_commands.Choice(name="Canada + US", value="ca+us"),
    app_commands.Choice(name="anywhere", value="all"),
]


def in_country(job: dict, want: str) -> bool:
    if want == "all":
        return True
    # A job collected before country tagging existed has no `countries` key.
    # Show it rather than hide it — an untagged role is a gap in our metadata,
    # not evidence the job is somewhere you don't want.
    tags = job.get("countries")
    if not tags:
        return True
    return bool(set(tags) & set(want.split("+")))


@bot.tree.command(name="latest", description="Most recently posted open roles")
@app_commands.describe(
    hours="Only roles posted in the last N hours (e.g. 24). Omit for all.",
    level="intern (co-op/internship), newgrad (entry-level full-time), or all",
    country="Canada, US, both, or anywhere. Defaults to your /prefs.",
    count="How many to show (1-10)")
@app_commands.choices(level=LEVEL_CHOICES, country=COUNTRY_CHOICES)
async def latest(interaction: discord.Interaction, hours: int = None,
                 level: app_commands.Choice[str] = None,
                 country: app_commands.Choice[str] = None, count: int = 5):
    await interaction.response.defer()
    jobs = await open_roles()
    if not jobs:
        return await interaction.followup.send("Nothing open right now.")

    # Unset parameters fall back to whatever this member saved with /prefs, so
    # a bare /latest answers the question THEY are asking, not a global default.
    mine = P.clean(((await P.load(http)).get("users") or {}).get(str(interaction.user.id)))
    lvl = level.value if level else ("all" if len(mine["levels"]) > 1 else mine["levels"][0])
    ctry = country.value if country else "+".join(mine["countries"])

    if lvl != "all":
        jobs = [j for j in jobs if j.get("level", "intern") == lvl]
    jobs = [j for j in jobs if in_country(j, ctry)]

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

    label = {"intern": "internship", "newgrad": "new-grad", "all": "role"}[lvl]
    where = {"ca": " in Canada", "us": " in the US", "ca+us": " in Canada/US",
             "us+ca": " in Canada/US", "all": ""}.get(ctry, "")
    note = f"**{total}** open {label}{'s' if total != 1 else ''}{where}"
    if hours is not None:
        note = (f"**{total}** {label}{'s' if total != 1 else ''}{where} posted "
                f"in the last **{hours}h**")
        if undated:
            note += (f"  ·  {undated} more hidden — Workday publishes no posting "
                     f"date, so they can't be filtered by age")
    if not jobs:
        return await interaction.followup.send(note)
    await interaction.followup.send(content=note,
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
# /watch — subscribe to an employer without touching the repo by hand
# --------------------------------------------------------------------------
# The flow is: resolve -> PROVE -> a human looks at real job titles -> commit.
# There is no LLM in it, and that is the right call rather than a shortcut. The
# four systems we poll are self-describing (a board URL contains its token) and
# they 404 on a wrong one, so the truth is one HTTP request away. A model
# guessing "Jane" -> jane / janeapp / janestreet would be strictly worse than
# showing the person three real job titles and letting them say yes.
#
# The one genuinely ambiguous step — is this board the company you meant — is
# the step a human answers in a second and a model cannot answer at all.


async def http(url, method="GET", payload=None, headers=None):
    """(status, parsed-body). Every network call in this module goes through
    here so resolve.py and repo.py stay pure and testable."""
    timeout = aiohttp.ClientTimeout(total=30)
    hdrs = {"User-Agent": "larpbuddy/1.0", "Accept": "application/json"}
    hdrs.update(headers or {})
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.request(method, url, json=payload, headers=hdrs) as r:
            try:
                body = await r.json(content_type=None)
            except Exception:
                body = None
            return r.status, body


def _resolution_embed(res, query: str) -> discord.Embed:
    if res.status == R.OK:
        colour, head = 0x25A36F, "Found it"
    elif res.status == R.EMPTY:
        colour, head = 0xC8922A, "Found it — the board is empty right now"
    else:
        colour, head = 0xB23A2E, "No board found"

    e = discord.Embed(title=head, colour=colour)
    if res.entry:
        ident = (f"`{res.entry['tenant']}` / `{res.entry['shard']}` / "
                 f"`{res.entry['site']}`" if res.ats == "workday"
                 else f"`{res.entry['token']}`")
        e.description = f"**{res.ats}** → {ident}"
        if res.total:
            e.description += (f"\n{res.total} open role"
                              f"{'s' if res.total != 1 else ''} on the board"
                              f" · {res.canadian} of the first {len(res.sample)}"
                              f" look Canadian")
    if res.sample:
        e.add_field(
            name="Sample of what's on it",
            value="\n".join(f"· **{j['title'][:70]}**"
                             + (f"  \n`{j['location'][:60]}`" if j.get("location") else "")
                             for j in res.sample[:5])[:1020],
            inline=False)
    if res.note:
        e.add_field(name="Note", value=res.note[:1020], inline=False)
    if res.evidence:
        e.set_footer(text=res.evidence[:200])
    return e


class ConfirmWatch(discord.ui.View):
    """The confirm step is not ceremony. Resolving by name is a guess, and the
    only thing that can tell Cohere from a same-named board is a person reading
    the job titles above."""

    def __init__(self, res, display_name: str, who: str, author_id: int):
        super().__init__(timeout=120)
        self.res, self.display_name, self.who = res, display_name, who
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran `/watch` can confirm it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Watch this board", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _button):
        await interaction.response.defer()
        line = repo_mod.format_line(self.res.entry, self.display_name, self.who)

        if not repo_mod.enabled():
            return await interaction.followup.send(
                "I can't commit without a `GITHUB_TOKEN`, but the board checks "
                f"out. Paste this into `watchlist.yml`:\n```yaml\n{line}\n```")

        def mutate(text):
            already = repo_mod.find_existing(text, self.res.entry)
            if already:
                return text, False, f"Already watching that board, as **{already}**."
            return (repo_mod.insert_line(text, line), True,
                    f"Watching **{self.display_name}**.")

        ok, msg = await repo_mod.apply(
            http, mutate,
            f"watchlist: add {self.display_name} ({self.res.ats}) via /watch by {self.who}")
        for c in self.children:
            c.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.followup.send(
            f"{msg} The next collector run picks it up — nothing to redeploy."
            if ok else f"Couldn't add it: {msg}")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="Cancelled.", view=self)


@bot.tree.command(name="watch", description="Start monitoring an employer's job board")
@app_commands.describe(
    company="A company name, or better, a link to their job board",
    name="What to call them in alerts (defaults to what you typed)")
async def watch(interaction: discord.Interaction, company: str, name: str = None):
    await interaction.response.defer()
    res = await R.resolve(http, company)
    display = (name or company).strip()
    if display.lower().startswith("http"):
        display = (res.entry or {}).get("token") or display

    embed = _resolution_embed(res, company)
    if not res.writable:
        return await interaction.followup.send(embed=embed)

    if repo_mod.enabled():
        try:
            text, _ = await repo_mod._get(http)
            already = repo_mod.find_existing(text, res.entry)
            if already:
                embed.colour = 0x8A8F98
                embed.add_field(name="Already watching",
                                value=f"This board is on the list as **{already}**.",
                                inline=False)
                return await interaction.followup.send(embed=embed)
        except Exception as e:
            log.warning("couldn't pre-check the watchlist: %s", e)

    await interaction.followup.send(
        embed=embed, view=ConfirmWatch(res, display, str(interaction.user), interaction.user.id))


@bot.tree.command(name="unwatch", description="Stop monitoring an employer")
@app_commands.describe(company="The company name or board token to drop")
async def unwatch(interaction: discord.Interaction, company: str):
    await interaction.response.defer()
    if not repo_mod.enabled():
        return await interaction.followup.send(
            "I can't edit the watchlist without a `GITHUB_TOKEN`.")

    needle = company.strip().lower()

    def mutate(text):
        import yaml as _y
        companies = _y.safe_load(text).get("companies") or []
        hits = [e for e in companies
                if needle in (e.get("name", "").lower())
                or needle == str(e.get("token", "")).lower()
                or needle == str(e.get("site", "")).lower()]
        if not hits:
            return text, False, f"Nothing on the list matches **{company}**."
        if len(hits) > 1:
            names = ", ".join(f"**{h['name']}**" for h in hits[:8])
            return text, False, f"That matches {len(hits)} entries — {names}. Be more specific."
        e = hits[0]
        new_text, removed = repo_mod.remove_line(text, e)
        if not removed:
            return text, False, (
                f"**{e['name']}** is written as a multi-line block with its own "
                "comments, so I won't rewrite it from here — that would throw "
                "away why it's on the list. Remove it in the repo instead.")
        return new_text, True, f"Stopped watching **{e['name']}**."

    ok, msg = await repo_mod.apply(http, mutate, f"watchlist: remove {company} via /unwatch")
    await interaction.followup.send(msg if ok else f"Couldn't remove it: {msg}")


@bot.tree.command(name="watching", description="Every employer the radar polls")
@app_commands.describe(query="Optional: filter by name or ATS")
async def watching(interaction: discord.Interaction, query: str = None):
    await interaction.response.defer()
    import collections
    import yaml as _y
    url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/watchlist.yml"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                return await interaction.followup.send("Couldn't read the watchlist.")
            companies = _y.safe_load(await r.text()).get("companies") or []

    if query:
        q = query.lower()
        companies = [e for e in companies
                     if q in e.get("name", "").lower() or q == e.get("ats")]
    by_ats = collections.Counter(e["ats"] for e in companies)
    names = sorted(e["name"] for e in companies)

    e = discord.Embed(
        title=f"Watching {len(companies)} employer{'s' if len(companies) != 1 else ''}"
              + (f" matching “{query}”" if query else ""),
        colour=0x0B5CAB,
        description=" · ".join(f"**{k}** {v}" for k, v in by_ats.most_common()) or "—")
    shown = ", ".join(names[:60])
    if len(names) > 60:
        shown += f" …and {len(names) - 60} more"
    e.add_field(name="Employers", value=shown[:1020] or "—", inline=False)
    e.set_footer(text="/watch <company or board URL> to add one")
    await interaction.followup.send(embed=e)


# --------------------------------------------------------------------------
# /prefs — what each member wants, and the daily digest that honours it
# --------------------------------------------------------------------------
DIGEST_CHECK_MINUTES = 15


def _prefs_embed(p: dict, user: discord.abc.User) -> discord.Embed:
    p = P.clean(p)
    e = discord.Embed(title="Your radar settings", colour=0x0B5CAB,
                      description=P.describe(p))
    e.add_field(name="Looking for",
                value=", ".join({"intern": "internships / co-ops",
                                 "newgrad": "new grad"}[x] for x in p["levels"]),
                inline=True)
    e.add_field(name="Where",
                value=", ".join({"ca": "Canada", "us": "United States",
                                 "other": "elsewhere"}[c] for c in p["countries"]),
                inline=True)
    e.add_field(name="Daily DM",
                value=(f"yes, {p['hour_utc']:02d}:00 UTC" if p["digest"] else "off"),
                inline=True)
    e.set_footer(text="/prefs to change · /prefs digest:off to stop the DMs")
    return e


@bot.tree.command(name="prefs",
                  description="Set what you want the radar to send you")
@app_commands.describe(
    level="What you're hunting for",
    country="Where you can work",
    digest="Send you a DM when new matching roles appear",
    hour="DAILY mode: hour of day to send it, 0-23 UTC (13 = 9am Eastern)",
    every="CYCLE mode: send as soon as something new appears, at most every N hours")
@app_commands.choices(
    level=[
        app_commands.Choice(name="internships / co-ops", value="intern"),
        app_commands.Choice(name="new grad (full-time)", value="newgrad"),
        app_commands.Choice(name="both", value="intern+newgrad"),
    ],
    country=[
        app_commands.Choice(name="Canada", value="ca"),
        app_commands.Choice(name="United States", value="us"),
        app_commands.Choice(name="Canada + US", value="ca+us"),
        app_commands.Choice(name="anywhere", value="ca+us+other"),
    ],
    digest=[
        app_commands.Choice(name="yes — DM me daily", value="on"),
        app_commands.Choice(name="no — don't DM me", value="off"),
    ])
async def prefs_cmd(interaction: discord.Interaction,
                    level: app_commands.Choice[str] = None,
                    country: app_commands.Choice[str] = None,
                    digest: app_commands.Choice[str] = None,
                    hour: int = None, every: int = None):
    # Ephemeral: these are one person's settings, and a channel full of
    # "Jack set his preferences" is noise for everyone else.
    await interaction.response.defer(ephemeral=True)
    uid = str(interaction.user.id)

    # No arguments at all = "show me what I have".
    if every is not None and hour is not None:
        return await interaction.followup.send(
            "Pick one or the other: `hour:` is a clock time (once a day), "
            "`every:` is a cycle (as soon as there's something new, at most "
            "that often). Setting one switches you to that mode.", ephemeral=True)
    if every is not None and not (P.MIN_EVERY <= every <= P.MAX_EVERY):
        return await interaction.followup.send(
            f"`every:` has to be between {P.MIN_EVERY} and {P.MAX_EVERY} hours.",
            ephemeral=True)

    if (level is None and country is None and digest is None
            and hour is None and every is None):
        data = await P.load(http)
        mine = (data.get("users") or {}).get(uid)
        if not mine:
            e = _prefs_embed(P.DEFAULTS, interaction.user)
            e.title = "You haven't set anything yet — these are the defaults"
            return await interaction.followup.send(embed=e, ephemeral=True)
        return await interaction.followup.send(
            embed=_prefs_embed(mine, interaction.user), ephemeral=True)

    seeded = {"value": False}

    def mutate(current: dict):
        new = dict(current)
        new["name"] = str(interaction.user)
        if level:
            new["levels"] = level.value.split("+")
        if country:
            new["countries"] = country.value.split("+")
        if digest:
            new["digest"] = digest.value == "on"
        if hour is not None:
            new["hour_utc"] = hour
            new["mode"] = "daily"
        if every is not None:
            new["every_hours"] = every
            new["mode"] = "every"
            # Switching cadence clears the daily marker, so a member who moves
            # to cycles at 14:00 isn't told "already sent today" and left
            # waiting until tomorrow for their first one.
            new["last_digest"] = None
        # A first-time user starts with a clean slate rather than a backlog:
        # seed `sent` from what is already open so their first digest is
        # tomorrow's new roles, not four hundred old ones.
        if not current.get("sent") and not current.get("name"):
            seeded["value"] = True
        return new, True, "saved"

    ok, msg = await P.save_user(
        http, uid, mutate, f"prefs: update for {interaction.user} via /prefs")
    if not ok:
        return await interaction.followup.send(
            f"Couldn't save that: {msg}", ephemeral=True)

    data = await P.load(http)
    mine = (data.get("users") or {}).get(uid, {})
    e = _prefs_embed(mine, interaction.user)
    e.title = "Saved"
    extra = ""
    if P.clean(mine)["digest"]:
        matching = [j for j in await open_roles() if P.matches(j, mine)]
        extra = (f"\n{len(matching)} open role{'s' if len(matching) != 1 else ''} "
                 f"match this right now. Your first DM goes out at "
                 f"{P.clean(mine)['hour_utc']:02d}:00 UTC with whatever is new by then.")
    await interaction.followup.send(content=(P.describe(mine) + extra),
                                    embed=e, ephemeral=True)


async def _send_digest(user_id: str, p: dict, jobs: list[dict]) -> bool:
    """DM one member their matching roles. Returns False if we couldn't."""
    try:
        user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
    except Exception as e:
        log.warning("digest: can't resolve user %s: %s", user_id, e)
        return False

    n = len(jobs)
    head = (f"**{n} new role{'s' if n != 1 else ''}** matching your settings "
            f"— _{P.describe(p)}_")
    try:
        await user.send(content=head, embeds=[embed_for(j) for j in jobs[:10]])
        if n > 10:
            await user.send(f"…and {n - 10} more — `/latest count:10` in the server.")
        return True
    except discord.Forbidden:
        # They have DMs from server members turned off. Nothing we can do from
        # here, and retrying every day forever is worse than saying so once.
        log.info("digest: %s has DMs closed", user_id)
        return False
    except Exception as e:
        log.warning("digest to %s failed: %s", user_id, e)
        return False


@tasks.loop(minutes=DIGEST_CHECK_MINUTES)
async def daily_digest():
    """Checks often, sends once a day per member.

    The send is gated on a stored date rather than on "did the loop fire at
    13:00", because a redeploy at 12:59 would otherwise skip that member for
    the whole day — and a loop that silently skips is exactly the kind of bug
    you only notice a week later.
    """
    try:
        data = await P.load(http)
    except Exception as e:
        log.warning("digest: couldn't read prefs: %s", e)
        return
    users = data.get("users") or {}
    if not users:
        return

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    due = {uid: P.clean(p) for uid, p in users.items() if P.due(p, now)}
    if not due:
        return

    jobs = await open_roles()
    if not jobs:
        return
    jobs.sort(key=lambda j: (j.get("posted_at") is not None, j.get("posted_at") or ""),
              reverse=True)

    for uid, p in due.items():
        fresh = P.pick(jobs, p)
        first_time = not p.get("sent")
        if first_time:
            # Don't open with a wall of four hundred roles.
            fresh = fresh[:P.FIRST_DIGEST]
        sent_ok = bool(fresh) and await _send_digest(uid, p, fresh)

        # What to remember as delivered:
        #   nothing to send      -> for a first-timer, remember the whole
        #                           current backlog so tomorrow is genuinely
        #                           "new since today", not four hundred roles.
        #   sent successfully    -> remember everything the message accounted
        #                           for, including the "…and N more" tail.
        #   send FAILED          -> remember nothing. Their DMs are closed; the
        #                           roles should still be waiting when they
        #                           open them, rather than silently consumed.
        if not fresh:
            remember = {j["id"] for j in P.pick(jobs, p)} if first_time else set()
        elif sent_ok:
            remember = {j["id"] for j in (P.pick(jobs, p) if first_time else fresh)}
        else:
            remember = set()

        # When to say "that's this cycle done" differs by mode, and getting it
        # backwards is how you either spam someone or go silent on them:
        #
        #   daily  — mark the DAY done regardless. A member with nothing new
        #            shouldn't be re-checked every 15 minutes until midnight,
        #            and one with DMs closed shouldn't be retried all day.
        #   every  — only start the clock when a message actually went out.
        #            The cadence is "at most every N hours", so a quiet cycle
        #            must cost nothing: no message, and no waiting another N
        #            hours before the next check.
        def mutate(current, _ids=remember, _today=today, _sent=sent_ok,
                   _mode=p["mode"], _now=now.isoformat()):
            new = dict(current)
            if _ids:
                new["sent"] = (list(current.get("sent") or []) + sorted(_ids))[-P.MAX_SENT:]
            if _mode == "every":
                if _sent:
                    new["last_digest_at"] = _now
            else:
                new["last_digest"] = _today
            return new, True, "ok"

        if p["mode"] == "every" and not sent_ok:
            continue          # nothing to record; check again on the next tick

        try:
            await P.save_user(http, uid, mutate, f"prefs: digest bookkeeping for {uid}")
        except Exception as e:
            log.warning("digest: couldn't record send for %s: %s", uid, e)
        if sent_ok:
            log.info("digest: sent %d roles to %s", len(fresh), uid)


@bot.tree.command(name="digest",
                  description="Send yourself your digest right now, as a preview")
@app_commands.describe(
    count="How many roles to include (1-10)")
async def digest_now(interaction: discord.Interaction, count: int = 10):
    """A dry run of the scheduled DM.

    Deliberately does NOT mark anything as sent or touch the cadence clock, so
    running this never costs you your real digest — the same roles still turn
    up at your scheduled time. That also makes it safe to run repeatedly while
    tuning /prefs, which is the whole reason it exists.
    """
    await interaction.response.defer(ephemeral=True)
    uid = str(interaction.user.id)
    data = await P.load(http)
    mine = (data.get("users") or {}).get(uid)
    if not mine:
        return await interaction.followup.send(
            "You haven't set anything yet — run `/prefs` first, then this will "
            "show you exactly what your DM will look like.", ephemeral=True)

    p = P.clean(mine)
    jobs = await open_roles()
    jobs.sort(key=lambda j: (j.get("posted_at") is not None, j.get("posted_at") or ""),
              reverse=True)
    matching = [j for j in jobs if P.matches(j, p)]
    unsent = P.pick(jobs, p)
    show = (unsent or matching)[:max(1, min(count, 10))]

    if not show:
        return await interaction.followup.send(
            f"Nothing matches your settings right now — _{P.describe(p)}_\n"
            f"({len(jobs)} roles open in total; try `/prefs country:Canada + US` "
            f"or `level:both` to widen it.)", ephemeral=True)

    head = (f"**Preview of your digest** — _{P.describe(p)}_\n"
            f"{len(matching)} open match your settings, {len(unsent)} you "
            f"haven't been sent yet. This is a dry run: nothing is marked as "
            f"sent, so your real digest is unaffected.")
    ok = await _send_digest(uid, p, show)
    if ok:
        return await interaction.followup.send(
            f"Sent you a DM. {head}", ephemeral=True)
    # DMs closed — show it here instead rather than just reporting a failure.
    await interaction.followup.send(
        content=head + "\n\n_(Couldn't DM you — your Discord privacy settings "
                       "block DMs from server members, so here it is instead.)_",
        embeds=[embed_for(j) for j in show], ephemeral=True)


@daily_digest.before_loop
async def before_digest():
    await bot.wait_until_ready()


# --------------------------------------------------------------------------
@bot.event
async def on_ready():
    log.info("logged in as %s", bot.user)
    try:
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=g)
            synced = await bot.tree.sync(guild=g)
            log.info("synced %d slash commands to guild %s", len(synced), GUILD_ID)
            # The first deploy ran without GUILD_ID and registered these
            # GLOBALLY. Those stale global copies keep advertising their OLD
            # parameters alongside the fresh guild ones, so Discord shows a
            # command with only `count`. Clear the global set now that the
            # guild copies are live. Sync order matters: guild first, then
            # wipe globals — clearing before copy_global_to would empty the
            # tree and sync nothing.
            bot.tree.clear_commands(guild=None)
            removed = await bot.tree.sync()
            log.info("cleared stale global commands (now %d)", len(removed))
        else:
            synced = await bot.tree.sync()
            log.info("synced %d global slash commands", len(synced))
    except Exception as e:
        log.warning("command sync failed: %s", e)
    if not poll_new.is_running():
        poll_new.start()
    if not daily_digest.is_running():
        daily_digest.start()


def main() -> int:
    if not TOKEN:
        log.error("DISCORD_TOKEN is not set — add it in Railway's Variables tab.")
        return 1
    if not CHANNEL_ID:
        log.error("CHANNEL_ID is not set.")
        return 1
    log.info("watching %s (branch %s), polling every %ss, state in %s",
             REPO, BRANCH, POLL, DATA_DIR)
    log.info("member preferences: %s backend", P.backend())
    warn = P.startup_warning()
    if warn:
        log.warning("%s", warn)
    bot.run(TOKEN, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
