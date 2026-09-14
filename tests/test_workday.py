"""Regression tests for the Workday paging strategy.

Workday caps `limit` at 20, so a board is read 20 roles at a time. With a flat
six-page cap that meant Salesforce, Adobe, Cisco and Samsung — thousands of
open roles each — were polled 120 roles deep and the internships were never in
them. The adapter now reads `total` from the first response and falls back to
server-side keyword probes when plain paging cannot cover the board.

These tests fake the HTTP layer, so they run with no network.

    python tests/test_workday.py
"""
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from collector import adapters as A  # noqa: E402

CALLS: list[str] = []


def fake_board(total, titles, honours_search=True):
    """A Workday board holding `titles`, declaring `total` (None = says nothing).

    honours_search=False models a tenant whose searchText is ignored.
    """
    def _post(api, payload):
        CALLS.append(payload["searchText"])
        st = (payload["searchText"] or "").lower()
        pool = [t for t in titles
                if not st or not honours_search or st in t.lower()]
        page = pool[payload["offset"]:payload["offset"] + payload["limit"]]
        return types.SimpleNamespace(json=lambda: {
            "total": total,
            "jobPostings": [{"externalPath": f"/job/{t}", "title": t,
                             "locationsText": "Toronto, ON"} for t in page]})
    return _post


def run(total, titles, **kw):
    CALLS.clear()
    A._post = fake_board(total, titles, **kw)
    return list(A.workday("Co", "tenant", "wd1", "Site"))


def probes_used():
    return sorted(set(CALLS) - {""})


def test_small_board_is_read_end_to_end():
    """A campus site fits in a few pages. Reading it whole is complete and
    cheap — spending ten probes on it would be pure waste."""
    got = run(46, [f"Role {i}" for i in range(45)] + ["Software Engineer Intern"])
    assert len(got) == 46
    assert probes_used() == [], "must not probe a board it can read whole"


def test_large_board_falls_back_to_probes():
    """3,000 roles: six pages sees 120 of them, and the intern is not there."""
    titles = [f"Role {i}" for i in range(3000)] + ["Software Engineer Intern"]
    got = run(3002, titles)
    assert "Software Engineer Intern" in {g["title"] for g in got}
    assert "intern" in probes_used()


def test_probes_cover_french_boards():
    """Intact, Desjardins and CGI post in French. An English-only probe list
    would silently miss every one of their roles."""
    titles = [f"Role {i}" for i in range(3000)] + ["Stagiaire en développement"]
    got = run(3002, titles)
    assert "Stagiaire en développement" in {g["title"] for g in got}


def test_board_reporting_no_total_still_probes_when_it_stays_full():
    """If a tenant omits `total`, the page cap alone can't tell us whether we
    reached the end — so a board still handing back full pages gets probed."""
    titles = [f"Role {i}" for i in range(500)] + ["ML Intern"]
    got = run(None, titles)
    assert "ML Intern" in {g["title"] for g in got}


def test_board_reporting_no_total_does_not_probe_when_it_runs_dry():
    run(None, [f"Role {i}" for i in range(30)])
    assert probes_used() == [], "a 30-role board was fully read; don't probe it"


def test_results_are_deduped_across_probes():
    """The probes overlap by design — "co-op" and "coop" and "student" all
    match the same role — so the same externalPath must yield only once."""
    titles = [f"Role {i}" for i in range(3000)] + [
        "Student Intern Co-op Graduate Campus Programme"]
    got = run(3002, titles)
    ids = [g["id"] for g in got]
    assert len(ids) == len(set(ids))


def test_search_ignoring_tenant_degrades_to_plain_paging():
    """Some tenants ignore searchText. That must not crash or duplicate — it
    just means the probes return the same first pages, which dedupe away."""
    titles = [f"Role {i}" for i in range(3000)]
    got = run(3002, titles, honours_search=False)
    assert len(got) == len({g["id"] for g in got})
    assert len(got) == 120, "no search support: we still get the first 6 pages"


if __name__ == "__main__":
    for fn in (test_small_board_is_read_end_to_end,
               test_large_board_falls_back_to_probes,
               test_probes_cover_french_boards,
               test_board_reporting_no_total_still_probes_when_it_stays_full,
               test_board_reporting_no_total_does_not_probe_when_it_runs_dry,
               test_results_are_deduped_across_probes,
               test_search_ignoring_tenant_degrades_to_plain_paging):
        fn()
        print(f"  ok  {fn.__name__}")
    print("all workday tests passed")
