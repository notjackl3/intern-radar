#!/usr/bin/env python3
"""Check every source in the watchlist actually works, from a machine that can
reach them.

    python -m collector.doctor

Run this once after you push, and again any time you add an employer. It hits
each board exactly once and tells you which endpoints answer, which are dead,
and which answer with zero jobs — that last one matters because
SmartRecruiters (and some Workday sites) return HTTP 200 with an empty list
for a BAD identifier, so a typo looks identical to a quiet board.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import adapters
from .main import DEFAULT_TIER, _tier_of, load_yaml

OK, EMPTY, DEAD = "ok", "empty", "DEAD"


def probe(entry) -> tuple[dict, str, int, str]:
    ats, name = entry["ats"], entry["name"]
    try:
        if ats == "workday":
            # One request, always. The probe fallback in the adapter would
            # fire on every board bigger than a single page and turn a
            # 121-request health check into a 1,000-request one — and it
            # answers a question doctor is not asking. All doctor wants to
            # know is whether this tenant/site answers at all.
            gen = adapters.workday(name, entry["tenant"], entry["shard"],
                                   entry["site"], max_pages=1, probes=False)
        elif ats in adapters.ATS:
            gen = adapters.ATS[ats](name, entry["token"])
        else:
            return entry, DEAD, 0, f"unknown ats {ats!r}"
        got = list(gen)
        return entry, (OK if got else EMPTY), len(got), ""
    except Exception as e:
        return entry, DEAD, 0, str(e)[:110]


def main() -> int:
    watchlist = load_yaml("watchlist.yml")
    entries = watchlist.get("companies", [])
    rows = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(probe, e) for e in entries]
        for fut in as_completed(futures):
            rows.append(fut.result())

    order = {DEAD: 0, EMPTY: 1, OK: 2}
    rows.sort(key=lambda r: (order[r[1]], r[0]["name"]))

    print(f"\n{'status':<7} {'tier':<5} {'ats':<16} {'employer':<38} {'jobs':>5}  note")
    print("-" * 100)
    for entry, status, n, note in rows:
        mark = {OK: "  ok  ", EMPTY: " EMPTY", DEAD: " DEAD "}[status]
        print(f"{mark:<7} {_tier_of(entry):<5} {entry['ats']:<16} "
              f"{entry['name'][:38]:<38} {n:>5}  {note}")

    counts = {s: sum(1 for _, st, _, _ in rows if st == s) for s in (OK, EMPTY, DEAD)}
    print("-" * 100)
    print(f"{counts[OK]} ok · {counts[EMPTY]} empty · {counts[DEAD]} dead "
          f"· {len(rows)} total")

    if counts[EMPTY]:
        print("\nEMPTY means the endpoint answered but returned no jobs. That is either\n"
              "a genuinely empty board or a wrong token/tenant/site — the two look\n"
              "identical on SmartRecruiters and some Workday sites. Open the employer's\n"
              "careers page and re-read the identifier from the URL.")
    if counts[DEAD]:
        print("\nDEAD means the request failed outright. A 404 usually means a bad\n"
              "identifier; a timeout or 403 usually means the host is throttling or\n"
              "blocking — back off before retrying.")

    # Broad sweeps are separate from the watchlist.
    print()
    for name, fn in (("simplify", adapters.simplify), ("hiring.cafe", adapters.hiringcafe)):
        try:
            n = len(list(fn()))
            print(f"  {'ok' if n else 'EMPTY':<6} {name:<16} {n:>6} rows")
        except Exception as e:
            print(f"  {'DEAD':<6} {name:<16} {'':>6}  {str(e)[:80]}")

    return 1 if counts[DEAD] else 0


if __name__ == "__main__":
    sys.exit(main())
