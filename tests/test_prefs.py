"""Tests for per-member preferences and the daily-digest selection rule.

The failure modes worth guarding here are all quiet ones:

  * a member's first digest dumping four hundred backlogged roles on them
  * the same roles arriving again tomorrow
  * roles being marked "sent" when the DM actually failed
  * a garbage value in the stored file taking the whole digest loop down

    python tests/test_prefs.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bot"))

os.environ["PREFS_STORE"] = "local"
os.environ["DATA_DIR"] = tempfile.mkdtemp()

import prefs as P  # noqa: E402


def job(i, countries, level="intern"):
    return {"id": f"job{i}", "title": f"Role {i}", "countries": countries,
            "level": level, "location": "wherever"}


def run(c):
    return asyncio.get_event_loop().run_until_complete(c)


async def nofetch(*a, **k):
    raise AssertionError("local backend must not hit the network")


# --- the matching rule ---------------------------------------------------
def test_country_and_level_must_both_match():
    p = {"countries": ["ca"], "levels": ["intern"]}
    assert P.matches(job(1, ["ca"]), p)
    assert not P.matches(job(2, ["us"]), p)
    assert not P.matches(job(3, ["ca"], "newgrad"), p)
    assert P.matches(job(4, ["ca", "us"]), p), "a dual-country role counts for both"


def test_watching_both_countries_gets_both():
    p = {"countries": ["ca", "us"], "levels": ["intern", "newgrad"]}
    assert all(P.matches(j, p) for j in
               (job(1, ["ca"]), job(2, ["us"]), job(3, ["ca"], "newgrad")))
    assert not P.matches(job(4, ["other"]), p)


def test_untagged_job_is_not_hidden():
    """feed.json written by a collector older than country tagging has no
    `countries` key. Those must still reach people, or the digest goes silent
    for a day after every deploy."""
    p = {"countries": ["ca"], "levels": ["intern"]}
    assert P.matches({"id": "x", "level": "intern"}, p)


# --- normalising junk ----------------------------------------------------
def test_clean_repairs_anything():
    for junk in ({}, None, {"countries": [], "levels": []},
                 {"countries": ["mars"], "levels": ["phd"]},
                 {"hour_utc": "banana"}, {"hour_utc": 99}, {"hour_utc": -4},
                 {"digest": "yes"}, {"sent": None}):
        c = P.clean(junk)
        assert c["countries"] and all(x in P.COUNTRIES for x in c["countries"])
        assert c["levels"] and all(x in P.LEVELS for x in c["levels"])
        assert 0 <= c["hour_utc"] <= 23
        assert isinstance(c["digest"], bool)
        assert isinstance(c["sent"], list)


def test_clean_keeps_a_valid_choice():
    c = P.clean({"countries": ["us"], "levels": ["newgrad"], "hour_utc": 0,
                 "digest": False})
    assert c["countries"] == ["us"] and c["levels"] == ["newgrad"]
    assert c["hour_utc"] == 0 and c["digest"] is False


def test_sent_list_is_capped():
    c = P.clean({"sent": [f"id{i}" for i in range(P.MAX_SENT + 500)]})
    assert len(c["sent"]) == P.MAX_SENT


def test_describe_is_human_readable():
    d = P.describe({"countries": ["ca"], "levels": ["intern"], "hour_utc": 13})
    assert "Canada" in d and "internships" in d and "13:00 UTC" in d
    assert "OFF" in P.describe({"digest": False})


# --- the digest selection ------------------------------------------------
def test_pick_excludes_already_sent():
    p = {"countries": ["ca"], "levels": ["intern"], "sent": ["job1"]}
    got = P.pick([job(1, ["ca"]), job(2, ["ca"]), job(3, ["us"])], p)
    assert [j["id"] for j in got] == ["job2"]


def test_pick_is_empty_once_everything_has_been_sent():
    """The second day with no new postings must produce nothing, not a repeat."""
    jobs = [job(i, ["ca"]) for i in range(5)]
    p = {"countries": ["ca"], "levels": ["intern"]}
    first = P.pick(jobs, p)
    assert len(first) == 5
    p["sent"] = [j["id"] for j in first]
    assert P.pick(jobs, p) == []
    # a genuinely new posting still comes through
    assert [j["id"] for j in P.pick(jobs + [job(99, ["ca"])], p)] == ["job99"]


def test_first_digest_is_capped_so_nobody_gets_a_wall():
    jobs = [job(i, ["ca"]) for i in range(400)]
    p = P.clean({"countries": ["ca"], "levels": ["intern"]})
    assert len(P.pick(jobs, p)[:P.FIRST_DIGEST]) == P.FIRST_DIGEST


# --- storage -------------------------------------------------------------
def test_save_and_load_round_trip():
    def mutate(cur):
        return {**cur, "countries": ["us"], "levels": ["newgrad"], "name": "jack"}, True, "saved"

    ok, msg = run(P.save_user(nofetch, "111", mutate, "msg"))
    assert ok and msg == "saved"
    data = run(P.load(nofetch))
    assert data["users"]["111"]["countries"] == ["us"]
    assert data["users"]["111"]["levels"] == ["newgrad"]


def test_two_members_do_not_overwrite_each_other():
    run(P.save_user(nofetch, "aaa", lambda c: ({**c, "countries": ["ca"]}, True, "ok"), "m"))
    run(P.save_user(nofetch, "bbb", lambda c: ({**c, "countries": ["us"]}, True, "ok"), "m"))
    users = run(P.load(nofetch))["users"]
    assert users["aaa"]["countries"] == ["ca"]
    assert users["bbb"]["countries"] == ["us"]


def test_a_corrupt_store_does_not_take_the_digest_down():
    P.local_path().write_text("{ this is not json")
    assert run(P.load(nofetch)) == {"users": {}}


def test_mutate_can_refuse():
    ok, msg = run(P.save_user(nofetch, "ccc", lambda c: (c, False, "nope"), "m"))
    assert not ok and msg == "nope"


def test_tmp_backend_warns_loudly():
    """The DATA_DIR=/data-without-a-volume bug, caught at startup instead of
    a week later when someone asks why their settings reset."""
    old = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = "/tmp"
    try:
        assert "lost on the next redeploy" in (P.startup_warning() or "")
    finally:
        os.environ["DATA_DIR"] = old



# --- cadence: daily clock time vs "every N hours" ------------------------
from datetime import datetime, timedelta, timezone  # noqa: E402

NOW = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)


def ago(h):
    return (NOW - timedelta(hours=h)).isoformat()


def test_daily_fires_once_per_day_after_the_hour():
    assert P.due({"mode": "daily", "hour_utc": 13}, NOW)
    assert not P.due({"mode": "daily", "hour_utc": 18}, NOW), "too early"
    assert not P.due({"mode": "daily", "hour_utc": 13,
                      "last_digest": "2026-09-16"}, NOW), "already sent today"
    assert P.due({"mode": "daily", "hour_utc": 13,
                  "last_digest": "2026-09-15"}, NOW), "yesterday doesn't count"


def test_daily_is_gated_on_the_date_not_on_the_exact_minute():
    """A redeploy at 12:59 must not cost that member their 13:00 digest — the
    loop ticks again at 13:14 and the stored date still says yesterday."""
    p = {"mode": "daily", "hour_utc": 13, "last_digest": "2026-09-15"}
    for hour in (13, 14, 20, 23):
        assert P.due(p, NOW.replace(hour=hour)), hour


def test_every_is_a_rate_limit_not_a_fixed_schedule():
    """"At most every N hours" — so a new posting five minutes after the last
    digest waits, but one six hours later goes straight out."""
    assert P.due({"mode": "every", "every_hours": 6}, NOW), "never sent"
    assert not P.due({"mode": "every", "every_hours": 6, "last_digest_at": ago(2)}, NOW)
    assert P.due({"mode": "every", "every_hours": 6, "last_digest_at": ago(7)}, NOW)
    assert P.due({"mode": "every", "every_hours": 1, "last_digest_at": ago(2)}, NOW)


def test_every_ignores_the_daily_marker_and_vice_versa():
    """Switching cadence must not leave the member gated by the other mode's
    bookkeeping — that is how someone ends up silently waiting a full day."""
    assert P.due({"mode": "every", "every_hours": 3,
                  "last_digest": "2026-09-16"}, NOW), "daily marker is irrelevant here"
    assert P.due({"mode": "daily", "hour_utc": 13,
                  "last_digest_at": ago(0.5)}, NOW), "cycle stamp is irrelevant here"


def test_unreadable_timestamp_sends_rather_than_going_silent():
    """A corrupt stamp should cost you one duplicate digest, never permanent
    silence — silence is the failure nobody notices."""
    for junk in ("banana", "", 12345, None, "2026-13-45T99:99:99"):
        assert P.due({"mode": "every", "every_hours": 6, "last_digest_at": junk}, NOW)


def test_digest_off_beats_every_cadence():
    assert not P.due({"mode": "every", "every_hours": 1, "digest": False}, NOW)
    assert not P.due({"mode": "daily", "hour_utc": 0, "digest": False}, NOW)


def test_every_hours_is_clamped():
    assert P.clean({"every_hours": 0})["every_hours"] == P.MIN_EVERY
    assert P.clean({"every_hours": 9999})["every_hours"] == P.MAX_EVERY
    assert P.clean({"every_hours": "six"})["every_hours"] == P.DEFAULTS["every_hours"]
    assert P.clean({"every_hours": 3})["every_hours"] == 3


def test_unknown_mode_falls_back_to_daily():
    assert P.clean({"mode": "hourly"})["mode"] == "daily"
    assert P.clean({"mode": None})["mode"] == "daily"
    assert P.clean({"mode": "every"})["mode"] == "every"


def test_cadence_reads_clearly_in_both_modes():
    assert "13:00 UTC" in P.cadence({"mode": "daily", "hour_utc": 13})
    assert "every 3h" in P.cadence({"mode": "every", "every_hours": 3})
    assert "OFF" in P.cadence({"digest": False})


def test_naive_timestamp_does_not_crash():
    """Anything written by an older build has no timezone on it."""
    naive = NOW.replace(tzinfo=None) - timedelta(hours=7)
    assert P.due({"mode": "every", "every_hours": 6,
                  "last_digest_at": naive.isoformat()}, NOW)


if __name__ == "__main__":
    import types
    fns = [v for k, v in sorted(vars().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"all {len(fns)} prefs tests passed")
