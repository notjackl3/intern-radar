"""Change detection for careers pages that have no JSON API.

The obvious plan — hash the page, call a model when the hash moves — does not
survive contact with a real careers site. Every fetch of the same unchanged
page differs, because pages carry:

    <input name="csrf" value="a3f9...">          a new token every request
    "serverTime": 1789012345                     a timestamp
    data-session="6f2c-..."  ?v=1789012345       cache-busters, analytics IDs
    <!-- rendered in 43ms -->                     render timings
    data-ab-bucket="7"                           A/B assignment

so a raw hash changes on every poll, the model runs every poll, and the
"only when it changed" saving evaporates — silently, while the bill grows.

So don't hash the page. Hash the IDENTITY SET.

Every careers page links to its jobs, and those links carry a stable ID —
Shopify's `/careers/<slug>_<uuid>`, EA's `/JobDetail/<slug>/<numericId>`,
SuccessFactors' `/job/<slug>/<id>`. Extract those, sort them, hash that. The
result is immune to tokens, timestamps and reordering, because none of that
is in the set.

And it gives you something a page hash never could: not "something changed"
but WHICH jobs are new. Which means the expensive step — reading a job's
title, location and dates — runs on the two new postings, not on the whole
page. Cost then scales with hiring activity rather than with polling
frequency, which is the difference between a few dollars a year and a few
hundred a month.

The whole-page hash is kept as `content_hash` for the case where no link
pattern matches at all, but it is the fallback, not the plan.
"""
from __future__ import annotations

import hashlib
import re

# Things that differ between two fetches of an UNCHANGED page. Stripped before
# the fallback hash. This list is why the fallback is a fallback: it can only
# ever be a list of the volatility we happen to know about, whereas the link
# set is immune by construction.
VOLATILE = [
    # CSRF / session / nonce / build tokens in attributes
    re.compile(r'(?i)\b(csrf|nonce|session|token|build|requestid|traceid|'
               r'correlationid)[-_a-z0-9]*\s*[=:]\s*["\'][^"\']*["\']'),
    # The same tokens in <meta name="csrf-token" content="..."> form, where
    # the value lives in a DIFFERENT attribute than the name that identifies
    # it. The pattern above cannot see these, and this is the single most
    # common way a page carries a per-request token.
    re.compile(r'(?i)<meta[^>]*\b(?:csrf|nonce|session|token|build)[^>]*>'),
    # cache-busting query strings on assets
    re.compile(r'(?i)[?&](v|ts|t|cb|_|rev|hash)=[a-z0-9._-]+'),
    # bare epoch timestamps (10 or 13 digits)
    re.compile(r'\b1[0-9]{9}(?:[0-9]{3})?\b'),
    # ISO timestamps
    re.compile(r'\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b'),
    # HTML comments — render timings and CDN pop names live here
    re.compile(r'<!--.*?-->', re.S),
    # A/B and experiment buckets
    re.compile(r'(?i)\bdata-(ab|experiment|variant|bucket)[-_a-z0-9]*='
               r'["\'][^"\']*["\']'),
]

WHITESPACE = re.compile(r"\s+")


def strip_volatile(html: str) -> str:
    out = html or ""
    for pat in VOLATILE:
        out = pat.sub(" ", out)
    return WHITESPACE.sub(" ", out).strip()


def content_hash(html: str) -> str:
    """Fallback only: a hash of the page with known volatility removed.

    Use when a source has no recognisable job-link pattern. Treat a change
    here as "worth a look", never as "these jobs are new" — it cannot tell
    you which, and it will still occasionally fire on volatility this module
    hasn't learned about yet.
    """
    return hashlib.sha1(strip_volatile(html).encode()).hexdigest()[:16]


def extract_ids(html: str, pattern: str | re.Pattern) -> list[str]:
    """Every job identity on the page, deduped and sorted.

    `pattern` must capture exactly one group: the stable part of the job URL.
    Sorted so that a page which merely reorders its listings — which they do
    constantly, "most relevant" being a moving target — is not mistaken for a
    page with new jobs.
    """
    rx = re.compile(pattern) if isinstance(pattern, str) else pattern
    found = set()
    for m in rx.finditer(html or ""):
        got = m.group(1) if rx.groups else m.group(0)
        if got:
            found.add(got.strip())
    return sorted(found)


def identity_hash(ids: list[str]) -> str:
    return hashlib.sha1("|".join(ids).encode()).hexdigest()[:16]


def diff(previous: list[str] | None, current: list[str]) -> dict:
    """What changed between two polls.

    `added` is the only thing that costs money downstream — it is the list the
    extraction step runs on. `removed` is roles that closed, which is free
    information we get for nothing.

    previous=None means "first time we've seen this source". Everything is
    technically new, so `first_run` says so and the caller should seed rather
    than alert — the same rule the ATS collector already follows, for the same
    reason: nobody wants four hundred alerts on day one.
    """
    if previous is None:
        return {"first_run": True, "added": list(current), "removed": [],
                "changed": bool(current)}
    prev, cur = set(previous), set(current)
    return {"first_run": False,
            "added": sorted(cur - prev),
            "removed": sorted(prev - cur),
            "changed": prev != cur}


def estimate_cost(added: int, tokens_in=2000, tokens_out=300,
                  price_in=1.0, price_out=5.0) -> float:
    """Dollars to extract `added` job pages with a Haiku-class model.

    Defaults are one JOB DETAIL page (~2k tokens), not a whole listing page —
    that is the point of the identity set. Prices are $ per million tokens and
    are arguments, not constants, because they change.
    """
    return added * (tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out)
