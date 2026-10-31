"""An independent heartbeat for one execution-producing application instance."""

from __future__ import annotations

import logging
import threading
import uuid

from prchef.models.execution_owner import HEARTBEAT_INTERVAL_SECONDS
from prchef.repositories.workflow_repository import WorkflowRepository

logger = logging.getLogger(__name__)


class ExecutionOwnerHeartbeat:
    def __init__(self, repository: WorkflowRepository) -> None:
        self._repository = repository
        self.owner_id = str(uuid.uuid4())
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._registered = False
        self._closed = False

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Execution owner has closed")
            if self._thread is not None:
                return
            # Failure here prevents all execution admission by the caller.
            self._repository.register_owner(self.owner_id)
            self._registered = True
            self._thread = threading.Thread(
                target=self._refresh, name="prchef-owner-heartbeat", daemon=True,
            )
            self._thread.start()

    def _refresh(self) -> None:
        # Event.wait uses a monotonic, interruptible wait, independent of the UI,
        # dispatcher and provider calls. Each refresh has its own short session.
        while not self._stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            try:
                self._repository.refresh_owner(self.owner_id)
            except Exception:
                logger.exception("Could not persist execution owner heartbeat; will retry")

    def close(self) -> None:
        """After local work is stopped, join before ending ownership. Safe to repeat."""
        with self._lock:
            if self._closed:
                return
            self._stop.set()
            if self._thread is not None and self._thread.ident is not None:
                self._thread.join()
            if self._registered:
                self._repository.interrupt_owned_and_end()
            self._closed = True
