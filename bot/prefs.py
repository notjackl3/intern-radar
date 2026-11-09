"""Per-member preferences, and the matching rule the daily digest runs on.

WHERE THIS LIVES, and why it matters
------------------------------------
The bot has no guaranteed disk. Railway gives it an ephemeral filesystem
unless a volume is attached, so anything written to /tmp is gone on the next
redeploy — and preferences that silently reset are worse than no preferences,
because nobody notices until their digest stops arriving.

So there are two backends and the choice is explicit, never guessed:

  repo  (default when GITHUB_TOKEN is set)
        state/prefs.json in the GitHub repo. Survives redeploys with no extra
        setup. NOTE: that repo is PUBLIC, so this records Discord user IDs and
        job preferences in public. User IDs are not secrets, but they are
        pseudonymous handles, so this is a real if small disclosure — tell
        your members. Use `local` + a volume if you'd rather not.

  local (default when there is no token)
        A JSON file under DATA_DIR. Private, and lost on redeploy unless
        DATA_DIR points at a mounted volume. The bot logs loudly at startup
        when it detects this combination, because a prefs store that quietly
        forgets is the exact failure mode this comment exists to prevent.

THE SHAPE
---------
    {"users": {"<discord_id>": {
        "countries": ["ca"],        # subset of ca / us / other
        "levels":    ["intern"],    # subset of intern / newgrad
        "digest":    true,
        "hour_utc":  13,
        "name":      "jack",        # for logs only
        "sent":      ["<job id>", ...]   # capped; see DIGEST below
    }}}

DIGEST
------
`sent` is what stops the daily message repeating yesterday's roles. It is a
per-user set of job IDs, capped at MAX_SENT — old IDs fall off the end, but
they also fall out of feed.json long before that, so a dropped ID can't
resurrect a job. A brand-new user is seeded rather than sent the entire
backlog: their first digest is the newest FIRST_DIGEST roles, not 400.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib

import matching as M

log = logging.getLogger("prefs")

COUNTRIES = ("ca", "us", "other")
LEVELS = ("intern", "newgrad")

DEFAULTS = {"countries": ["ca"], "levels": ["intern"], "digest": True,
            "mode": "daily", "hour_utc": 13, "every_hours": 6,
            # `query` is what the member typed; `parsed` is the structured form
            # of it. Both are stored because the parse may have cost an API
            # call: re-deriving it on every digest would be a daily bill for a
            # sentence that hasn't changed. It is recomputed only when `query`
            # changes (or when they ask for it), which is the auto-mode half of
            # the design — /search parses per call instead, since each ad-hoc
            # query is new.
            "query": "", "parsed": None}
# 13:00 UTC ≈ 9am Eastern

MODES = ("daily", "every")
MIN_EVERY, MAX_EVERY = 1, 168

MAX_SENT = 3000
FIRST_DIGEST = 10

PATH = "state/prefs.json"


def backend() -> str:
    explicit = (os.environ.get("PREFS_STORE") or "").strip().lower()
    if explicit in ("repo", "local"):
        return explicit
    return "repo" if os.environ.get("GITHUB_TOKEN") else "local"


def local_path() -> pathlib.Path:
    d = pathlib.Path(os.environ.get("DATA_DIR") or "/tmp")
    return d / "larpbuddy_prefs.json"


def startup_warning() -> str | None:
    """A one-line warning for the logs if prefs will silently evaporate."""
    if backend() != "local":
        return None
    p = local_path()
    if str(p).startswith("/tmp"):
        return ("prefs are stored in /tmp and WILL be lost on the next redeploy. "
                "Attach a Railway volume and set DATA_DIR to it, or set a "
                "GITHUB_TOKEN to store them in the repo instead.")
    return None


# --------------------------------------------------------------------------
# normalising whatever a human typed into something storable
# --------------------------------------------------------------------------
def clean(p: dict | None) -> dict:
    p = dict(p or {})
    out = dict(DEFAULTS)
    out.update({k: v for k, v in p.items() if k in
                ("countries", "levels", "digest", "mode", "hour_utc",
                 "every_hours", "name", "sent", "last_digest",
                 "last_digest_at", "query", "parsed")})

    c = [x for x in (out.get("countries") or []) if x in COUNTRIES]
    out["countries"] = c or list(DEFAULTS["countries"])
    l = [x for x in (out.get("levels") or []) if x in LEVELS]
    out["levels"] = l or list(DEFAULTS["levels"])
    out["digest"] = bool(out.get("digest", True))
    out["mode"] = out.get("mode") if out.get("mode") in MODES else "daily"
    try:
        out["hour_utc"] = max(0, min(23, int(out.get("hour_utc", 13))))
    except (TypeError, ValueError):
        out["hour_utc"] = DEFAULTS["hour_utc"]
    try:
        out["every_hours"] = max(MIN_EVERY, min(MAX_EVERY,
                                                int(out.get("every_hours", 6))))
    except (TypeError, ValueError):
        out["every_hours"] = DEFAULTS["every_hours"]
    out["sent"] = list(out.get("sent") or [])[-MAX_SENT:]
    out["query"] = str(out.get("query") or "")[:500]
    # A stored parse from an older bot version, or hand-edited JSON, must not
    # be able to take the digest loop down — merge_query normalises every key
    # and drops anything unrecognised.
    parsed = out.get("parsed")
    out["parsed"] = M.merge_query(M.empty_query(), parsed) if parsed else None
    return out


def cadence(p: dict) -> str:
    p = clean(p)
    if not p["digest"]:
        return "digest OFF"
    if p["mode"] == "every":
        n = p["every_hours"]
        return f"as soon as something new appears, at most every {n}h"
    return f"daily at {p['hour_utc']:02d}:00 UTC"


def describe(p: dict) -> str:
    p = clean(p)
    names = {"ca": "Canada", "us": "US", "other": "elsewhere",
             "intern": "internships / co-ops", "newgrad": "new grad"}
    where = " + ".join(names[c] for c in p["countries"])
    what = " + ".join(names[x] for x in p["levels"])
    bits = [what, where]
    if p.get("parsed"):
        bits.append(M.describe_query(p["parsed"]))
    bits.append(cadence(p))
    return " · ".join(bits)


def due(p: dict, now) -> bool:
    """Should this member's digest go out right now?

    Two cadences, and they differ in a way that matters:

      daily  — a clock time. Fires once per calendar day once the hour has
               passed, EVEN IF there is nothing new (the caller then sends
               nothing but still marks the day done, so it stops re-checking
               until midnight). Gated on a stored DATE rather than on the loop
               ticking at exactly :00, so a redeploy at 12:59 doesn't silently
               skip that day.

      every  — a rate limit, not a schedule. "At most every N hours" rather
               than "every N hours on the dot": the digest goes out as soon as
               something new appears, but never more often than N hours apart.
               That makes every:1 genuinely useful — near-live without being a
               firehose — and means a quiet day sends nothing at all rather
               than a string of empty messages.
    """
    p = clean(p)
    if not p["digest"]:
        return False
    if p["mode"] == "every":
        last = p.get("last_digest_at")
        if not last:
            return True
        try:
            from datetime import datetime
            prev = datetime.fromisoformat(str(last))
            if prev.tzinfo is None:
                prev = prev.replace(tzinfo=now.tzinfo)
        except (TypeError, ValueError):
            return True      # unreadable stamp: treat as never sent
        return (now - prev).total_seconds() >= p["every_hours"] * 3600
    return p["hour_utc"] <= now.hour and p.get("last_digest") != now.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# the matching rule — one place, used by both /latest and the digest
# --------------------------------------------------------------------------
def matches(job: dict, p: dict) -> bool:
    p = clean(p)
    # Jobs collected before the country tagging existed have no `countries`
    # key. Treat those as "wherever you are" rather than dropping them, so a
    # feed written by an older collector still produces a digest.
    tags = set(job.get("countries") or p["countries"])
    if not (tags & set(p["countries"])):
        return False
    return (job.get("level") or "intern") in p["levels"]


def pick(jobs: list[dict], p: dict) -> list[dict]:
    p = clean(p)
    already = set(p.get("sent") or [])
    return [j for j in jobs if j.get("id") not in already and matches(j, p)]


def query_of(p: dict) -> dict:
    """This member's structured query, or an empty one.

    An empty query is not a failure state — it scores every job on freshness
    alone, which is precisely the behaviour every existing member had before
    queries existed. That is what makes this change safe to deploy to a live
    bot: nobody's digest changes until they set a query.
    """
    return clean(p).get("parsed") or M.empty_query()


def rank_pick(jobs: list[dict], p: dict, *, limit: int = 10,
              unsent_only: bool = True):
    """The digest selection: hard-filter, then rank, then take the top N.

    Hard filters are COUNTRY and LEVEL only (see matching.py for why). The
    member's query is a weight, so a narrow query can never produce an empty
    digest — it reorders what they'd have received anyway. Returns
    matching.rank()'s (job, score, why) triples so the caller can show why a
    role was chosen.
    """
    p = clean(p)
    already = set(p.get("sent") or []) if unsent_only else set()
    pool = [j for j in jobs
            if (not unsent_only or j.get("id") not in already) and matches(j, p)]
    return M.rank(pool, query_of(p), limit=limit)


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
async def load(fetch) -> dict:
    if backend() == "local":
        p = local_path()
        if not p.exists():
            return {"users": {}}
        try:
            return json.loads(p.read_text())
        except Exception as e:
            log.warning("prefs file unreadable (%s) — starting empty", e)
            return {"users": {}}
    import repo as repo_mod
    try:
        text, _ = await repo_mod._get_path(fetch, PATH)
        return json.loads(text)
    except FileNotFoundError:
        return {"users": {}}
    except Exception as e:
        log.warning("couldn't read %s from GitHub: %s", PATH, e)
        return {"users": {}}


async def save_user(fetch, user_id: str, mutate, message: str) -> tuple[bool, str]:
    """Apply `mutate(user_prefs) -> (prefs, ok, msg)` to ONE user's entry.

    Scoped to one user on purpose: two people running /prefs at the same
    moment must not overwrite each other. On the repo backend the commit is
    pinned to the file's sha, so a concurrent write is retried rather than
    clobbered; here we additionally re-read that one user's entry inside the
    retry, so the second writer edits the winner's file, not a stale copy.
    """
    if backend() == "local":
        data = await load(fetch)
        users = data.setdefault("users", {})
        new, ok, msg = mutate(clean(users.get(user_id)))
        if not ok:
            return False, msg
        users[user_id] = clean(new)
        p = local_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2))
        return True, msg

    import repo as repo_mod
    holder = {}

    def text_mutate(text: str):
        try:
            data = json.loads(text) if text.strip() else {"users": {}}
        except Exception:
            data = {"users": {}}
        users = data.setdefault("users", {})
        new, ok, msg = mutate(clean(users.get(user_id)))
        holder["msg"] = msg
        if not ok:
            return text, False, msg
        users[user_id] = clean(new)
        return json.dumps(data, indent=2) + "\n", True, msg

    return await repo_mod.apply_path(fetch, PATH, text_mutate, message)
