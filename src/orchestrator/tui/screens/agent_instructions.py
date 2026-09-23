"""Repository-local role instruction editor."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus
from textual.screen import Screen
from textual.widgets import Button, Static, TextArea

from orchestrator.models.agent import AgentRole
from orchestrator.tui.bridge import WorkflowController
from orchestrator.tui.widgets import (
    OrchestratorSelect,
    RepositorySelect,
    is_orchestrator_select_widget,
)

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


class AgentInstructionsScreen(Screen[None]):
    """Edit local role-specific Markdown files without touching Git state."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, repository: str | None = None) -> None:
        super().__init__()
        self._initial_repository = repository

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="agent-instructions"):
            yield Static("Agent instructions", classes="screen-title")
            yield Static(
                "Optional local Markdown guidance. It supplements task requirements and "
                "orchestrator invariants; saving never stages or commits files."
            )
            with Vertical(classes="workflow-form"):
                yield RepositorySelect(id="agent-instructions-repository")
                for role, label in (
                    ("planner", "Planner instructions (PLANNER.md)"),
                    ("implementer", "Implementer instructions (IMPLEMENTER.md)"),
                    ("reviewer", "Reviewer instructions (REVIEWER.md)"),
                ):
                    yield Static(label, classes="field-label")
                    yield TextArea("", id=f"{role}-instructions")
                    with Horizontal(classes="screen-actions"):
                        yield Button(
                            f"Save {role.title()} instructions",
                            variant="success",
                            id=f"save-{role}-instructions",
                        )
                    yield Static("", id=f"{role}-instructions-status")
            yield Static(
                "Choose a repository to view or create its local instruction files.",
                id="agent-instructions-status",
            )

    @property
    def controller(self) -> WorkflowController:
        return cast("OrchestratorApp", self.app).controller

    def on_mount(self) -> None:
        try:
            repositories = self.controller.repositories()
        except Exception as exc:
            self._status(f"Cannot load configured repositories: {exc}")
            return
        selector = self.query_one("#agent-instructions-repository", RepositorySelect)
        if not repositories:
            selector.set_repositories(repositories)
            self._status("No repositories are configured in orchestrator.yml.")
            return
        selected_repository = (
            self._initial_repository or self.controller.selected_repository
        )
        selector.set_repositories(repositories, selected_repository=selected_repository)
        selector.focus()

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        if is_orchestrator_select_widget(event.widget):
            return
        self.query_one("#agent-instructions", VerticalScroll).scroll_to_widget(
            event.widget, animate=False, top=True
        )

    def on_select_changed(self, event: OrchestratorSelect.Changed) -> None:
        if (
            event.select.id == "agent-instructions-repository"
            and isinstance(event.value, str)
        ):
            self.controller.select_repository(event.value)
            self._load_repository(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        role_by_button = {
            "save-planner-instructions": ("planner", AgentRole.PLANNER),
            "save-implementer-instructions": ("implementer", AgentRole.IMPLEMENTER),
            "save-reviewer-instructions": ("reviewer", AgentRole.REVIEWER),
        }
        selection = role_by_button.get(event.button.id or "")
        if selection is None:
            return
        repository = self.query_one(
            "#agent-instructions-repository", OrchestratorSelect
        ).value
        if not isinstance(repository, str):
            self._status("Choose a repository before saving instructions.")
            return
        role, agent_role = selection
        content = self.query_one(f"#{role}-instructions", TextArea).text
        try:
            path = self.controller.save_agent_instructions(repository, agent_role, content)
        except Exception as exc:
            self._role_status(role, f"Could not save {role} instructions: {exc}")
            self._status(f"Could not save {role} instructions.")
            return
        self._role_status(role, f"Saved {path.name} to the repository root.")
        self._status("Instruction file saved locally; Git was not changed by the orchestrator.")

    def _load_repository(self, repository: str) -> None:
        try:
            planner, implementer, reviewer = self.controller.agent_instructions(repository)
        except Exception as exc:
            self._status(f"Cannot load agent instructions: {exc}")
            return
        for role, content, filename in (
            ("planner", planner, "PLANNER.md"),
            ("implementer", implementer, "IMPLEMENTER.md"),
            ("reviewer", reviewer, "REVIEWER.md"),
        ):
            self.query_one(f"#{role}-instructions", TextArea).text = content or ""
            self._role_status(
                role,
                (
                    f"Loaded {filename}."
                    if content is not None
                    else f"No {filename} yet; save to create it."
                ),
            )
        self._status(f"Loaded local agent instructions for {repository}.")

    def _role_status(self, role: str, message: str) -> None:
        self.query_one(f"#{role}-instructions-status", Static).update(message)

    def _status(self, message: str) -> None:
        self.query_one("#agent-instructions-status", Static).update(message)
