"""Field-level cleaning for CRM records.

Every function here is pure: it takes a raw value and returns a cleaned value
(or None when the input carries no usable information). Keeping them pure makes
them easy to test and safe to run on any connector's output.
"""

from __future__ import annotations

import re
import unicodedata

# Placeholder values that people type into CRMs instead of leaving a field blank.
_JUNK_VALUES = {
    "", "n/a", "na", "none", "null", "unknown", "tbd", "-", "--", ".", "x",
    "test", "no email", "noemail", "none@none.com", "na@na.com",
}

_NAME_PREFIXES = {"mr", "mrs", "ms", "miss", "dr", "prof"}
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "phd", "esq", "cpa", "cfa"}

# Legal suffixes stripped for matching only (display name keeps them).
_COMPANY_SUFFIXES = {
    "llc", "l l c", "inc", "incorporated", "corp", "corporation", "co", "company",
    "lp", "l p", "llp", "ltd", "limited", "plc", "group", "holdings", "the",
    "na", "n a", "fsb",
}

# Tokens kept upper-case when tidying an ALL-CAPS name from public records.
_UPPER_TOKENS = {"llc", "lp", "llp", "inc", "na", "fsb", "reit", "usa", "ny", "nj"}

# USPS-style street suffix and directional abbreviations.
_STREET_ABBREVIATIONS = {
    "street": "st", "str": "st", "avenue": "ave", "av": "ave", "boulevard": "blvd",
    "road": "rd", "drive": "dr", "lane": "ln", "court": "ct", "place": "pl",
    "parkway": "pkwy", "highway": "hwy", "suite": "ste", "apartment": "apt",
    "square": "sq", "terrace": "ter", "circle": "cir", "floor": "fl",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}

_EMAIL_RE = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9.-]+\.[a-z]{2,}$")

# Consumer mail domains; a shared domain here says nothing about the company.
FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "me.com", "live.com", "msn.com", "protonmail.com",
}


def is_junk(value: object) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in _JUNK_VALUES


def _ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def clean_whitespace(value: object) -> str | None:
    if is_junk(value):
        return None
    return re.sub(r"\s+", " ", str(value)).strip() or None


def normalize_email(value: object) -> str | None:
    """Lowercase, trim, strip mailto: and angle brackets; None if invalid."""
    if is_junk(value):
        return None
    email = str(value).strip().lower()
    email = email.removeprefix("mailto:").strip("<> \t;,")
    if not _EMAIL_RE.match(email) or email in _JUNK_VALUES:
        return None
    return email


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[1]


def normalize_phone(value: object, default_country: str = "1") -> str | None:
    """Return E.164 (+15551234567) for North American numbers; None otherwise.

    Extensions ("x204", "ext. 12") are dropped because they do not identify a
    person across systems.
    """
    if is_junk(value):
        return None
    text = re.split(r"(?i)\s*(?:ext\.?|x|#)\s*\d+\s*$", str(value).strip())[0]
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10:
        digits = default_country + digits
    if len(digits) == 11 and digits.startswith("1"):
        # Reject obvious fake numbers like 555-555-5555 or all one digit.
        if len(set(digits[1:])) == 1:
            return None
        return "+" + digits
    return None


def split_person_name(first: object = None, last: object = None,
                      full: object = None) -> tuple[str | None, str | None]:
    """Return (first, last) in title case with prefixes/suffixes removed.

    Handles "Last, First" full names and records where the whole name was typed
    into the first-name field.
    """
    first_s = clean_whitespace(first)
    last_s = clean_whitespace(last)
    if not first_s and not last_s and full:
        full_s = clean_whitespace(full) or ""
        if "," in full_s:
            last_s, first_s = [p.strip() for p in full_s.split(",", 1)]
        else:
            parts = [p for p in full_s.split(" ")
                     if p.lower().strip(".,") not in _NAME_PREFIXES | _NAME_SUFFIXES]
            if parts:
                first_s, last_s = parts[0], " ".join(parts[1:]) or None
    elif first_s and not last_s and " " in first_s:
        parts = first_s.split(" ")
        first_s, last_s = parts[0], " ".join(parts[1:])

    def tidy(part: str | None, drop: set[str]) -> str | None:
        if not part:
            return None
        tokens = [t for t in re.split(r"\s+", part)
                  if t.lower().strip(".,") not in drop]
        if not tokens:
            return None
        return " ".join(_title_token(t.strip(".,")) for t in tokens) or None

    return (tidy(first_s, _NAME_PREFIXES | _NAME_SUFFIXES),
            tidy(last_s, _NAME_PREFIXES | _NAME_SUFFIXES))


def _title_token(token: str) -> str:
    """Title-case a name token while keeping O'Brien, McDonald, Smith-Jones."""
    def cap(piece: str) -> str:
        if piece.lower().startswith("mc") and len(piece) > 2:
            return "Mc" + piece[2:].capitalize()
        return piece.capitalize()
    token = "-".join(cap(p) for p in token.split("-"))
    return "'".join(p[:1].upper() + p[1:] for p in token.split("'"))


def name_key(first: str | None, last: str | None) -> str:
    """Lowercase ASCII key for comparing names across systems."""
    return _ascii_fold(f"{first or ''} {last or ''}").lower().strip()


def clean_company(value: object) -> str | None:
    """Tidy casing: "acme capital" -> "Acme capital", "GOLD STREET LP" -> "Gold Street LP"."""
    text = clean_whitespace(value)
    if not text:
        return None
    if text.isupper():
        return " ".join(t if t.lower().strip(".,") in _UPPER_TOKENS else t.capitalize()
                        for t in text.split(" "))
    return text[0].upper() + text[1:] if text.islower() else text


def company_key(value: object) -> str | None:
    """Matching key: lowercase, no punctuation, no legal suffixes.

    "The Acme Capital Group, LLC" and "ACME Capital" both become "acme capital".
    """
    text = clean_whitespace(value)
    if not text:
        return None
    text = _ascii_fold(text).lower().replace("&", " and ")
    text = re.sub(r"\bn\.\s?a\.?(?=\s|$)", " na ", text)  # "N.A." bank suffix
    text = re.sub(r"\bl\.\s?l\.\s?c\.?(?=\s|$)", " llc ", text)
    text = re.sub(r"\bl\.\s?p\.?(?=\s|$)", " lp ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    tokens = [t for t in text.split() if t not in _COMPANY_SUFFIXES]
    return " ".join(tokens) or None


def normalize_state(value: object) -> str | None:
    text = clean_whitespace(value)
    if not text:
        return None
    upper = text.upper().strip(".")
    if len(upper) == 2 and upper in US_STATES.values():
        return upper
    return US_STATES.get(text.lower())


def address_key(street: object, city: object = None, state: object = None,
                postal_code: object = None) -> str | None:
    """Canonical address string used to match the same property across sources.

    Lowercases, removes punctuation, abbreviates street types and directionals,
    and drops unit numbers so "100 North Main Street, Suite 4" matches
    "100 N Main St".
    """
    street_s = clean_whitespace(street)
    if not street_s:
        return None
    text = _ascii_fold(street_s).lower()
    text = re.sub(r"[.,#]", " ", text)
    text = re.split(r"\b(?:ste|suite|apt|apartment|unit|fl|floor)\b", text)[0]
    tokens = [_STREET_ABBREVIATIONS.get(t, t) for t in text.split()]
    zip5 = None
    if postal_code and not is_junk(postal_code):
        match = re.match(r"\d{5}", str(postal_code).strip())
        zip5 = match.group(0) if match else None
    city_s = (clean_whitespace(city) or "").lower()
    parts = [" ".join(tokens), city_s, normalize_state(state) or "", zip5 or ""]
    return "|".join(parts)


US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN",
    "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}

# Contact roles as they appear in the wild, mapped to the unified vocabulary.
_ROLE_ALIASES = {
    "owner": "owner", "property owner": "owner", "borrower": "owner",
    "sponsor": "sponsor", "investor": "sponsor", "gp": "sponsor",
    "broker": "broker", "mortgage broker": "broker", "intermediary": "broker",
    "lender": "lender", "bank": "lender", "debt fund": "lender",
}


def normalize_role(value: object) -> str | None:
    text = clean_whitespace(value)
    if not text:
        return None
    return _ROLE_ALIASES.get(text.lower(), "other")
