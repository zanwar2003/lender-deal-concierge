"""Route a qualified visitor to the right team.

Rules are data (sample_data/routing.json), so the lender can change who gets
what without touching code. The first matching rule wins.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .signals import Signals


@dataclass
class Team:
    key: str
    name: str
    owner_email: str
    reason: str


def load_rules(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def _matches(rule: dict, sig: Signals) -> bool:
    when = rule.get("when", {})
    if when.get("existing_borrower") and not sig.existing_borrower:
        return False
    if "property_type" in when and sig.property_type not in when["property_type"]:
        return False
    if "min_amount" in when and (sig.loan_amount is None or sig.loan_amount < when["min_amount"]):
        return False
    if "max_amount" in when and (sig.loan_amount is None or sig.loan_amount >= when["max_amount"]):
        return False
    if "states" in when and sig.state and sig.state not in when["states"]:
        return False
    return True


def route(sig: Signals, rules: dict) -> Team | None:
    """Return the team for these signals, or None if we don't know enough yet."""
    known = sig.existing_borrower or sig.property_type or sig.loan_amount
    if not known:
        return None
    for rule in rules["rules"]:
        if _matches(rule, sig):
            team = rules["teams"][rule["team"]]
            return Team(rule["team"], team["name"], team["owner_email"], rule["reason"])
    fallback = rules["teams"][rules["default_team"]]
    return Team(rules["default_team"], fallback["name"], fallback["owner_email"],
                "no specific rule matched")


def in_footprint(sig: Signals, rules: dict) -> bool:
    return not sig.state or sig.state in rules.get("footprint_states", [])
