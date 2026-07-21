"""Provider adapter interface (ADR-0002).

Role: the one interface every provider module in this package implements.
Called by: app/services/chat.py (once it exists) — never anything above
services/. Calls nothing; this file only declares the contract.
Gotcha: a single stream() method, not stream()+complete() — ADR-0002 decision
3: every provider call is made in streaming mode, even to serve a
non-streaming (Accept: application/json) client request. The caller buffers
a fully-consumed stream into one JSON body when it needs to; adapters never
implement a second request mode.
Gotcha: the return type is `AsyncGenerator`, not the more general
`AsyncIterator` — deliberately, so callers can `.aclose()` it. The SSE chat
path (app/services/chat.py's `emit_stream`) needs to abort an in-flight
provider call on client disconnect, which relies on `.aclose()` propagating
into the adapter's own `async with httpx.AsyncClient(...)` cleanup.
See: docs/DECISIONS/0002 Provider Abstraction.md
"""

from collections.abc import AsyncGenerator
from typing import Protocol

from app.core.llm.types import LLMEvent, LLMRequest


class ProviderAdapter(Protocol):
    """Translates a normalized request into a normalized event stream.

    Implementations own everything provider-specific: authentication,
    request/response translation, and mapping provider errors to
    app.core.errors's taxonomy (§2) — nothing outside this package may see a
    raw provider error code or a provider client's own exception type.
    """

    def stream(self, request: LLMRequest) -> AsyncGenerator[LLMEvent, None]:
        """Call the provider and yield normalized events.

        Args:
            request: the normalized request. `request.model` is the bare
                model name, without a "provider:" prefix — the registry
                strips that before resolving which adapter to call.

        Yields:
            Zero or more content-block start/delta/stop events, in index
            order, followed by exactly one terminal MessageDelta.

        Raises:
            app.core.errors.DomainError: a subclass matching §2's taxonomy —
                never a raw httpx exception or provider error code.
        """
        ...
