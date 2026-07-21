# core/llm/

Provider adapters and the normalized event stream (ADR-0002). `types.py`
(normalized request/event shapes), `adapter.py` (the `ProviderAdapter`
interface), `registry.yaml` + `registry.py` (the model registry, §4),
`pricing.py` (cost math), and four real adapters — `anthropic_adapter.py`,
`openai_adapter.py`, `groq_adapter.py`, `together_adapter.py` — exist and
are wired into `app/services/chat.py`'s adapter dispatch. `gemini_adapter.py`
doesn't exist yet.

## What lives here

- One adapter module per provider, named `<provider>_adapter.py` (not just
  `<provider>.py` — avoids any ambiguity with importing that provider's own
  SDK/package name from inside its own adapter module), each implementing
  `adapter.py`'s `ProviderAdapter.stream()` and translating its provider's
  wire format into the normalized SSE event vocabulary in
  `docs/API_CONTRACT.md` §5.5.
- `types.py` — `LLMRequest`/`LLMEvent`, the shape every adapter translates
  into and out of. Reuses `app.schemas.content_block.ContentBlock` and
  `app.schemas.message.StopReason`; does **not** reuse `Usage` (see
  `LLMUsage` there, and ADR-0002 for why).
- `registry.yaml` — the static, declarative model registry (§4). Loaded and
  validated at import time (`registry.py`), never fetched from providers at
  runtime. Add a provider's models here only alongside that provider's
  adapter — data for a provider with no adapter to serve it is untestable.
- `ModelRegistry` (`registry.py`) — resolves `provider:model` strings to a
  `RegistryEntry`; computes `available` from configured API keys.
- `pricing.py` — `compute_cost_usd(usage, pricing) -> str`, pure Decimal
  arithmetic over this package's own types (`LLMUsage`, `registry.Pricing`).
  **Correction to an earlier version of this file:** this used to say cost
  computation "must never live here." The rule was really narrower than
  that wording — an *adapter* must never compute its own cost (it has no
  business reaching into registry pricing; that's why `MessageDelta.usage`
  is `LLMUsage`, raw counts only, not the wire `Usage` with `cost_usd`
  baked in — see ADR-0002). A pure function operating only on this
  package's own types is different: putting it here once means the future
  SSE chat service reuses the identical formula instead of a second copy
  drifting into existence. `ARCHITECTURE.md`'s "the service computes cost"
  describes *who calls it and when* (a step in the request lifecycle), not
  literally which file the arithmetic must be typed into.

## What must never live here

- Anything about conversations, users, or persistence — this package takes a
  normalized request and yields normalized events, nothing else.
- A runtime capability probe. Capabilities come only from `registry.yaml`.
- An *adapter* computing its own cost — see `pricing.py` above for the
  distinction between that and a shared pure function.

## How to add a new one

1. Add the provider's models to `registry.yaml` with pricing and capabilities.
2. Add `<provider>_adapter.py` implementing `adapter.py`'s `ProviderAdapter`.
3. Add one line to `app/services/chat.py`'s `_ADAPTER_CLASSES` dict, mapping
   the provider name to the new adapter class — that's the only place a new
   adapter needs to be wired in for `chat.py` to be able to call it. Every
   adapter's `__init__` takes exactly `(api_key: str)`; `_get_adapter()`
   relies on that shape holding for whatever you add here too.
4. Never import a provider SDK anywhere outside this package — and per
   ADR-0002, this package itself uses `httpx` directly, not a per-provider
   SDK, so "never import a provider SDK" is true throughout the whole repo,
   not just outside `core/llm/`.
5. See `docs/DECISIONS/0002 Provider Abstraction.md` before deviating from
   any pattern `anthropic_adapter.py` established — most of its decisions
   (single `stream()` method, raising `DomainError` directly, dropped-param
   handling) are meant to generalize to every future adapter.
