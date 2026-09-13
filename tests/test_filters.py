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

# --- domain filter -------------------------------------------------------
# Every string below is a REAL title from the first live run. Before the
# domain filter existed, 172 of 271 seeded roles were non-software.
import yaml as _yaml
_CFG = _yaml.safe_load(
    (pathlib.Path(__file__).resolve().parent.parent / "config.yml").read_text())
DOM_IN = [x.lower() for x in _CFG["domain_include_any"]]
DOM_OUT = [x.lower() for x in _CFG["domain_exclude_any"]]

IS_SOFTWARE = [
    "Software Engineer Intern/Co-op",
    "Graphics Software Engineer Intern/Co-op",
    "Firmware Engineer Intern/Co-op",
    "Machine Learning/Artificial Intelligence Intern/Co-op",
    "Software Developer Intern, MyGeotab (Winter/January 2027, 8 Months)",
    "Embedded Developer Intern (Winter/January 2027, 4 Months)",
    "Full-Stack Developer Intern - Route Optimization",
    "Front-End Developer Intern - Power Platform Integration",
    "DevOps Intern",
    "Data Engineering Intern",
    "Business Intelligence (BI) Developer Intern",
    "AI Data Analyst Intern",
    "Student, Junior Security Data Analyst",
    "Developer DevOPS - 4 months Co-op Internship",
    "Data Analyst Student - (4 Months) - Winter Term 2027",
    # French — Intact and Autodesk post in French; an English-only list misses these
    "Développeur logiciel I - 4 mois Coop/Stagiaire (Hiver 2027)",
    "Intern, AI Developer/ Stagiaire en développement IA",
    "Stagiaire en Développement Cloud, Intern Cloud Developer",
]

NOT_SOFTWARE = [
    # finance / business — the bulk of what leaked through before
    "2027 Winter Compliance Internal Audit Co-op (4 months)",
    "2027 CAE, Winter Internal Auditor (4 months)",
    "Intern - Investments, Capital Markets, Credit (May 2027 - 4 months)",
    "BMO Capital Markets Summer 2027 Internship Global Markets Analyst",
    "Intern, Capital Markets (8 month term)",
    "Intern- Finance, Risk Analytics (January 2027- 8 months)",
    "Financial Reporting & Accounting Intern",
    "Human Resources Intern - Ontario",
    "Law Student - Legal Affairs",
    "Campus Talent Acquisition Intern (Summer/May 2027, 12 Months)",
    "Channel Marketing Intern (Winter/ January 2027, 8-16 months)",
    "Total Rewards Intern (Winter/January 2027, 8+ Months)",
    "2027 Winter - Procurement, Business Analyst Intern (4 months)",
    "Account Executive Development Program (AEDP) 2026/2027 New Grad",
    # non-software engineering
    "Hardware Design Engineer Intern/Co-op",
    "Mechanical Engineering Intern (Summer/May 2027, 16 Months)",
    "Electrical Engineer Intern - Space Systems Division",
    "NPI Hardware Co-op (8 month - January 2027)",
    "Analog and Mixed Signal Engineer Intern/Co-op",
    # the tricky ones: contain a domain word but are not software jobs
    "Sales Systems & Data Governance Intern (Winter/January 2027, 8 Months)",
    "Intern - Portfolio Engineering, Capital Markets, CMIA (January 2027)",
    "New Graduate Program - 2027 Finance Associate Analyst Rotation",
]


def test_domain_keeps_software():
    for t in IS_SOFTWARE:
        assert F.domain_matches({"title": t}, DOM_IN, DOM_OUT), f"should KEEP: {t!r}"


def test_domain_drops_non_software():
    for t in NOT_SOFTWARE:
        assert not F.domain_matches({"title": t}, DOM_IN, DOM_OUT), f"should DROP: {t!r}"


def test_domain_exclude_beats_include():
    """A title-level exclusion wins even when a domain word is present —
    otherwise 'Sales Systems & Data Governance' sneaks in on 'data'."""
    j = {"title": "Sales Systems & Data Governance Intern"}
    assert not F.domain_matches(j, DOM_IN, DOM_OUT)


def test_domain_reads_description_but_not_for_exclusion():
    """Workday and Simplify publish no description, so a vague title must be
    rescuable by the description when one exists — but an excluded title stays
    excluded no matter what the description says."""
    vague = {"title": "Technology Intern (8 months)",
             "description": "You will build backend services in Python."}
    assert F.domain_matches(vague, DOM_IN, DOM_OUT)
    excluded = {"title": "Marketing Intern",
                "description": "Some software exposure; work with our developer team."}
    assert not F.domain_matches(excluded, DOM_IN, DOM_OUT)


def test_domain_passthrough_when_unconfigured():
    assert F.domain_matches({"title": "Anything At All"}, [], [])


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
    for fn in (test_canadian, test_not_canadian, test_titles, test_terms,
               test_domain_keeps_software, test_domain_drops_non_software,
               test_domain_exclude_beats_include,
               test_domain_reads_description_but_not_for_exclusion,
               test_domain_passthrough_when_unconfigured):
        fn()
        print(f"  ok  {fn.__name__}")
    print("all filter tests passed")
