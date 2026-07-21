"""Provider adapter interface (ADR-0002).

Role: the one interface every provider module in this package implements,
plus the registry of which concrete class implements it for which provider.
Called by: app/api/v1/ (never anything above services/, transitively).
Calls: nothing beyond the four adapter modules themselves.
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
Gotcha: ADAPTER_CLASSES lives here, not duplicated once in app/services/chat.py
and again in app/core/llm/registry.py (both need "provider name -> concrete
adapter class") — this module already is the one place that knows every
ProviderAdapter implementation that exists, since they all implement the
Protocol declared here. One definition, not two that can silently drift out
of sync when a fifth adapter is added.
See: docs/DECISIONS/0002 Provider Abstraction.md
"""

from collections.abc import AsyncGenerator, Callable
from typing import Protocol

from app.core.llm.anthropic_adapter import AnthropicAdapter
from app.core.llm.catalog import Provider
from app.core.llm.groq_adapter import GroqAdapter
from app.core.llm.openai_adapter import OpenAIAdapter
from app.core.llm.together_adapter import TogetherAdapter
from app.core.llm.types import LLMEvent, LLMRequest, ProviderModel


class ProviderAdapter(Protocol):
    """Translates a normalized request into a normalized event stream, and
    reports what models this provider currently offers.

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

    async def list_models(self) -> list[ProviderModel]:
        """Fetch this provider's current model list, live, from its own API.

        Returns:
            Every model the provider's API reports right now — not
            filtered or curated here; app.core.llm.registry.py does that
            merge against the curated catalog.

        Raises:
            app.core.errors.DomainError: same contract as stream(). The
                caller (registry.py's refresh_if_stale()) treats any raised
                error as "this provider's data is stale, not fatal" — never
                let this propagate into a 500.
        """
        ...


ADAPTER_CLASSES: dict[Provider, Callable[[str], ProviderAdapter]] = {
    "anthropic": AnthropicAdapter,
    "openai": OpenAIAdapter,
    "groq": GroqAdapter,
    "together": TogetherAdapter,
}
