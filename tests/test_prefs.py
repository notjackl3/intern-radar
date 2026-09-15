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


if __name__ == "__main__":
    import types
    fns = [v for k, v in sorted(vars().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"all {len(fns)} prefs tests passed")
