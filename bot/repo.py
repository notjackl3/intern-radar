"""Write watchlist.yml back to GitHub, so /watch takes effect with no deploy.

The collector reads watchlist.yml from the repo on every run. So the moment a
line lands on `main`, the next run polls that employer — nothing to redeploy,
nothing to restart, and the bot and collector still share no state beyond a
file in git.

The edit is TEXTUAL on purpose. watchlist.yml is two thirds comments — which
ATS each employer is on, why Qualcomm was dropped, which boards are quiet
rather than broken. Round-tripping it through a YAML parser would throw all of
that away on the first /watch. So we insert a line under a marker and leave
every other byte alone, then parse the result only to check we didn't break it.

Needs a GITHUB_TOKEN with contents:write on this repo and nothing else. Without
one the bot still resolves and validates — it just hands you the line to paste
instead of committing it.
"""
from __future__ import annotations

import base64
import logging
import os

import yaml

log = logging.getLogger("repo")

REPO = os.environ.get("GITHUB_REPO", "notjackl3/intern-radar")
BRANCH = os.environ.get("GITHUB_BRANCH", "main")
TOKEN = os.environ.get("GITHUB_TOKEN")
PATH = "watchlist.yml"

MARKER = "  # ---- added from Discord with /watch ------------------------------------"
FALLBACK_ANCHOR = "  # ---- NOT POLLABLE"


def enabled() -> bool:
    return bool(TOKEN)


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "larpbuddy"}


def format_line(entry: dict, name: str, who: str = "") -> str:
    """One watchlist line, aligned like the hand-written ones."""
    note = f"  # added by {who}" if who else ""
    safe = name.replace(",", " ").replace("{", "").replace("}", "").strip()
    if '"' in safe or ":" in safe or "'" in safe:
        safe = '"' + safe.replace('"', "") + '"'
    if entry["ats"] == "workday":
        return (f"  - {{ name: {safe}, ats: workday, tenant: {entry['tenant']}, "
                f"shard: {entry['shard']}, site: {entry['site']} }}{note}")
    return (f"  - {{ name: {safe}, ats: {entry['ats']}, "
            f"token: {entry['token']} }}{note}")


def entry_key(e: dict) -> tuple:
    if e.get("ats") == "workday":
        return ("workday", f"{e.get('tenant')}|{e.get('shard')}|{e.get('site')}".lower())
    return (e.get("ats"), str(e.get("token", "")).lower())


def find_existing(text: str, entry: dict) -> str | None:
    """Return the name we already watch this board under, or None."""
    try:
        companies = yaml.safe_load(text).get("companies") or []
    except Exception:
        return None
    want = entry_key(entry)
    for e in companies:
        if entry_key(e) == want:
            return e.get("name") or "(unnamed)"
    return None


def insert_line(text: str, line: str) -> str:
    if MARKER in text:
        return text.replace(MARKER, MARKER + "\n" + line, 1)
    block = MARKER + "\n" + line + "\n\n"
    if FALLBACK_ANCHOR in text:
        return text.replace(FALLBACK_ANCHOR, block + FALLBACK_ANCHOR, 1)
    return text.rstrip("\n") + "\n\n" + block


def remove_line(text: str, entry: dict) -> tuple[str, bool]:
    """Drop the single line whose identifier matches. Only ever removes a line
    added under the marker style — a multi-line hand-written block is left
    alone and reported, because silently rewriting those loses their comments."""
    tok = (entry.get("token") or entry.get("site") or "").lower()
    out, removed = [], False
    for ln in text.splitlines():
        s = ln.strip()
        if (not removed and s.startswith("- {")
                and f"ats: {entry['ats']}" in s and tok in s.lower()):
            removed = True
            continue
        out.append(ln)
    return "\n".join(out) + "\n", removed


def validate(text: str) -> str | None:
    """Return an error string if the edit produced something the collector
    would choke on. Committing a broken watchlist.yml stops every future run,
    so this is the one place that must not be optimistic."""
    try:
        data = yaml.safe_load(text)
    except Exception as e:
        return f"the file no longer parses as YAML ({e})"
    companies = (data or {}).get("companies")
    if not isinstance(companies, list) or not companies:
        return "the file no longer has a `companies:` list"
    for e in companies:
        if not isinstance(e, dict) or not e.get("ats") or not e.get("name"):
            return f"an entry is malformed: {e!r}"
        if e["ats"] == "workday":
            if not all(e.get(k) for k in ("tenant", "shard", "site")):
                return f"workday entry missing tenant/shard/site: {e.get('name')!r}"
        elif not e.get("token"):
            return f"{e['ats']} entry missing a token: {e.get('name')!r}"
    keys = [entry_key(e) for e in companies]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        return f"duplicate identifiers: {sorted(dupes)}"
    return None


def _api(path: str) -> str:
    return f"https://api.github.com/repos/{REPO}/contents/{path}"


async def _get_path(fetch, path: str):
    """(text, sha) for any file in the repo. Raises FileNotFoundError on 404,
    which is a normal state for prefs.json before anyone has set a preference
    — distinct from a real failure, so callers can start empty instead of
    treating a first run as an outage."""
    code, body = await fetch(f"{_api(path)}?ref={BRANCH}", "GET", None, _headers())
    if code == 404:
        raise FileNotFoundError(path)
    if code != 200:
        raise RuntimeError(f"couldn't read {path} from GitHub (HTTP {code})")
    return base64.b64decode(body["content"]).decode(), body["sha"]


async def _get(fetch):
    return await _get_path(fetch, PATH)


async def apply_path(fetch, path: str, mutate, message: str,
                     validator=None) -> tuple[bool, str]:
    """Read `path`, apply `mutate(text) -> (text, ok, msg)`, commit.

    `sha` is optimistic locking: GitHub rejects the write if the file moved
    since we read it. The collector commits to this repo on every run and two
    members can run /prefs in the same second, so that is a real race, not a
    theoretical one — hence the retry, which re-reads and re-applies rather
    than re-sending the stale body.
    """
    if not enabled():
        return False, "no GITHUB_TOKEN set"
    for attempt in (1, 2, 3):
        try:
            text, sha = await _get_path(fetch, path)
        except FileNotFoundError:
            text, sha = "", None
        new_text, ok, msg = mutate(text)
        if not ok:
            return False, msg
        if validator:
            bad = validator(new_text)
            if bad:
                return False, f"refusing to commit — {bad}"
        payload = {"message": message, "branch": BRANCH,
                   "content": base64.b64encode(new_text.encode()).decode()}
        if sha:
            payload["sha"] = sha
        code, body = await fetch(_api(path), "PUT", payload, _headers())
        if code in (200, 201):
            return True, msg
        if code in (409, 422) and attempt < 3:
            log.info("%s moved under us; re-reading and retrying", path)
            continue
        return False, f"GitHub rejected the commit (HTTP {code})"
    return False, "the file kept changing underneath the write"


async def apply(fetch, mutate, message: str) -> tuple[bool, str]:
    """The watchlist. Validated before every write, because the collector reads
    it on every run and a broken commit stops the pipeline silently."""
    return await apply_path(fetch, PATH, mutate, message, validator=validate)
