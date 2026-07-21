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
Gotcha: the login/signup screen below stores the JWT in
`st.session_state.access_token` — server-side memory tied to one browser
tab's connection. Closing the tab or restarting this process logs everyone
out; there is no persistent "remember me" cookie. See API_CONTRACT §1.
See: docs/API_CONTRACT.md
"""

import api_client
import streamlit as st

st.set_page_config(page_title="AgentOS", page_icon="💬")

if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = None
if "access_token" not in st.session_state:
    st.session_state.access_token = None
if "email" not in st.session_state:
    st.session_state.email = None

# WHY 8, matching backend/app/core/auth/manager.py's _MIN_PASSWORD_LENGTH:
# a duplicated constant, not a shared import — frontend/ and backend/ are
# separate apps talking over HTTP, nothing here can import backend code
# (ARCHITECTURE.md). Catching a too-short password client-side just saves a
# round trip; the backend's own check is still the real enforcement.
_MIN_PASSWORD_LENGTH = 8


def _render_auth_screen() -> None:
    """Login/signup gate, shown instead of the chat UI until a token exists."""
    st.title("AgentOS")
    login_tab, signup_tab = st.tabs(["Log in", "Sign up"])

    with login_tab, st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Log in", use_container_width=True):
            try:
                token = api_client.login(email, password)["access_token"]
            except api_client.ApiError as exc:
                st.error(str(exc))
            else:
                st.session_state.access_token = token
                st.session_state.email = email
                st.rerun()

    with signup_tab, st.form("signup_form"):
        email = st.text_input("Email", key="signup_email")
        password = st.text_input("Password", type="password", key="signup_password")
        confirm = st.text_input("Confirm password", type="password", key="signup_confirm")
        if st.form_submit_button("Sign up", use_container_width=True):
            if password != confirm:
                st.error("Passwords don't match.")
            elif len(password) < _MIN_PASSWORD_LENGTH:
                st.error(f"Password must be at least {_MIN_PASSWORD_LENGTH} characters.")
            else:
                try:
                    api_client.register(email, password)
                    # WHY log in right after registering, not asking the
                    # user to do it themselves: register() returns the new
                    # user's profile, not a token (API_CONTRACT §1.1) — a
                    # second call is the only way to get one, and doing it
                    # here means "sign up" actually lands the user in the
                    # app instead of back at a login form they just filled
                    # the same credentials into.
                    token = api_client.login(email, password)["access_token"]
                except api_client.ApiError as exc:
                    st.error(str(exc))
                else:
                    st.session_state.access_token = token
                    st.session_state.email = email
                    st.rerun()


if st.session_state.access_token is None:
    _render_auth_screen()
    st.stop()

token = st.session_state.access_token


def _display_title(conversation: dict) -> str:
    return conversation["title"] or "New conversation"


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


with st.sidebar:
    st.title("AgentOS")
    st.caption(st.session_state.email)
    if st.button("Log out", use_container_width=True):
        # WHY conversation_id is cleared too, not just access_token/email:
        # a second account logging in on this same browser tab would
        # otherwise start with a stale conversation id it doesn't own —
        # the API's ownership check would just 404 it (never leaking
        # whose it was), but it's a confusing dead end rather than the
        # clean "no conversation selected" state a fresh login should show.
        st.session_state.access_token = None
        st.session_state.email = None
        st.session_state.conversation_id = None
        st.rerun()

    if st.button("+ New conversation", use_container_width=True):
        created = api_client.create_conversation(token)
        st.session_state.conversation_id = created["id"]
        st.rerun()

    st.divider()

    try:
        # WHY no pagination UI: MVP scope per ROADMAP.md's frontend bullet
        # ("conversation list, streaming render, title placeholder
        # handling") — the default page (20, newest first) is enough to
        # exercise the backend end-to-end, which is this slice's actual goal.
        conversations = api_client.list_conversations(token)["data"]
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
                api_client.delete_conversation(token, conversation["id"])
                if is_selected:
                    st.session_state.conversation_id = None
                st.rerun()


if st.session_state.conversation_id is None:
    st.info("Select a conversation, or start a new one, from the sidebar.")
    st.stop()

conversation_id = st.session_state.conversation_id

try:
    messages = api_client.list_messages(token, conversation_id)["data"]
except api_client.ApiError as exc:
    if exc.status_code == 404:
        # WHY reset instead of just showing the error: the selected
        # conversation was deleted (by this client or another) since it was
        # last listed — falling back to the empty state is more useful than
        # a dead-end error screen for a conversation that no longer exists.
        st.session_state.conversation_id = None
        st.rerun()
    st.error(f"Couldn't load messages: {exc}")
    st.stop()

for message in messages:
    with st.chat_message(message["role"]):
        _render_content(message["content"])
        if message["status"] == "failed":
            st.error("This turn failed.")
        elif message["status"] == "incomplete":
            st.caption("⚠️ interrupted")

prompt = st.chat_input("Message AgentOS…")
if prompt:
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        # WHY a one-element list, not a plain bool + nonlocal: this whole
        # block is script-level code (inside `if prompt:`, not inside a
        # `def`), so there is no enclosing *function* scope for `nonlocal`
        # to bind to — `nonlocal had_error` fails to even compile here
        # (SyntaxError, caught by py_compile before this ever reached a
        # browser). Mutating a container sidesteps needing nonlocal/global
        # at all.
        #
        # WHY track this at all: an in-stream `error` SSE event (§5.5 — the
        # turn started, then failed mid-generation) does NOT raise;
        # stream_chat_message() only raises for a *pre-stream* failure
        # (§5.5: "errors before the first byte use the normal error
        # envelope"). Both cases need to suppress the rerun below, not just
        # the one that happens to raise.
        had_error = [False]

        def _text_chunks():
            for event in api_client.stream_chat_message(token, conversation_id, prompt):
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
        # real browser session, not the AppTest verification script, which
        # doesn't chase st.rerun() the same way) — rerunning right after
        # st.error() immediately discards that render and starts a fresh
        # script pass, so the error flashes and vanishes before a real user
        # can read it. Success still reruns: the backend is the source of
        # truth for real message IDs, final content, and
        # conversation.message_count/title.
        st.rerun()
