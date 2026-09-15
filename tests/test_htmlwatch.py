"""Tests for careers-page change detection.

Every test here exists because the obvious implementation has a quiet failure
mode. Hashing the raw page looks like it works — it returns a hash, the hash
compares fine — and then bills you for a model call on every single poll,
forever, because the page carries a fresh CSRF token each time.

    python tests/test_htmlwatch.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from collector import htmlwatch as W  # noqa: E402

# Two fetches of the SAME unchanged careers page. Only the volatile junk
# differs — this is what a real page does between two polls a minute apart.
FETCH_A = """
<html><head><meta name="csrf-token" content="a3f9c2e17b4d">
<link rel="stylesheet" href="/s/main.css?v=1789012345">
<script>window.__DATA={"serverTime":1789012345,"renderedAt":"2026-09-15T04:21:09Z"}</script>
</head><body data-ab-bucket="7" data-session="6f2c-11ee-aaaa">
<!-- rendered in 43ms by edge-yyz-3 -->
<ul>
  <li><a href="/careers/software-engineer-intern_e1c60e78-e451-4242-8473-5b8217394c36">SWE Intern</a></li>
  <li><a href="/careers/data-scientist_aa110000-1111-2222-3333-444455556666">Data Scientist</a></li>
</ul></body></html>
"""

FETCH_B = """
<html><head><meta name="csrf-token" content="99bb04de71ff">
<link rel="stylesheet" href="/s/main.css?v=1789012913">
<script>window.__DATA={"serverTime":1789012913,"renderedAt":"2026-09-15T04:31:53Z"}</script>
</head><body data-ab-bucket="2" data-session="8d41-11ee-bbbb">
<!-- rendered in 61ms by edge-yul-1 -->
<ul>
  <li><a href="/careers/data-scientist_aa110000-1111-2222-3333-444455556666">Data Scientist</a></li>
  <li><a href="/careers/software-engineer-intern_e1c60e78-e451-4242-8473-5b8217394c36">SWE Intern</a></li>
</ul></body></html>
"""

# Same page, one genuinely new posting.
FETCH_C = FETCH_B.replace(
    "</ul>",
    '<li><a href="/careers/ml-intern_bb220000-1111-2222-3333-444455556666">ML Intern</a></li></ul>')

SHOPIFY = r'/careers/[a-z0-9-]+_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'


def test_raw_hashing_is_defeated_by_volatile_junk():
    """The premise. If this ever fails, someone removed the junk from the
    fixtures and the rest of these tests stopped proving anything."""
    import hashlib
    a = hashlib.sha1(FETCH_A.encode()).hexdigest()
    b = hashlib.sha1(FETCH_B.encode()).hexdigest()
    assert a != b, "fixtures must differ, or this file tests nothing"


def test_identity_set_is_stable_across_two_fetches_of_an_unchanged_page():
    """The whole design. Same jobs, different tokens, different ORDER —
    identical fingerprint, so no model call and no cost."""
    ids_a = W.extract_ids(FETCH_A, SHOPIFY)
    ids_b = W.extract_ids(FETCH_B, SHOPIFY)
    assert ids_a == ids_b
    assert W.identity_hash(ids_a) == W.identity_hash(ids_b)
    assert W.diff(ids_a, ids_b)["changed"] is False


def test_reordering_alone_is_not_a_change():
    """Careers pages re-sort themselves constantly — "most relevant" is a
    moving target. Sorting the set is what stops that costing money."""
    assert W.extract_ids(FETCH_A, SHOPIFY) == W.extract_ids(FETCH_B, SHOPIFY)


def test_a_new_posting_is_detected_and_named():
    """Not "something changed" — WHICH job, so extraction runs on one page."""
    d = W.diff(W.extract_ids(FETCH_B, SHOPIFY), W.extract_ids(FETCH_C, SHOPIFY))
    assert d["changed"] is True
    assert d["added"] == ["bb220000-1111-2222-3333-444455556666"]
    assert d["removed"] == []


def test_a_closed_role_is_detected_for_free():
    d = W.diff(W.extract_ids(FETCH_C, SHOPIFY), W.extract_ids(FETCH_B, SHOPIFY))
    assert d["added"] == []
    assert d["removed"] == ["bb220000-1111-2222-3333-444455556666"]


def test_first_run_is_flagged_rather_than_alerted():
    """Day one must seed, not fire four hundred alerts — the same rule the ATS
    collector already follows."""
    d = W.diff(None, W.extract_ids(FETCH_A, SHOPIFY))
    assert d["first_run"] is True and len(d["added"]) == 2


# FETCH_B with the listings put back in FETCH_A's order: differs from FETCH_A
# ONLY in volatile junk.
FETCH_B_SAME_ORDER = FETCH_B.replace(
    """  <li><a href="/careers/data-scientist_aa110000-1111-2222-3333-444455556666">Data Scientist</a></li>
  <li><a href="/careers/software-engineer-intern_e1c60e78-e451-4242-8473-5b8217394c36">SWE Intern</a></li>""",
    """  <li><a href="/careers/software-engineer-intern_e1c60e78-e451-4242-8473-5b8217394c36">SWE Intern</a></li>
  <li><a href="/careers/data-scientist_aa110000-1111-2222-3333-444455556666">Data Scientist</a></li>""")


def test_fallback_hash_survives_the_junk_the_module_knows_about():
    """The fallback, on its best day: same jobs, same order, fresh tokens and
    timestamps. It must not fire here, or it is worse than useless."""
    assert FETCH_A != FETCH_B_SAME_ORDER, "fixtures must still differ textually"
    assert W.content_hash(FETCH_A) == W.content_hash(FETCH_B_SAME_ORDER)
    assert W.content_hash(FETCH_B) != W.content_hash(FETCH_C)


def test_fallback_hash_DOES_false_positive_on_reordering():
    """Pinning the fallback's real limitation rather than pretending it away.

    A whole-page hash cannot be order-insensitive — the order IS the page. So
    a site that re-sorts its listings (they all do) triggers a model call on
    every poll that re-sorts, which is most of them. That is precisely why the
    identity set is the plan and this is only the fallback. If this test ever
    starts failing, someone has taught content_hash to sort, and it should be
    replaced by an identity pattern instead."""
    assert W.content_hash(FETCH_A) != W.content_hash(FETCH_B)
    # ...whereas the identity set shrugs it off.
    assert W.extract_ids(FETCH_A, SHOPIFY) == W.extract_ids(FETCH_B, SHOPIFY)


def test_patterns_for_the_real_sites():
    """The link shapes actually observed on the sites with no JSON API."""
    cases = [
        # EA / Avature
        ('<a href="https://jobs.ea.com/en_US/careers/JobDetail/Lead-Infra/210357">x</a>',
         r'/careers/JobDetail/[^/"]+/(\d+)', ["210357"]),
        # SuccessFactors (Scotiabank, Rogers, Telus)
        ('<a href="/job/Toronto-Software-Developer-Co-op-ON/588123500/">x</a>',
         r'/job/[^/"]+/(\d+)', ["588123500"]),
        # Shopify
        ('<a href="/careers/backend-dev_0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9">x</a>',
         SHOPIFY, ["0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"]),
    ]
    for html, pattern, want in cases:
        assert W.extract_ids(html, pattern) == want, pattern


def test_no_match_yields_an_empty_set_not_a_crash():
    """A redesign that changes the URL shape shows up as zero IDs — which the
    caller must treat as breakage, not as "every job closed". This is the same
    trap as a wrong ATS token returning 200 with an empty list."""
    assert W.extract_ids("<html>redesigned</html>", SHOPIFY) == []
    d = W.diff(["a", "b"], [])
    assert d["removed"] == ["a", "b"] and d["added"] == []


def test_cost_scales_with_new_jobs_not_with_polling():
    """The arithmetic that justifies the whole module."""
    assert W.estimate_cost(0) == 0.0
    one = W.estimate_cost(1)
    assert 0.002 < one < 0.006, one
    # 8 employers polled every 20 minutes for a month, with ~40 new postings
    # in that month, costs the price of those 40 — not 34,560 page reads.
    month = W.estimate_cost(40)
    assert month < 0.25, f"${month:.2f}"


if __name__ == "__main__":
    import types
    fns = [v for k, v in sorted(vars().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"all {len(fns)} change-detection tests passed")
    print(f"\n  1 new posting      ${W.estimate_cost(1):.4f}")
    print(f"  40 in a month      ${W.estimate_cost(40):.2f}")
    print(f"  whole-page instead ${W.estimate_cost(8*72*30, tokens_in=8000, tokens_out=2500):.2f}")
