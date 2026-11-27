"""Read and commit files in this repo through the GitHub contents API.

Used by prefs.py to keep state/prefs.json in the repo when GITHUB_TOKEN is set.
Needs a token with contents:write on this repo and nothing else.
"""
from __future__ import annotations

import base64
import logging
import os

log = logging.getLogger("repo")

REPO = os.environ.get("GITHUB_REPO", "notjackl3/intern-radar")
BRANCH = os.environ.get("GITHUB_BRANCH", "main")
TOKEN = os.environ.get("GITHUB_TOKEN")


def enabled() -> bool:
    return bool(TOKEN)


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "larpbuddy"}


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
