"""CRM side: find existing contacts, draft records, and hold them for approval.

Before anything is created, the visitor is checked against contacts already
in the CRM (Salesforce and/or HubSpot). The outcome decides the draft:
  - exact match (same email, or same phone + last name): no new record. A
    follow-up Task is drafted on the existing contact and routed to their
    relationship owner. This is what stops duplicate leads.
  - possible match (same name and company, different contact details): a new
    lead is drafted but flagged so the reviewer can merge instead.
  - no match: a new Lead (Salesforce) or Contact (HubSpot) is drafted.

Drafts go into an approval queue. Nothing is written to a CRM until a person
approves it, and approval sends in dry-run mode unless live credentials are
configured.
"""

from __future__ import annotations

import json
import os
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import normalize as n
from .signals import ContactInfo, Signals


@dataclass
class CRMContact:
    system: str
    id: str
    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    company: str | None
    owner_email: str | None = None


@dataclass
class DuplicateCheck:
    status: str                 # exact | possible | none
    contact: CRMContact | None = None
    reason: str = ""


class ContactDirectory:
    """Existing CRM contacts, loaded from sample exports or a live sync."""

    def __init__(self, contacts: list[CRMContact]):
        self.contacts = contacts

    @classmethod
    def from_sample(cls, path: str | Path) -> "ContactDirectory":
        data = json.loads(Path(path).read_text())
        contacts = []
        for r in data.get("salesforce", {}).get("records", []):
            first, last = n.split_person_name(r.get("FirstName"), r.get("LastName"))
            contacts.append(CRMContact(
                "salesforce", r["Id"], first, last, n.normalize_email(r.get("Email")),
                n.normalize_phone(r.get("Phone")), n.clean_company((r.get("Account") or {}).get("Name")),
                (r.get("Owner") or {}).get("Email")))
        for r in data.get("hubspot", {}).get("results", []):
            p = r.get("properties", {})
            first, last = n.split_person_name(p.get("firstname"), p.get("lastname"))
            contacts.append(CRMContact(
                "hubspot", r["id"], first, last, n.normalize_email(p.get("email")),
                n.normalize_phone(p.get("phone")), n.clean_company(p.get("company")),
                p.get("hubspot_owner_email")))
        return cls(contacts)

    def check(self, info: ContactInfo) -> DuplicateCheck:
        last = n.name_key(None, info.last_name)
        for c in self.contacts:
            if info.email and c.email == info.email:
                return DuplicateCheck("exact", c, "same email")
        for c in self.contacts:
            if info.phone and c.phone == info.phone and last and n.name_key(None, c.last_name) == last:
                return DuplicateCheck("exact", c, "same phone and last name")
        for c in self.contacts:
            same_name = last and n.name_key(c.first_name, c.last_name) == n.name_key(info.first_name, info.last_name)
            same_company = (n.company_key(c.company) and
                            n.company_key(c.company) == n.company_key(info.company))
            if same_name and same_company:
                return DuplicateCheck("possible", c, "same name and company, different contact details")
        return DuplicateCheck("none")


def _summary(sig: Signals) -> str:
    parts = []
    if sig.property_type:
        parts.append(sig.property_type)
    if sig.units:
        parts.append(f"{sig.units} units")
    if sig.loan_amount:
        parts.append(f"${sig.loan_amount / 1e6:.1f}M requested" if sig.loan_amount >= 1e6
                     else f"${sig.loan_amount / 1e3:.0f}K requested")
    if sig.state:
        parts.append(sig.state)
    if sig.timing:
        parts.append(f"timing: {sig.timing}")
    return ", ".join(parts) or "details not given"


def draft_records(system: str, info: ContactInfo, sig: Signals, dup: DuplicateCheck,
                  team_name: str, team_owner: str, transcript: list[dict]) -> dict:
    """Build the API payload(s) for one handoff, in the target CRM's format."""
    description = (f"Website concierge handoff to {team_name}. Deal: {_summary(sig)}.\n\n"
                   + "\n".join(f"{t['role']}: {t['text']}" for t in transcript))
    if dup.status == "exact":
        c = dup.contact
        owner = c.owner_email or team_owner
        if c.system == "salesforce":
            return {"action": "create_task_on_existing_contact", "system": "salesforce",
                    "endpoint": "/services/data/v61.0/sobjects/Task",
                    "payload": {"WhoId": c.id, "Subject": "Website concierge: follow up",
                                "Priority": "High", "Status": "Not Started",
                                "Description": description},
                    "assign_to": owner}
        return {"action": "create_note_on_existing_contact", "system": "hubspot",
                "endpoint": "/crm/v3/objects/notes",
                "payload": {"properties": {"hs_note_body": description,
                                           "hs_timestamp": datetime.now(timezone.utc).isoformat()},
                            "associations": [{"to": {"id": c.id}, "types": [
                                {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}]},
                "assign_to": owner}

    if system == "salesforce":
        payload = {"FirstName": info.first_name, "LastName": info.last_name or "(unknown)",
                   "Email": info.email, "Phone": info.phone,
                   "Company": info.company or "(not provided)", "LeadSource": "Website Concierge",
                   "Description": description}
        record = {"action": "create_lead", "system": "salesforce",
                  "endpoint": "/services/data/v61.0/sobjects/Lead",
                  "payload": {k: v for k, v in payload.items() if v is not None},
                  "assign_to": team_owner}
    else:
        props = {"firstname": info.first_name, "lastname": info.last_name, "email": info.email,
                 "phone": info.phone, "company": info.company,
                 "lifecyclestage": "lead", "hs_lead_status": "NEW"}
        record = {"action": "create_contact", "system": "hubspot",
                  "endpoint": "/crm/v3/objects/contacts",
                  "payload": {"properties": {k: v for k, v in props.items() if v is not None}},
                  "note": description, "assign_to": team_owner}
    if dup.status == "possible":
        record["review_flag"] = (f"Possible duplicate of {dup.contact.system} record "
                                 f"{dup.contact.id} ({dup.reason}). Consider merging instead.")
    return record


@dataclass
class QueueItem:
    id: str
    created_at: str
    status: str                  # pending | approved (dry run or failed send) | rejected | sent
    team: str
    duplicate_check: dict
    record: dict
    signals: dict
    contact: dict
    decided_by: str | None = None
    decided_at: str | None = None
    send_result: dict = field(default_factory=dict)


class ApprovalQueue:
    """A simple JSON-file queue. A production version would use a database."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]")

    def _load(self) -> list[QueueItem]:
        return [QueueItem(**i) for i in json.loads(self.path.read_text())]

    def _save(self, items: list[QueueItem]) -> None:
        self.path.write_text(json.dumps([asdict(i) for i in items], indent=2))

    def add(self, team: str, dup: DuplicateCheck, record: dict, sig: Signals,
            info: ContactInfo) -> QueueItem:
        item = QueueItem(
            id="Q-" + uuid.uuid4().hex[:8].upper(),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            status="pending", team=team,
            duplicate_check={"status": dup.status, "reason": dup.reason,
                             "match": asdict(dup.contact) if dup.contact else None},
            record=record, signals=sig.as_dict(), contact=info.as_dict())
        items = self._load()
        items.append(item)
        self._save(items)
        return item

    def list(self, status: str | None = None) -> list[QueueItem]:
        return [i for i in self._load() if status is None or i.status == status]

    def decide(self, item_id: str, approve: bool, reviewer: str,
               sender: "CRMSender | None" = None) -> QueueItem:
        items = self._load()
        item = next((i for i in items if i.id == item_id), None)
        if item is None:
            raise KeyError(item_id)
        if item.status != "pending":
            raise ValueError(f"{item_id} is already {item.status}")
        item.decided_by = reviewer
        item.decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not approve:
            item.status = "rejected"
        else:
            item.status = "approved"
            if sender is not None:
                item.send_result = sender.send(item.record)
                ok, dry = item.send_result.get("ok"), item.send_result.get("dry_run")
                item.status = "sent" if ok and not dry else "approved"
        self._save(items)
        return item


class CRMSender:
    """Sends an approved record. Dry-run unless credentials are set."""

    def __init__(self, dry_run: bool | None = None):
        self.sf_url = (os.getenv("SF_INSTANCE_URL") or "").rstrip("/")
        self.sf_token = os.getenv("SF_ACCESS_TOKEN")
        self.hs_token = os.getenv("HUBSPOT_TOKEN")
        self.dry_run = dry_run if dry_run is not None else not (self.sf_token or self.hs_token)

    def send(self, record: dict) -> dict:
        if self.dry_run:
            return {"ok": True, "dry_run": True, "would_post": record["endpoint"]}
        if record["system"] == "salesforce":
            url, token = self.sf_url + record["endpoint"], self.sf_token
        else:
            url, token = "https://api.hubapi.com" + record["endpoint"], self.hs_token
        if not token:
            return {"ok": False, "error": f"no credentials for {record['system']}"}
        request = urllib.request.Request(
            url, data=json.dumps(record["payload"]).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return {"ok": True, "status": response.status,
                        "body": json.loads(response.read() or b"{}")}
        except Exception as exc:  # surface the error to the reviewer, never retry silently
            return {"ok": False, "error": str(exc)}
