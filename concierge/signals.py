"""Pull deal signals and contact details out of free-text chat messages.

Signals accumulate across the conversation: a visitor might mention the
property type in one message and the loan size three messages later.
Contact details are cleaned with the same rules used for CRM data, so the
duplicate check against the CRM compares like with like.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from . import normalize as n

PROPERTY_PATTERNS = [
    ("residential-investor", r"\b(1-4|one to four|2-4|duplex|triplex|fourplex|single[- ]family|sfr|rental home|rental house)\b"),
    ("multifamily", r"\b(multi-?family|apartment|apartments|units?\b(?! of)|garden[- ]style|walk-?up)"),
    ("mixed-use", r"\bmixed[- ]use\b"),
    ("office", r"\boffice\b"),
    ("retail", r"\b(retail|shopping center|strip center)\b"),
    ("industrial", r"\b(industrial|warehouse|flex space|distribution)\b"),
    ("hospitality", r"\b(hotel|motel|hospitality)\b"),
]

AMOUNT_RE = re.compile(
    r"\$?\s?(\d+(?:\.\d+)?)\s?(million|mil|mm|m|k|thousand)\b|\$\s?(\d{1,3}(?:,\d{3})+)", re.I)
UNITS_RE = re.compile(r"\b(\d{1,4})[- ]?(?:unit|units|doors|apartments)\b", re.I)
EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
NAME_RE = re.compile(r"\b(?i:my name is|this is|i am|i'm|name's)\s+([A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-]+)+)")
COMPANY_RE = re.compile(r"\b(?:with|at|from|of)\s+([A-Z][\w&'\-]*(?:\s+[A-Z][\w&'\-]*)*\s+(?:LLC|LP|Capital|Partners|Properties|Group|Holdings|Realty|Management))\b")
TIMING_RE = re.compile(
    r"\b(asap|immediately|this month|next month|in (\d{1,2}) (?:months?|weeks?)|"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)|"
    r"(q[1-4])|(?:matures?|maturing|due) (?:in|next|this)[^.,;]*)", re.I)
EXISTING_RE = re.compile(
    r"\b(already have a loan|existing (?:loan|borrower)|current borrower|my loan with you|"
    r"payoff|relationship manager|my (?:loan )?officer|loan number)\b", re.I)
HUMAN_RE = re.compile(r"\b(speak|talk|connect)\b.{0,20}\b(person|human|someone|loan officer|agent|rep)\b", re.I)


@dataclass
class Signals:
    property_type: str | None = None
    units: int | None = None
    loan_amount: float | None = None
    state: str | None = None
    timing: str | None = None
    existing_borrower: bool = False
    wants_human: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ContactInfo:
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    company: str | None = None

    @property
    def reachable(self) -> bool:
        return bool(self.email or self.phone)

    def as_dict(self) -> dict:
        return asdict(self)


def _amount(text: str) -> float | None:
    for m in AMOUNT_RE.finditer(text):
        if m.group(3):
            return float(m.group(3).replace(",", ""))
        value, unit = float(m.group(1)), m.group(2).lower()
        if unit in ("million", "mil", "mm", "m"):
            return value * 1e6
        return value * 1e3
    return None


def _state(text: str) -> str | None:
    lower = text.lower()
    for name, code in sorted(n.US_STATES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{name}\b", lower):
            return code
    for code in re.findall(r"\b([A-Z]{2})\b", text):
        if code in n.US_STATES.values() and code not in {"IN", "OR", "ME", "OK", "HI"}:
            return code
    if re.search(r"\b(nyc|manhattan|brooklyn|queens|bronx)\b", lower):
        return "NY"
    return None


def update_signals(sig: Signals, text: str) -> Signals:
    lower = text.lower()
    for ptype, pattern in PROPERTY_PATTERNS:
        if re.search(pattern, lower):
            sig.property_type = sig.property_type if sig.property_type == "residential-investor" and ptype == "multifamily" else ptype
            break
    units = UNITS_RE.search(text)
    if units:
        sig.units = int(units.group(1))
        if sig.units <= 4:
            sig.property_type = "residential-investor"
        elif sig.property_type in (None, "residential-investor"):
            sig.property_type = "multifamily"
    text_wo_contacts = PHONE_RE.sub(" ", EMAIL_RE.sub(" ", text))
    sig.loan_amount = _amount(text_wo_contacts) or sig.loan_amount
    sig.state = _state(text_wo_contacts) or sig.state
    timing = TIMING_RE.search(text)
    if timing:
        sig.timing = timing.group(0).strip()
    if EXISTING_RE.search(text):
        sig.existing_borrower = True
    if HUMAN_RE.search(text):
        sig.wants_human = True
    return sig


def update_contact(info: ContactInfo, text: str) -> ContactInfo:
    email = EMAIL_RE.search(text)
    if email:
        info.email = n.normalize_email(email.group(0)) or info.email
    phone = PHONE_RE.search(text)
    if phone:
        info.phone = n.normalize_phone(phone.group(0)) or info.phone
    name = NAME_RE.search(text)
    if name:
        info.first_name, info.last_name = n.split_person_name(full=name.group(1))
    company = COMPANY_RE.search(text)
    if company:
        info.company = n.clean_company(company.group(1))
    return info
