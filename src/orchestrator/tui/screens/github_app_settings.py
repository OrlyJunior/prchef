"""Repository-local GitHub App configuration screen."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus
from textual.screen import Screen
from textual.widgets import Button, Input, Select, Static

from orchestrator.config.project_config import GitHubAppSettings, GitHubAppSettingsDestination
from orchestrator.tui.bridge import WorkflowController

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


class GitHubAppSettingsScreen(Screen[None]):
    """Edit GitHub App references in an explicitly selected configuration layer."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self._destinations: dict[str, GitHubAppSettingsDestination] = {}

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="github-app-settings"):
            yield Static("GitHub App identities", classes="screen-title")
            yield Static(
                "Only App IDs and private-key references are stored. "
                "Never paste private keys or tokens."
            )
            with Vertical(classes="workflow-form"):
                yield Select([], prompt="Configured repository", id="github-app-repository")
                yield Select([], prompt="Save destination", id="github-app-destination")
                yield Static(
                    "Save destination: select a repository.",
                    id="github-app-destination-path",
                )
                with Horizontal(classes="github-app-roles"):
                    for role in ("implementer", "reviewer"):
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
        repository_select = self.query_one("#github-app-repository", Select)
        repository_select.set_options([(item.key, item.key) for item in repositories])
        repository_select.focus()

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        self.query_one("#github-app-settings", VerticalScroll).scroll_to_widget(
            event.widget, animate=False, top=True
        )

    def _set_destinations(self, repository: str) -> None:
        destinations = self.controller.github_app_settings_destinations(repository)
        self._destinations = {destination.key: destination for destination in destinations}
        destination_select = self.query_one("#github-app-destination", Select)
        destination_select.set_options(
            [
                (
                    f"{self._destination_label(destination)} - {destination.path}",
                    destination.key,
                )
                for destination in destinations
            ]
        )
        config = self.controller.project_config(repository)
        if config.local_override_path is None:
            # There is one active source. A local override remains available,
            # but can only be created by intentionally changing this selector.
            destination_select.value = "global"
            self._show_destination("global")
            self._status(f"Edits will be saved to {self._destinations['global'].path}.")
        else:
            destination_select.value = Select.BLANK
            self.query_one("#github-app-destination-path", Static).update(
                "Save destination: choose the global configuration or repository-local override."
            )
            self._status("Settings are composed from two files; choose a save destination.")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "github-app-destination" and isinstance(event.value, str):
            self._show_destination(event.value)
            return
        if event.select.id != "github-app-repository" or not isinstance(event.value, str):
            return
        try:
            implementer, reviewer = self.controller.github_app_settings(event.value)
            self._populate("implementer", implementer)
            self._populate("reviewer", reviewer)
            self._set_destinations(event.value)
        except Exception as exc:
            self._status(f"Cannot load GitHub App configuration: {exc}")

    @staticmethod
    def _destination_label(destination: GitHubAppSettingsDestination) -> str:
        if destination.key == "global":
            return "Active configuration"
        return "Repository-local override"

    def _show_destination(self, key: str) -> None:
        destination = self._destinations.get(key)
        if destination is not None:
            self.query_one("#github-app-destination-path", Static).update(
                f"Save destination: {destination.path}"
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "save-github-apps":
            return
        repository = self.query_one("#github-app-repository", Select).value
        if not isinstance(repository, str):
            self._status("Choose a repository first.")
            return
        destination_key = self.query_one("#github-app-destination", Select).value
        if not isinstance(destination_key, str) or destination_key not in self._destinations:
            self._status("Choose the configuration file to update before saving.")
            return
        try:
            saved_path = self.controller.save_github_app_settings(
                repository,
                destination=self._destinations[destination_key],
                implementer_app=self._settings("implementer"),
                reviewer_app=self._settings("reviewer"),
            )
        except Exception as exc:
            self._status(f"Cannot save GitHub App configuration: {exc}")
            return
        self._status(f"Saved App references to {saved_path}.")

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
