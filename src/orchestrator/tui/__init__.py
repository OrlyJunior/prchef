"""Interactive terminal presentation layer for the orchestrator."""

from typing import Any

from orchestrator.tui.bridge import WorkflowController, WorkflowLaunch

__all__ = ["OrchestratorApp", "WorkflowController", "WorkflowLaunch"]


def __getattr__(name: str) -> Any:
    """Avoid importing Textual when callers only need the controller layer."""

    if name == "OrchestratorApp":
        from orchestrator.tui.app import OrchestratorApp

        return OrchestratorApp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
