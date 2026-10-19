"""Interactive terminal presentation layer for the prchef."""

from typing import Any

from prchef.tui.bridge import WorkflowController, WorkflowLaunch

__all__ = ["PrchefApp", "WorkflowController", "WorkflowLaunch"]


def __getattr__(name: str) -> Any:
    """Avoid importing Textual when callers only need the controller layer."""

    if name == "PrchefApp":
        from prchef.tui.app import PrchefApp

        return PrchefApp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
