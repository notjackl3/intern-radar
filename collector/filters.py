"""Turn the firehose into the handful of roles actually worth your evening.

The hard part here is deciding what counts as Canadian. Naive city-name
matching does not work: Burlington, London, Hamilton, Windsor, Victoria,
Waterloo, Kingston and Richmond all exist on both sides of the border, and a
US listing like "Palo Alto, CA; Burlington, MA" will happily match a keyword
list. So the rule is: a location is Canadian only if it carries a Canadian
QUALIFIER — the word Canada, a province name, or a province code in the
`City, XX` position — or names a city that exists nowhere else.
"""
from __future__ import annotations

import re

# Province codes as they appear in a listing: after a comma, or at the end.
# Matched case-sensitively on the uppercase form to avoid firing on prose
# ("on site", "ab initio", "PE ratio").
PROV_CODES = r"(?:ON|BC|AB|QC|MB|SK|NS|NB|NL|PE|PEI|YT|NT|NU)"
PROV_PATTERN = re.compile(rf",\s*{PROV_CODES}\b")

PROVINCES = [
    "ontario", "british columbia", "alberta", "quebec", "québec", "manitoba",
    "saskatchewan", "nova scotia", "new brunswick", "newfoundland",
    "prince edward island", "yukon", "nunavut", "northwest territories",
]

# Cities that are unambiguously Canadian — safe to match on their own, for
# sources that write a bare city with no province.
UNAMBIGUOUS = [
    "toronto", "montreal", "montréal", "vancouver", "ottawa", "calgary",
    "edmonton", "mississauga", "markham", "brampton", "vaughan", "oakville",
    "burnaby", "kitchener", "saskatoon", "winnipeg", "halifax", "moncton",
    "fredericton", "gatineau", "sherbrooke", "laval", "regina", "scarborough",
    "etobicoke", "north york", "kanata", "oshawa", "barrie", "guelph",
    "st. john's", "trois-rivières", "chicoutimi", "lethbridge", "nanaimo",
    "coquitlam", "surrey, bc", "richmond hill", "newmarket, on", "milton, on",
]

# Cities that exist in both countries. These are deliberately NOT in the list
# above; they only count when a province qualifier is also present, which the
# PROV_PATTERN / "canada" checks already handle:
#   London, Hamilton, Windsor, Victoria, Waterloo, Kingston, Cambridge,
#   Richmond, Burlington, Sarnia, Chatham, Stratford, Peterborough, Woodstock.

# --------------------------------------------------------------------------
# United States
# --------------------------------------------------------------------------
# Same discipline as the Canadian rule above: a bare city name proves nothing.
# The state codes are the reliable signal, and conveniently no US state code
# collides with a Canadian province code, so the two sets can't cross-match.
#
# Canada is ALWAYS tested first. "Toronto, ON, Canada" must never come back as
# American, and a listing like "New York, NY; Toronto, ON" is both — which is
# correct, and why country_of returns the set it does rather than one label.
STATE_CODES = (
    r"(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|"
    r"MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|"
    r"VA|WA|WV|WI|WY|DC)")
STATE_PATTERN = re.compile(rf",\s*{STATE_CODES}\b")

STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
]

# "america" is deliberately absent — it would fire on "Latin America" and on
# employer names that leak into location strings.
US_NAMES = ["united states", "u.s.a", "u.s.", " usa", "(usa)", "usa)"]

# "Remote - US" is extremely common and matches none of the above, because a
# bare "us" is far too dangerous to match on its own (it appears inside
# "Houston", "campus", "Columbus"). So the remote forms are spelled out.
US_REMOTE = re.compile(
    r"\bremote\s*[-–,(]?\s*(us|usa|u\.s\.?)\b|\b(us|usa)\s*[-–,]?\s*remote\b", re.I)

# Cities safe to match bare. "Washington" is NOT here — it is also a state, and
# "Washington, ON" exists. Nor is "Portland" (Maine and Oregon are both US, so
# it is actually safe, but the pattern is to require a qualifier by default).
US_UNAMBIGUOUS = [
    "san francisco", "new york city", "nyc", "seattle", "silicon valley",
    "palo alto", "mountain view", "sunnyvale", "cupertino", "menlo park",
    "redmond", "bellevue, wa", "austin, tx", "boston, ma", "chicago, il",
    "los angeles", "san jose", "san diego", "atlanta, ga", "denver, co",
    "brooklyn", "manhattan", "bay area",
]


def _is_american_part(part: str) -> bool:
    # One location is in one country. Canada wins outright, which is what
    # stops "Washington, ON" and "London, ON" being read as American by the
    # bare state-name check below. A listing spanning both countries is still
    # both — that is handled per-part, one level up.
    if _is_canadian_part(part):
        return False
    low = part.lower()
    if any(n in low for n in US_NAMES):
        return True
    if US_REMOTE.search(part):
        return True
    if STATE_PATTERN.search(part):
        return True
    if any(f", {s}" in low or low.startswith(s) for s in STATES):
        return True
    return any(c in low for c in US_UNAMBIGUOUS)


def is_american(loc: str) -> bool:
    """True if ANY location in the listing is in the United States."""
    if not loc:
        return False
    parts = [p.strip() for p in re.split(r"[;|]", loc) if p.strip()]
    return any(_is_american_part(p) for p in (parts or [loc]))


def country_of(loc: str) -> set[str]:
    """Which countries a listing covers: a SUBSET of {"ca", "us"}, possibly
    empty.

    A set, not a label, because multi-site reqs are common and genuinely
    belong to both — "Toronto, ON; New York, NY" should reach someone watching
    either country. An empty set means neither matched: a UK/India/remote-
    unspecified role, or a location string we can't read. Those are kept in the
    feed and tagged "other" rather than silently dropped, so a bad location
    string costs you visibility, not the job.
    """
    out = set()
    if is_canadian(loc):
        out.add("ca")
    if is_american(loc):
        out.add("us")
    return out


# Excluded first: a "Senior Manager, Intern Programs" is not an internship.
EXCLUDE_TITLE = [
    "senior", "sr.", "staff", "principal", "lead ", "manager", "director",
    "head of", "vp ", "vice president", "architect", "phd required",
    "postdoc", "faculty", "professor", "recruiter",
]


def _is_canadian_part(part: str) -> bool:
    low = part.lower()
    if "canada" in low:
        return True
    if any(p in low for p in PROVINCES):
        return True
    if PROV_PATTERN.search(part):
        return True
    return any(c in low for c in UNAMBIGUOUS)


def is_canadian(loc: str) -> bool:
    """True if ANY location in a multi-location listing is Canadian.

    Locations arrive as "Toronto, ON, Canada" or, for multi-site reqs, as a
    ';'-joined list. Each part is judged on its own so that one Canadian site
    in a list of eight US ones still counts — but a US-only list never does.
    """
    if not loc:
        return False
    parts = [p.strip() for p in re.split(r"[;|]", loc) if p.strip()]
    return any(_is_canadian_part(p) for p in (parts or [loc]))


def title_matches(title: str, include: list[str]) -> bool:
    low = (title or "").lower()
    if any(x in low for x in EXCLUDE_TITLE):
        return False
    return any(x in low for x in include)


def term_matches(job: dict, wanted: list[str]) -> bool:
    """Sources that carry a term (Simplify) are filtered on it. Sources that
    don't (every raw ATS endpoint) fall back to the title, then pass through —
    an unlabelled intern req in Canada is worth seeing either way."""
    if not wanted:
        return True
    hay = f"{job.get('term') or ''} {job.get('title') or ''}".lower()
    if job.get("term"):
        return any(w.lower() in hay for w in wanted)
    # No term metadata: only reject if the title explicitly names a term we
    # did not ask for.
    if re.search(r"(summer|winter|fall|spring)\s*20\d\d", hay):
        return any(w.lower() in hay for w in wanted)
    return True


# An internship keyword WINS over a new-grad one: "New Grad Intern" and
# "University Graduate Co-op" are internships. Only when no internship word is
# present does a new-grad marker decide it.
INTERN_RE = re.compile(
    r"intern|co-?\s?op|stagiaire|\bstage\b|student|étudiant|summer analyst|practicum",
    re.I)
NEWGRAD_RE = re.compile(
    r"new[\s-]?grad|new graduate|university graduate|graduate program|"
    r"early career|early talent|campus hire|entry[\s-]?level|"
    r"associate (software|engineer|developer|data)|junior |jr\.? |"
    r"(software |data )?engineer i\b|swe i\b|developer i\b",
    re.I)


def classify_level(job: dict) -> str:
    """'intern' or 'newgrad'.

    Derived from the title, so it works across every source — unlike Simplify's
    `degrees` field, which two thirds of the feed (all Workday, most
    Greenhouse/Lever/Ashby) simply doesn't carry.
    """
    title = job.get("title") or ""
    if INTERN_RE.search(title):
        return "intern"
    if NEWGRAD_RE.search(title):
        return "newgrad"
    # No marker in the title: trust which repo it came from, else assume intern
    # (the level filter already required an intern-ish keyword to get here).
    return "newgrad" if job.get("source") == "simplify-newgrad" else "intern"


def domain_matches(job: dict, include: list[str], exclude: list[str]) -> bool:
    """Is this a SOFTWARE-ish role? ANDed with the intern-level filter.

    Without this, "intern" alone lets through capital markets, internal audit,
    HR, marketing and mechanical engineering — on the first real run that was
    172 of 271 roles.

    The title decides a rejection (a "Sales Systems & Data Governance Intern"
    is a sales job however much it says "data"), but a match may come from the
    description too, since Workday and the Simplify sweep publish no
    description and their titles are often vague.
    """
    if not include:
        return True
    title = (job.get("title") or "").lower()
    if any(x in title for x in exclude):
        return False
    hay = f"{title} {(job.get('description') or '')[:400].lower()}"
    return any(x in hay for x in include)


def wanted_countries(cfg) -> list[str]:
    """Which countries to COLLECT. `canada_only: true` is still honoured so an
    old config keeps behaving exactly as it did."""
    if cfg.get("countries"):
        return [c.lower() for c in cfg["countries"]]
    return ["ca"] if cfg.get("canada_only", True) else ["ca", "us", "other"]


def apply_filters(jobs, cfg) -> list[dict]:
    include = [x.lower() for x in cfg.get("title_include_any", [])]
    dom_in = [x.lower() for x in cfg.get("domain_include_any", [])]
    dom_out = [x.lower() for x in cfg.get("domain_exclude_any", [])]
    terms = cfg.get("terms", [])
    countries = wanted_countries(cfg)

    out, seen_urls = [], set()
    for j in jobs:
        # Tag first, filter second. Every surviving job carries `countries`, so
        # the bot can filter per-person at query time without re-deriving it
        # from the location string — and a role in both countries reaches
        # someone watching either.
        found = country_of(j.get("location", "")) or {"other"}
        if not (found & set(countries)):
            continue
        if not title_matches(j.get("title", ""), include):
            continue
        if not domain_matches(j, dom_in, dom_out):
            continue
        if not term_matches(j, terms):
            continue
        url = (j.get("url") or "").split("?")[0]
        if url in seen_urls:  # same req surfaced by two sources
            continue
        seen_urls.add(url)
        j["level"] = classify_level(j)
        j["countries"] = sorted(found)
        out.append(j)
    return out
