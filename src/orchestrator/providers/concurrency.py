"""Process-scoped admission control for shared agent provider capacity.

Concurrent workflow executions share real, finite resources: local-model
providers share one GPU's VRAM and each hosted provider has its own quota
pool. The orchestrator therefore bounds how many agent calls one *named*
provider instance may run at the same time. The name is the resource identity:
one configured provider instance is expected to mean one real connection.

Slots are acquired narrowly — only while ``AgentProvider.execute`` runs — and
always released afterwards, so a workflow never reserves a provider for its
whole duration.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from orchestrator.models.agent import AgentRequest, AgentResult, AgentStartedCallback
from orchestrator.providers.agent_provider import AgentProvider

DEFAULT_PROVIDER_CONCURRENCY = 1


class ProviderSlotCancelled(RuntimeError):
    """Raised when queued work is cancelled before it is admitted to a slot."""


@dataclass(eq=False, slots=True)
class _Ticket:
    """One queued or admitted request for a named provider's capacity."""

    provider: str
    execution_id: str | None
    queued: bool = True
    cancelled: bool = False


class ProviderConcurrencyCoordinator:
    """Admit provider calls in FIFO order without exceeding each ceiling.

    Ceilings are fixed for the lifetime of the process: changing a provider's
    ``max_concurrency`` requires a restart, which avoids having to decide what
    happens to slots already held under a previous limit.
    """

    def __init__(
        self,
        limits: Mapping[str, int] | None = None,
        *,
        default_limit: int = DEFAULT_PROVIDER_CONCURRENCY,
    ) -> None:
        if default_limit < 1:
            raise ValueError("Default provider concurrency must be at least 1")
        self._default_limit = default_limit
        self._condition = threading.Condition()
        self._limits: dict[str, int] = {}
        self._active: dict[str, int] = {}
        self._queues: dict[str, deque[_Ticket]] = {}
        self._execution_tickets: dict[str, set[_Ticket]] = {}
        for name, limit in (limits or {}).items():
            self.register(name, limit)

    def register(self, provider: str, limit: int) -> int:
        """Fix a provider's ceiling the first time it is seen.

        Later values are ignored on purpose; the limiter is restart-only.
        """

        with self._condition:
            existing = self._limits.get(provider)
            if existing is not None:
                return existing
            resolved = max(1, int(limit))
            self._limits[provider] = resolved
            return resolved

    def limit_for(self, provider: str) -> int:
        with self._condition:
            return self._limits.get(provider, self._default_limit)

    def total_capacity(self) -> int:
        """Return the summed known ceilings, used only to size worker pools."""

        with self._condition:
            return max(self._default_limit, sum(self._limits.values()))

    def active_count(self, provider: str) -> int:
        with self._condition:
            return self._active.get(provider, 0)

    def queued_count(self, provider: str) -> int:
        with self._condition:
            return len(self._queues.get(provider, ()))

    def cancel(self, execution_id: str) -> bool:
        """Wake any of an execution's queued requests so they can abort.

        Requests already holding a slot are untouched: the workflow's normal
        cooperative cancellation check stops it at the next boundary.
        """

        with self._condition:
            tickets = self._execution_tickets.get(execution_id)
            if not tickets:
                return False
            woken = False
            for ticket in tickets:
                if ticket.queued and not ticket.cancelled:
                    ticket.cancelled = True
                    woken = True
            if woken:
                self._condition.notify_all()
            return woken

    @contextmanager
    def slot(
        self,
        provider: str,
        *,
        execution_id: str | None = None,
        limit: int | None = None,
        on_wait: Callable[[], None] | None = None,
        on_admitted: Callable[[], None] | None = None,
    ) -> Iterator[None]:
        """Hold one of ``provider``'s slots for the duration of the block."""

        if limit is not None:
            self.register(provider, limit)
        ticket = _Ticket(provider=provider, execution_id=execution_id)
        with self._condition:
            self._queues.setdefault(provider, deque()).append(ticket)
            if execution_id is not None:
                self._execution_tickets.setdefault(execution_id, set()).add(ticket)
            immediate = self._admit_locked(ticket)
        if not immediate:
            if on_wait is not None:
                on_wait()
            self._await_admission(ticket)
            if on_admitted is not None:
                on_admitted()
        try:
            yield
        finally:
            self._release(ticket)

    def _await_admission(self, ticket: _Ticket) -> None:
        with self._condition:
            while True:
                if ticket.cancelled:
                    self._abandon_locked(ticket)
                    raise ProviderSlotCancelled(
                        f"Cancelled while waiting for {ticket.provider!r} capacity"
                    )
                if self._admit_locked(ticket):
                    return
                self._condition.wait()

    def _admit_locked(self, ticket: _Ticket) -> bool:
        """Admit ``ticket`` only when it is the head of its provider queue.

        Explicit head-of-line admission keeps the queue FIFO; a plain semaphore
        would let a late arrival overtake work that has already been waiting.
        """

        queue = self._queues.get(ticket.provider)
        if queue is None or not queue or queue[0] is not ticket:
            return False
        limit = self._limits.get(ticket.provider, self._default_limit)
        if self._active.get(ticket.provider, 0) >= limit:
            return False
        queue.popleft()
        ticket.queued = False
        self._active[ticket.provider] = self._active.get(ticket.provider, 0) + 1
        # A remaining slot may now belong to the next waiter in line.
        self._condition.notify_all()
        return True

    def _abandon_locked(self, ticket: _Ticket) -> None:
        queue = self._queues.get(ticket.provider)
        if queue is not None:
            try:
                queue.remove(ticket)
            except ValueError:
                pass
        ticket.queued = False
        self._forget_locked(ticket)
        self._condition.notify_all()

    def _release(self, ticket: _Ticket) -> None:
        with self._condition:
            if not ticket.queued:
                remaining = self._active.get(ticket.provider, 0) - 1
                self._active[ticket.provider] = max(0, remaining)
            self._forget_locked(ticket)
            self._condition.notify_all()

    def _forget_locked(self, ticket: _Ticket) -> None:
        if ticket.execution_id is None:
            return
        tickets = self._execution_tickets.get(ticket.execution_id)
        if tickets is None:
            return
        tickets.discard(ticket)
        if not tickets:
            self._execution_tickets.pop(ticket.execution_id, None)


class ConcurrencyLimitedAgentProvider:
    """Hold a provider's concurrency slot only while one agent call runs."""

    def __init__(
        self,
        provider: AgentProvider,
        *,
        provider_name: str,
        coordinator: ProviderConcurrencyCoordinator,
        limit: int | None = None,
        execution_id: str | None = None,
        on_wait: Callable[[str], None] | None = None,
        on_admitted: Callable[[str], None] | None = None,
        cancellation_check: Callable[[], None] | None = None,
    ) -> None:
        self._provider = provider
        self._provider_name = provider_name
        self._coordinator = coordinator
        self._limit = limit
        self._execution_id = execution_id
        self._on_wait = on_wait
        self._on_admitted = on_admitted
        self._cancellation_check = cancellation_check

    @property
    def provider_name(self) -> str:
        """The configured provider identity whose capacity this call consumes."""

        return self._provider_name

    def execute(
        self,
        request: AgentRequest,
        *,
        on_started: AgentStartedCallback | None = None,
    ) -> AgentResult:
        self._check_cancellation()
        try:
            with self._coordinator.slot(
                self._provider_name,
                execution_id=self._execution_id,
                limit=self._limit,
                on_wait=self._notify_wait,
                on_admitted=self._notify_admitted,
            ):
                self._check_cancellation()
                return self._provider.execute(request, on_started=on_started)
        except ProviderSlotCancelled:
            # The persisted cancellation request is authoritative; let the
            # workflow raise its own cancellation error from the same check.
            self._check_cancellation()
            raise

    def _check_cancellation(self) -> None:
        if self._cancellation_check is not None:
            self._cancellation_check()

    def _notify_wait(self) -> None:
        if self._on_wait is not None:
            self._on_wait(self._provider_name)

    def _notify_admitted(self) -> None:
        if self._on_admitted is not None:
            self._on_admitted(self._provider_name)
