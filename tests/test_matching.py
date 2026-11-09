"""Tests for role families, city normalising, query parsing and scoring.

Every case here is a failure that was actually measured against the live
1,623-role feed on 9 Nov 2026, or a quiet regression that would be expensive
to notice in production:

  * "backend security" returning zero roles (the AND bug)
  * "AI engineer" finding 2 of 34 real AI/ML roles (the literal-match bug)
  * a city filter missing two thirds of Toronto (21 spellings)
  * a time filter silently dropping every Workday role (no posting date)
  * a narrow query producing an EMPTY digest instead of a ranked one
  * the semantic layer raising instead of degrading when there is no API key

    python tests/test_matching.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bot"))

# The semantic layer must be provably inert without a key — that is the state
# the bot deploys in until billing is sorted, so it is the state we test.
os.environ.pop("OPENAI_API_KEY", None)

import matching as M  # noqa: E402
import semantic as SEM  # noqa: E402


def job(title, location="Toronto, ON, Canada", company="Acme",
        posted_at=None, term=None, level="intern", jid=None):
    return {"id": jid or title, "title": title, "location": location,
            "company": company, "posted_at": posted_at, "term": term,
            "level": level, "countries": ["ca"], "source": "greenhouse"}


# ---------------------------------------------------------------------------
# the two bugs that motivated the module
# ---------------------------------------------------------------------------
def test_two_words_are_or_not_and():
    """"backend security" matched 0 of 1,623 roles as a substring, and 0 as an
    AND of its words: no single title carries both. It must return both."""
    jobs = [job("Software Engineer Intern, Backend"),
            job("Security Engineer Intern"),
            job("Marketing Intern")]
    q = M.parse_query("backend security")
    assert set(q["families"]) == {"backend", "security"}, q["families"]
    hits = [j["title"] for j, _, w in M.rank(jobs, q) if w]
    assert "Software Engineer Intern, Backend" in hits
    assert "Security Engineer Intern" in hits
    assert "Marketing Intern" not in hits


def test_ai_engineer_finds_the_whole_family():
    """A literal "ai engineer" found 2 of 34. The family must reach the titles
    employers actually use."""
    jobs = [job("Deep Learning Research Intern"),
            job("Machine Learning Intern 2027"),
            job("AI/ML Research Intern"),
            job("Applied Scientist Intern"),
            job("Backend Developer Intern")]
    q = M.parse_query("AI engineer intern")
    assert q["families"] == ["ml"], q["families"]
    assert q["level"] == "intern"
    hits = {j["title"] for j, _, w in M.rank(jobs, q) if w}
    assert len(hits) == 4, hits
    assert "Backend Developer Intern" not in hits


def test_short_tokens_match_whole_words_only():
    """"ai" must not fire on "maintain", "ml" must not fire on "html"."""
    assert not M.families_in_title("Maintenance Intern") & {"ml"}
    assert "ml" in M.families_in_title("AI Engineer Intern")
    jobs = [job("HTML Email Maintenance Intern")]
    assert not [w for _, _, w in M.rank(jobs, M.parse_query("ai")) if w]


# ---------------------------------------------------------------------------
# cities
# ---------------------------------------------------------------------------
def test_toronto_spellings_all_normalise():
    """21 distinct strings for Toronto in the live feed; all must count."""
    for loc in ("Toronto, ON, Canada", "Toronto, Ontario", "Toronto, ON",
                "Toronto", "Toronto, CAN", "North York, ON", "Scarborough"):
        assert "toronto" in M.cities_in_location(loc), loc


def test_multi_site_role_counts_for_both_cities():
    got = M.cities_in_location("Toronto, ON; New York, NY")
    assert "toronto" in got and "new york" in got


def test_city_scores_but_does_not_exclude():
    """A Vancouver role must still be rankable for someone who asked for
    Toronto — just below the Toronto ones. Excluding it is how digests empty."""
    jobs = [job("SWE Intern", location="Vancouver, BC, Canada", jid="v"),
            job("SWE Intern", location="Toronto, ON, Canada", jid="t")]
    q = M.parse_query("software intern in toronto")
    ranked = M.rank(jobs, q)
    assert len(ranked) == 2, "the non-matching city must not be dropped"
    assert ranked[0][0]["id"] == "t", "Toronto should rank first"


# ---------------------------------------------------------------------------
# full-sentence parsing
# ---------------------------------------------------------------------------
def test_parses_the_three_dimensions_at_once():
    q = M.parse_query("ML internships in Toronto for next summer", year_hint=2027)
    assert q["families"] == ["ml"]
    assert q["cities"] == ["toronto"]
    assert q["terms"] == ["Summer 2027"]
    assert q["level"] == "intern"


def test_explicit_year_beats_the_hint():
    q = M.parse_query("winter 2026 co-op", year_hint=2027)
    assert q["terms"] == ["Winter 2026"], q["terms"]


def test_newgrad_phrasing():
    for text in ("new grad roles", "entry-level software", "new graduate"):
        assert M.parse_query(text)["level"] == "newgrad", text


# ---------------------------------------------------------------------------
# scoring invariants
# ---------------------------------------------------------------------------
def test_company_query_outranks_a_family_match():
    """`/search RBC` must lead with RBC, not with every software role."""
    jobs = [job("Software Engineer Intern", company="Shopify", jid="s"),
            job("Capital Markets Technology Intern", company="RBC", jid="r")]
    ranked = M.rank(jobs, M.parse_query("RBC"))
    assert ranked[0][0]["id"] == "r", [(j["company"], s) for j, s, _ in ranked]


def test_keywords_none_is_a_hard_exclusion():
    jobs = [job("Security Engineer Intern")]
    q = M.merge_query(M.parse_query("engineer"), {"keywords_none": ["security"]})
    assert M.rank(jobs, q) == [], "an excluded role must not be ranked at all"


def test_mid_level_roles_are_demoted_not_dropped():
    """"Software Engineer II, Backend" arrives tagged `intern` (the collector's
    new-grad regex has `engineer i\\b`, which misses "Engineer II") and ranked
    FIRST for "backend". It must fall below a real intern role — but still be
    present, since some employers title new-grad reqs that way."""
    jobs = [job("Software Engineer II, Backend", jid="mid"),
            job("Backend Engineer Intern", jid="intern")]
    ranked = M.rank(jobs, M.parse_query("backend"))
    assert [j["id"] for j, _, _ in ranked] == ["intern", "mid"], \
        [(j["id"], s) for j, s, _ in ranked]
    assert len(ranked) == 2, "demote, never drop"


def test_mid_level_penalty_skips_real_newgrad_roles():
    """A role the collector tagged `newgrad` is SUPPOSED to be level-2-ish,
    so the penalty must not apply to it."""
    j = job("Software Engineer II", level="newgrad")
    assert M.score(j, M.parse_query("software"))[0] > 0


def test_a_year_in_the_title_is_not_a_seniority_marker():
    assert not M.MID_LEVEL_RE.search("AI/ML Engineer Intern (2027)")
    assert not M.MID_LEVEL_RE.search("Machine Learning Intern 2027")


def test_generic_engineering_does_not_trigger_the_software_family():
    """"data engineering in Vancouver" must not light up the whole software
    family and rank firmware roles above the data ones asked for."""
    q = M.parse_query("data engineering in Vancouver")
    assert q["families"] == ["data"], q["families"]


def test_undated_roles_are_not_buried():
    """Workday publishes no readable date for ~60% of the Canadian market.
    Those must score above a genuinely stale role, not below it."""
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    undated, stale = job("SWE Intern", jid="u"), job("SWE Intern", posted_at=old, jid="s")
    assert M.score(undated, None)[0] > M.score(stale, None)[0]


def test_empty_query_ranks_everything_by_freshness():
    """Existing members have no query. Their behaviour must not change: every
    role still eligible, ordered newest-first."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    jobs = [job("A", posted_at=(now - timedelta(days=10)).isoformat(), jid="a"),
            job("B", posted_at=now.isoformat(), jid="b")]
    ranked = M.rank(jobs, M.empty_query())
    assert len(ranked) == 2
    assert ranked[0][0]["id"] == "b"
    assert M.count_hits(ranked) == 0, "freshness alone is not a query 'hit'"


def test_a_narrow_query_still_returns_a_ranked_list():
    """The single most important invariant. CA+intern is ~150 roles; a query
    for something absent must still rank, or the digest goes silent and the
    member mutes it."""
    jobs = [job("Marketing Intern"), job("Finance Intern")]
    ranked = M.rank(jobs, M.parse_query("quantum compiler engineering in Halifax"))
    assert len(ranked) == 2, "nothing matched, but nothing may be dropped"
    assert M.count_hits(ranked) == 0, "and it must report honestly that 0 matched"


def test_count_hits_separates_matches_from_filler():
    jobs = [job("Machine Learning Intern", jid="m"), job("Marketing Intern", jid="k")]
    ranked = M.rank(jobs, M.parse_query("machine learning"))
    assert len(ranked) == 2
    assert M.count_hits(ranked) == 1


# ---------------------------------------------------------------------------
# merge + describe
# ---------------------------------------------------------------------------
def test_negation_kills_the_whole_family_not_just_the_word():
    """"low-level systems work, not web" parses to families {infra, frontend}
    — frontend because "web" is one of its aliases. Before this fix, the
    family's +4 made "Frontend Engineer Intern" rank FIRST at 4.4 for a query
    that said NOT web, with "Systems Software Intern" at 0.4."""
    q = M.merge_query(M.parse_query("low-level systems work, not web"),
                      {"keywords_none": ["web"]})
    assert "frontend" in q["families"], "precondition: the alias matched"
    q = M.drop_negated_families(q)
    assert "frontend" not in q["families"]
    assert "infra" in q["families"], "the rest of the query must survive"

    jobs = [job("Frontend Engineer Intern", jid="f"),
            job("Systems Software Intern", jid="s")]
    ranked = M.rank(jobs, q)
    assert ranked[0][0]["id"] == "s", [(j["id"], sc) for j, sc, _ in ranked]


def test_drop_negated_families_is_a_noop_without_negations():
    q = M.parse_query("backend security")
    assert M.drop_negated_families(q)["families"] == q["families"]


def test_infra_terms_match_real_titles():
    """"systems" was an ALIAS (what people type) but not a TERM (what
    employers write), so "Systems Software Intern" matched nothing."""
    assert "infra" in M.families_in_title("Systems Software Intern")
    assert "infra" in M.families_in_title("Low-Level Firmware Intern")


def test_merge_keeps_base_where_override_is_silent():
    base = M.parse_query("ml in toronto")
    merged = M.merge_query(base, {"families": [], "keywords_none": ["senior"]})
    assert merged["families"] == ["ml"], "an empty override must not erase"
    assert merged["cities"] == ["toronto"]
    assert merged["keywords_none"] == ["senior"]


def test_describe_never_raises_on_junk():
    for junk in ({}, None, {"families": ["nope"]}, {"cities": None}):
        assert isinstance(M.describe_query(junk), str)


# ---------------------------------------------------------------------------
# the semantic layer must be inert, not broken, without a key
# ---------------------------------------------------------------------------
def test_semantic_disabled_without_a_key():
    assert not SEM.enabled()
    assert "OPENAI_API_KEY" in SEM.status()


def test_parse_falls_back_to_the_deterministic_parser():
    import asyncio
    q = asyncio.run(SEM.parse_query(None, "ML internships in Toronto", year_hint=2027))
    assert q["families"] == ["ml"] and q["cities"] == ["toronto"]


def test_rerank_is_a_passthrough_without_a_key():
    import asyncio
    scored = M.rank([job("SWE Intern")], M.parse_query("software"))
    out = asyncio.run(SEM.rerank(None, scored, "software"))
    assert out == scored


def test_embed_returns_none_without_a_key():
    import asyncio
    got = asyncio.run(SEM.embed(None, ["x"]))
    assert got is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"all {len(tests)} matching tests passed")
