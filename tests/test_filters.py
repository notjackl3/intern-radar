"""Regression tests for the Canada filter.

The first version of this filter matched bare city names and let
"Palo Alto, CA; Seattle, WA; Burlington, MA" through as Canadian, because
Burlington is also in Ontario. Every ambiguous city below is here so that
never silently comes back.

    python -m pytest tests/ -q      (or just: python tests/test_filters.py)
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from collector import filters as F  # noqa: E402

CANADIAN = [
    "Toronto, ON, Canada", "Vancouver, BC", "Montreal, QC", "Waterloo, ON",
    "London, ON", "Victoria, BC, Canada", "Ottawa, Ontario", "Remote in Canada",
    "Toronto", "Mississauga", "Chicago, IL; Toronto, ON, Canada",
    "Calgary, AB", "Halifax, NS", "Winnipeg, MB", "Québec City, QC",
]

NOT_CANADIAN = [
    # Cities that exist in both countries — must NOT match without a province.
    "Burlington, MA", "London, UK", "London, England", "Victoria, TX",
    "Hamilton, NJ", "Windsor, CT", "Waterloo, IA", "Cambridge, MA",
    "Richmond, VA", "Kingston, NY", "Stratford, CT",
    # Plain US / international.
    "Palo Alto, CA; Seattle, WA; Burlington, MA", "San Francisco, CA",
    "New York, NY", "Remote - US", "Atlanta, GA", "Boston, MA",
    "Austin, TX", "Bengaluru, India", "",
]

TITLES_IN = ["Software Engineer Intern", "Data Science Co-op",
             "Summer 2027 Intern - Backend", "Campus Analyst",
             "Early Talent Software Developer"]
TITLES_OUT = ["Senior Software Engineer", "Manager, Intern Programs",
              "Director of Engineering", "Staff ML Engineer",
              "Technical Recruiter, Campus"]

KEYWORDS = ["intern", "co-op", "campus", "early talent", "student"]


def test_canadian():
    for loc in CANADIAN:
        assert F.is_canadian(loc), f"should be Canadian: {loc!r}"


def test_not_canadian():
    for loc in NOT_CANADIAN:
        assert not F.is_canadian(loc), f"should NOT be Canadian: {loc!r}"


def test_titles():
    for t in TITLES_IN:
        assert F.title_matches(t, KEYWORDS), f"should match: {t!r}"
    for t in TITLES_OUT:
        assert not F.title_matches(t, KEYWORDS), f"should not match: {t!r}"


def test_terms():
    want = ["Summer 2027"]
    # A source that publishes a term is filtered on it.
    assert F.term_matches({"term": "Summer 2027", "title": "SWE Intern"}, want)
    assert not F.term_matches({"term": "Winter 2027", "title": "SWE Intern"}, want)
    # No term metadata, no term in the title -> pass through.
    assert F.term_matches({"term": None, "title": "SWE Intern"}, want)
    # No metadata, but the title names a term we didn't ask for -> reject.
    assert not F.term_matches({"term": None, "title": "Fall 2026 Co-op"}, want)
    assert F.term_matches({"term": None, "title": "Summer 2027 Intern"}, want)


if __name__ == "__main__":
    for fn in (test_canadian, test_not_canadian, test_titles, test_terms):
        fn()
        print(f"  ok  {fn.__name__}")
    print("all filter tests passed")
