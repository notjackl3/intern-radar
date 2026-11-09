"""The optional OpenAI layer: query parsing and embedding-based ranking.

THE CONTRACT THIS MODULE KEEPS
------------------------------
Everything here is optional. With no `OPENAI_API_KEY` set, every function in
this file returns a value that makes the caller fall back to `matching.py`'s
deterministic path — no exceptions raised, no degraded-mode flags to thread
through, nothing logged every five minutes. That is deliberate: the bot is
deployed to Railway on every push, and a feature that half-exists must not be
able to take the live bot down or spam the channel while the key is pending.

So the honest capability ladder is:

    no key   -> deterministic parse + keyword/family scoring   (good)
    key      -> + LLM query parse, + embedding re-rank          (better)

WHY AN LLM AT ALL, GIVEN THE PARSER WORKS
-----------------------------------------
The deterministic parser in `matching.py` handles the phrasings people
actually use, because the vocabulary is closed (9 families, ~18 cities, 4
seasons, 2 levels). The model earns its place on the tail the closed list
can't express — "something like Palantir but in Canada", "low-level systems
work, not web" — and on turning one sentence into `keywords_none`, which the
parser never infers.

WHY THE PARSE IS CACHED, NOT CALLED PER DIGEST
----------------------------------------------
A member sets a query once and receives a digest every day. Re-parsing the
same sentence every morning for every member would be a daily API bill for a
deterministic answer. So the parse result is stored on the member's prefs and
reused until they CHANGE the query; `/search` parses per call because each
ad-hoc query is genuinely new. That is the auto-mode / menu-mode split.

WHY EMBEDDINGS RANK BUT NEVER DECIDE
------------------------------------
Titles average 6.3 words. On strings that short, cosine similarity cheerfully
rates "Data Analyst Intern" and "Data Engineer Intern" as near-identical, so
using it as a gate would quietly admit the wrong discipline. It is therefore a
tie-breaker layered on top of the keyword score, never a replacement for it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os

import matching as M

log = logging.getLogger("semantic")

API_KEY = os.environ.get("OPENAI_API_KEY") or ""
BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
CHAT_MODEL = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")
EMBED_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")

# One parse is ~90 tokens and one title ~8, so the per-call budget here is set
# to catch a runaway prompt, not to economise.
MAX_PARSE_TOKENS = 300
EMBED_BATCH = 256
TIMEOUT = 25

_warned = False


def enabled() -> bool:
    """True when an OpenAI key is configured. Checked at call time, not import
    time, so adding the Railway variable and restarting is all it takes."""
    return bool(os.environ.get("OPENAI_API_KEY") or API_KEY)


def _key() -> str:
    return os.environ.get("OPENAI_API_KEY") or API_KEY


def status() -> str:
    """One line for //prefs and the startup log."""
    if not enabled():
        return ("keyword matching (no OPENAI_API_KEY set — natural-language "
                "parsing and semantic ranking are off)")
    return f"semantic matching on ({CHAT_MODEL} + {EMBED_MODEL})"


def _warn_once() -> None:
    global _warned
    if not _warned:
        log.info("OPENAI_API_KEY not set — using the deterministic parser. "
                 "This is a supported mode, not an error.")
        _warned = True


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
async def _post(session, path: str, payload: dict) -> dict | None:
    """POST to OpenAI. Returns None on ANY failure — see the module docstring:
    a semantic-layer outage must degrade to keyword matching, never surface as
    a broken command."""
    import aiohttp
    try:
        async with session.post(
            f"{BASE}{path}",
            headers={"Authorization": f"Bearer {_key()}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        ) as r:
            body = await r.text()
            if r.status != 200:
                # 429 here is usually "no credits remaining", which is worth
                # saying plainly once rather than burying as a generic failure.
                log.warning("openai %s -> %s: %s", path, r.status, body[:200])
                return None
            return json.loads(body)
    except asyncio.TimeoutError:
        log.warning("openai %s timed out after %ss", path, TIMEOUT)
        return None
    except Exception as e:
        log.warning("openai %s failed: %s", path, e)
        return None


# ---------------------------------------------------------------------------
# query parsing
# ---------------------------------------------------------------------------
_PARSE_SYSTEM = """You convert a job-seeker's sentence into a search filter.

Reply with ONLY a JSON object, no prose, with exactly these keys:
  families      array, any of: {families}
  keywords_any  array of lowercase single words or short phrases from the
                request that should appear in a job TITLE
  keywords_none array of lowercase words that should DISQUALIFY a job
  cities        array, any of: {cities}
  terms         array of strings like "Summer 2027" or "Winter 2027"
  level         one of "intern", "newgrad", or null

Rules:
- Only use values from the allowed lists for `families` and `cities`.
- Job descriptions are NOT available, only titles. Put only words that
  plausibly appear in a title into keywords_any.
- Omit a key's values (use an empty array) rather than guessing.
- "next summer" with no year means Summer {year}."""


async def parse_query(session, text: str, *, year_hint: int) -> dict:
    """Structured query from free text.

    Always returns a usable query: the deterministic parse is computed first
    and the model's output is layered on top, so a refusal, a timeout or a
    malformed reply costs nothing. The model can only ADD signal here, which
    is also why its families/cities are intersected with the allowed lists
    rather than trusted.
    """
    base = M.parse_query(text, year_hint=year_hint)
    if not enabled():
        _warn_once()
        return base

    system = _PARSE_SYSTEM.format(
        families=", ".join(M.ROLE_FAMILIES), cities=", ".join(M.CITY_KEYS),
        year=year_hint)
    data = await _post(session, "/chat/completions", {
        "model": CHAT_MODEL,
        "max_tokens": MAX_PARSE_TOKENS,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": text[:500]}],
    })
    if not data:
        return base
    try:
        raw = data["choices"][0]["message"]["content"]
        got = json.loads(raw)
    except Exception as e:
        log.warning("couldn't read the parse reply: %s", e)
        return base

    # Trust nothing: clamp to the closed vocabularies before merging. A model
    # inventing a family key would otherwise raise a KeyError deep inside
    # describe_query, in the digest loop, at 13:00 UTC.
    clean = {
        "raw": text,
        "families": [f for f in _strlist(got.get("families"))
                     if f in M.ROLE_FAMILIES],
        "cities": [c for c in _strlist(got.get("cities")) if c in M.CITY_ALIASES],
        "keywords_any": _strlist(got.get("keywords_any"))[:8],
        "keywords_none": _strlist(got.get("keywords_none"))[:8],
        "terms": _strlist(got.get("terms"))[:4],
        "level": got.get("level") if got.get("level") in ("intern", "newgrad") else None,
    }
    merged = M.merge_query(base, clean)
    # keywords_none is the one field the deterministic parser never produces,
    # so take the model's as-is rather than letting merge_query's "non-empty
    # wins" rule hide it behind the base's empty list.
    merged["keywords_none"] = clean["keywords_none"]
    return merged


def _strlist(v) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip().lower() for x in v if str(x).strip()]


# ---------------------------------------------------------------------------
# embeddings
# ---------------------------------------------------------------------------
async def embed(session, texts: list[str]) -> list[list[float]] | None:
    """Embed a list of strings, or None if the semantic layer is unavailable."""
    if not enabled() or not texts:
        return None
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        chunk = texts[i:i + EMBED_BATCH]
        data = await _post(session, "/embeddings",
                           {"model": EMBED_MODEL, "input": chunk})
        if not data:
            return None
        try:
            out.extend(d["embedding"] for d in data["data"])
        except Exception as e:
            log.warning("malformed embedding reply: %s", e)
            return None
    return out


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return num / (na * nb) if na and nb else 0.0


# How much the embedding can move a result. Kept below matching.W_FAMILY (4.0)
# on purpose: a semantic near-miss must never outrank an explicit family hit,
# because on 6-word titles the embedding is the less trustworthy signal.
W_SEMANTIC = 2.5
# Below this, similarity is noise on short strings and adds nothing but churn.
SIM_FLOOR = 0.3


async def rerank(session, scored, query_text: str):
    """Add a semantic component to an already-keyword-scored list.

    Takes and returns `matching.rank()`'s shape, so the caller can use it or
    skip it interchangeably. Returns the input untouched when the semantic
    layer is off or the API call fails — the common path on a fresh deploy.

    Only the top slice is embedded: re-ranking position 400 changes nothing a
    member will ever see, and embedding the whole feed per search would be the
    one genuinely wasteful thing in this design.
    """
    if not enabled() or not scored or not (query_text or "").strip():
        return scored
    head, tail = scored[:60], scored[60:]
    vecs = await embed(session, [query_text] + [j.get("title") or "" for j, _, _ in head])
    if not vecs or len(vecs) != len(head) + 1:
        return scored
    qv, title_vs = vecs[0], vecs[1:]
    out = []
    for (j, s, why), tv in zip(head, title_vs):
        sim = cosine(qv, tv)
        if sim >= SIM_FLOOR:
            s = s + W_SEMANTIC * sim
            if not why:
                # A job surfaced ONLY by the embedding needs to say so, or it
                # looks like an unexplained result in the digest.
                why = [f"similar ({sim:.2f})"]
        out.append((j, s, why))
    out.sort(key=lambda t: (-t[1], -(t[0].get("posted_at") is not None),
                            t[0].get("company") or ""))
    return out + tail
