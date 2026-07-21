# core/llm/

Provider adapters and the normalized event stream (ADR-0002). `types.py`
(normalized request/event shapes), `adapter.py` (the `ProviderAdapter`
interface + `ADAPTER_CLASSES`), `catalog.yaml` + `catalog.py` (the curated
enrichment table), `registry.py` (the live model registry, §4),
`pricing.py` (cost math), and four real adapters — `anthropic_adapter.py`,
`openai_adapter.py`, `groq_adapter.py`, `together_adapter.py` — exist and
are wired into `app/services/chat.py`'s adapter dispatch. `gemini_adapter.py`
doesn't exist yet.

**2026-07-21 update:** the model list used to be exactly `catalog.yaml`
(then named `registry.yaml`), static, never fetched from providers at
runtime. It's now a hybrid — see `registry.py`'s own module docstring and
`docs/DECISIONS/0002 Provider Abstraction.md` decision 5's appended update
before touching either file.

## What lives here

- One adapter module per provider, named `<provider>_adapter.py` (not just
  `<provider>.py` — avoids any ambiguity with importing that provider's own
  SDK/package name from inside its own adapter module), each implementing
  `adapter.py`'s `ProviderAdapter` — both `stream()` (translating its
  provider's wire format into the normalized SSE event vocabulary in
  `docs/API_CONTRACT.md` §5.5) and `list_models()` (that provider's own
  live model list, normalized to `types.ProviderModel`).
- `types.py` — `LLMRequest`/`LLMEvent`, the shape every adapter translates
  into and out of. Reuses `app.schemas.content_block.ContentBlock` and
  `app.schemas.message.StopReason`; does **not** reuse `Usage` (see
  `LLMUsage` there, and ADR-0002 for why). `ProviderModel` (added
  2026-07-21) deliberately carries no pricing field — see its own
  docstring for why live-reported pricing must never reach billing code.
- `adapter.py` — the `ProviderAdapter` Protocol (`stream()` + `list_models()`)
  and `ADAPTER_CLASSES`, the one `provider -> concrete class` dict every
  other module that needs to construct an adapter (`app/services/chat.py`,
  `registry.py`) imports from here rather than each keeping its own copy.
- `catalog.yaml` — the curated, static enrichment table (§4). Loaded and
  validated at import time (`catalog.py`), still crash-loud on a malformed
  row — a checked-in file is an operator mistake, unrelated to any
  provider's live reachability. Add a provider's models here only alongside
  that provider's adapter — data for a provider with no adapter to serve it
  is untestable. This is *not* the full list of models the API can serve
  (that's `registry.py`'s job) — it only supplies capabilities/pricing for
  models verified by hand.
- `ModelRegistry` (`registry.py`) — resolves `provider:model` strings to a
  `ModelEntry`, merging `catalog.py`'s curated data with each configured
  provider's live model list (`refresh_if_stale()`, TTL-cached,
  single-flighted, best-effort per provider). `resolve()`/`is_available()`
  stay synchronous and network-free, reading only the in-memory cache —
  critical, since `app/services/chat.py` calls them on every chat message.
  `ModelEntry.capabilities`/`.pricing` are `None` for a model discovered
  live but absent from the catalog.
- `pricing.py` — `compute_cost_usd(usage, pricing) -> str`, pure Decimal
  arithmetic over this package's own types (`LLMUsage`, `catalog.Pricing`).
  Callers must guard `pricing is not None` themselves (see
  `app/services/chat.py`'s `_usage_dict()`) — this function still requires
  real `Pricing`, it doesn't itself decide what an absent price means.
  **Correction to an earlier version of this file:** this used to say cost
  computation "must never live here." The rule was really narrower than
  that wording — an *adapter* must never compute its own cost (it has no
  business reaching into catalog pricing; that's why `MessageDelta.usage`
  is `LLMUsage`, raw counts only, not the wire `Usage` with `cost_usd`
  baked in — see ADR-0002). A pure function operating only on this
  package's own types is different: putting it here once means the chat
  service reuses the identical formula instead of a second copy drifting
  into existence. `ARCHITECTURE.md`'s "the service computes cost" describes
  *who calls it and when* (a step in the request lifecycle), not literally
  which file the arithmetic must be typed into.

## What must never live here

- Anything about conversations, users, or persistence — this package takes a
  normalized request and yields normalized events, nothing else.
- A runtime capability probe. Capabilities come only from `catalog.yaml` —
  a model with no catalog match has *unknown* capabilities (`None`), never
  probed or guessed at.
- An *adapter* computing its own cost, or an adapter's `list_models()`
  smuggling provider-reported pricing into something that looks
  authoritative — see `pricing.py` and `types.ProviderModel` above.

## How to add a new provider

1. Add the provider's models to `catalog.yaml` with pricing and capabilities.
2. Add `<provider>_adapter.py` implementing `adapter.py`'s `ProviderAdapter`
   — both `stream()` and `list_models()`.
3. Add one line to `adapter.py`'s `ADAPTER_CLASSES` dict, mapping the
   provider name to the new adapter class — the only place a new adapter
   needs to be wired in for `chat.py` and `registry.py` to both be able to
   use it. Every adapter's `__init__` takes exactly `(api_key: str)`; both
   callers rely on that shape holding for whatever you add here too.
4. Never import a provider SDK anywhere outside this package — and per
   ADR-0002, this package itself uses `httpx` directly, not a per-provider
   SDK, so "never import a provider SDK" is true throughout the whole repo,
   not just outside `core/llm/`.
5. See `docs/DECISIONS/0002 Provider Abstraction.md` before deviating from
   any pattern `anthropic_adapter.py` established — most of its decisions
   (single `stream()` method, raising `DomainError` directly, dropped-param
   handling) are meant to generalize to every future adapter.
