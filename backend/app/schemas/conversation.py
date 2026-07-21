"""Conversation schemas (API_CONTRACT.md §3.2).

Role: wire shapes for reading/creating/updating a conversation. Field names
and optionality mirror the contract exactly — don't invent names here.
Called by: app/api/v1/conversations.py (once it exists). Calls nothing internal.
Gotcha: the wire field is `metadata`, but the ORM column attribute is
`metadata_` (see app/models/conversation.py's gotcha) — building this schema
from an ORM row is a field-by-field job for the service layer, not something
`model_validate(obj, from_attributes=True)` can do correctly on its own.
See: docs/API_CONTRACT.md#32-conversation
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    system_prompt: str | None = None
    default_model: str | None = None
    default_params: dict[str, Any] = {}
    metadata: dict[str, Any] = {}


class ConversationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    system_prompt: str | None = None
    default_model: str | None = None
    default_params: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class Conversation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str | None
    system_prompt: str | None
    default_model: str | None
    default_params: dict[str, Any]
    metadata: dict[str, Any]
    message_count: int
    created_at: datetime
    updated_at: datetime
