from typing import Protocol

from orchestrator.models.agent import AgentRequest, AgentResult, AgentStartedCallback


class AgentNeedsHumanError(RuntimeError):
    def __init__(self, message: str, external_id: str | None = None) -> None:
        super().__init__(message)
        self.external_id = external_id


class AgentProviderError(RuntimeError):
    """A provider failed without exposing provider-specific SDK exceptions."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "unknown",
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retry_after_seconds = retry_after_seconds

    @property
    def is_retryable_quota_failure(self) -> bool:
        """Whether a configured one-hop preset fallback may handle this failure."""

        return self.category in {"quota", "rate_limit", "capacity"}


class AgentProvider(Protocol):
    """Provider-neutral boundary for one agent execution."""

    def execute(
        self,
        request: AgentRequest,
        *,
        on_started: AgentStartedCallback | None = None,
    ) -> AgentResult: ...
