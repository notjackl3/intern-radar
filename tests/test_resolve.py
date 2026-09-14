"""Tests for the /watch resolver.

The resolver's one job is to never write an identifier that polls nothing.
Everything here exists to pin that down, especially the distinction the whole
design rests on: 404 means the token is WRONG, 200-with-no-jobs means the
board is REAL and quiet. Confusing those two either rejects good employers or
silently subscribes to nothing.

    python tests/test_resolve.py
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bot"))

import resolve as R  # noqa: E402


def fetcher(routes):
    """routes: {url_substring: (status, body)}; anything unmatched 404s."""
    async def fetch(url, method="GET", payload=None):
        for frag, resp in routes.items():
            if frag in url:
                return resp
        return 404, None
    return fetch


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# --- URL parsing ---------------------------------------------------------
def test_parses_each_board_url():
    cases = {
        "https://job-boards.greenhouse.io/cohere/jobs/123":
            {"ats": "greenhouse", "token": "cohere"},
        "https://boards.greenhouse.io/stackadapt":
            {"ats": "greenhouse", "token": "stackadapt"},
        "https://jobs.lever.co/altaml/abc-def":
            {"ats": "lever", "token": "altaml"},
        "https://jobs.ashbyhq.com/wealthsimple/some-role":
            {"ats": "ashby", "token": "wealthsimple"},
        "jobs.ashbyhq.com/jobber":
            {"ats": "ashby", "token": "jobber"},
    }
    for url, want in cases.items():
        assert R.parse_url(url).entry == want, url


def test_workday_url_drops_locale_and_job_path():
    """https://rbc.wd3.myworkdayjobs.com/en-US/RBCEARLYTALENT1/job/Toronto/...
    The locale is optional and the site is the segment before /job/."""
    for url in ("https://rbc.wd3.myworkdayjobs.com/en-US/RBCEARLYTALENT1/job/Toronto/SWE_R-1",
                "https://rbc.wd3.myworkdayjobs.com/RBCEARLYTALENT1",
                "https://rbc.wd3.myworkdayjobs.com/fr-CA/RBCEARLYTALENT1/job/x"):
        assert R.parse_url(url).entry == {
            "ats": "workday", "tenant": "rbc", "shard": "wd3",
            "site": "RBCEARLYTALENT1"}, url


def test_greenhouse_embed_url_yields_the_real_token():
    """People paste the embed URL constantly. "embed" is not an employer."""
    r = R.parse_url("https://boards.greenhouse.io/embed/job_board?for=vidyard")
    assert r.entry == {"ats": "greenhouse", "token": "vidyard"}


def test_known_unpollable_ats_is_named_not_shrugged_at():
    for url, expect in (
            ("https://careers.smartrecruiters.com/Ubisoft", "SmartRecruiters"),
            ("https://ibm.avature.net/careers", "Avature"),
            ("https://careers.oracle.com/jobs", "Oracle")):
        r = R.parse_url(url)
        assert r.status == R.UNSUPPORTED and expect in r.note, url


def test_plain_name_is_not_mistaken_for_a_url():
    for name in ("Cohere", "Arc'teryx", "Thomson Reuters", "Neo Financial"):
        assert R.parse_url(name) is None, name


# --- slug guessing -------------------------------------------------------
def test_slug_candidates_cover_the_real_shapes():
    assert "cohere" in R.slug_candidates("Cohere")
    assert "stackadapt" in R.slug_candidates("StackAdapt Inc.")
    assert "top-hat" in R.slug_candidates("Top Hat")
    assert "arcteryx.com" in R.slug_candidates("Arc'teryx"), "accents and .com tokens"
    assert "neofinancial" in R.slug_candidates("Neo Financial")


# --- the 404 / empty distinction -----------------------------------------
def test_404_is_a_wrong_token_and_is_never_written():
    r = run(R.probe(fetcher({}), {"ats": "greenhouse", "token": "nope"}))
    assert r.status == R.NOT_FOUND
    assert not r.writable, "a 404 must never become a watchlist line"


def test_200_with_no_jobs_is_a_real_board_and_IS_writable():
    """PlayStation's Waterloo co-op board sits empty between cycles. Rejecting
    it would drop one of the best Canadian intern boards there is."""
    r = run(R.probe(fetcher({"waterloocoop": (200, {"jobs": []})}),
                    {"ats": "greenhouse", "token": "waterloocoop"}))
    assert r.status == R.EMPTY
    assert r.writable, "a quiet board is a legitimate subscription"
    assert "no open roles" in r.note


def test_live_board_reports_sample_and_canadian_count():
    body = {"jobs": [
        {"title": "SWE Intern", "location": {"name": "Toronto, ON, Canada"}},
        {"title": "ML Intern", "location": {"name": "Vancouver, BC"}},
        {"title": "Staff Eng", "location": {"name": "San Francisco, CA"}}]}
    r = run(R.probe(fetcher({"cohere": (200, body)}),
                    {"ats": "greenhouse", "token": "cohere"}))
    assert r.status == R.OK and r.total == 3 and r.canadian == 2
    assert r.sample[0]["title"] == "SWE Intern"
    assert "boards-api.greenhouse.io" in r.evidence


def test_each_ats_response_shape_is_normalised():
    shapes = {
        "lever": (R.LEVER.format(t="x"),
                  [{"text": "Dev Co-op", "categories": {"location": "Ottawa, ON"}}]),
        "ashby": (R.ASHBY.format(t="x"),
                  {"jobs": [{"title": "Dev Co-op", "location": "Ottawa, ON"}]}),
        "workday": (R.WORKDAY.format(tenant="x", shard="wd1", site="S"),
                    {"total": 91, "jobPostings": [
                        {"title": "Dev Co-op", "locationsText": "Ottawa, ON"}]}),
    }
    for ats, (url, body) in shapes.items():
        entry = ({"ats": ats, "token": "x"} if ats != "workday" else
                 {"ats": "workday", "tenant": "x", "shard": "wd1", "site": "S"})
        r = run(R.probe(fetcher({url.split("?")[0]: (200, body)}), entry))
        assert r.status == R.OK, ats
        assert r.sample[0] == {"title": "Dev Co-op", "location": "Ottawa, ON"}, ats
        assert r.canadian == 1, ats
    # Workday reports the true board size, not the page size.
    r = run(R.probe(fetcher({"myworkdayjobs": (200, shapes["workday"][1])}),
                    {"ats": "workday", "tenant": "x", "shard": "wd1", "site": "S"}))
    assert r.total == 91


# --- end-to-end resolve --------------------------------------------------
def test_resolve_by_name_finds_the_board_and_flags_the_guess():
    body = {"jobs": [{"title": "SWE Intern", "location": {"name": "Toronto, ON"}}]}
    r = run(R.resolve(fetcher({"/boards/cohere/": (200, body)}), "Cohere"))
    assert r.writable and r.entry["token"] == "cohere"
    assert "confirm" in r.note.lower(), "a guess must be flagged as a guess"


def test_resolve_by_name_tries_other_ats_before_giving_up():
    body = [{"text": "Dev Co-op", "categories": {"location": "Waterloo, ON"}}]
    r = run(R.resolve(fetcher({"api.lever.co/v0/postings/magnetforensics":
                               (200, body)}), "Magnet Forensics"))
    assert r.writable and r.ats == "lever"


def test_unresolvable_name_explains_workday_rather_than_just_failing():
    r = run(R.resolve(fetcher({}), "Some Private Company"))
    assert r.status == R.NOT_FOUND and not r.writable
    assert "Workday" in r.note and "careers page" in r.note


def test_url_wins_over_guessing():
    """A pasted URL is authoritative — we must not silently fall back to a
    name guess that resolves to a different company."""
    body = {"jobs": [{"title": "X", "location": {"name": "Toronto, ON"}}]}
    r = run(R.resolve(fetcher({"/boards/realtoken/": (200, body)}),
                      "https://boards.greenhouse.io/realtoken"))
    assert r.entry["token"] == "realtoken"


if __name__ == "__main__":
    import types
    fns = [v for k, v in sorted(vars().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"all {len(fns)} resolver tests passed")
