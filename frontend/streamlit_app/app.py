"""AgentOS chat UI — Streamlit MVP (docs/ROADMAP.md's "Frontend" phase).

Role: disposable chat client (CLAUDE.md: "Streamlit is disposable — Next.js
replaces it"). All backend calls go through api_client.py — this file never
imports backend/ or talks HTTP directly (ARCHITECTURE.md).
Called by: `streamlit run app.py` (docker-compose's `streamlit` service).
Calls: frontend/streamlit_app/api_client.py.
Gotcha: `title: null` is rendered as a client-side-only placeholder
("New conversation") — never sent back to the server, never invented as a
real title. §5.2: "Clients must handle title: null and render a
placeholder; they must not generate titles themselves." Every conversation
here stays untitled forever in practice — titling.py doesn't exist yet
(blocked on a Groq adapter, per ROADMAP.md).
Gotcha (2026-07-21): the model choice is a single session-global "what to
use for the next send" value, not per-conversation — matching how
ChatGPT/Claude's own selector behaves (opening a different existing chat
doesn't change it; only picking a new one does). It's sent as a per-message
override (`ChatRequest.model`, §5.4) on every send — a conversation's own
`default_model` is now just its initial seed at creation time, never
mutated after.
Gotcha (2026-07-21, later same day): both provider and model are chosen
entirely on the Settings page (`st.navigation`/`st.Page`, Streamlit
1.36+), not on Chat at all — an explicit choice, not a Streamlit
limitation: `st.chat_input` only auto-pins to the bottom of the viewport
when it isn't nested inside a layout container like `st.columns`, so
putting a selector "beside" it on the Chat page would have meant the whole
composer stops staying visible on a long conversation. Keeping Chat to
just the conversation (plain, pinned `st.chat_input`, no picker) avoided
that tradeoff entirely rather than accepting it.
See: docs/API_CONTRACT.md
"""

import api_client
import streamlit as st

st.set_page_config(page_title="AgentOS", page_icon="💬")

if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = None
# WHY groq specifically, not the first provider alphabetically or the
# first one returned: the user's own explicit instruction — "Default
# provider is Groq."
if "selected_provider" not in st.session_state:
    st.session_state.selected_provider = "groq"
if "selected_model" not in st.session_state:
    st.session_state.selected_model = None


def _display_title(conversation: dict) -> str:
    return conversation["title"] or "New conversation"


def _model_label(model: dict) -> str:
    if model["available"]:
        return model["display_name"]
    return f"{model['display_name']} — no API key configured"


def _render_content(content: list[dict]) -> None:
    for block in content:
        block_type = block["type"]
        if block_type == "text":
            st.markdown(block["text"])
        elif block_type == "tool_use":
            # WHY just a caption, not a full render: tools.py (§5.6) isn't
            # built yet — nothing today can produce a tool_result for this,
            # so there's nothing more useful to show than that it happened.
            st.caption(f"🔧 called `{block['name']}`")
        elif block_type == "reasoning":
            with st.expander("Reasoning"):
                st.markdown(block["text"])
        # WHY no image/tool_result branch: those only appear in user
        # messages from client-side tool execution or file uploads, and
        # neither tools.py nor files.py exist yet (ROADMAP.md items 6-7).


def _load_models() -> list[dict]:
    try:
        return api_client.list_models()["data"]
    except api_client.ApiError as exc:
        st.error(f"Couldn't load models: {exc}")
        return []


def _sync_selected_model(models: list[dict]) -> None:
    """Keep selected_provider/selected_model valid against the live model
    list — called from both pages, since either can be visited first in a
    session and the live list can shift under a stale choice (a provider
    losing its last model, in theory)."""
    providers = sorted({m["provider"] for m in models})
    if providers and st.session_state.selected_provider not in providers:
        st.session_state.selected_provider = "groq" if "groq" in providers else providers[0]

    # WHY a list, not a set, for provider_models: order matters for the
    # "else" fallback below — catalog-curated entries come back from the
    # registry before live-only ones (see core/llm/registry.py), so
    # provider_models[0] is a meaningful "best default," not an arbitrary
    # pick the way sorting/min() on an unordered set would be.
    provider_models = [m for m in models if m["provider"] == st.session_state.selected_provider]
    provider_model_ids = [m["id"] for m in provider_models]
    if provider_models and st.session_state.selected_model not in provider_model_ids:
        st.session_state.selected_model = (
            api_client.DEFAULT_MODEL
            if api_client.DEFAULT_MODEL in provider_model_ids
            else provider_models[0]["id"]
        )
    elif not provider_models:
        st.session_state.selected_model = None


def _chat_page() -> None:
    models = _load_models()
    models_by_id = {m["id"]: m for m in models}
    _sync_selected_model(models)

    with st.sidebar:
        st.title("AgentOS")

        if st.button(
            "+ New conversation",
            use_container_width=True,
            disabled=st.session_state.selected_model is None,
        ):
            created = api_client.create_conversation(default_model=st.session_state.selected_model)
            st.session_state.conversation_id = created["id"]
            st.rerun()

        st.divider()

        try:
            # WHY no pagination UI: MVP scope per ROADMAP.md's frontend
            # bullet ("conversation list, streaming render, title
            # placeholder handling") — the default page (20, newest first)
            # is enough to exercise the backend end-to-end, which is this
            # slice's actual goal.
            conversations = api_client.list_conversations()["data"]
        except api_client.ApiError as exc:
            st.error(f"Couldn't load conversations: {exc}")
            conversations = []

        if not conversations:
            st.caption("No conversations yet.")

        for conversation in conversations:
            is_selected = conversation["id"] == st.session_state.conversation_id
            col_select, col_delete = st.columns([5, 1])
            with col_select:
                if st.button(
                    _display_title(conversation),
                    key=f"select_{conversation['id']}",
                    use_container_width=True,
                    type="primary" if is_selected else "secondary",
                ):
                    st.session_state.conversation_id = conversation["id"]
                    st.rerun()
            with col_delete:
                if st.button("🗑️", key=f"delete_{conversation['id']}"):
                    api_client.delete_conversation(conversation["id"])
                    if is_selected:
                        st.session_state.conversation_id = None
                    st.rerun()

    if st.session_state.selected_model is None:
        st.warning(
            f"No models available from **{st.session_state.selected_provider}** — "
            "pick a different provider on the Settings page."
        )

    if st.session_state.conversation_id is None:
        st.info("Select a conversation, or start a new one, from the sidebar.")
        return

    conversation_id = st.session_state.conversation_id

    try:
        messages = api_client.list_messages(conversation_id)["data"]
    except api_client.ApiError as exc:
        if exc.status_code == 404:
            # WHY reset instead of just showing the error: the selected
            # conversation was deleted (by this client or another) since it
            # was last listed — falling back to the empty state is more
            # useful than a dead-end error screen for a conversation that
            # no longer exists.
            st.session_state.conversation_id = None
            st.rerun()
        st.error(f"Couldn't load messages: {exc}")
        return

    for message in messages:
        with st.chat_message(message["role"]):
            _render_content(message["content"])
            if message["status"] == "failed":
                st.error("This turn failed.")
            elif message["status"] == "incomplete":
                st.caption("⚠️ interrupted")
            # WHY only for assistant messages: message["model"] is always
            # null on a user message (§3.3) — nothing to show. WHY this
            # stays even though the picker moved to Settings: it's
            # informational (which model actually answered), not a
            # control — doesn't clutter a "focused chat" the way an
            # interactive picker on this page would.
            if message["role"] == "assistant" and message.get("model"):
                model = models_by_id.get(message["model"])
                st.caption(model["display_name"] if model else message["model"])

    prompt = st.chat_input("Message AgentOS…", disabled=st.session_state.selected_model is None)

    if prompt:
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            # WHY a one-element list, not a plain bool + nonlocal: this
            # whole block is script-level code (inside `if prompt:`, not
            # inside a `def` *of its own* — it's still inside _chat_page(),
            # but `nonlocal` would need `_text_chunks` and this code to
            # share a scope one level up from `_text_chunks` itself, which
            # a mutable container sidesteps needing at all).
            #
            # WHY track this at all: an in-stream `error` SSE event (§5.5 —
            # the turn started, then failed mid-generation) does NOT
            # raise; stream_chat_message() only raises for a *pre-stream*
            # failure (§5.5: "errors before the first byte use the normal
            # error envelope"). Both cases need to suppress the rerun
            # below, not just the one that happens to raise.
            had_error = [False]
            selected_model = st.session_state.selected_model

            def _text_chunks():
                for event in api_client.stream_chat_message(
                    conversation_id, prompt, model=selected_model
                ):
                    name, data = event["event"], event["data"]
                    if name == "content_block_delta" and data["delta"]["type"] == "text_delta":
                        yield data["delta"]["text"]
                    elif name == "content_block_start" and data["block"]["type"] == "tool_use":
                        st.caption(f"🔧 called `{data['block']['name']}`")
                    elif name == "error":
                        had_error[0] = True
                        st.error(data["error"]["message"])

            try:
                st.write_stream(_text_chunks())
            except api_client.ApiError as exc:
                had_error[0] = True
                st.error(f"Couldn't send message: {exc}")

        if not had_error[0]:
            # WHY rerun only on success, not unconditionally: found live (a
            # real browser session, not the AppTest verification script,
            # which doesn't chase st.rerun() the same way) — rerunning
            # right after st.error() immediately discards that render and
            # starts a fresh script pass, so the error flashes and
            # vanishes before a real user can read it. Success still
            # reruns: the backend is the source of truth for real message
            # IDs, final content, and conversation.message_count/title.
            st.rerun()


def _settings_page() -> None:
    st.title("Settings")
    st.caption("Preferences for this browser session only — nothing here is saved server-side.")

    models = _load_models()
    providers = sorted({m["provider"] for m in models})
    if not providers:
        st.warning("No models available — check that the backend is reachable.")
        return

    _sync_selected_model(models)

    # WHY distinct widget keys ("_provider_widget"/"_model_widget"), not
    # "selected_provider"/"selected_model" themselves, with a manual
    # sync-back below: those two are also written by plain assignment
    # elsewhere (_sync_selected_model, called from both pages) — mixing a
    # widget-owned key with external plain-assignment writes to that same
    # key is what caused a real, observed bug here (the provider selectbox
    # silently defaulted to index 0 instead of the pre-set session_state
    # value; see BUILD_LOG). Keeping each widget's own key entirely
    # separate, with an explicit `index=`, sidesteps it regardless of root
    # cause.
    chosen_provider = st.selectbox(
        "Default provider",
        options=providers,
        index=providers.index(st.session_state.selected_provider),
        key="_provider_widget",
        help="Only this provider's models are usable on the Chat page.",
    )
    if chosen_provider != st.session_state.selected_provider:
        st.session_state.selected_provider = chosen_provider
        _sync_selected_model(models)  # picks a default model for the new provider immediately

    provider_models = [m for m in models if m["provider"] == st.session_state.selected_provider]
    if provider_models:
        provider_model_ids = [m["id"] for m in provider_models]
        models_by_id = {m["id"]: m for m in provider_models}
        chosen_model = st.selectbox(
            "Model",
            options=provider_model_ids,
            index=provider_model_ids.index(st.session_state.selected_model),
            format_func=lambda mid: _model_label(models_by_id[mid]),
            key="_model_widget",
        )
        st.session_state.selected_model = chosen_model
    else:
        st.caption(f"No models available from {st.session_state.selected_provider}.")


pg = st.navigation(
    [
        st.Page(_chat_page, title="Chat", icon="💬", default=True),
        st.Page(_settings_page, title="Settings", icon="⚙️"),
    ]
)
pg.run()
