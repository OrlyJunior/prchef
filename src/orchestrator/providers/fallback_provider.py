"""Single-hop fallback behavior for provider-neutral agent requests."""

from __future__ import annotations

from collections.abc import Callable

from orchestrator.models.agent import AgentRequest, AgentResult, AgentStartedCallback
from orchestrator.providers.agent_provider import AgentProvider, AgentProviderError

FallbackCallback = Callable[[AgentProviderError], None]
FallbackFactory = Callable[[], AgentProvider]


class FallbackAgentProvider:
    """Try a fallback exactly once for explicitly classified capacity failures."""

    def __init__(
        self,
        primary: AgentProvider,
        fallback_factory: FallbackFactory,
        *,
        on_fallback: FallbackCallback,
    ) -> None:
        self._primary = primary
        self._fallback_factory = fallback_factory
        self._fallback: AgentProvider | None = None
        self._on_fallback = on_fallback

    def execute(
        self,
        request: AgentRequest,
        *,
        on_started: AgentStartedCallback | None = None,
    ) -> AgentResult:
        try:
            return self._primary.execute(request, on_started=on_started)
        except AgentProviderError as exc:
            if not exc.is_retryable_quota_failure:
                raise
            self._on_fallback(exc)
        # Deliberately do not catch and route the fallback error. This bounds
        # failover to one hop and preserves the fallback's final failure reason.
        if self._fallback is None:
            self._fallback = self._fallback_factory()
        return self._fallback.execute(request, on_started=on_started)
