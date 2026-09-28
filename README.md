# Lender Deal Concierge

A website chat assistant for commercial real estate lenders. It answers visitors only from content the lender has approved, figures out what kind of deal they have, checks the CRM so an existing borrower is never logged as a new lead, and hands the conversation to the right team. Nothing reaches the CRM until a person approves it.

> Portfolio project built on synthetic sample data. "Northgate Street Capital," its loan terms, and all contacts are fictional.

## Why it's built this way

A lender's website chatbot has two ways to fail: saying something the lender never approved (a rate, a promise, an expired promotion), and creating messy CRM data (duplicate leads, existing borrowers treated as strangers). Each design choice below targets one of those.

| Risk | How it's handled |
| --- | --- |
| Making up terms | Answers come only from an approved knowledge base. No confident match means a polite decline and an offer of a person. |
| Stale or unapproved content | The knowledge base is cleaned on load: unapproved entries and entries past their review date are excluded, and duplicate entries are merged (newest approval wins). |
| Rate quotes and promises | Fixed, pre-approved replies for rate requests and "will I be approved?" questions. |
| Prompt injection | Attempts to override instructions get a fixed reply and never reach answer generation. |
| Sensitive data | SSNs, account and card numbers are redacted from the transcript, and the visitor is told not to share them. |
| Duplicate CRM records | Every handoff is checked against existing Salesforce and HubSpot contacts first (see below). |
| Unsupervised writes | Drafts go to an approval queue; a reviewer approves or rejects each one. |
| Optional LLM wording | If Claude is used to phrase answers, any reply containing a number not in the approved answer is discarded and the approved text is used instead. |

## How a conversation flows

Each visitor message runs through a small [LangGraph](https://github.com/langchain-ai/langgraph) state graph:

```
screen ──(blocked)───────────────────────────────────────────► END
  │
understand   deal signals: property type, units, loan size, state, timing,
  │          existing borrower; contact details cleaned (email, E.164 phone, name)
answer       approved answer, or decline
  │
qualify      route to a team; ask for the next missing detail
  ├──(qualified + reachable)──► handoff: CRM duplicate check → draft → approval queue
  │                                │
compose ◄──────────────────────────┘
```

**Routing** comes from `sample_data/routing.json`, so the lender can change it without touching code:
- Existing borrower → Relationship Management
- 1-4 unit investor property → Residential Investment
- Multifamily, $10M and up → Multifamily Origination
- Everything else → General Origination
- Properties outside the lending footprint are told so honestly instead of being handed off.

**CRM duplicate check** (the data-cleaning step before any write):
- **Exact match** (same email, or same phone and last name): no new record. A follow-up Task (Salesforce) or Note (HubSpot) is drafted on the existing contact and assigned to the person who already owns that relationship.
- **Possible match** (same name and company, different contact details): a new lead is drafted but flagged so the reviewer can merge instead.
- **No match:** a new Lead (Salesforce) or Contact (HubSpot) is drafted in that API's real payload format, with a deal summary and the transcript.

## Quick start

```bash
git clone https://github.com/zanwar2003/lender-deal-concierge.git
cd lender-deal-concierge
pip install -r requirements.txt      # LangGraph

python -m concierge demo             # four scripted conversations, then the approval queue
python -m concierge serve            # chat widget at http://127.0.0.1:8765, reviewer page at /review
python -m concierge chat             # talk to it in the terminal
python -m concierge kb-report        # what knowledge-base cleaning kept and dropped
python -m concierge queue            # list drafts waiting for approval
python -m concierge approve Q-XXXXXXXX
```

Example from the demo:

```
Visitor:   We're refinancing a 120-unit apartment building in Brooklyn, the loan matures in 5 months
Assistant: Roughly how large is the loan you're looking for?
Visitor:   Looking for about $14 million. What's your max LTV?
Assistant: Multifamily loans go up to 75% LTV, and 1-4 unit investor loans go up to 80% LTV on
           purchases. [...] If you'd like a loan officer to follow up, what's your name and the best
           email or phone number to reach you?
Visitor:   My name is Dana Okafor with Riverside Capital, dana@riversidecap.example
Assistant: Thanks, Dana. I've passed this conversation to our Multifamily Origination team, and a
           loan officer will reach out shortly.

Visitor:   I already have a loan with you and need a payoff letter
Visitor:   julia.rivera@harborpoint.example
Assistant: Thanks, Julia. I found you in our system, so I've passed this conversation to the person
           who already works with you. They'll follow up shortly.

=== Approval queue
Q-...  pending  Dana Okafor -> Multifamily Origination   create_lead (salesforce), CRM match: none
Q-...  pending  julia...    -> Relationship Management   create_task_on_existing_contact, CRM match: exact
```

## Connecting real systems

- **CRM writes:** approval sends in dry-run mode (it reports what it would POST) unless credentials are set: `SF_INSTANCE_URL` + `SF_ACCESS_TOKEN` for Salesforce, or `HUBSPOT_TOKEN` for HubSpot.
- **Existing contacts:** loaded from `sample_data/crm_contacts.json`, which mirrors the Salesforce query and HubSpot contacts API response shapes. In production this would come from a read-only sync.
- **Claude phrasing (optional):** `ANTHROPIC_API_KEY=... python -m concierge serve --claude`. Claude only rephrases the matched approved answer, and the output is checked before it is shown. Without the flag, approved answers are served word for word.

## Tests

```bash
pip install pytest
python -m pytest
```

35 tests cover knowledge-base cleaning (unapproved, expired, duplicate entries), retrieval, signal and contact extraction, routing rules, CRM duplicate matching and payload shapes, full conversations, every guardrail, the LLM grounding check, and the HTTP API.

## Project layout

```
concierge/
  agent.py       LangGraph conversation graph and optional Claude responder
  knowledge.py   approved-answer knowledge base: cleaning and retrieval
  signals.py     deal signals and contact details from free text
  routing.py     team routing from JSON rules
  crm.py         CRM duplicate check, draft payloads, approval queue, sender
  guardrails.py  input screening, redaction, output grounding check
  normalize.py   shared field cleaning (emails, phones, names, companies)
  server.py      local web server for the widget and reviewer page
web/             widget.html (chat), review.html (approval queue)
sample_data/     approved answers, routing rules, existing CRM contacts
tests/
```

## Limitations

- Retrieval is keyword-based so it stays deterministic and explainable. A larger knowledge base would use embeddings, with the same confidence threshold and decline behavior.
- Signal extraction uses patterns, which can miss unusual phrasing; the visitor is simply asked again.
- The demo server keeps sessions in memory and has no authentication. A real deployment needs auth on the reviewer page, rate limiting, and HTTPS.

## License

MIT
