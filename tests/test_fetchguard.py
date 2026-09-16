"""Tests for fetching a URL a stranger typed into Discord.

The cost question is the easy half. The dangerous half is that a bot which
fetches any URL it is given becomes a proxy into its own private network and
reads the result back out into a public channel. Most of this file is about
that.

    python tests/test_fetchguard.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from collector import fetchguard as G  # noqa: E402


def resolver(mapping):
    def r(host):
        if host not in mapping:
            raise OSError("NXDOMAIN")
        return mapping[host]
    return r


PUBLIC = resolver({"jobs.example.com": ["93.184.216.34"],
                   "evil.example.com": ["127.0.0.1"],
                   "split.example.com": ["93.184.216.34", "10.0.0.5"],
                   "v6.example.com": ["2606:2800:220:1:248:1893:25c8:1946"],
                   "v6bad.example.com": ["::1"],
                   "v4mapped.example.com": ["::ffff:127.0.0.1"]})


def rejects(url, fragment=None, res=PUBLIC):
    try:
        G.check(url, resolver=res)
    except G.Rejected as e:
        if fragment:
            assert fragment.lower() in str(e).lower(), f"{url}: {e}"
        return True
    raise AssertionError(f"should have been rejected: {url}")


# --- SSRF ----------------------------------------------------------------
def test_cloud_metadata_and_loopback_are_refused():
    """169.254.169.254 hands out IAM credentials on most cloud providers. A
    bot that will fetch it and print the result is a credential leak with a
    Discord front-end."""
    for url in ("http://169.254.169.254/latest/meta-data/iam/security-credentials/",
                "http://127.0.0.1:8080/admin",
                "http://localhost/",
                "http://[::1]/",
                "http://10.0.0.5/",
                "http://192.168.1.1/",
                "http://172.16.0.9/"):
        rejects(url, res=resolver({"localhost": ["127.0.0.1"]}))


def test_a_public_name_pointing_at_loopback_is_refused():
    """The reason this checks the resolved ADDRESS and not the name. A
    hostname-based blocklist never sees this one — the name looks fine."""
    rejects("https://evil.example.com/careers", "127.0.0.1")


def test_a_name_with_one_bad_record_is_refused_entirely():
    """A name with both a public and a private record would otherwise be a coin
    flip at connect time, and the bad outcome is the interesting one."""
    rejects("https://split.example.com/careers", "10.0.0.5")


def test_ipv6_loopback_is_refused_and_public_ipv6_is_fine():
    rejects("https://v6bad.example.com/careers")
    assert G.check("https://v6.example.com/careers", resolver=PUBLIC)


def test_ipv4_mapped_ipv6_loopback_is_refused():
    """::ffff:127.0.0.1 is loopback wearing a v6 hat. The v6 flags alone do
    not catch it, which is exactly the kind of gap that makes an SSRF guard
    look like it works right up until it doesn't."""
    rejects("https://v4mapped.example.com/careers")


def test_non_http_schemes_are_refused():
    for url in ("file:///etc/passwd", "ftp://x.example.com/",
                "gopher://x.example.com/", "data:text/html,<b>x",
                "jar:http://x.example.com!/"):
        rejects(url)


def test_odd_ports_are_refused():
    """6379 is redis, 5432 postgres, 9200 elasticsearch — all things that
    answer without authentication on a private network."""
    for p in (6379, 5432, 9200, 8080, 22):
        rejects(f"http://jobs.example.com:{p}/careers", "port")


def test_embedded_credentials_are_refused():
    rejects("https://user:pass@jobs.example.com/careers", "credential")


def test_bare_internal_hostnames_are_refused():
    rejects("http://metadata/computeMetadata/v1/", "internal",
            res=resolver({"metadata": ["169.254.169.254"]}))


def test_a_normal_careers_url_passes():
    assert G.check("https://jobs.example.com/careers", resolver=PUBLIC)


def test_unresolvable_host_is_refused_not_attempted():
    rejects("https://nope.example.com/careers", "resolve")


# --- size and tokens -----------------------------------------------------
def test_size_cap_aborts_mid_stream():
    """Enforced while reading, not from Content-Length — that header can be
    absent, wrong, or a deliberate lie."""
    chunks = (b"x" * 1000 for _ in range(10_000))       # 10 MB
    try:
        G.read_capped(chunks, max_bytes=50_000)
    except G.Rejected as e:
        assert "exceeded" in str(e)
        return
    raise AssertionError("a 10MB response should have been refused")


def test_size_cap_allows_a_normal_page():
    assert G.read_capped([b"a" * 1000, b"b" * 1000], max_bytes=50_000)


def test_script_and_style_are_stripped_before_counting_tokens():
    """On a modern careers page these are most of the bytes and none of the
    meaning. Leaving them in is the single biggest avoidable cost."""
    html = ("<html><head><style>" + "a{color:red}" * 2000 + "</style></head>"
            "<body><script>" + "var x=1;" * 2000 + "</script>"
            "<h1>Software Engineer Intern</h1><p>Toronto, ON</p></body></html>")
    text = G.to_text(html)
    assert "Software Engineer Intern" in text and "Toronto, ON" in text
    assert "color:red" not in text and "var x" not in text
    assert G.estimate_tokens(text) < 50, G.estimate_tokens(text)


def test_token_cap_truncates_and_says_so():
    """Truncation is returned rather than hidden, so a partial read is never
    mistaken for a whole page."""
    text, cut = G.cap_tokens("x" * 1_000_000, max_tokens=100)
    assert cut and G.estimate_tokens(text) <= 100
    text, cut = G.cap_tokens("short", max_tokens=100)
    assert not cut


def test_content_type_gate():
    G.check_content_type("text/html; charset=utf-8")
    G.check_content_type(None)                      # absent is fine; size caps
    for bad in ("application/pdf", "image/png", "video/mp4",
                "application/octet-stream"):
        try:
            G.check_content_type(bad)
        except G.Rejected:
            continue
        raise AssertionError(f"{bad} should be refused")


# --- prompt injection ----------------------------------------------------
def test_extracted_links_off_host_are_dropped():
    """A page can say "apply at evil.com" — whether because it is hostile or
    because a model was talked into it. Nothing a model reads off an untrusted
    page is trusted on its own say-so."""
    out = G.same_host_only(
        ["https://jobs.example.com/careers/1",
         "https://careers.jobs.example.com/2",
         "https://evil.com/apply",
         "http://169.254.169.254/",
         "/careers/relative-is-fine"],
        "https://jobs.example.com/careers")
    assert out == ["https://jobs.example.com/careers/1",
                   "https://careers.jobs.example.com/2",
                   "/careers/relative-is-fine"]


# --- budget --------------------------------------------------------------
def test_per_source_cap_stops_a_renumbering_site():
    b = G.Budget({"per_source_per_day": 3})
    for _ in range(3):
        ok, _ = b.allow("shopify", "2026-09-16")
        assert ok
        b.record("shopify", "2026-09-16")
    ok, why = b.allow("shopify", "2026-09-16")
    assert not ok and "renumbering" in why


def test_one_bad_source_does_not_starve_the_others():
    b = G.Budget({"per_source_per_day": 2, "global_per_day": 100})
    for _ in range(2):
        b.record("shopify", "2026-09-16")
    assert not b.allow("shopify", "2026-09-16")[0]
    assert b.allow("ea", "2026-09-16")[0], "a different source is unaffected"


def test_global_cap_stops_forty_well_behaved_sources_adding_up():
    b = G.Budget({"per_source_per_day": 10, "global_per_day": 5})
    for i in range(5):
        b.record(f"src{i}", "2026-09-16")
    ok, why = b.allow("src9", "2026-09-16")
    assert not ok and "daily extraction cap" in why


def test_the_counter_resets_the_next_day():
    b = G.Budget({"global_per_day": 2})
    b.record("a", "2026-09-16")
    b.record("a", "2026-09-16")
    assert not b.allow("a", "2026-09-16")[0]
    assert b.allow("a", "2026-09-17")[0]
    assert b.spent("2026-09-17") == 0


def test_worst_possible_day_is_a_number_you_can_look_at():
    """If the worst case isn't acceptable, the ceilings are wrong — and you
    find that out here rather than on an invoice."""
    cost = G.Budget().worst_case_cost()
    assert cost < 2.0, f"${cost:.2f} per day worst case is too much"
    assert cost * 30 < 40


if __name__ == "__main__":
    import types
    fns = [v for k, v in sorted(vars().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"all {len(fns)} fetch-guard tests passed")
    b = G.Budget()
    print(f"\n  worst possible day  ${b.worst_case_cost():.2f}")
    print(f"  worst possible month ${b.worst_case_cost()*30:.2f}")
