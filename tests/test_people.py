"""Tests for the MixRank people lookup behind /who.

The API cannot be trusted to filter, so every guard here is load-bearing:

  * MixRank silently IGNORES unknown query params, so there is no company
    filter on person search — `linkedin_company_id` returned an identical
    list to no filter at all, and `company_id=<Shopify>` returned Aptean,
    Zoox and Netflix. Filtering happens client-side or not at all.
  * MixRank returns every address it holds for a person, including previous
    employers and personal accounts. A /who for Jobber was offering
    `a.person@othercorp.com` — the wrong company, and someone's unrelated
    address. A non-matching email must be dropped, never shown.

    python tests/test_people.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bot"))

os.environ.pop("MIXRANK_KEY", None)

import people as PE  # noqa: E402


# ---------------------------------------------------------------------------
# company name matching
# ---------------------------------------------------------------------------
def test_matches_through_suffixes_and_parens():
    assert PE._same_company("Shopify", "Shopify Inc.")
    assert PE._same_company("Royal Bank of Canada (Early Talent)",
                            "Royal Bank of Canada")
    assert PE._same_company("Jobber", "Jobber")


def test_does_not_match_an_unrelated_company():
    assert not PE._same_company("Shopify", "Zoox")
    assert not PE._same_company("Jobber", "FundThrough")
    assert not PE._same_company("RBC", "TD Bank")


def test_known_limitation_containment_can_over_match():
    """Documented, not fixed: matching is containment-based, so a staffing
    agency with the employer's name inside it counts as the employer. The
    alternative — exact match — loses "Shopify" vs "Shopify Inc." and
    "Royal Bank of Canada (Early Talent)", which costs far more. The email
    guard is what stops a bad match reaching the user as a contact."""
    assert PE._same_company("Jobber", "Hotel Jobber Recruitment Services") is True


def test_empty_names_never_match():
    assert not PE._same_company("", "Shopify")
    assert not PE._same_company("Shopify", "")


# ---------------------------------------------------------------------------
# the email guard — this one shipped a bug
# ---------------------------------------------------------------------------
def test_accepts_an_address_at_the_employer():
    assert PE._is_work_email("a.person@shopify.com", "Shopify", "shopify.com")
    assert PE._is_work_email("b.person@rbc.com", "RBC", "rbc.com")


def test_accepts_when_the_real_domain_differs_from_the_listed_website():
    """MixRank lists Jobber's site as jobber.com; its staff are on
    getjobber.com. Equality would reject a correct address."""
    assert PE._is_work_email("a.person@getjobber.com", "Jobber", "jobber.com")


def test_accepts_a_known_subsidiary_domain():
    """RBC Capital Markets staff are on rbccm.com."""
    assert PE._is_work_email("c.person@rbccm.com", "RBC", "rbc.com")


def test_rejects_another_companys_address():
    """The actual bug: these were being shown under /who Jobber."""
    assert not PE._is_work_email("a.person@othercorp.com", "Jobber", "jobber.com")
    assert not PE._is_work_email("b.person@thirdparty.com", "Jobber", "jobber.com")
    assert not PE._is_work_email("personal.account@gmail.com", "Jobber", "jobber.com")


def test_rejects_junk():
    assert not PE._is_work_email("", "Shopify", "shopify.com")
    assert not PE._is_work_email("not-an-email", "Shopify", "shopify.com")


def test_summarise_drops_a_non_matching_email_rather_than_falling_back():
    detail = {"linkedin": {"name": {"full": "A B"}, "positions": [
        {"company_name": "Jobber", "title": "AE", "is_current": True,
         "start_date": "2026-06-01"}]},
        "emails": [{"email": "a.person@othercorp.com"}]}
    got = PE.summarise(detail, "Jobber", "University of Toronto", "jobber.com")
    assert got["email"] == "", "a wrong address must be dropped, not shown"


# ---------------------------------------------------------------------------
# domains, tenure, roles
# ---------------------------------------------------------------------------
def test_domain_of():
    assert PE.domain_of("http://www.jobber.com") == "jobber.com"
    assert PE.domain_of("https://www.shopify.com/") == "shopify.com"
    assert PE.domain_of(None) == ""


def test_tenure_labels():
    assert PE.tenure_label(None) == "tenure unknown"
    assert PE.tenure_label(0) == "just joined"
    assert PE.tenure_label(6) == "6mo there"
    assert "y" in PE.tenure_label(30)


def test_recruiter_detection():
    for t in ("Senior Recruiter", "Campus Recruiting Lead",
              "Talent Acquisition Partner", "University Relations",
              "Technical Sourcer"):
        assert PE._is_recruiter(t), t
    for t in ("Senior Software Engineer", "VP of Products", "Account Executive"):
        assert not PE._is_recruiter(t), t


def test_summarise_prefers_the_role_at_this_company():
    """People keep side projects flagged current for years — a DJ gig must not
    be reported as their role at the employer you asked about."""
    detail = {"linkedin": {"name": {"full": "Example Person"}, "positions": [
        {"company_name": "CKUT Radio", "title": "Host / DJ", "is_current": True,
         "start_date": "2007-07-01"},
        {"company_name": "Shopify", "title": "Lead Technical Recruiter",
         "is_current": True, "start_date": "2025-12-01"}]}}
    got = PE.summarise(detail, "Shopify", "University of Toronto", "shopify.com")
    assert got["title"] == "Lead Technical Recruiter"
    assert got["started"] == "2025-12-01"
    assert got["recruiter"] is True


def test_summarise_flags_alumni():
    detail = {"linkedin": {"name": {"full": "X"}, "positions": [],
                           "education": [{"school_name": "University of Toronto",
                                          "field_of_study": "Computer Science"}]}}
    got = PE.summarise(detail, "Shopify", "University of Toronto", "shopify.com")
    assert got["alumni"] is True
    assert "Computer Science" in got["alumni_detail"]


def test_summarise_survives_an_empty_profile():
    got = PE.summarise({}, "Shopify", "University of Toronto", "shopify.com")
    assert got["name"] == "Unknown" and got["email"] == ""


# ---------------------------------------------------------------------------
# inert without a key
# ---------------------------------------------------------------------------
def test_disabled_without_a_key():
    assert not PE.enabled()
    assert "MIXRANK_KEY" in PE.status()


def test_lookups_return_empty_without_a_key():
    import asyncio
    assert asyncio.run(PE.search_people(None, "Shopify", "recruiter")) == []
    assert asyncio.run(PE.resolve_company(None, "Shopify")) is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"all {len(tests)} people tests passed")
