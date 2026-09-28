"""The approved-answer knowledge base, cleaned on load.

The concierge may only say what the lender has signed off on. Before any
answer is served, the knowledge base itself is cleaned:
  - entries with no approver or approval date are excluded,
  - entries past their review-by date are excluded (stale terms are a risk),
  - near-duplicate entries are merged so one question has one answer,
and every exclusion is reported so the content owner can fix it.

Retrieval is a small keyword ranker (IDF-weighted overlap with synonyms). It
is deliberately simple and deterministic: if nothing matches well enough, the
concierge declines and offers a person instead of improvising.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from .normalize import US_STATES

STOPWORDS = {
    "a", "an", "the", "is", "are", "do", "does", "you", "your", "i", "we", "my", "our",
    "to", "of", "for", "on", "in", "and", "or", "what", "how", "can", "could", "would",
    "will", "with", "it", "be", "me", "about", "any", "there", "this", "that", "have",
    "has", "much", "many", "which", "who", "when", "where", "if", "at", "by", "get",
    "offer", "offers", "provide", "tell", "please", "hi", "hello", "just", "am", "so",
    "loan", "loans", "lender", "financing", "finance", "property", "properties", "kind", "type",
}

# Map everyday wording onto the vocabulary used in the approved answers.
SYNONYMS = {
    "leverage": "ltv", "loan-to-value": "ltv", "ltc": "ltv",
    "interest": "rate", "rates": "rate", "pricing": "rate", "spread": "rate",
    "close": "closing", "closes": "closing", "fast": "closing", "quickly": "closing",
    "timeline": "closing", "long": "closing",
    "apartment": "multifamily", "apartments": "multifamily", "multi-family": "multifamily",
    "rental": "investor", "rentals": "investor", "duplex": "investor", "sfr": "investor",
    "states": "state", "lend": "lending", "markets": "state", "areas": "state",
    "docs": "documents", "paperwork": "documents", "need": "documents",
    "minimum": "size", "smallest": "size", "biggest": "size", "largest": "size",
    "prepay": "prepayment", "penalty": "prepayment", "payoff": "prepayment",
    "guarantee": "recourse", "guaranty": "recourse", "personal": "recourse",
    "refi": "refinance", "refinancing": "refinance", "maturing": "refinance",
}


_STATE_RE = re.compile(r"\b(" + "|".join(sorted(US_STATES, key=len, reverse=True)) + r")\b")


def tokens(text: str) -> list[str]:
    out = []
    lowered = _STATE_RE.sub(" state ", text.lower())  # "Pennsylvania" asks about footprint
    for raw in re.findall(r"[a-z0-9$%\-]+", lowered):
        word = SYNONYMS.get(raw, raw)
        if word.endswith("s") and len(word) > 4 and word not in SYNONYMS.values():
            word = word[:-1]
        word = SYNONYMS.get(word, word)
        if word not in STOPWORDS and len(word) > 1:
            out.append(word)
    return out


@dataclass
class Entry:
    id: str
    topic: str
    questions: list[str]
    answer: str
    approved_by: str | None
    approved_on: str | None
    review_by: str | None
    keywords: list[str] = field(default_factory=list)
    handoff: bool = False            # answer should be followed by an offer of a person

    @property
    def terms(self) -> set[str]:
        return set(tokens(" ".join(self.questions + self.keywords + [self.topic])))


@dataclass
class CleaningReport:
    loaded: int = 0
    served: int = 0
    excluded: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)


@dataclass
class Match:
    entry: Entry | None
    confidence: float
    runner_up: float = 0.0


class KnowledgeBase:
    def __init__(self, entries: list[Entry], report: CleaningReport):
        self.entries = entries
        self.report = report
        n_docs = max(len(entries), 1)
        df: dict[str, int] = {}
        for e in entries:
            for t in e.terms:
                df[t] = df.get(t, 0) + 1
        self.idf = {t: math.log(1 + n_docs / c) for t, c in df.items()}

    @classmethod
    def load(cls, path: str | Path, today: date | None = None) -> "KnowledgeBase":
        raw = json.loads(Path(path).read_text())
        return cls.from_records(raw["entries"], today or date.today())

    @classmethod
    def from_records(cls, records: list[dict], today: date) -> "KnowledgeBase":
        report = CleaningReport(loaded=len(records))
        kept: list[Entry] = []
        for r in records:
            entry = Entry(
                id=r["id"], topic=r.get("topic", ""),
                questions=[q.strip() for q in r.get("questions", []) if q and q.strip()],
                answer=" ".join((r.get("answer") or "").split()),
                approved_by=r.get("approved_by"), approved_on=r.get("approved_on"),
                review_by=r.get("review_by"), keywords=r.get("keywords", []),
                handoff=bool(r.get("handoff", False)),
            )
            if not entry.answer or not entry.questions:
                report.excluded.append(f"{entry.id}: missing question or answer")
                continue
            if not (entry.approved_by and entry.approved_on):
                report.excluded.append(f"{entry.id}: not approved")
                continue
            if entry.review_by and date.fromisoformat(entry.review_by) < today:
                report.excluded.append(f"{entry.id}: past review date {entry.review_by}")
                continue
            duplicate = next((k for k in kept if _near_duplicate(k, entry)), None)
            if duplicate:
                # Keep the most recently approved answer; keep all question wordings.
                newer, older = sorted([duplicate, entry], key=lambda e: e.approved_on, reverse=True)
                merged_questions = list(dict.fromkeys(newer.questions + older.questions))
                newer.questions = merged_questions
                newer.keywords = list(dict.fromkeys(newer.keywords + older.keywords))
                kept[kept.index(duplicate)] = newer
                report.merged.append(f"{older.id} merged into {newer.id}")
                continue
            kept.append(entry)
        report.served = len(kept)
        return cls(kept, report)

    def search(self, text: str) -> Match:
        query = tokens(text)  # a list: "personal guarantee" is two votes for recourse
        if not query:
            return Match(None, 0.0)
        scored = []
        for e in self.entries:
            terms = e.terms
            overlap = [t for t in query if t in terms]
            if not overlap:
                continue
            weight = sum(self.idf.get(t, 0) for t in overlap)
            # A word no approved answer uses ("construction") counts as heavily as
            # the rarest known word, so an off-topic question cannot ride on a
            # generic word and score as a confident match.
            unknown = max(self.idf.values(), default=1.0)
            total = sum(self.idf.get(t, unknown) for t in query)
            scored.append((weight / total if total else 0.0, e))
        if not scored:
            return Match(None, 0.0)
        scored.sort(key=lambda x: -x[0])
        best, entry = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        return Match(entry, round(best, 3), round(second, 3))


def _near_duplicate(a: Entry, b: Entry) -> bool:
    if a.topic and a.topic == b.topic:
        return True
    return any(SequenceMatcher(None, qa.lower(), qb.lower()).ratio() > 0.9
               for qa in a.questions for qb in b.questions)
