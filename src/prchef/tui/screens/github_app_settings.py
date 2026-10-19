"""GitHub App configuration screen."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from prchef.config.project_config import GitHubAppSettings
from prchef.tui.bridge import WorkflowController
from prchef.tui.widgets import (
    OrchestratorSelect,
    RepositorySelect,
    is_orchestrator_select_widget,
)

if TYPE_CHECKING:
    from prchef.tui.app import OrchestratorApp


class GitHubAppSettingsScreen(Screen[None]):
    """Edit key references for the selected repository without handling secrets."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="github-app-settings"):
            yield Static("GitHub App identities", classes="screen-title")
            yield Static(
                "Only App IDs and private-key references are stored. "
                "Never paste private keys or tokens."
            )
            with Vertical(classes="workflow-form"):
                yield RepositorySelect(id="github-app-repository")
                with Vertical(classes="github-app-roles"):
                    for role in ("planner", "implementer", "reviewer"):
                        with Vertical(classes="github-app-role"):
                            yield Static(role.title(), classes="field-label")
                            yield Input(placeholder="App ID", id=f"{role}-app-id")
                            yield Input(placeholder="Installation ID", id=f"{role}-installation-id")
                            yield Input(
                                placeholder="Private key environment variable",
                                id=f"{role}-key-env",
                            )
                            yield Input(
                                placeholder="Private key file path", id=f"{role}-key-path"
                            )
                            yield Input(placeholder="GitHub API URL", id=f"{role}-api-url")
                            yield Input(placeholder="Bot login override", id=f"{role}-bot-name")
                            yield Input(
                                placeholder="Bot user ID override", id=f"{role}-bot-user-id"
                            )
                with Horizontal(classes="screen-actions"):
                    yield Button(
                        "Save configuration",
                        variant="success",
                        id="save-github-apps",
                    )
            yield Static(
                "Select a repository to inspect its App identities.", id="github-app-status"
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
        repository_select = self.query_one("#github-app-repository", RepositorySelect)
        if not repositories:
            repository_select.set_repositories(repositories)
            self._status("No repositories are configured in prchef.yml.")
            return
        repository_select.set_repositories(
            repositories, selected_repository=self.controller.selected_repository
        )
        repository_select.focus()

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        if is_orchestrator_select_widget(event.widget):
            return
        self.query_one("#github-app-settings", VerticalScroll).scroll_to_widget(
            event.widget, animate=False, top=True
        )

    def on_select_changed(self, event: OrchestratorSelect.Changed) -> None:
        if event.select.id == "github-app-repository" and isinstance(event.value, str):
            self.controller.select_repository(event.value)
            try:
                planner, implementer, reviewer = self.controller.github_app_settings(event.value)
            except Exception as exc:
                self._status(f"Cannot load GitHub App configuration: {exc}")
                return
            self._populate("planner", planner)
            self._populate("implementer", implementer)
            self._populate("reviewer", reviewer)
            self._status("Edit App references, then save to prchef.yml.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "save-github-apps":
            return
        repository = self.query_one("#github-app-repository", OrchestratorSelect).value
        if not isinstance(repository, str):
            self._status("Choose a repository first.")
            return
        try:
            config_path = self.controller.save_github_app_settings(
                repository,
                implementer_app=self._settings("implementer"),
                reviewer_app=self._settings("reviewer"),
                planner_app=self._settings("planner"),
            )
        except Exception as exc:
            self._status(f"Cannot save GitHub App configuration: {exc}")
            return
        self._status(f"Saved App references to {config_path}.")

    def _populate(self, role: str, settings: GitHubAppSettings) -> None:
        self.query_one(f"#{role}-app-id", Input).value = str(settings.app_id or "")
        self.query_one(f"#{role}-installation-id", Input).value = str(
            settings.installation_id or ""
        )
        self.query_one(f"#{role}-key-env", Input).value = settings.private_key_env or ""
        self.query_one(f"#{role}-key-path", Input).value = str(settings.private_key_path or "")
        self.query_one(f"#{role}-api-url", Input).value = settings.api_url
        self.query_one(f"#{role}-bot-name", Input).value = settings.bot_name or ""
        self.query_one(f"#{role}-bot-user-id", Input).value = str(settings.bot_user_id or "")

    def _settings(self, role: str) -> GitHubAppSettings:
        values = {
            "app_id": self.query_one(f"#{role}-app-id", Input).value.strip() or None,
            "installation_id": (
                self.query_one(f"#{role}-installation-id", Input).value.strip() or None
            ),
            "private_key_env": self.query_one(f"#{role}-key-env", Input).value.strip() or None,
            "private_key_path": self.query_one(f"#{role}-key-path", Input).value.strip() or None,
            "api_url": self.query_one(f"#{role}-api-url", Input).value.strip()
            or "https://api.github.com",
            "bot_name": self.query_one(f"#{role}-bot-name", Input).value.strip() or None,
            "bot_user_id": self.query_one(f"#{role}-bot-user-id", Input).value.strip() or None,
        }
        return GitHubAppSettings.model_validate(values)

    def _status(self, message: str) -> None:
        self.query_one("#github-app-status", Static).update(message)
