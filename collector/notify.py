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
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps({"title": title, "body": body[:60000],
                         "labels": ["new-postings"]}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "intern-radar"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        log.info("opened issue: %s", json.loads(r.read())["html_url"])


def discord(body: str) -> None:
    hook = os.environ.get("DISCORD_WEBHOOK")
    if not hook:
        return
    # Discord caps a message at 2000 characters.
    chunk, buf = [], ""
    for line in body.split("\n"):
        if len(buf) + len(line) > 1900:
            chunk.append(buf)
            buf = ""
        buf += line + "\n"
    chunk.append(buf)
    for c in chunk[:5]:
        req = urllib.request.Request(
            hook, data=json.dumps({"content": c}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "intern-radar"},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=20)
        except Exception as e:
            log.warning("discord post failed: %s", e)


def send(new_jobs: list[dict], run_label: str) -> None:
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
    discord(body)
