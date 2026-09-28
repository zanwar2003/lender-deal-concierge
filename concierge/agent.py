"""The conversation flow, built as a LangGraph state graph.

One call to `Concierge.reply()` runs one turn through these nodes:

    screen ──blocked──────────────────────────────────────► END
       │
       ▼
    understand  (update deal signals + contact details)
       │
       ▼
    answer      (approved answer, or a polite decline)
       │
       ▼
    qualify     (route to a team, decide what to ask next)
       │                         │
       ├──ready + reachable──► handoff (CRM duplicate check,
       │                               draft record, approval queue)
       ▼                         │
    compose ◄────────────────────┘
       │
       ▼
      END
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .crm import ApprovalQueue, ContactDirectory, draft_records
from .guardrails import SAFE_REPLIES, output_is_grounded, redact, screen_input
from .knowledge import Entry, KnowledgeBase
from .routing import in_footprint, load_rules, route
from .signals import ContactInfo, Signals, update_contact, update_signals

CONFIDENCE = 0.45
QUESTION_RE = re.compile(
    r"\?|^\s*(what'?s?|how'?s?|do|does|can|could|is|are|which|where|when|who|will|any)\b", re.I)


@dataclass
class Session:
    id: str
    signals: Signals = field(default_factory=Signals)
    contact: ContactInfo = field(default_factory=ContactInfo)
    transcript: list[dict] = field(default_factory=list)
    handed_off: bool = False
    queue_item_id: str | None = None
    last_prompt: str | None = None


class TurnState(TypedDict, total=False):
    session: Session
    message: str
    blocked: str | None
    answer: str | None
    answer_entry: Entry | None
    prompt: str | None
    team: Any
    ready: bool
    handoff_note: str | None
    reply: str
    trace: list[str]


class TemplateResponder:
    """Default: serve the approved answer word for word."""

    def phrase(self, entry: Entry, question: str) -> str:
        return entry.answer


class ClaudeResponder:
    """Optional: let Claude adapt the approved answer to the visitor's wording.

    The model only sees the approved answer and is told not to add facts. The
    result is still checked: if it introduces any number that is not in the
    approved answer, the approved answer is used verbatim instead.
    Requires ANTHROPIC_API_KEY. Model defaults to CONCIERGE_MODEL or claude-haiku-4-5.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ["ANTHROPIC_API_KEY"]
        self.model = model or os.getenv("CONCIERGE_MODEL", "claude-haiku-4-5")

    def phrase(self, entry: Entry, question: str) -> str:
        body = {
            "model": self.model, "max_tokens": 300,
            "system": ("You answer website visitors for a commercial real estate lender. "
                       "Rephrase ONLY the approved answer so it responds naturally to the "
                       "question. Do not add facts, numbers, promises, or rates. "
                       "Two or three sentences."),
            "messages": [{"role": "user", "content":
                          f"Question: {question}\n\nApproved answer: {entry.answer}"}],
        }
        request = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                text = json.loads(response.read())["content"][0]["text"].strip()
        except Exception:
            return entry.answer
        return text if output_is_grounded(text, entry.answer) else entry.answer


class Concierge:
    def __init__(self, kb: KnowledgeBase, rules: dict, directory: ContactDirectory,
                 queue: ApprovalQueue, crm_system: str = "salesforce",
                 responder=None, lender_name: str = "Northgate Street Capital"):
        self.kb, self.rules, self.directory, self.queue = kb, rules, directory, queue
        self.crm_system = crm_system
        self.responder = responder or TemplateResponder()
        self.lender = lender_name
        self.graph = self._build()

    @classmethod
    def from_sample(cls, queue_path: str | Path, sample_dir: str | Path | None = None,
                    today: date | None = None, **kw) -> "Concierge":
        d = Path(sample_dir or Path(__file__).resolve().parent.parent / "sample_data")
        return cls(KnowledgeBase.load(d / "approved_answers.json", today),
                   load_rules(d / "routing.json"),
                   ContactDirectory.from_sample(d / "crm_contacts.json"),
                   ApprovalQueue(queue_path), **kw)

    def greeting(self) -> str:
        return (f"Hi, I'm the {self.lender} virtual assistant. I'm an AI, and I answer only "
                "from information our team has approved. I can tell you about our loan "
                "programs and connect you with the right loan officer. What are you looking "
                "to finance?")

    # ---- graph nodes ---------------------------------------------------------

    def _screen(self, s: TurnState) -> TurnState:
        category = screen_input(s["message"])
        s["session"].transcript.append({"role": "visitor", "text": redact(s["message"])})
        s["trace"] = [f"screen:{category or 'ok'}"]
        s["blocked"] = category
        if category:
            s["reply"] = SAFE_REPLIES[category]
        return s

    def _understand(self, s: TurnState) -> TurnState:
        session = s["session"]
        update_signals(session.signals, s["message"])
        update_contact(session.contact, s["message"])
        s["trace"].append("understand")
        return s

    def _answer(self, s: TurnState) -> TurnState:
        text = s["message"]
        s["answer"], s["answer_entry"] = None, None
        if QUESTION_RE.search(text):
            match = self.kb.search(text)
            if match.entry and match.confidence >= CONFIDENCE:
                s["answer"] = self.responder.phrase(match.entry, text)
                s["answer_entry"] = match.entry
                s["trace"].append(f"answer:{match.entry.id}@{match.confidence}")
            else:
                s["answer"] = ("I don't have an approved answer for that, and I don't want to "
                               "guess. A loan officer can answer it directly.")
                s["trace"].append(f"answer:declined@{match.confidence}")
        return s

    def _qualify(self, s: TurnState) -> TurnState:
        session, sig, info = s["session"], s["session"].signals, s["session"].contact
        team = route(sig, self.rules)
        s["team"] = team
        entry = s.get("answer_entry")
        wants_person = sig.wants_human or sig.existing_borrower or bool(entry and entry.handoff)
        qualified = bool(team and sig.property_type and sig.loan_amount and sig.state)
        s["ready"] = False
        s["prompt"] = None

        if session.handed_off:
            s["prompt"] = None
        elif sig.state and not in_footprint(sig, self.rules):
            s["prompt"] = (f"We currently lend in {', '.join(self.rules['footprint_states'])}, so "
                           f"we may not be the right fit for a property in {sig.state}. If you'd "
                           "still like a loan officer to take a look, share your name and email.")
        elif wants_person or qualified:
            if info.reachable and (info.last_name or sig.existing_borrower):
                s["ready"] = True
            elif not info.reachable:
                s["prompt"] = ("If you'd like a loan officer to follow up, what's your name and the "
                               "best email or phone number to reach you?")
            else:
                s["prompt"] = "Thanks. And what's your name?"
        elif not sig.property_type:
            s["prompt"] = "What type of property is it (for example multifamily, a 1-4 unit rental, office, or retail)?"
        elif not sig.loan_amount:
            s["prompt"] = "Roughly how large is the loan you're looking for?"
        elif not sig.state:
            s["prompt"] = "Where is the property located?"
        s["trace"].append(f"qualify:team={team.key if team else None},ready={s['ready']}")
        return s

    def _handoff(self, s: TurnState) -> TurnState:
        session, team = s["session"], s["team"]
        dup = self.directory.check(session.contact)
        record = draft_records(self.crm_system, session.contact, session.signals, dup,
                               team.name, team.owner_email, session.transcript)
        item = self.queue.add(team.name, dup, record, session.signals, session.contact)
        session.handed_off, session.queue_item_id = True, item.id
        who = session.contact.first_name or (dup.contact.first_name if dup.contact else None)
        who = f", {who}" if who else ""
        if dup.status == "exact":
            s["handoff_note"] = (f"Thanks{who}. I found you in our system, so I've passed this "
                                 "conversation to the person who already works with you. "
                                 "They'll follow up shortly.")
        else:
            s["handoff_note"] = (f"Thanks{who}. I've passed this conversation to our "
                                 f"{team.name} team, and a loan officer will reach out shortly.")
        s["trace"].append(f"handoff:{dup.status}:{item.id}")
        return s

    def _compose(self, s: TurnState) -> TurnState:
        parts = [p for p in (s.get("answer"), s.get("handoff_note")) if p]
        prompt = s.get("prompt")
        if prompt and prompt != s["session"].last_prompt or (prompt and not parts):
            parts.append(prompt)
        s["session"].last_prompt = prompt
        if not parts:
            parts.append("Is there anything else I can help with?" if s["session"].handed_off
                         else "Tell me a bit more about the property and I'll point you to the right team.")
        s["reply"] = " ".join(parts)
        return s

    def _build(self):
        g = StateGraph(TurnState)
        for name, fn in [("screen", self._screen), ("understand", self._understand),
                         ("answer", self._answer), ("qualify", self._qualify),
                         ("handoff", self._handoff), ("compose", self._compose)]:
            g.add_node(name, fn)
        g.add_edge(START, "screen")
        g.add_conditional_edges("screen", lambda s: "end" if s.get("blocked") else "go",
                                {"end": END, "go": "understand"})
        g.add_edge("understand", "answer")
        g.add_edge("answer", "qualify")
        g.add_conditional_edges("qualify", lambda s: "handoff" if s.get("ready") else "compose",
                                {"handoff": "handoff", "compose": "compose"})
        g.add_edge("handoff", "compose")
        g.add_edge("compose", END)
        return g.compile()

    # ---- public API ------------------------------------------------------------

    def reply(self, session: Session, message: str) -> str:
        result = self.graph.invoke({"session": session, "message": message})
        session.transcript.append({"role": "assistant", "text": result["reply"]})
        self.last_trace = result.get("trace", [])
        return result["reply"]
