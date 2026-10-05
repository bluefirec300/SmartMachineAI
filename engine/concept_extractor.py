from __future__ import annotations
import re
from datetime import date
from difflib import SequenceMatcher
from .models import Concepts

# Matches an explicit plant qualifier ("P01", "p2", "plant 2",
# "plant02") as its own bounded token/phrase - not e.g. "pressure"
# (starts with "p" but not followed by a digit) or an equipment
# designator like "ac01" (doesn't start with "p"). Deliberately not
# hardcoded to a specific count of plants ("P01"/"P02" only) - matches
# any "p" + 1-2 digits or "plant" + number, so a future P03/P04/etc.
# needs no change here. Canonicalized to zero-padded "p01"/"p02"/...
# form so it can be compared directly against a tag_name's plant
# prefix (see _plant_of() in industrial_query_engine.py).
PLANT_PATTERN = re.compile(r"\bplant\s*0*(\d{1,2})\b|\bp0*(\d{1,2})\b")


def extract_plant(text: str) -> tuple[str, str]:
    match = PLANT_PATTERN.search(text)

    if not match:
        return "", ""

    number = match.group(1) or match.group(2)
    return f"p{int(number):02d}", match.group(0)


# "N days ago"/"N days before yesterday" - a genuine gap TIMES (a
# fixed set of named buckets: yesterday/today/last_7_days/...) has no
# way to express: "yesterday" only means exactly 1 day back, so a
# follow-up like "how about one day before [yesterday]?" - which the
# LLM correction fallback (_try_correct_question() in app/ask.py)
# reliably expands to "...one day before yesterday" - would otherwise
# just re-match the plain "yesterday" bucket via substring containment
# and silently ignore the "one day before" part entirely. Produces a
# dynamic "days_ago:N" time_expression instead of a fixed bucket name
# (handled in app/ask.py's _factory_timeline_range()), so this scales
# to any N without a fixed enum of buckets to keep extending.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_NUMBER_GROUP = r"(\d+|" + "|".join(_NUMBER_WORDS) + r")"

DAYS_BEFORE_YESTERDAY_PATTERN = re.compile(
    rf"\b(?:{_NUMBER_GROUP}\s+)?days?\s+before\s+yesterday\b"
)
DAYS_AGO_PATTERN = re.compile(
    rf"\b{_NUMBER_GROUP}\s+days?\s+(?:ago|before\s+today)\b"
)


def _parse_day_count(token: str | None) -> int:
    if token is None:
        return 1

    return int(token) if token.isdigit() else _NUMBER_WORDS.get(token, 1)


def extract_days_ago(text: str) -> tuple[str, str]:
    # "yesterday" is already 1 day back, so "N days before yesterday"
    # (or the bare idiom "day before yesterday", no number = 1) lands
    # N+1 days back.
    match = DAYS_BEFORE_YESTERDAY_PATTERN.search(text)

    if match:
        return f"days_ago:{_parse_day_count(match.group(1)) + 1}", match.group(0)

    match = DAYS_AGO_PATTERN.search(text)

    if match:
        return f"days_ago:{_parse_day_count(match.group(1))}", match.group(0)

    return "", ""


# Two-period comparison questions ("compare power consumption for
# today and yesterday", "AC01 pressure on Aug 10 vs Aug 12") - a
# genuinely different shape from every other question type, since it
# names TWO points in time rather than one. Deliberately scanned
# against the RAW question text (not the normalized/spell-corrected
# `text` used everywhere else in this file), since date parsing needs
# real punctuation/casing/digits that normalize() strips (hyphens in
# "2026-08-10" become spaces, etc.) - correct_spelling() is also
# skipped, since digit-bearing tokens are already exempt from it and
# month-name words risk an unwanted fuzzy "correction" otherwise.
COMPARE_TRIGGER_PHRASES = (
    "compare", "comparison", "compared to", "versus", "vs", "against",
)

MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_GROUP = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))

ISO_DATE_PATTERN = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
MONTH_DAY_PATTERN = re.compile(
    rf"\b({_MONTH_GROUP})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b"
)
DAY_MONTH_PATTERN = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_GROUP})\.?(?:,?\s+(\d{{4}}))?\b"
)


def _comparison_trigger_present(raw_lower: str) -> bool:
    padded = f" {raw_lower} "
    return any(f" {phrase} " in padded for phrase in COMPARE_TRIGGER_PHRASES)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _explicit_dates(raw_lower: str, today: date) -> list[tuple[int, str, date]]:
    """
    Find explicit calendar dates in raw (lowercased) text - ISO
    (2026-08-10), "Aug 10"/"August 10th"/"Aug 10, 2026", or "10 Aug"/
    "10th of August". Year defaults to the current year when omitted.
    Returns (start_pos, matched_text, date) triples, longest/most-
    specific match wins on overlap (ISO and month-name forms can't
    overlap in practice, but two month-name patterns could both try to
    claim the same span).
    """
    spans: list[tuple[int, int]] = []
    found: list[tuple[int, str, date]] = []

    def _claim(match: re.Match) -> bool:
        start, end = match.span()
        if any(not (end <= s or start >= e) for s, e in spans):
            return False
        spans.append((start, end))
        return True

    for match in ISO_DATE_PATTERN.finditer(raw_lower):
        if not _claim(match):
            continue
        parsed = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if parsed:
            found.append((match.start(), match.group(0), parsed))

    for match in MONTH_DAY_PATTERN.finditer(raw_lower):
        if not _claim(match):
            continue
        month = MONTH_NAMES[match.group(1)]
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year
        parsed = _safe_date(year, month, day)
        if parsed:
            found.append((match.start(), match.group(0), parsed))

    for match in DAY_MONTH_PATTERN.finditer(raw_lower):
        if not _claim(match):
            continue
        day = int(match.group(1))
        month = MONTH_NAMES[match.group(2)]
        year = int(match.group(3)) if match.group(3) else today.year
        parsed = _safe_date(year, month, day)
        if parsed:
            found.append((match.start(), match.group(0), parsed))

    found.sort(key=lambda item: item[0])
    return found


def extract_comparison_periods(
    raw_question: str, today: date | None = None
) -> tuple[str, str, str, str]:
    """
    Detect a two-period comparison question. Returns (period_a,
    period_a_text, period_b, period_b_text) - both period values empty
    unless a comparison trigger word (compare/vs/versus/...) AND at
    least two distinct time periods are both present. Each period
    value reuses the "today"/"yesterday"/"days_ago:N" vocabulary
    app/ask.py's _factory_timeline_range() already resolves, plus a
    new "date:YYYY-MM-DD" form for an explicit calendar date.
    """
    today = today or date.today()
    raw_lower = raw_question.lower()

    if not _comparison_trigger_present(raw_lower):
        return "", "", "", ""

    candidates: list[tuple[int, str, str]] = []

    for match in re.finditer(r"\btoday\b", raw_lower):
        candidates.append((match.start(), "today", match.group(0)))

    for match in re.finditer(r"\byesterday\b", raw_lower):
        candidates.append((match.start(), "yesterday", match.group(0)))

    before_yesterday = DAYS_BEFORE_YESTERDAY_PATTERN.search(raw_lower)
    if before_yesterday:
        n = _parse_day_count(before_yesterday.group(1)) + 1
        candidates.append((before_yesterday.start(), f"days_ago:{n}", before_yesterday.group(0)))
    else:
        days_ago = DAYS_AGO_PATTERN.search(raw_lower)
        if days_ago:
            n = _parse_day_count(days_ago.group(1))
            candidates.append((days_ago.start(), f"days_ago:{n}", days_ago.group(0)))

    for start, matched_text, parsed_date in _explicit_dates(raw_lower, today):
        candidates.append((start, f"date:{parsed_date.isoformat()}", matched_text))

    candidates.sort(key=lambda item: item[0])

    chosen: list[tuple[str, str]] = []
    for _, expr, matched_text in candidates:
        if expr in (period for period, _ in chosen):
            continue
        chosen.append((expr, matched_text))
        if len(chosen) == 2:
            break

    if len(chosen) < 2:
        return "", "", "", ""

    (period_a, text_a), (period_b, text_b) = chosen
    return period_a, text_a, period_b, text_b


MEASUREMENTS = {
    "pressure": ("pressure", "bar", "psi"),
    "temperature": ("temperature", "temp", "celsius", "how hot", "how cold"),
    "humidity": ("humidity", "relative humidity", "moisture"),
    "current": ("motor current", "electrical current", "amps", "ampere", "amp"),
    "voltage": ("voltage", "volts"),
    "power": ("power", "kw", "kilowatt", "power consumption", "power usage", "how much power"),
    "energy": ("energy", "kwh", "energy consumption", "energy usage"),
    # Deliberately multi-word phrases only, not bare "water" - "water"
    # alone must stay in equipment_terms so it can still help identify
    # actual water-related equipment (Water Supply Pump, Water
    # Treatment System, ...); it's only stripped out (and this
    # measurement recognized) when paired with a consumption-style word,
    # matching the same pattern "power"/"energy" already use above.
    "waterconsumption": ("water consumption", "water usage", "how much water"),
    "flow": ("flow rate", "flowrate", "flow"),
    "level": ("tank level", "level", "how full", "how empty", "fill level"),
    "speed": ("speed", "rpm", "how fast"),
    "frequency": ("frequency", "hertz", "hz"),
    "weight": ("weight", "kilogram", "kg"),
    "running": ("run status", "running", "operating"),
    "status": ("status", "state", "condition"),
    # Added for the Phase 2/3 water/wastewater treatment dataset -
    # these measurement types didn't exist when this vocabulary was
    # first written (only pressure/temp/current-style signals existed
    # then), so "what is the water pH" etc. never matched anything.
    # "p h" (not a typo) - normalize()'s camelCase-splitter inserts a
    # space between "p" and "H" in "pH" before this ever gets matched,
    # the same rule that correctly turns tag names like "OutletTemp"
    # into "Outlet Temp" - so the alias has to match what the text
    # actually becomes, not the literal input spelling.
    "ph": ("ph", "p h", "acidity", "alkalinity"),
    "turbidity": ("turbidity", "cloudiness", "clarity"),
    "conductivity": ("conductivity", "salinity"),
    # No underscore in the canonical key, unlike its alias phrase below
    # - normalize() (used when comparing against a tag's own stored
    # measurement column) strips underscores to spaces, so
    # "dissolved_oxygen" would never equal "dissolved oxygen" and this
    # would silently never score an exact measurement match. Every
    # other canonical key in this file is already a single bare word
    # for the same reason.
    "dissolvedoxygen": ("dissolved oxygen", "do level", "oxygen level"),
    "orp": ("orp", "oxidation reduction potential", "redox"),
    "dewpoint": ("dew point", "dewpoint"),
    "vibration": ("vibration", "shaking", "shakiness"),
    "battery": ("battery", "battery charge", "battery level", "state of charge"),
    "runtime": ("runtime", "backup time", "estimated runtime"),
}
LOCATIONS = {
    "discharge": ("discharge", "output side"),
    "suction": ("suction", "intake"),
    "inlet": ("inlet", "incoming"),
    "outlet": ("outlet", "outgoing"),
    "supply": ("supply",),
    "return": ("return",),
    "motor": ("motor",),
    "room": ("room",),
    "tank": ("tank",),
    "bearing": ("bearing",),
    "header": ("header",),
    "incomer": ("incomer",),
    "breaker": ("breaker",),
    # Phase 2/3 additions - RO/water-treatment stage names and a few
    # other equipment-specific locations that show up in real
    # questions about that equipment ("feed pressure", "hopper level").
    "feed": ("feed",),
    "permeate": ("permeate", "product water"),
    "reject": ("reject", "concentrate"),
    "raw": ("raw water", "untreated"),
    "treated": ("treated water", "treated"),
    "hopper": ("hopper",),
    "filter": ("filter",),
}
CONDITIONS = {
    "high": ("too high", "high"),
    "low": ("too low", "low"),
    "running": ("running", "operating"),
    "stopped": ("not running", "stopped"),
    "open": ("opened", "open"),
    "closed": ("closed", "shut"),
    "increasing": ("increasing", "rising", "going up"),
    "decreasing": ("decreasing", "falling", "dropping"),
    "abnormal": ("abnormal", "unusual", "weird", "off", "strange"),
    # Common-sense everyday phrasing for the same underlying concepts -
    # an operator says "is the tank full" or "is it too hot" far more
    # naturally than "is the level high" or "is the temperature high".
    "full": ("full", "overflowing"),
    "empty": ("empty", "running dry", "running low"),
    "hot": ("hot", "too hot", "overheating"),
    "cold": ("cold", "too cold", "freezing"),
    "leaking": ("leaking", "leak", "leaky"),
    "noisy": ("noisy", "loud"),
    "ok": ("ok", "okay", "fine", "good", "healthy", "normal"),
}
EVENTS = {
    "alarm": ("alarms", "alarm"),
    "warning": ("warnings", "warning"),
    "fault": ("tripped", "trip", "fault", "error", "issue", "problem"),
}
TIMES = {
    "last_30_minutes": ("last 30 minutes", "past 30 minutes"),
    "last_hour": ("last hour", "past hour"),
    "last_7_days": (
        "last 7 days", "last week",
        "past few days", "few days", "last few days",
        "couple of days", "past couple of days", "couple days",
    ),
    "yesterday": ("yesterday",),
    "today": ("today", "this morning", "this afternoon", "this evening"),
    "recent": ("recently", "recent", "just now", "a moment ago", "a few minutes ago"),
    "now": ("right now", "now", "at the moment"),
    "latest": ("currently", "current", "latest"),
}

# The subset of TIMES buckets that represent a genuine PAST range,
# i.e. "look at history" rather than "give me the current/latest
# value" - "now"/"latest" are deliberately excluded, so "what is the
# current alarm status" (a live-state question) doesn't get pulled
# into the timeline intent alongside genuine history questions like
# "any alarm or warning yesterday". Used by the timeline branch above.
HISTORICAL_TIME_EXPRESSIONS = {
    "last_30_minutes", "last_hour", "last_7_days", "yesterday",
    "today", "recent",
}
STOPWORDS = {
    "what","is","the","a","an","of","for","to","from","in","on","at",
    "show","me","please","tell","give","value","reading","currently",
    "current","latest","now","status","are","was","did","has","have",
    "why","when","where","how","does","do","will","would","can","could",
    "it","its","this","that","be","been","being",
    "trend","limit","trip",
    "there","anything","happen","happened","wrong","any",
    "with","doing","ok","okay","fine","healthy","good","well","everything",
    # Common short connector/function words - added after finding
    # several of these get mis-"corrected" into unrelated vocabulary
    # by _correct_word()'s fuzzy matching otherwise: short words get a
    # low similarity bar (see its threshold comment), so e.g. "or"
    # (only in the vocabulary via the "orp" measurement alias) scored
    # a 0.8 ratio against "orp" - well above the 0.72 bar for a
    # 2-letter word - and got silently rewritten to "orp" mid-question
    # ("...has alarm or warning now" -> "...has alarm orp warning
    # now"), which then falsely set measurement="orp" and blocked the
    # equipment_status match below. A broader scan turned up "and"->
    # "an", "no"->"now"/"not", "then"->"the", "than"->"thank",
    # "over"->"very", "here"->"where", and more - not an exhaustive
    # fix, but stopwords are never typo-corrected (see the
    # `word in VOCABULARY_WORDS` early-return in _correct_word()) and
    # never carry domain meaning, so this class of short, common
    # connector word belongs here defensively rather than only adding
    # back whichever one the next real question happens to break.
    "and","or","nor","no","not","then","than","while","yet","since",
    "until","unless","because","although","though","before","after",
    "during","into","onto","upon","across","through","around",
    "between","among","under","over","above","below","here","all",
    "both","each","more","most","such","only","own","too","already",
    "still","even","ever","never","always","often","whom","whose",
    # Found the same way, same session: "down" scored a 0.857 ratio
    # against "own" (also just added above) and "idle" scored high
    # enough against "side" to get silently rewritten too - short
    # equipment-status words are exactly as vulnerable to this as the
    # short connector words above were.
    "down","idle",
    # "range" (5 chars) scored a 0.833 ratio against "strange" (an
    # EVENTS/CONDITIONS alias) - not even a short-word case this time,
    # just two real words that happen to share a lot of characters.
    "range",
    # Spelled-out number words (needed for extract_days_ago() to parse
    # "two days before yesterday" etc.) - a full scan found 8 of 10
    # vulnerable: "one"->"on", "two"->"to", "four"->"for", "five"->
    # "give", "seven"->"even", "eight"->"weight", "nine"->"fine",
    # "ten"->"then". Only "three"/"six" happened to survive - adding
    # the complete set defensively rather than just the broken ones,
    # same reasoning as the connector-word batch above.
    "one","two","three","four","five","six","seven","eight","nine","ten",
    # Comparison-question vocabulary (see extract_comparison_periods())
    # - generic connector/trigger words, never equipment names, so
    # always safe to strip from equipment_terms. "today"/"yesterday"
    # are TIMES aliases already handled by first_match() for the
    # single-match case; adding them here too means BOTH periods of a
    # "today and yesterday" comparison get stripped, not just whichever
    # one first_match() happened to pick first.
    "compare","comparison","compared","versus","vs","against",
    "today","yesterday",
    # Same recurring bug class as the connector-word batch above, found
    # while wiring "every"/"overall" into the equipment_status broad-
    # match branches: "every" (not previously in the vocabulary at all)
    # scored a high enough ratio against "ever" (already a stopword,
    # one character away) to get silently rewritten - "every equipment
    # in P01" was becoming "ever equipment in p01" before the intent
    # classifier ever saw it, so the "every" in discovery_words the
    # equipment_status branches check for was never actually there.
    # "overall" added defensively alongside it for the same reason
    # (never checked in practice, but the exact same length/shape risk).
    "every","overall",
    # "equipment"/"plant" are the other half of the same "every
    # equipment in P01 plant" phrasing - both were surviving into
    # equipment_terms as literal (non-typo) generic filler words (no
    # real equipment name contains either - confirmed against
    # config.db), so the tag-ranking step was scoring them against
    # real tag/equipment names as if they were meaningful search terms
    # and confidently "resolving" to one arbitrary equipment (Main
    # Incomer) instead of falling through to the intended plant-wide
    # status summary. "equipment" is already treated as pure generic
    # vocabulary for the *discovery* intent (see DISCOVERY_TARGETS
    # above) - this extends the same treatment to equipment_terms
    # stripping. extract_plant() only strips the matched "p01"/"plant
    # 2" fragment itself, not a bare trailing "plant" with no digit
    # attached, so this is still needed even with that already in place.
    "equipment","equipments","plant",
}

DISCOVERY_TARGETS = {
    "component", "components", "equipment", "equipments",
    "tag", "tags", "sensor", "sensors", "device", "devices",
    "instrument", "instruments", "asset", "assets", "machine",
    "machines",
}

DISCOVERY_TRIGGER_WORDS = {"available", "monitor", "list", "show", "help"}

# Small talk that isn't really a factory question at all - checked as
# a *whole-message* exact match (not contains()/first_match()), so a
# real question that happens to start with "hi" or end with "thanks"
# ("hi, why is the compressor tripping") is never swallowed by this -
# only a message that IS just the greeting/thanks qualifies.
GREETING_PHRASES = {
    "hi", "hello", "hey", "hey there", "hiya", "yo", "greetings",
    "good morning", "good afternoon", "good evening",
}
THANKS_PHRASES = {
    "thanks", "thank you", "thanks a lot", "thank you very much",
    "cheers", "appreciate it", "much appreciated", "thanks so much",
}

# Every word this pipeline actually recognizes - used to spell-correct
# typos before any matching happens. Equipment/tag names have their
# own separate fuzzy-matching path (engine.industrial_query_engine);
# this only covers the fixed vocabulary below (intent keywords,
# measurement/location/condition/event/time words, stopwords).
VOCABULARY_WORDS = set(STOPWORDS) | DISCOVERY_TARGETS | DISCOVERY_TRIGGER_WORDS

for _phrase_set in (GREETING_PHRASES, THANKS_PHRASES):
    for _phrase in _phrase_set:
        VOCABULARY_WORDS.update(_phrase.split())

for _mapping in (MEASUREMENTS, LOCATIONS, CONDITIONS, EVENTS, TIMES):
    for _aliases in _mapping.values():
        for _alias in _aliases:
            VOCABULARY_WORDS.update(_alias.replace("_", " ").split())

# Real words from equipment display names (Bead Mill, Area Monitoring,
# Dust Collector, Filling Machine, Fire Water System, UPS) that aren't
# part of the fixed vocabulary above, so _correct_word() treated them
# as typos - the same recurring bug class as the STOPWORDS batches
# below ("or"->"orp", the connector-word batch, the number-word batch:
# a real, correctly-spelled word coincidentally resembles something in
# the vocabulary and gets silently rewritten), just hitting equipment-
# name words instead of common English ones this time. Found by
# systematically running every word from every equipment display name
# through correct_spelling() (6 fresh-process repeats each, to also
# catch PYTHONHASHSEED-dependent set-iteration-order ties like the
# ones documented below) rather than waiting to be bitten by each one
# individually - confirmed corrections: "area"->"are", "dust"->"just",
# "filling"->"falling", "ups"->"up" (deterministic every run), "fire"->
# "fine"/"five" and "mill"->"fill"/"will" (varies by process, the same
# tie-breaking instability already documented for other words below).
# Deliberately added here (not STOPWORDS) - these words must still
# survive into equipment_terms so they can identify their equipment
# (e.g. "mill" needs to match "Bead Mill"), unlike a true stopword,
# which is deliberately stripped.
EQUIPMENT_NAME_WORDS = {"area", "dust", "filling", "fire", "mill", "ups"}
VOCABULARY_WORDS.update(EQUIPMENT_NAME_WORDS)

def normalize(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = text.replace("_", " ").replace("-", " ").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def _edit_distance(left: str, right: str) -> int:
    """
    Plain Levenshtein distance (single-character insert/delete/
    substitute) between two short words - used as a second guard in
    _correct_word(), see the comment there for why.
    """
    if left == right:
        return 0

    previous_row = list(range(len(right) + 1))

    for i, left_char in enumerate(left, start=1):
        current_row = [i]

        for j, right_char in enumerate(right, start=1):
            current_row.append(
                min(
                    current_row[j - 1] + 1,
                    previous_row[j] + 1,
                    previous_row[j - 1] + (left_char != right_char),
                )
            )

        previous_row = current_row

    return previous_row[-1]


def _correct_word(word: str) -> str:
    """
    Spell-correct one word against the known vocabulary.

    Skips words that are already correct, too short to correct
    reliably, or contain a digit (likely a tag/instance code like
    "cr01", which must never be "corrected" into something else).
    """
    if len(word) <= 1 or word in VOCABULARY_WORDS or any(ch.isdigit() for ch in word):
        return word

    best_word = word
    best_ratio = 0.0

    for candidate in VOCABULARY_WORDS:
        if abs(len(candidate) - len(word)) > 2:
            continue

        ratio = SequenceMatcher(None, word, candidate).ratio()

        if ratio > best_ratio:
            best_ratio = ratio
            best_word = candidate

    # Short words naturally score lower ratios for the same one-letter
    # edit ("wy" -> "why" is only 0.80), so the bar is lower for them -
    # still conservative enough to avoid miscorrecting real short words.
    threshold = 0.82 if len(word) > 4 else 0.72

    if best_ratio < threshold:
        return word

    # A real typo is almost always a single character edit away from
    # its intended word ("wat"/"what", "comprssor"/"compressor",
    # "availble"/"available" are all distance 1). SequenceMatcher's
    # ratio alone doesn't guarantee that, though - it can also rate
    # two genuinely different, correctly-spelled real words as "close
    # enough" ("range"/"strange" is 0.83, but a 2-edit gap; "or"/"orp"
    # and "down"/"own" both cleared the ratio bar too before being
    # protected in STOPWORDS/VOCABULARY_WORDS by hand). Requiring the
    # edit distance to actually be <=1 catches this whole class
    # generically, rather than one more word at a time as each is
    # found the hard way.
    if _edit_distance(word, best_word) > 1:
        return word

    return best_word

def correct_spelling(text: str) -> str:
    return " ".join(_correct_word(word) for word in text.split())

def contains(text: str, phrase: str) -> bool:
    return f" {normalize(phrase)} " in f" {normalize(text)} "

def first_match(text: str, mapping: dict[str, tuple[str, ...]]) -> tuple[str, str]:
    for canonical, aliases in mapping.items():
        for alias in sorted(aliases, key=len, reverse=True):
            if contains(text, alias):
                return canonical, alias
    return "", ""

class ConceptExtractor:
    def extract(self, question: str) -> Concepts:
        question = str(question or "").strip()
        if not question:
            raise ValueError("Question cannot be empty.")
        text = correct_spelling(normalize(question))

        discovery_words = set(text.split())

        # Computed early (not with the other concept fields below)
        # specifically so the equipment_status check just below can
        # tell "how is the compressor doing" (broad, no measurement
        # named - a real status-summary question) apart from "how is
        # the compressor pressure doing" (a specific measurement was
        # named, so this is really current_data for that measurement,
        # phrased conversationally).
        measurement, m_alias = first_match(text, MEASUREMENTS)

        # Also computed early, for the same reason - lets the broad
        # equipment_status check below recognize "is any equipment has
        # alarm now" / "tell me if anything has alarm or warning now"
        # (a live status check phrased around "alarm"/"warning" rather
        # than "ok"/"broken") without that phrasing having to be
        # enumerated as fixed contains() phrases, which natural
        # word-order variation keeps slipping past.
        event_type, e_alias = first_match(text, EVENTS)

        # Also computed early - lets the timeline check below tell "is
        # there any alarm or warning yesterday" (a real historical time
        # range named alongside an EVENTS word - a machine_events
        # history question) apart from "is any equipment has alarm
        # now" (no genuine past-range word - a live status check,
        # handled by the equipment_status branch further down).
        time_expression, t_alias = first_match(text, TIMES)

        # Overrides the plain "yesterday" bucket match above when a
        # more specific "N days ago"/"N days before yesterday" phrase
        # is present - "one day before yesterday" contains "yesterday"
        # as a substring too, so without this override it would
        # silently collapse to the same bucket as a bare "yesterday"
        # and lose the "one day before" part entirely.
        days_ago_expression, days_ago_alias = extract_days_ago(text)

        if days_ago_expression:
            time_expression, t_alias = days_ago_expression, days_ago_alias

        # Checked against the RAW question (see extract_comparison_periods()'s
        # own docstring for why), before spelling-correction/normalization
        # ever touches it. Checked first, ahead of every other intent -
        # naming two explicit periods is specific enough that it should
        # never be shadowed by e.g. "why is..." (root_cause) also being
        # present in the same question.
        compare_period_a, compare_text_a, compare_period_b, compare_text_b = (
            extract_comparison_periods(question)
        )

        if compare_period_a and compare_period_b:
            intent = "comparison"
            time_expression = compare_period_a
        elif text in GREETING_PHRASES:
            intent = "chitchat_greeting"
        elif text in THANKS_PHRASES:
            intent = "chitchat_thanks"
        elif (
            "available" in discovery_words
            or "monitor" in discovery_words
            or (
                discovery_words & {"list", "show"}
                and discovery_words & DISCOVERY_TARGETS
            )
            or any(
                contains(text, p)
                for p in (
                    "what can i check", "what can i ask", "help",
                    "what can you do", "what do you do",
                    "what can you help", "what can you help with",
                )
            )
        ):
            intent = "discovery"
        elif any(
            contains(text, p)
            for p in (
                "why did", "why is", "why does", "what caused",
                "root cause",
                # correct_spelling() always folds "whats" down to
                # "what" (no vocabulary entry for the contraction
                # itself, and it scores a high ratio against "what") -
                # checked in that post-correction form, plus the
                # fully-spelled-out "what is ..." form for when someone
                # types it that way from the start.
                "what wrong with", "what is wrong with",
                "what causing", "what is causing",
            )
        ):
            intent = "root_cause"
        elif any(
            contains(text, p)
            for p in (
                "alarm history", "event history", "when did", "timeline",
                "anything happen", "anything happened",
                "what happened", "any events", "any alarms",
                "anything wrong", "days ago", "days before yesterday",
                "day before yesterday",
            )
        ) or (
            # An EVENTS word (alarm/warning/fault) paired with a real
            # PAST time range ("yesterday", "last 7 days", "3 days
            # ago", ...) - "latest"/"now" don't count here (see
            # HISTORICAL_TIME_EXPRESSIONS below), since "what is the
            # current alarm status" is a live-state question, not a
            # request for machine_events history.
            event_type and (
                time_expression in HISTORICAL_TIME_EXPRESSIONS
                or time_expression.startswith("days_ago:")
            )
        ):
            intent = "timeline"
        elif any(contains(text, p) for p in ("trend", "over time", "increasing", "decreasing", "history of")):
            intent = "trend"
        elif any(
            contains(text, p)
            for p in (
                "alarm limit", "warning limit", "threshold", "setpoint",
                "trip point", "what triggers", "what triggers an alarm",
                "at what point", "safe range", "normal range",
            )
        ):
            intent = "threshold"
        elif (
            # "is/any/all/everything <...> ok/fine/down/..." - the
            # middle varies with whatever was asked about ("is the
            # chiller ok", "is AC01 running fine", "any equipment
            # down"), so this can't be a fixed contains() phrase the
            # way "why is"/"trend" can - it's checked as a start/end
            # shape instead. Not gated on "no measurement found",
            # unlike the other branches below - ending in one of these
            # state words is specific enough on its own even when a
            # word like "running" (also a measurement alias) shows up
            # in the middle: "is AC01 running fine" is a health check,
            # not a request for the raw run-status bit. Positive
            # ("ok"/"fine"/"smoothly") and negative ("down"/"broken")
            # state words are lumped into one set on purpose - this
            # only has to recognize "this is a status-check question",
            # not judge which way the answer will go (RuleEngine does
            # that from real data afterwards).
            text.startswith(("is ", "any ", "anything ", "all ", "everything "))
            and text.split()[-1] in {
                "ok", "okay", "fine", "healthy", "good", "well", "smoothly",
                "up", "online", "down", "offline", "broken",
            }
        ):
            intent = "equipment_status"
        elif not measurement and (
            text.startswith(("how is ", "how are ", "hows "))
            or any(
                contains(text, p)
                for p in (
                    "how everything",
                    # correct_spelling() normalizes "problems"/"issues"
                    # towards their singular forms (both are in the
                    # vocabulary via EVENTS' "fault" entry) - checking
                    # both forms here doesn't rely on that happening.
                    "any problem with", "any problems with",
                    "any issue with", "any issues with",
                    # "is anything wrong"/"any alarms" without a time
                    # word already route to the timeline intent above
                    # (factory-wide recent-events fallback) - these are
                    # the current-state-only phrasings that don't fit
                    # that history-log framing as well as a live
                    # RuleEngine status check.
                    "anything broken", "is anything broken",
                    # correct_spelling() mangles "faulty" towards the
                    # shorter "fault" already in the vocabulary (EVENTS'
                    # "fault" alias tuple) - checked in its corrected
                    # form, matching what actually reaches this point.
                    "anything fault", "anything damaged",
                    "any warnings", "show me alarms", "show alarms",
                    "show me warnings", "current alarms",
                    # "all good"/"everything ok" style openers followed
                    # by trailing text ("all good on the factory
                    # floor?") - the end-anchor branch above only
                    # catches these when the state word is the very
                    # last word, so a phrase-contains check picks up
                    # the rest.
                    "all good", "all ok", "all okay", "all fine",
                    "everything good", "everything ok", "everything okay",
                    "everything fine", "everything alright",
                    "equipment down", "anything down", "anything offline",
                )
            )
            # Broad "any equipment has alarm now" / "tell me if
            # anything has alarm or warning" style phrasing - an
            # EVENTS word (alarm/warning/fault) paired with generic
            # "any"/"anything"/"equipment"/"everything" framing, not a
            # request for one specific tag's literal AlarmCode value
            # ("what is the AC01 alarm code" has neither of those
            # framing words, so it's untouched and stays current_data).
            or (
                event_type
                and discovery_words & {"any", "anything", "equipment", "everything", "all", "every"}
            )
            # "what is having alarm or warning now"/"what's showing a
            # fault" - same broad live-status framing as the block
            # above, phrased as "is/are having/showing" instead of
            # "any"/"anything". Still gated on event_type for the same
            # reason: without an alarm/fault/warning word, "is having"
            # is too generic a phrase to safely claim on its own (found
            # live - the earlier "any"/"all"/"every" word-set alone
            # didn't cover this phrasing).
            or (
                event_type
                and any(
                    contains(text, p)
                    for p in ("is having", "are having", "is showing", "are showing")
                )
            )
        ):
            # A broad "how's it doing" / "is everything ok" question -
            # no specific measurement named, so this isn't asking for
            # one tag's value, it's asking for a whole-equipment (or,
            # with no equipment named either, whole-factory) status
            # summary. See app/ask.py's "equipment_status" handling.
            intent = "equipment_status"
        elif (
            measurement == "status" or "equipment" in discovery_words
        ) and discovery_words & {"all", "every", "overall"}:
            # "show me all compressor status"/"what is the status of
            # all compressors" - "status" alone matched MEASUREMENTS
            # (a real per-tag concept, e.g. RunStatus/LoadStatus),
            # which is the right read for a single-instance question
            # ("what is the AC01 status"). But "all"/"every"/"overall"
            # signals a broad request across every instance of a type -
            # the same equipment_status use case as "is everything
            # ok", not a request for one specific status tag's raw
            # value. Without this, "show me all compressor status"
            # fell through to current_data and landed on an unhelpfully
            # ambiguous 5-way Load/UnloadStatus tag menu (AC01/02/03 x
            # 2 tags each) - found via live testing, not hypothetical.
            intent = "equipment_status"
        else:
            intent = "current_data"
        location, l_alias = first_match(text, LOCATIONS)
        condition, c_alias = first_match(text, CONDITIONS)
        plant, p_alias = extract_plant(text)

        threshold_type = ""
        if intent == "threshold":
            if contains(text, "high"):
                threshold_type = "high"
            elif contains(text, "low"):
                threshold_type = "low"

        if not time_expression:
            time_expression = "latest" if intent == "current_data" else ""

        removable = set(STOPWORDS)
        for phrase in (m_alias, l_alias, c_alias, e_alias, t_alias, p_alias):
            removable.update(normalize(phrase).split())
        if intent == "comparison":
            # Month-name words (e.g. "aug") aren't in STOPWORDS (they're
            # real equipment-unrelated words, but not generic enough to
            # blanket-exclude everywhere) - strip them here, scoped to
            # comparison questions only, alongside the exact matched
            # period phrases so neither period leaks into equipment
            # matching. Digits are already excluded from equipment_terms
            # below regardless, so "10"/"2026" need no special handling.
            for phrase in (compare_text_a, compare_text_b):
                removable.update(normalize(phrase).split())
        equipment_terms = tuple(
            token for token in text.split()
            if token not in removable and len(token) > 1 and not token.isdigit()
        )

        return Concepts(
            question=question,
            normalized=text,
            intent=intent,
            equipment_terms=equipment_terms,
            measurement=measurement,
            location=location,
            condition=condition,
            event_type=event_type,
            threshold_type=threshold_type,
            time_expression=time_expression,
            plant=plant,
            compare_period_a=compare_period_a if intent == "comparison" else "",
            compare_period_b=compare_period_b if intent == "comparison" else "",
        )
