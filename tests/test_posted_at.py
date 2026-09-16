"""Which timestamp each source counts as "posted".

This exists because of a real, quiet bug. Greenhouse publishes BOTH
`first_published` and `updated_at`, and the adapter used `updated_at` — which
Greenhouse bumps on any edit, including a typo fix or a salary-band tweak. So a
role first posted three weeks ago reported as "7 days ago" and sorted to the
top of a digest as though it were brand new.

Observed live on 2026-09-16: a Robinhood req with
    first_published  2026-08-21T15:30:30-04:00
    updated_at       2026-09-09T01:06:59-04:00
— nineteen days apart.

Getting this wrong doesn't crash anything. It just quietly makes "newest first"
mean "most recently edited", which is a different and much less useful thing.

    python tests/test_posted_at.py
"""
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from collector import adapters as A  # noqa: E402


def fake(payload):
    A._get = lambda url: types.SimpleNamespace(json=lambda: payload)


def test_greenhouse_prefers_first_published_over_updated_at():
    fake({"jobs": [{"id": 1, "title": "SWE Intern", "location": {"name": "Toronto, ON"},
                    "absolute_url": "u",
                    "first_published": "2026-08-21T15:30:30-04:00",
                    "updated_at": "2026-09-09T01:06:59-04:00"}]})
    got = list(A.greenhouse("Robinhood", "robinhood"))[0]
    assert got["posted_at"].startswith("2026-08-21"), got["posted_at"]


def test_greenhouse_falls_back_to_updated_at_when_absent():
    """Don't trade a wrong date for no date — an undated role sorts last and
    can't be age-filtered at all, which is strictly worse."""
    fake({"jobs": [{"id": 2, "title": "SWE Intern", "location": {"name": "Toronto, ON"},
                    "absolute_url": "u", "updated_at": "2026-09-09T01:06:59-04:00"}]})
    got = list(A.greenhouse("X", "x"))[0]
    assert got["posted_at"].startswith("2026-09-09")


def test_greenhouse_with_neither_field_is_undated_not_crashing():
    fake({"jobs": [{"id": 3, "title": "SWE Intern", "location": {"name": "Toronto, ON"},
                    "absolute_url": "u"}]})
    assert list(A.greenhouse("X", "x"))[0]["posted_at"] is None


def test_lever_uses_created_not_updated():
    """Lever's createdAt is epoch MILLIseconds and is already first-published —
    there is no edit-bumped field to confuse it with."""
    fake([{"id": "abc", "text": "Dev Co-op", "categories": {"location": "Ottawa, ON"},
           "hostedUrl": "u", "createdAt": 1788525580208}])
    got = list(A.lever("Promise Robotics", "promiserobotics"))[0]
    assert got["posted_at"].startswith("2026-09-"), got["posted_at"]


def test_ashby_uses_published_at():
    fake({"jobs": [{"id": "z", "title": "SWE Intern", "location": "Toronto, ON",
                    "jobUrl": "u", "publishedAt": "2026-09-01T10:00:00Z"}]})
    got = list(A.ashby("Cohere", "cohere"))[0]
    assert got["posted_at"].startswith("2026-09-01")


def test_workday_is_honestly_undated():
    """Workday's postedOn is a localised display string ("Posted 3 Days Ago").
    Inventing a date from it would be worse than admitting we don't have one —
    /latest hours: tells the member how many were hidden for this reason."""
    A._post = lambda api, payload: types.SimpleNamespace(json=lambda: {
        "total": 1, "jobPostings": [{"externalPath": "/p", "title": "Intern",
                                     "locationsText": "Toronto, ON",
                                     "postedOn": "Posted 3 Days Ago"}]})
    got = list(A.workday("RBC", "rbc", "wd3", "S"))[0]
    assert got["posted_at"] is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(vars().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"all {len(fns)} posted-at tests passed")
