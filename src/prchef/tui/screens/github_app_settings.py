"""GitHub App configuration screen."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import ValidationError
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from prchef.config.project_config import GitHubAppSettings
from prchef.tui.bridge import WorkflowController
from prchef.tui.widgets import (
    PrchefSelect,
    RepositorySelect,
    is_prchef_select_widget,
)
from prchef.tui.workflow_presentation import github_identity_summary

if TYPE_CHECKING:
    from prchef.tui.app import PrchefApp


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
            yield Static("", id="github-app-identity-summary")
            yield Static(
                "Select a repository to inspect its App identities.", id="github-app-status"
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
                            yield Input(placeholder="Private key file path", id=f"{role}-key-path")
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

    @property
    def controller(self) -> WorkflowController:
        return cast("PrchefApp", self.app).controller

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
        if is_prchef_select_widget(event.widget):
            return
        self.query_one("#github-app-settings", VerticalScroll).scroll_to_widget(
            event.widget, animate=False, top=True
        )

    def on_select_changed(self, event: PrchefSelect.Changed) -> None:
        if event.select.id == "github-app-repository" and isinstance(event.value, str):
            self.controller.select_repository(event.value)
            try:
                planner, implementer, reviewer = self.controller.github_app_settings(event.value)
            except Exception as exc:
                self._status(f"Cannot load GitHub App configuration: {exc}")
                return
            self._show_summary(planner, implementer, reviewer)
            self._populate("planner", planner)
            self._populate("implementer", implementer)
            self._populate("reviewer", reviewer)
            self._status("Edit App references, then save to prchef.yml.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "save-github-apps":
            return
        repository = self.query_one("#github-app-repository", PrchefSelect).value
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
            self.query_one("#github-app-settings", VerticalScroll).scroll_to_widget(
                self.query_one("#github-app-status", Static), animate=False
            )
            return
        planner, implementer, reviewer = self.controller.github_app_settings(repository)
        self._show_summary(planner, implementer, reviewer)
        self._status(f"Saved App references to {config_path}.")
        self.query_one("#github-app-settings", VerticalScroll).scroll_to_widget(
            self.query_one("#github-app-status", Static), animate=False
        )

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
        messages = []
        required = (
            values["app_id"],
            values["installation_id"],
            values["private_key_env"] or values["private_key_path"],
        )
        if any(required):
            if not required[0]:
                messages.append("provide an App ID")
            if not required[1]:
                messages.append("provide an installation ID")
            if not required[2]:
                messages.append(
                    "provide a private key environment variable or a private key file path"
                )
        if values["private_key_env"] and values["private_key_path"]:
            messages.append(
                "choose either a private key environment variable or a private key file path"
            )
        if bool(values["bot_name"]) != bool(values["bot_user_id"]):
            messages.append("provide both bot login and bot user ID overrides")
        try:
            settings = GitHubAppSettings.model_validate(values)
        except ValidationError as exc:
            fields = {error["loc"][0] for error in exc.errors() if error["loc"]}
            for field, message in (
                ("app_id", "App ID must be a positive integer"),
                ("installation_id", "installation ID must be a positive integer"),
                ("bot_user_id", "bot user ID must be a positive integer"),
                ("api_url", "API URL must use http or https"),
                ("private_key_env", "invalid private key environment variable"),
                ("private_key_path", "invalid private key file path"),
                ("bot_name", "invalid bot login override"),
            ):
                if field in fields:
                    messages.append(message)
            if not messages:
                messages.append("check the App credential fields")
            raise ValueError(f"{role.title()}: {'; '.join(messages)}") from None
        if messages:
            raise ValueError(f"{role.title()}: {'; '.join(messages)}")
        return settings

    def _show_summary(
        self,
        planner: GitHubAppSettings,
        implementer: GitHubAppSettings,
        reviewer: GitHubAppSettings,
    ) -> None:
        self.query_one("#github-app-identity-summary", Static).update(
            github_identity_summary(
                planner.is_configured, implementer.is_configured, reviewer.is_configured
            )
        )

    def _status(self, message: str) -> None:
        self.query_one("#github-app-status", Static).update(message)
