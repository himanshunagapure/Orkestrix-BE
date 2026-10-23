"""Context variables for correlating nested spans within a workflow run."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from workflow_observability.session import ObservabilitySession

_session: ContextVar[Optional["ObservabilitySession"]] = ContextVar("workflow_obs_session", default=None)
_node_span_id: ContextVar[Optional[str]] = ContextVar("workflow_obs_node_span", default=None)
_tool_span_id: ContextVar[Optional[str]] = ContextVar("workflow_obs_tool_span", default=None)


def get_session() -> Optional["ObservabilitySession"]:
    return _session.get()


def set_session(session: "ObservabilitySession") -> Token:
    return _session.set(session)


def reset_session(token: Token) -> None:
    _session.reset(token)


def get_node_span_id() -> Optional[str]:
    return _node_span_id.get()


def set_node_span_id(span_id: Optional[str]) -> Token:
    return _node_span_id.set(span_id)


def reset_node_span_id(token: Token) -> None:
    _node_span_id.reset(token)


def get_tool_span_id() -> Optional[str]:
    return _tool_span_id.get()


def set_tool_span_id(span_id: Optional[str]) -> Token:
    return _tool_span_id.set(span_id)


def reset_tool_span_id(token: Token) -> None:
    _tool_span_id.reset(token)
