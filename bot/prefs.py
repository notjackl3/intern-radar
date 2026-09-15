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

log = logging.getLogger("prefs")

COUNTRIES = ("ca", "us", "other")
LEVELS = ("intern", "newgrad")

DEFAULTS = {"countries": ["ca"], "levels": ["intern"], "digest": True,
            "hour_utc": 13}          # 13:00 UTC ≈ 9am Eastern

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
                ("countries", "levels", "digest", "hour_utc", "name", "sent")})

    c = [x for x in (out.get("countries") or []) if x in COUNTRIES]
    out["countries"] = c or list(DEFAULTS["countries"])
    l = [x for x in (out.get("levels") or []) if x in LEVELS]
    out["levels"] = l or list(DEFAULTS["levels"])
    out["digest"] = bool(out.get("digest", True))
    try:
        out["hour_utc"] = max(0, min(23, int(out.get("hour_utc", 13))))
    except (TypeError, ValueError):
        out["hour_utc"] = DEFAULTS["hour_utc"]
    out["sent"] = list(out.get("sent") or [])[-MAX_SENT:]
    return out


def describe(p: dict) -> str:
    p = clean(p)
    names = {"ca": "Canada", "us": "US", "other": "elsewhere",
             "intern": "internships / co-ops", "newgrad": "new grad"}
    where = " + ".join(names[c] for c in p["countries"])
    what = " + ".join(names[x] for x in p["levels"])
    when = (f"daily at {p['hour_utc']:02d}:00 UTC" if p["digest"]
            else "daily digest OFF")
    return f"{what} · {where} · {when}"


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
