"""Tests for the country filter.

The Canadian rule already had teeth: a bare city proves nothing, because
Burlington, London, Windsor, Victoria and Waterloo all exist on both sides of
the border. Adding the US introduces the mirror-image trap — "Washington, ON"
and "London, ON" must not become American just because a US state shares the
name. So the rule is per-part exclusivity: Canada wins a single location
outright, while a listing spanning both countries is genuinely both.

    python tests/test_country.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from collector import filters as F  # noqa: E402

CASES = {
    # --- Canada only ---
    "Toronto, ON, Canada": {"ca"},
    "Vancouver, BC": {"ca"},
    "Remote in Canada": {"ca"},
    "Waterloo, ON": {"ca"},
    "Montréal, QC": {"ca"},
    # Cities that share a name with a US state or city, but carry a province.
    "Washington, ON": {"ca"},
    "London, ON": {"ca"},
    "Windsor, ON": {"ca"},
    "Victoria, BC": {"ca"},
    "Cambridge, ON": {"ca"},

    # --- US only ---
    "New York, NY": {"us"},
    "Seattle, WA": {"us"},
    "Palo Alto, CA; Burlington, MA": {"us"},
    "Austin, TX": {"us"},
    "Remote - US": {"us"},
    "Remote (US)": {"us"},
    "US Remote": {"us"},
    "Remote, USA": {"us"},
    "United States": {"us"},
    "Houston, TX": {"us"},
    "Columbus, OH": {"us"},
    "San Francisco": {"us"},

    # --- both: a genuine multi-site req ---
    "Toronto, ON; New York, NY": {"ca", "us"},
    "Vancouver, BC | Seattle, WA": {"ca", "us"},
    "Canada; United Kingdom; United States": {"ca", "us"},

    # --- neither ---
    "London, UK": set(),
    "Bengaluru, India": set(),
    "Berlin, Germany": set(),
    "Remote": set(),
    "": set(),
}


def test_country_of():
    for loc, want in CASES.items():
        got = F.country_of(loc)
        assert got == want, f"{loc!r}: got {sorted(got)}, want {sorted(want)}"


def test_canada_still_beats_us_on_a_single_location():
    """The regression this file exists for. Every one of these has a US
    state or city name in it and is unambiguously Canadian."""
    for loc in ("Washington, ON", "London, ON", "Hamilton, ON", "Windsor, ON",
                "Cambridge, ON", "Richmond, BC", "Kingston, ON"):
        assert F.country_of(loc) == {"ca"}, loc
        assert not F.is_american(loc), loc


def test_bare_us_is_never_matched_inside_a_word():
    """A bare "us" appears inside Houston, campus and Columbus. Matching it
    would tag half the Canadian feed as American."""
    for loc in ("Campus - Waterloo, ON", "Campus Recruiting, Toronto, ON"):
        assert F.country_of(loc) == {"ca"}, loc


def test_unreadable_location_is_kept_as_other_not_dropped():
    """A role we can't place is a visibility problem, not a reason to bin it."""
    cfg = {"countries": ["ca", "us", "other"], "title_include_any": ["intern"],
           "domain_include_any": ["software"]}
    jobs = [{"title": "Software Engineer Intern", "location": "???", "url": "x"}]
    out = F.apply_filters(jobs, cfg)
    assert len(out) == 1 and out[0]["countries"] == ["other"]


def test_other_is_excluded_unless_asked_for():
    cfg = {"countries": ["ca", "us"], "title_include_any": ["intern"],
           "domain_include_any": ["software"]}
    jobs = [{"title": "Software Engineer Intern", "location": "Berlin, Germany", "url": "x"}]
    assert F.apply_filters(jobs, cfg) == []


def test_every_surviving_job_is_tagged():
    cfg = {"countries": ["ca", "us"], "title_include_any": ["intern"],
           "domain_include_any": ["software"]}
    jobs = [{"title": "Software Engineer Intern", "location": "Toronto, ON", "url": "a"},
            {"title": "Software Engineer Intern", "location": "Seattle, WA", "url": "b"},
            {"title": "Software Engineer Intern", "location": "Toronto, ON; NYC", "url": "c"}]
    out = F.apply_filters(jobs, cfg)
    assert [j["countries"] for j in out] == [["ca"], ["us"], ["ca", "us"]]
    assert all("level" in j for j in out)


def test_legacy_canada_only_still_works():
    """An old config with no `countries:` key must behave exactly as before."""
    assert F.wanted_countries({"canada_only": True}) == ["ca"]
    assert F.wanted_countries({"canada_only": False}) == ["ca", "us", "other"]
    assert F.wanted_countries({"countries": ["us"]}) == ["us"]


def test_no_province_code_is_also_a_state_code():
    """The two code sets must not overlap, or every "City, XX" would be
    ambiguous and the whole scheme collapses."""
    import re
    prov = set(re.findall(r"[A-Z]{2,3}", F.PROV_CODES))
    state = set(re.findall(r"[A-Z]{2}", F.STATE_CODES))
    assert not (prov & state), f"overlapping codes: {sorted(prov & state)}"


if __name__ == "__main__":
    import types
    fns = [v for k, v in sorted(vars().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"all {len(fns)} country tests passed")
