"""Маршруты сообщений открывают запись и чтение полной истории сессии."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from local_agent.api.routes.sessions import get_session_service
from local_agent.memory.conversation import ConversationService
from local_agent.memory.models import Message
from local_agent.sessions.service import SessionService

router = APIRouter(prefix="/sessions/{session_id}/messages", tags=["messages"])


class MessageCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    content: str = Field(min_length=1)


def get_conversation_service(request: Request) -> ConversationService:
    return request.app.state.conversation_service


def require_session(session_id: str, service: SessionService) -> None:
    if service.get(session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")


@router.get("", response_model=list[Message])
def list_messages(
    session_id: str,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    conversation: Annotated[ConversationService, Depends(get_conversation_service)],
) -> list[Message]:
    require_session(session_id, sessions)
    return conversation.list(session_id)


@router.post("", response_model=Message, status_code=status.HTTP_201_CREATED)
def create_message(
    session_id: str,
    payload: MessageCreate,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    conversation: Annotated[ConversationService, Depends(get_conversation_service)],
) -> Message:
    require_session(session_id, sessions)
    message = conversation.add_user_message(session_id, payload.content)
    sessions.name_from_first_message(
        session_id, payload.content, conversation.count(session_id)
    )
    return message
