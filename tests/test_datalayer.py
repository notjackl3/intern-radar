"""Tests for the live Datalayer search and the hard level filter.

No network: these cover how results are shaped, filtered and merged.

    python tests/test_datalayer.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bot"))
os.environ.pop("DATALAYER_API_KEY", None)

import asyncio  # noqa: E402

import datalayer as DL  # noqa: E402
import matching as M  # noqa: E402


def posting(title, url="https://x/1", status="open", location="Toronto, Ontario, Canada"):
    return {"title": title, "source_url": url, "status": status, "location": location,
            "posted_at": "2026-11-26T00:00:00Z", "description": "d", "salary": None}


# --- the leaks seen in the live feed on 26 Nov 2026 ------------------------
LEAKS = [
    ("greenhouse", "intern", "Software Engineer II"),
    ("greenhouse", "intern", "Software Engineer II, Backend"),
    ("greenhouse", "intern", "AI Engineer, Internal Tools"),
    ("greenhouse", "intern", "Data Scientist, International"),
    ("ashby", "intern", "QA Engineer III (Internal Systems)"),
    ("lever", "intern", "Software Engineer in Test"),
    ("greenhouse", "newgrad", "Sr Software Engineer I/II, Operational AI"),
]
REAL = [
    ("greenhouse", "intern", "Software Engineering Intern"),
    ("lever", "intern", "Backend Co-op (Winter 2027)"),
    ("workday", "intern", "Student, Data Engineering"),
    ("simplify", "intern", "Software Engineer"),          # Simplify publishes a level
    ("greenhouse", "newgrad", "Software Engineer I, Entry-Level"),
    ("greenhouse", "newgrad", "Software Engineer II"),     # new-grad numerals are allowed
]


def test_leaks_are_rejected():
    for src, lvl, title in LEAKS:
        assert not M.level_ok({"source": src, "level": lvl, "title": title}), title


def test_real_roles_survive():
    for src, lvl, title in REAL:
        assert M.level_ok({"source": src, "level": lvl, "title": title}), title


def test_job_without_source_is_not_judged_on_title():
    # Older feeds and the prefs tests use bare jobs; they must still pass.
    assert M.level_ok({"title": "Role 1", "level": "intern"})


# --- shaping Datalayer results ---------------------------------------------
def test_non_software_internships_are_dropped():
    assert DL._job("Town", posting("310T Truck and Coach Technician"), "intern", "CA") is None
    assert DL._job("X", posting("2027 Summer Sales Intern"), "intern", "CA") is None
    assert DL._job("Seahawks", posting("Part Time Security Officer"), "intern", "US") is None
    assert DL._job("Synthires", posting("Postdoctoral Research Scientist (Remote)"), "intern", "CA") is None


def test_software_internship_kept_in_feed_shape():
    j = DL._job("Super.com", posting("Data Engineering Intern"), "intern", "CA")
    assert j["source"] == "datalayer" and j["level"] == "intern"
    assert j["countries"] == ["ca"] and j["id"].startswith("dl-")
    assert j["url"] == "https://x/1"


def test_closed_and_senior_dropped():
    assert DL._job("A", posting("Backend Intern", status="closed"), "intern", "CA") is None
    assert DL._job("A", posting("Senior Backend Engineer"), "intern", "CA") is None
    assert DL._job("A", posting("Software Engineer II"), "intern", "CA") is None
    assert DL._job("WeRide", posting("New Grads 2027 - Software Engineer"), "intern", "US") is None


def test_term_read_from_title():
    j = DL._job("Conifer", posting("Software Developer Intern - Summer 2027 - Onsite"),
                "intern", "US")
    assert j["term"] == "Summer 2027"


def test_terms_one_word_per_family():
    q = M.parse_query("backend or infra internships, Toronto")
    assert DL.terms_for(q) == ["backend", "infrastructure"]
    assert DL.terms_for({"families": ["swe"]}) == ["developer", "software"]
    assert DL.terms_for(None) == DL.DEFAULT_TERMS
    assert DL.terms_for(M.empty_query()) == DL.DEFAULT_TERMS
    # what the LLM parse returned for "low-level systems or infra internships"
    llm = {"families": ["infra"], "keywords_any": ["internship", "systems"]}
    assert DL.terms_for(llm) == ["infrastructure", "systems"]


def test_remote_is_a_hard_filter():
    q = M.parse_query("software engineer intern remote")
    assert "remote" in q["cities"]
    onsite = {"title": "Software Engineer Intern", "location": "Kent, WA", "level": "intern"}
    remote = {"title": "Software Engineer Intern", "location": "Remote in USA", "level": "intern"}
    dl_remote = {"title": "Dashboard Developer (Remote)", "location": "United States, US"}
    hybrid = {"title": "Software Intern", "location": "Remote", "description": "Hybrid, 3 days in office"}
    ranked = [j["title"] + j["location"] for j, _, _ in M.rank([onsite, remote, dl_remote, hybrid], q)]
    assert ranked and all("Kent" not in r for r in ranked) and len(ranked) == 2
    assert DL.terms_for(q) == ["developer remote", "software remote"]


def test_part_time_is_parsed_not_split():
    q = M.parse_query("part time software internship")
    assert q["employment"] == "part"
    assert "part" not in q["keywords_any"] and "time" not in q["keywords_any"]
    assert DL.terms_for(q) == ["developer part-time", "software part-time"]
    assert M.parse_query("full-time new grad")["employment"] == "full"
    assert M.parse_query("paid backend intern")["pay"] == "paid"
    assert M.parse_query("unpaid ok")["pay"] == "unpaid"
    assert M.parse_query("remote part time software intern canada")["keywords_any"] == []


def test_part_time_groups():
    q = M.parse_query("part time software internship")
    confirmed = {"title": "Software Intern (Part-time)", "level": "intern"}
    hours = {"title": "Developer Intern", "description": "20 hours per week, flexible"}
    unknown = {"title": "Software Engineer Intern", "location": "Toronto"}
    full = {"title": "Software Intern", "description": "This is a full-time, 40 hours/week role"}
    ranked = [j["title"] for j, _, _ in M.rank([unknown, full, hours, confirmed], q)]
    assert ranked[-1] == "Software Engineer Intern"           # unknown kept, last
    assert "Software Intern" not in ranked                     # full-time removed
    assert set(ranked[:2]) == {"Software Intern (Part-time)", "Developer Intern"}


def test_datalayer_full_time_label_is_not_trusted():
    # Datalayer labels many plain internships "Full-time".
    assert M.employment_of({"title": "Data Engineering Intern",
                            "employment_type": "Full-time"}) is None
    assert M.employment_of({"title": "Intern", "employment_type": "Part-time"}) == "part"


def test_paid_groups():
    q = M.parse_query("paid software internship")
    paid = {"title": "Software Intern", "salary": {"min": 25, "max": 30, "unit": "HOUR"}}
    unpaid = {"title": "Software Intern", "description": "This is an unpaid volunteer role"}
    unknown = {"title": "Developer Intern"}
    ranked = [j for j, _, _ in M.rank([unknown, unpaid, paid], q)]
    assert ranked[0] is paid and unpaid not in ranked and ranked[-1] is unknown


def test_merge_prefers_feed_copy():
    feed = [{"id": "f1", "company": "Super.com", "title": "Data Engineering Intern",
             "url": "https://a/1"}]
    live = [DL._job("Super.com", posting("Data Engineering Intern", url="https://b/2"),
                    "intern", "CA"),
            DL._job("Tecsys", posting("Intern - Developer", url="https://a/1?trk=x"),
                    "intern", "CA"),
            DL._job("Shopify", posting("Backend Intern", url="https://c/3"),
                    "intern", "CA")]
    merged, added = DL.merge(feed, live)
    assert added == 1 and merged[-1]["company"] == "Shopify"


def test_no_key_means_no_calls():
    assert not DL.enabled()
    assert asyncio.run(DL.search(None, ["intern"], ["ca"])) == []


if __name__ == "__main__":
    import types
    fns = [v for k, v in sorted(vars().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"all {len(fns)} datalayer tests passed")
