"""Delivery. GitHub Issues is the default because it is free, needs no extra
account, and GitHub emails you when one opens. Discord is optional."""
from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger("notify")


def _fmt_line(j: dict) -> str:
    bits = [f"**{j['company']}** — {j['title']}"]
    if j.get("location"):
        bits.append(f"  \n`{j['location']}`")
    if j.get("term"):
        bits.append(f" · _{j['term']}_")
    bits.append(f"  \n<{j['url']}>")
    return "".join(bits)


def build_body(new_jobs: list[dict]) -> str:
    by_source: dict[str, list[dict]] = {}
    for j in new_jobs:
        by_source.setdefault(j["source"], []).append(j)

    lines = [f"**{len(new_jobs)} new** since the last run.", ""]
    for src in sorted(by_source, key=lambda s: -len(by_source[s])):
        lines.append(f"### {src} ({len(by_source[src])})")
        for j in by_source[src][:40]:
            lines.append("- " + _fmt_line(j))
        if len(by_source[src]) > 40:
            lines.append(f"- …and {len(by_source[src]) - 40} more in `state/new.json`")
        lines.append("")
    return "\n".join(lines)


def github_issue(title: str, body: str) -> None:
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        log.info("no GITHUB_TOKEN/REPOSITORY — skipping issue")
        return
    # Assign the issue to the repo owner. GitHub always emails you about your
    # own assignments, so this works whether or not you "watch" the repo —
    # more robust than relying on a notification setting.
    owner = repo.split("/")[0]
    payload = {"title": title, "body": body[:60000],
               "labels": ["new-postings"], "assignees": [owner]}
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "intern-radar"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        log.info("opened issue: %s", json.loads(r.read())["html_url"])


SOURCE_COLOR = {
    "workday": 0x0B5CAB,
    "greenhouse": 0x25A36F,
    "ashby": 0x6B5BD2,
    "lever": 0xE05C3E,
    "simplify": 0x8A8F98,
}


def _embed(j: dict) -> dict:
    """One role as a Discord embed."""
    bits = []
    if j.get("location"):
        bits.append(j["location"][:120])
    if j.get("term"):
        bits.append(f"_{j['term']}_")
    e = {
        "title": (j.get("title") or "Untitled")[:250],
        "url": j.get("url") or None,
        "description": " · ".join(bits)[:400] or None,
        "color": SOURCE_COLOR.get(j.get("source"), 0x8A8F98),
        "author": {"name": (j.get("company") or "")[:250]},
        "footer": {"text": f"via {j.get('source', '?')}"},
    }
    if j.get("posted_at"):
        e["timestamp"] = j["posted_at"]
    return {k: v for k, v in e.items() if v is not None}


def _post(hook: str, payload: dict) -> bool:
    req = urllib.request.Request(
        hook, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "intern-radar"},
        method="POST")
    try:
        urllib.request.urlopen(req, timeout=20)
        return True
    except Exception as e:
        log.warning("discord post failed: %s", e)
        return False


def discord(new_jobs: list[dict], cfg: dict | None = None) -> None:
    """Post to a Discord channel webhook.

    A webhook needs no bot, no token and nothing hosted — GitHub Actions POSTs
    straight to the channel. `username` and `avatar_url` control how the
    message appears, which is how it shows up as LarpBuddy without a bot
    account existing at all.
    """
    hook = os.environ.get("DISCORD_WEBHOOK")
    if not hook or not new_jobs:
        return
    cfg = (cfg or {}).get("discord", {}) or {}
    name = cfg.get("username") or "intern-radar"
    avatar = cfg.get("avatar_url") or None
    cap = int(cfg.get("max_embeds", 10))

    n = len(new_jobs)
    header = (f"**{n} new Canadian software internship"
              f"{'s' if n != 1 else ''}**")

    # Discord allows at most 10 embeds per message, so page through.
    pages = [new_jobs[i:i + cap] for i in range(0, min(n, cap * 3), cap)]
    for idx, page in enumerate(pages):
        payload = {
            "username": name,
            "content": header if idx == 0 else None,
            "embeds": [_embed(j) for j in page],
        }
        if avatar:
            payload["avatar_url"] = avatar
        payload = {k: v for k, v in payload.items() if v is not None}
        if not _post(hook, payload):
            return
    if n > cap * 3:
        _post(hook, {"username": name,
                     "content": f"…and {n - cap * 3} more — see `state/new.json`."})


def send(new_jobs: list[dict], run_label: str, cfg: dict | None = None) -> None:
    if not new_jobs:
        log.info("nothing new — staying quiet")
        return
    body = build_body(new_jobs)
    top = new_jobs[0]
    title = (f"{len(new_jobs)} new Canadian intern posting"
             f"{'s' if len(new_jobs) != 1 else ''} — {top['company']}"
             f"{' +' + str(len(new_jobs) - 1) if len(new_jobs) > 1 else ''}"
             f" ({run_label})")
    github_issue(title, body)
    discord(new_jobs, cfg)
