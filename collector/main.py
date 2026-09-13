#!/usr/bin/env python3
"""intern-radar — poll job sources at the origin, report only what's new.

    python -m collector.main            # normal run
    python -m collector.main --dry-run  # no state written, no notifications
    python -m collector.main --seed     # record everything as already-seen
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import yaml

from . import adapters, filters, notify

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
SEEN = STATE / "seen.json"
NEW = STATE / "new.json"
FEED = STATE / "feed.json"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("radar")


def load_yaml(name):
    return yaml.safe_load((ROOT / name).read_text())


# Which tier a source belongs to when the watchlist doesn't say.
#   fast — one GET returns the whole board. Cheap enough to hit every 20 min.
#   full — needs paging (Workday: 20 results/page) or a large download.
DEFAULT_TIER = {
    "greenhouse": "fast", "lever": "fast", "ashby": "fast",
    "smartrecruiters": "full", "workday": "full",
}


def _tier_of(entry) -> str:
    return entry.get("tier") or DEFAULT_TIER.get(entry["ats"], "full")


def _fetch(entry) -> tuple[str, list[dict] | None, str | None]:
    ats, name = entry["ats"], entry["name"]
    try:
        if ats == "workday":
            gen = adapters.workday(name, entry["tenant"], entry["shard"], entry["site"])
        elif ats in adapters.ATS:
            gen = adapters.ATS[ats](name, entry["token"])
        else:
            return name, None, f"unknown ats {ats!r}"
        return name, list(gen), None
    except Exception as e:
        return name, None, str(e)


def collect(watchlist, cfg, tier: str) -> list[dict]:
    """tier 'fast' polls only the single-GET boards. tier 'full' polls
    everything, including the paged ones and the broad sweeps."""
    entries = [e for e in watchlist.get("companies", [])
               if tier == "full" or _tier_of(e) == "fast"]
    jobs, errors = [], []

    # Concurrency is across HOSTS — adapters.py holds a per-host lock and a
    # 1.2s per-host delay, so no single employer ever sees parallel requests.
    workers = min(int(cfg.get("max_workers", 8)), max(1, len(entries)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch, e): e for e in entries}
        for fut in as_completed(futures):
            name, got, err = fut.result()
            if err:
                errors.append(f"{name}: {err}")
                log.warning("FAILED %-34s %s", name[:34], err)
            else:
                jobs.extend(got)
                log.info("%-34s %4d roles", name[:34], len(got))

    if tier == "full":
        for name, fn in (("simplify", adapters.simplify),
                         ("hiring.cafe", adapters.hiringcafe)):
            if not cfg.get("sources", {}).get(name, True):
                continue
            try:
                got = list(fn())
                jobs.extend(got)
                log.info("%-34s %4d roles  (broad sweep)", name, len(got))
            except Exception as e:
                errors.append(f"{name}: {e}")
                log.warning("FAILED %s: %s", name, e)

    if errors:
        log.warning("%d source(s) failed this run", len(errors))
    return jobs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true",
                    help="mark everything currently open as already-seen")
    ap.add_argument("--tier", choices=["fast", "full"], default="full",
                    help="fast = single-GET boards only; full = everything")
    args = ap.parse_args()

    cfg = load_yaml("config.yml")
    watchlist = load_yaml("watchlist.yml")
    STATE.mkdir(exist_ok=True)

    t0 = datetime.now(timezone.utc)
    raw = collect(watchlist, cfg, args.tier)
    matched = filters.apply_filters(raw, cfg)
    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    log.info("[%s tier] %d raw → %d matched, in %.0fs",
             args.tier, len(raw), len(matched), elapsed)

    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    first_run = not SEEN.exists()

    new = [j for j in matched if j["id"] not in seen]
    new.sort(key=lambda j: (j.get("posted_at") or "", j["company"]), reverse=True)

    stamp = datetime.now(timezone.utc).strftime("%d %b %H:%M UTC") + f" · {args.tier}"

    if args.dry_run:
        log.info("DRY RUN — %d new", len(new))
        for j in new[:25]:
            print(f"  {j['company'][:28]:30} {j['title'][:52]:54} {j['location'][:28]}")
        return 0

    SEEN.write_text(json.dumps(sorted(seen | {j['id'] for j in matched}), indent=0))
    NEW.write_text(json.dumps(new, indent=2))
    # feed.json is "everything currently open". Only a full run sees
    # everything, so a fast run must not overwrite it with its subset.
    if args.tier == "full":
        FEED.write_text(json.dumps(
            {"generated_at": datetime.now(timezone.utc).isoformat(),
             "open_count": len(matched), "new_count": len(new),
             "tier": args.tier, "jobs": matched}, indent=2))

    if args.seed or first_run:
        log.info("seeded %d currently-open roles — alerts start from the next run",
                 len(matched))
        return 0

    notify.send(new, stamp)
    log.info("done — %d new, %d open, %d ids tracked", len(new), len(matched), len(seen) + len(new))
    return 0


if __name__ == "__main__":
    sys.exit(main())
