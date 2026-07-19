# Docs Policy

Goal: when you sit down to write or read code, the explanation is already there — without
burning tokens regenerating it. Docs are a **byproduct of implementation**, never a
separate pass.

## The one rule

**Write each fact once.** The contract lives in `API_CONTRACT.md`. Layering lives in
`ARCHITECTURE.md`. Trade-offs live in `DECISIONS/`. Code comments explain *why this line*.
If a doc restates any of those, delete it and link instead. Duplication is what makes
documentation expensive to maintain and expensive to generate.

## Four tiers, nothing else

**1. Module header — every file, ≤6 lines.** Written first, before the code. This is the
highest-value doc in the repo because it is what you re-read at 11pm.

```
"""Anthropic streaming adapter.

Role: translate Anthropic SSE into the normalized event stream (ADR-0002).
Called by: services/chat.py via the ProviderRegistry. Calls nothing internal.
Gotcha: usage arrives split across message_start and message_delta — merge both.
See: docs/DECISIONS/0002-provider-abstraction.md
"""
```

Fixed shape: what it is · role · who calls it and what it calls · the one gotcha · link.
No prose paragraphs.

**2. Docstrings — public functions only.** Args, returns, raises, and *one* line of why.
Private helpers get nothing unless the logic is non-obvious. Type hints already document
types; do not repeat them in prose.

**3. Inline comments — only for surprises.** A comment explains why the obvious approach
was wrong. Never what the code does. Prefix the two that matter so they are greppable:

```
# WHY: ordered before validation because the provider mutates the payload.
# GOTCHA: X-Accel-Buffering is required or nginx buffers the whole stream.
```

`grep -rn "GOTCHA:"` is a legitimate onboarding tool.

**4. Package README — one per package, ≤30 lines.** `services/README.md`,
`core/llm/README.md`, etc. Contents: what lives here, what must never live here, the
files in one line each, and how to add a new one. This is what Claude Code reads instead
of grepping ten files, which is where the real token savings come from.

## Docs that are generated, never hand-written

- **API reference** — from FastAPI's OpenAPI output. Never describe an endpoint by hand
  outside `API_CONTRACT.md`.
- **Schema reference** — from Pydantic models.
- **DB schema** — from Alembic history.

If you catch yourself writing a table of fields, stop: it is already generated and will
drift within a week.

## When a decision is a decision

Write an ADR only when a choice is (a) hard to reverse and (b) will look wrong to someone
later. Five sections, under 400 words: context, decision, alternatives rejected,
consequences, status. If it doesn't hurt to change later, it's a comment, not an ADR.

## Token discipline for Claude Code

- Docs are written **in the same pass** as the code, never as a follow-up request.
  A separate "now document this" turn re-reads the whole file and doubles the cost.
- Module header and docstrings are drafted **during** the plan phase, from the plan —
  before the implementation exists. They then guide the code rather than describe it.
- Never regenerate a doc that hasn't changed. Update the specific line.
- Never paste code into a doc. Link to `path:line` instead.
- `CLAUDE.md` stays under ~60 lines. It loads into every session; every line is rent.
- Package READMEs are the cheap read. Point Claude at those before it starts grepping.

## Checklist — part of every PR

- [ ] Every new file has a module header in the fixed shape
- [ ] Public functions have docstrings; private ones don't unless surprising
- [ ] Any non-obvious line carries `WHY:` or `GOTCHA:`
- [ ] Package README updated if a file was added
- [ ] `API_CONTRACT.md` updated if the wire format moved
- [ ] An ADR exists if something irreversible was chosen
- [ ] Nothing restates a fact that already lives elsewhere