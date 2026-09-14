"""Tests for the watchlist writer behind /watch and /unwatch.

Two things matter here and nothing else does:

  1. The comments survive. watchlist.yml is two thirds explanation — which ATS
     each employer is on, why Qualcomm was dropped, which boards are quiet
     rather than broken. A YAML round-trip would erase all of it on the first
     /watch, so the edit is textual and this pins that down.
  2. A broken file is never committed. The collector reads this file on every
     run; committing something unparseable stops the whole pipeline silently.

    python tests/test_repo.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bot"))

import repo as Rp  # noqa: E402

SAMPLE = """# Tier 0 — poll the ATS at the origin.
#
# Adding a Workday employer: read the tenant/shard/site off the careers URL.

companies:

  # ---- Workday -----------------------------------------------------------
  # RBC runs a DEDICATED early-talent site — highest-value endpoint in here.
  - name: Royal Bank of Canada (Early Talent)
    ats: workday
    tenant: rbc
    shard: wd3
    site: RBCEARLYTALENT1
    tier: fast

  # ---- Greenhouse --------------------------------------------------------
  - { name: Cohere, ats: greenhouse, token: cohere }   # Toronto HQ

  # ---- NOT POLLABLE — documented so nobody re-researches them -------------
  # Avature: IBM, Ericsson   iCIMS: Kinaxis
"""


def test_insert_keeps_every_comment():
    out = Rp.insert_line(SAMPLE, Rp.format_line(
        {"ats": "ashby", "token": "jobber"}, "Jobber", "jack"))
    for comment in ("# ---- Workday", "# RBC runs a DEDICATED early-talent site",
                    "# ---- NOT POLLABLE", "# Avature: IBM, Ericsson",
                    "# Toronto HQ", "highest-value endpoint"):
        assert comment in out, f"lost: {comment}"
    assert Rp.validate(out) is None


def test_insert_lands_before_the_not_pollable_block_first_time():
    out = Rp.insert_line(SAMPLE, Rp.format_line(
        {"ats": "ashby", "token": "jobber"}, "Jobber"))
    assert out.index("token: jobber") < out.index("NOT POLLABLE")
    assert Rp.MARKER in out


def test_second_insert_reuses_the_marker_rather_than_nesting():
    out = Rp.insert_line(SAMPLE, Rp.format_line({"ats": "ashby", "token": "jobber"}, "Jobber"))
    out = Rp.insert_line(out, Rp.format_line({"ats": "lever", "token": "kabam"}, "Kabam"))
    assert out.count(Rp.MARKER) == 1
    assert "token: jobber" in out and "token: kabam" in out
    assert Rp.validate(out) is None


def test_workday_line_carries_all_three_identifiers():
    line = Rp.format_line(
        {"ats": "workday", "tenant": "bb", "shard": "wd3", "site": "QNX"},
        "BlackBerry QNX", "jack")
    out = Rp.insert_line(SAMPLE, line)
    assert Rp.validate(out) is None
    import yaml
    e = [c for c in yaml.safe_load(out)["companies"] if c["name"] == "BlackBerry QNX"][0]
    assert (e["tenant"], e["shard"], e["site"]) == ("bb", "wd3", "QNX")


def test_name_with_punctuation_does_not_break_the_flow_mapping():
    """`Ontario Teachers' Pension Plan` and names with commas or colons would
    otherwise produce YAML that parses as something else entirely."""
    for name in ("Ontario Teachers' Pension Plan", "Thomson Reuters, Legal",
                 "Jane: Software", 'The "Weather" Network'):
        out = Rp.insert_line(SAMPLE, Rp.format_line(
            {"ats": "lever", "token": "t" + str(abs(hash(name)) % 999)}, name))
        assert Rp.validate(out) is None, name


def test_duplicate_is_detected_under_a_different_display_name():
    assert Rp.find_existing(SAMPLE, {"ats": "greenhouse", "token": "cohere"}) \
        == "Cohere"
    assert Rp.find_existing(SAMPLE, {"ats": "greenhouse", "token": "COHERE"}) \
        == "Cohere", "tokens compare case-insensitively"
    assert Rp.find_existing(SAMPLE, {"ats": "workday", "tenant": "rbc",
                                     "shard": "wd3", "site": "RBCEARLYTALENT1"})
    assert Rp.find_existing(SAMPLE, {"ats": "ashby", "token": "jobber"}) is None


def test_validate_rejects_what_would_break_the_collector():
    assert Rp.validate("companies: []")
    assert Rp.validate("nothing: here")
    assert Rp.validate("companies:\n  - { name: X, ats: greenhouse }"), "no token"
    assert Rp.validate("companies:\n  - { name: X, ats: workday, tenant: t }"), "no site"
    assert Rp.validate("companies:\n  - {{{ broken"), "not YAML"
    dupe = SAMPLE + "\n  - { name: Cohere Again, ats: greenhouse, token: cohere }\n"
    assert "duplicate" in Rp.validate(dupe)


def test_remove_takes_exactly_one_line():
    out = Rp.insert_line(SAMPLE, Rp.format_line({"ats": "ashby", "token": "jobber"}, "Jobber"))
    out, removed = Rp.remove_line(out, {"ats": "ashby", "token": "jobber"})
    assert removed and "jobber" not in out
    assert "token: cohere" in out, "must not touch neighbouring entries"
    assert Rp.validate(out) is None


def test_remove_leaves_hand_written_multiline_blocks_alone():
    """RBC is written as an indented block with its own comment. Deleting one
    line out of it would produce a half-entry, so remove_line declines."""
    out, removed = Rp.remove_line(SAMPLE, {"ats": "workday", "token": "",
                                           "site": "RBCEARLYTALENT1"})
    assert not removed
    assert Rp.validate(out) is None


if __name__ == "__main__":
    import types
    fns = [v for k, v in sorted(vars().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"all {len(fns)} watchlist-writer tests passed")
