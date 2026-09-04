from typing import Protocol

from orchestrator.models.agent import AgentRequest, AgentResult, AgentStartedCallback


class AgentNeedsHumanError(RuntimeError):
    def __init__(self, message: str, external_id: str | None = None) -> None:
        super().__init__(message)
        self.external_id = external_id


class AgentProviderError(RuntimeError):
    """A provider failed without exposing provider-specific SDK exceptions."""


class AgentProvider(Protocol):
    """Provider-neutral boundary for one agent execution."""

    def execute(
        self,
        request: AgentRequest,
        *,
        on_started: AgentStartedCallback | None = None,
    ) -> AgentResult: ...
