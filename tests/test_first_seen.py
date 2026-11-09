"""Tests for first-seen stamping and the age fallback it feeds.

Workday publishes `postedOn` only as a display string ("Posted 3 Days Ago"),
so `posted_at` is None for 60% of the Canadian intern market — every bank on
the watchlist. `/search hours:72` was therefore hiding 35 of 150 Canadian
intern roles. Recording when the collector first saw a posting fixes that.

The two ways this could go wrong are both silent and both expensive:

  * back-dating the existing board to "now" -> several thousand long-open
    roles look brand new and everyone gets a wall of alerts
  * letting "first seen" be presented as "posted" -> the bot states a posting
    date it does not have

    python tests/test_first_seen.py
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collector.main import stamp_first_seen  # noqa: E402

NOW = "2026-11-09T06:00:00+00:00"
EARLIER = "2026-11-01T06:00:00+00:00"


def job(jid, posted_at=None):
    return {"id": jid, "title": f"Role {jid}", "posted_at": posted_at}


def test_new_postings_get_stamped():
    a, b = job("a"), job("b")
    fs = stamp_first_seen([a, b], [b], {}, NOW)
    assert fs == {"b": NOW}
    assert b["first_seen"] == NOW
    assert "first_seen" not in a, "a was already open; it must stay undated"


def test_an_existing_stamp_is_never_overwritten():
    """A posting re-reported as new must keep the date we FIRST saw it."""
    a = job("a")
    fs = stamp_first_seen([a], [a], {"a": EARLIER}, NOW)
    assert fs["a"] == EARLIER
    assert a["first_seen"] == EARLIER


def test_seed_and_first_run_stamp_nothing():
    """The whole open board is marked seen on a seed. Stamping it would
    backdate thousands of roles to today and fire a wall of alerts."""
    jobs = [job(str(i)) for i in range(5)]
    fs = stamp_first_seen(jobs, jobs, {}, NOW, skip=True)
    assert fs == {}
    assert all("first_seen" not in j for j in jobs)


def test_stamps_attach_to_already_open_roles_on_later_runs():
    """A role stamped last week must carry that stamp on every later run,
    even though it is no longer in `new`."""
    a = job("a")
    stamp_first_seen([a], [], {"a": EARLIER}, NOW)
    assert a["first_seen"] == EARLIER


# ---------------------------------------------------------------------------
# the bot's half of the contract
# ---------------------------------------------------------------------------
def _age_helpers():
    """Import the bot's age logic without importing discord.py."""
    import datetime as _dt
    src = (ROOT / "bot" / "larpbuddy.py").read_text()
    start = src.index("def _hours_since(")
    end = src.index("def age_label(")
    ns = {"datetime": _dt.datetime, "timezone": _dt.timezone,
          "annotations": None}
    exec("from __future__ import annotations\n" + src[start:end], ns)
    return ns


def test_age_falls_back_to_first_seen():
    ns = _age_helpers()
    from datetime import datetime, timedelta, timezone
    seen = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    j = {"posted_at": None, "first_seen": seen}
    h = ns["age_hours"](j)
    assert h is not None and 4.5 < h < 5.5, h
    assert ns["age_is_estimated"](j) is True


def test_a_published_date_always_wins():
    ns = _age_helpers()
    from datetime import datetime, timedelta, timezone
    posted = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    seen = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    j = {"posted_at": posted, "first_seen": seen}
    h = ns["age_hours"](j)
    assert 1.5 < h < 2.5, h
    assert ns["age_is_estimated"](j) is False, "this role has a real date"


def test_no_date_and_no_stamp_is_still_unknown():
    """The pre-existing board. We must say we don't know, not guess."""
    ns = _age_helpers()
    assert ns["age_hours"]({"posted_at": None}) is None
    assert ns["age_is_estimated"]({"posted_at": None}) is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"all {len(tests)} first-seen tests passed")
