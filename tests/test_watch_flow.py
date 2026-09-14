"""End-to-end test of what /watch actually does, minus Discord.

resolve -> format the line -> insert it -> validate the result. If this passes,
the only untested part of /watch is the button click and the HTTP PUT.

    python tests/test_watch_flow.py
"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bot"))

import repo as Rp     # noqa: E402
import resolve as R   # noqa: E402

WATCHLIST = (ROOT / "watchlist.yml").read_text()


def fetcher(routes):
    async def fetch(url, method="GET", payload=None, headers=None):
        for frag, resp in routes.items():
            if frag in url:
                return resp
        return 404, None
    return fetch


def run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_the_real_watchlist_is_valid_right_now():
    """If this fails, the collector is broken, not the test."""
    assert Rp.validate(WATCHLIST) is None


def test_adding_a_new_employer_to_the_real_watchlist():
    body = {"jobs": [{"title": "Software Engineer Intern",
                      "location": {"name": "Toronto, ON, Canada"}}]}
    res = run(R.resolve(fetcher({"/boards/exampleco/": (200, body)}), "ExampleCo"))
    assert res.writable

    assert Rp.find_existing(WATCHLIST, res.entry) is None
    out = Rp.insert_line(WATCHLIST, Rp.format_line(res.entry, "ExampleCo", "jack"))
    assert Rp.validate(out) is None

    import yaml
    before = yaml.safe_load(WATCHLIST)["companies"]
    after = yaml.safe_load(out)["companies"]
    assert len(after) == len(before) + 1
    # and the file is still a document a human wants to read
    assert "NOT POLLABLE" in out and out.count("# ----") >= WATCHLIST.count("# ----")


def test_watching_something_already_watched_is_caught_before_the_commit():
    """Cohere is already on the list. /watch must say so rather than writing a
    duplicate the collector would then poll twice."""
    res = run(R.probe(
        fetcher({"job-board/cohere": (200, {"jobs": []})}),
        {"ats": "ashby", "token": "cohere"}))
    assert Rp.find_existing(WATCHLIST, res.entry) == "Cohere"


def test_workday_url_round_trips_into_a_valid_line():
    res = R.parse_url(
        "https://bb.wd3.myworkdayjobs.com/en-US/QNX/job/Ottawa/Dev_R-123")
    line = Rp.format_line(res.entry, "BlackBerry QNX", "jack")
    # already on the list, so drop it first to avoid a duplicate error
    text, _ = Rp.remove_line(WATCHLIST, {"ats": "workday", "token": "", "site": "QNX"})
    assert Rp.validate(Rp.insert_line(text, line)) is None


if __name__ == "__main__":
    import types
    fns = [v for k, v in sorted(vars().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"all {len(fns)} watch-flow tests passed")
