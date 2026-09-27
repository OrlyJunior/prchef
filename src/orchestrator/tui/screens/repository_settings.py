"""Dedicated editor for repositories stored in the global configuration."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from types import EllipsisType
from typing import TYPE_CHECKING, cast

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus, Resize
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, Input, Static

from orchestrator.models.repository_context import IndexLifecycleState, RepositoryIndexStatus
from orchestrator.tui.bridge import WorkflowController
from orchestrator.tui.widgets import (
    OrchestratorSelect,
    RepositorySelect,
    is_orchestrator_select_widget,
)

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


class DeleteRepositoryConfirmation(ModalScreen[bool]):
    """Require explicit confirmation for a configuration-only deletion."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    DeleteRepositoryConfirmation {
        align: center middle;
    }
    DeleteRepositoryConfirmation > Vertical {
        width: 70;
        max-width: 95%;
        height: auto;
        padding: 1 2;
        border: round $error;
        background: $surface;
    }
    DeleteRepositoryConfirmation .screen-actions {
        height: auto;
        margin-top: 1;
    }
    """

    def __init__(self, repository: str, path: str) -> None:
        super().__init__()
        self._repository = repository
        self._path = path

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Remove repository configuration?", classes="screen-title")
            yield Static(
                f"Remove {self._repository!r} from orchestrator.yml?\n"
                f"The directory {self._path!r} and all files on disk will be left untouched."
            )
            with Horizontal(classes="screen-actions"):
                yield Button("Remove configuration", variant="error", id="confirm-delete")
                yield Button("Cancel", id="cancel-delete")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-delete")

    def action_cancel(self) -> None:
        self.dismiss(False)


class RepositorySettingsScreen(Screen[None]):
    """Create, update, rename, and remove global repository entries."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]
    DEFAULT_CSS = """
    #repository-settings {
        width: 100%;
        height: 1fr;
        padding: 1 2;
        overflow-y: auto;
    }
    #repository-settings-layout {
        width: 100%;
        height: auto;
        layout: horizontal;
    }
    #repository-list-pane {
        width: 1fr;
        min-width: 24;
        height: auto;
        margin-right: 2;
    }
    #repository-editor-pane {
        width: 2fr;
        min-width: 36;
        height: auto;
    }
    RepositorySettingsScreen.-narrow #repository-settings-layout {
        layout: vertical;
    }
    RepositorySettingsScreen.-narrow #repository-list-pane,
    RepositorySettingsScreen.-narrow #repository-editor-pane {
        width: 100%;
        min-width: 0;
        margin-right: 0;
    }
    #repository-picker, #validation-command-list {
        width: 100%;
    }
    #repository-path-warning { color: $warning; }
    #repository-errors, #index-errors { color: $error; }
    #review-budget-error, #validation-budget-error { color: $error; }
    #review-budget-actions, #validation-budget-actions { height: auto; }
    #validation-command-actions, #repository-actions, #index-actions {
        height: auto;
        margin-top: 1;
    }
    #validation-command-actions Button, #repository-actions Button, #index-actions Button {
        width: auto;
        margin-right: 1;
    }
    RepositorySettingsScreen.-narrow #validation-command-actions,
    RepositorySettingsScreen.-narrow #repository-actions,
    RepositorySettingsScreen.-narrow #index-actions {
        layout: vertical;
    }
    RepositorySettingsScreen.-narrow #validation-command-actions Button,
    RepositorySettingsScreen.-narrow #repository-actions Button,
    RepositorySettingsScreen.-narrow #index-actions Button {
        width: 100%;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, repository: str | None = None) -> None:
        super().__init__()
        self._initial_repository = repository
        self._editing_key: str | None = None
        self._creating_repository = False
        self._commands: list[list[str]] = []
        self._selected_command: int | None = None
        self._index_status_token = 0
        self._budget_original: dict[str, tuple[int, bool, int]] = {}
        self._cleared_budgets: set[str] = set()

    @property
    def controller(self) -> WorkflowController:
        return cast("OrchestratorApp", self.app).controller

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="repository-settings"):
            yield Static("Repositories", classes="screen-title")
            yield Static(
                "Manage repositories in the global orchestrator.yml. "
                "This screen never clones repositories or changes Git state."
            )
            with Horizontal(id="repository-settings-layout"):
                with Vertical(id="repository-list-pane"):
                    yield Static("Configured repositories", classes="field-label")
                    yield RepositorySelect(id="repository-picker")
                    yield Button("Add repository", id="add-repository")
                with Vertical(id="repository-editor-pane"):
                    yield Static("Repository key", classes="field-label")
                    yield Input(
                        placeholder="owner/repository",
                        id="repository-key",
                    )
                    yield Static("Local path", classes="field-label")
                    yield Input(
                        placeholder="C:/Git/repository",
                        id="repository-path",
                    )
                    yield Static("", id="repository-path-warning")
                    yield Checkbox(
                        "Automatically detect validation commands",
                        value=True,
                        id="repository-auto-detect",
                    )
                    yield Static("Review correction rounds", classes="field-label")
                    yield Static("How many rounds can fix changes requested by code review.")
                    yield Static("", id="review-budget-source")
                    yield Input(id="review-budget", placeholder="Non-negative integer")
                    yield Static("", id="review-budget-error")
                    with Horizontal(id="review-budget-actions"):
                        yield Button("Use default review budget", id="clear-review-budget")
                    yield Static("Validation correction attempts", classes="field-label")
                    yield Static("How many attempts can fix failed validation commands.")
                    yield Static("", id="validation-budget-source")
                    yield Input(id="validation-budget", placeholder="Non-negative integer")
                    yield Static("", id="validation-budget-error")
                    with Horizontal(id="validation-budget-actions"):
                        yield Button("Use default validation budget", id="clear-validation-budget")
                    yield Static("Index management", classes="field-label")
                    yield Static("Context strategy: checking…", id="index-context-strategy")
                    yield Static("Index status: checking…", id="index-status")
                    yield Static("", id="index-metadata")
                    yield Static("", id="index-errors")
                    with Horizontal(id="index-actions"):
                        yield Button("Build / Rebuild", id="build-index")
                        yield Button("Refresh changed files", id="refresh-index")
                        yield Button("Clear index", variant="error", id="clear-index")
                    yield Static("Validation commands", classes="field-label")
                    yield Static(
                        "Commands the orchestrator runs to verify changes before commit/PR. "
                        "Enter each command normally; argument boundaries are preserved in YAML."
                    )
                    yield OrchestratorSelect(
                        [],
                        prompt="Select a validation command",
                        id="validation-command-list",
                    )
                    yield Input(
                        placeholder="python -m pytest",
                        id="validation-command-input",
                    )
                    with Horizontal(id="validation-command-actions"):
                        yield Button("Add", id="add-validation-command")
                        yield Button("Update", id="update-validation-command")
                        yield Button("Remove", id="remove-validation-command")
                        yield Button("Move up", id="move-validation-command-up")
                        yield Button("Move down", id="move-validation-command-down")
                    yield Static("", id="repository-errors")
                    with Horizontal(id="repository-actions"):
                        yield Button(
                            "Save repository",
                            variant="success",
                            id="save-repository",
                        )
                        yield Button(
                            "Remove repository",
                            variant="error",
                            id="delete-repository",
                        )
            yield Static("Choose a repository to edit or add a new one.", id="repository-status")

    def on_mount(self) -> None:
        self._set_layout_mode(self.size.width)
        # A screen may be pushed from a button message while the previous
        # screen is still completing its refresh. Defer child queries until
        # this screen's composed widgets have participated in a layout pass.
        self.call_after_refresh(self._refresh_repositories, self._initial_repository)

    def on_resize(self, event: Resize) -> None:
        self._set_layout_mode(event.size.width)

    def _set_layout_mode(self, width: int) -> None:
        self.set_class(width < 90, "-narrow")

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        # Scrolling a button on mouse-down can move it before mouse-up and turn
        # the first click into focus-only. Keyboard-focused inputs still scroll.
        if isinstance(event.widget, Button) or is_orchestrator_select_widget(event.widget):
            return
        self.query_one("#repository-settings", VerticalScroll).scroll_to_widget(
            event.widget, animate=False, top=True
        )

    def on_select_changed(self, event: OrchestratorSelect.Changed) -> None:
        if event.select.id == "repository-picker" and isinstance(event.value, str):
            # RepositorySelect restores its configured value after mounting.
            # Do not let that deferred programmatic event overwrite a form the
            # user has already switched to create mode. A real picker choice
            # owns focus and deliberately exits create mode.
            if self._creating_repository and not event.select.has_focus:
                return
            self._creating_repository = False
            self._load_repository(event.value)
        elif event.select.id == "validation-command-list" and isinstance(event.value, int):
            if 0 <= event.value < len(self._commands):
                self._selected_command = event.value
                self.query_one("#validation-command-input", Input).value = shlex.join(
                    self._commands[event.value]
                )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "repository-path":
            self._update_path_warning(event.value)
        elif event.input.id in ("review-budget", "validation-budget"):
            kind = event.input.id.removesuffix("-budget")
            if kind in self._budget_original:
                if kind in self._cleared_budgets and event.value != str(
                    self._budget_original[kind][2]
                ):
                    self._cleared_budgets.remove(kind)
                self._validate_budget(kind)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions: dict[str, Callable[[], None]] = {
            "add-repository": self._new_repository,
            "save-repository": self._save_repository,
            "delete-repository": self._request_delete,
            "add-validation-command": self._add_command,
            "update-validation-command": self._update_command,
            "remove-validation-command": self._remove_command,
            "move-validation-command-up": lambda: self._move_command(-1),
            "move-validation-command-down": lambda: self._move_command(1),
            "build-index": lambda: self._start_index_action("build"),
            "refresh-index": lambda: self._start_index_action("refresh"),
            "clear-index": lambda: self._start_index_action("clear"),
            "clear-review-budget": lambda: self._clear_budget("review"),
            "clear-validation-budget": lambda: self._clear_budget("validation"),
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()

    def _refresh_repositories(self, selected: str | None = None) -> None:
        try:
            repositories = self.controller.repositories()
        except Exception as exc:
            self._disable_editor()
            self._error(f"Cannot load configured repositories: {exc}")
            return
        selector = self.query_one("#repository-picker", RepositorySelect)
        selected = selector.set_repositories(
            repositories,
            selected_repository=selected,
        )
        if selected is None:
            self._new_repository()
            self._status("No repositories are configured. Add the first repository.")
            return
        if self._creating_repository:
            return
        self._load_repository(selected)

    def _load_repository(self, repository: str) -> None:
        try:
            settings = self.controller.repository_settings(repository)
            self.controller.select_repository(repository)
        except Exception as exc:
            self._disable_editor()
            self._error(f"Cannot load repository configuration: {exc}")
            return
        self.query_one("#repository-editor-pane", Vertical).disabled = False
        self._creating_repository = False
        self._editing_key = repository
        self.query_one("#repository-key", Input).value = settings.key
        self.query_one("#repository-path", Input).value = settings.path
        self.query_one("#repository-auto-detect", Checkbox).value = settings.auto_detect
        self._set_budgets(
            (
                settings.review_retries,
                settings.review_retries_overridden,
                settings.inherited_review_retries,
            ),
            (
                settings.validation_retries,
                settings.validation_retries_overridden,
                settings.inherited_validation_retries,
            ),
        )
        self._commands = [list(command) for command in settings.commands]
        self._selected_command = None
        self.query_one("#validation-command-input", Input).value = ""
        self._refresh_command_list()
        self.query_one("#delete-repository", Button).disabled = False
        self._error("")
        self._update_path_warning(settings.path)
        self._load_index_status(repository)
        self._status(f"Editing repository {repository!r}.")

    def _new_repository(self) -> None:
        self.query_one("#repository-editor-pane", Vertical).disabled = False
        self._creating_repository = True
        self._editing_key = None
        self._commands = []
        self._selected_command = None
        self.query_one("#repository-key", Input).value = ""
        self.query_one("#repository-path", Input).value = ""
        self.query_one("#repository-auto-detect", Checkbox).value = True
        try:
            review_default, validation_default = self.controller.repository_retry_defaults()
        except Exception as exc:
            self._disable_editor()
            self._error(f"Cannot load default correction budgets: {exc}")
            return
        self._set_budgets(
            (review_default, False, review_default),
            (validation_default, False, validation_default),
        )
        self.query_one("#validation-command-input", Input).value = ""
        self.query_one("#delete-repository", Button).disabled = True
        self._index_status_token += 1
        self._render_index_status(
            RepositoryIndexStatus(
                strategy="unavailable",
                state=IndexLifecycleState.NOT_INDEXED,
                error_message="Save this repository before managing an index.",
            )
        )
        self._refresh_command_list()
        self._error("")
        self._status("Enter repository settings, then save.")
        self.query_one("#repository-key", Input).focus()

    def _disable_editor(self) -> None:
        """Discard a stale form when its selected repository cannot be loaded."""
        self._creating_repository = False
        self._editing_key = None
        self._commands = []
        self._selected_command = None
        self._budget_original.clear()
        self._cleared_budgets.clear()
        self._index_status_token += 1
        self.query_one("#repository-editor-pane", Vertical).disabled = True
        for input_id in (
            "repository-key",
            "repository-path",
            "review-budget",
            "validation-budget",
            "validation-command-input",
        ):
            self.query_one(f"#{input_id}", Input).value = ""
        for status_id in (
            "repository-path-warning",
            "review-budget-source",
            "review-budget-error",
            "validation-budget-source",
            "validation-budget-error",
            "index-context-strategy",
            "index-status",
            "index-metadata",
            "index-errors",
        ):
            self.query_one(f"#{status_id}", Static).update("")
        self._refresh_command_list()
        self._status("Repository settings are unavailable. Select another repository or add one.")

    def _command_from_input(self) -> list[str] | None:
        value = self.query_one("#validation-command-input", Input).value.strip()
        if not value:
            self._error("Enter a validation command first.")
            return None
        try:
            command = shlex.split(value)
        except ValueError as exc:
            self._error(f"Invalid validation command quoting: {exc}")
            return None
        if not command:
            self._error("Validation command must contain a program name.")
            return None
        return command

    def _add_command(self) -> None:
        command = self._command_from_input()
        if command is None:
            return
        self._commands.append(command)
        self._selected_command = None
        self.query_one("#validation-command-input", Input).value = ""
        self._refresh_command_list()
        self._error("")

    def _update_command(self) -> None:
        if self._selected_command is None:
            self._error("Select a validation command to update.")
            return
        command = self._command_from_input()
        if command is None:
            return
        self._commands[self._selected_command] = command
        self._refresh_command_list(self._selected_command)
        self._error("")

    def _remove_command(self) -> None:
        if self._selected_command is None:
            self._error("Select a validation command to remove.")
            return
        del self._commands[self._selected_command]
        self._selected_command = None
        self.query_one("#validation-command-input", Input).value = ""
        self._refresh_command_list()
        self._error("")

    def _move_command(self, offset: int) -> None:
        if self._selected_command is None:
            self._error("Select a validation command to reorder.")
            return
        destination = self._selected_command + offset
        if destination < 0 or destination >= len(self._commands):
            self._error("The selected command cannot move farther in that direction.")
            return
        self._commands[self._selected_command], self._commands[destination] = (
            self._commands[destination],
            self._commands[self._selected_command],
        )
        self._selected_command = destination
        self._refresh_command_list(destination)
        self._error("")

    def _refresh_command_list(self, selected: int | None = None) -> None:
        selector = self.query_one("#validation-command-list", OrchestratorSelect)
        options = [
            (f"{index + 1}. {shlex.join(command)}", index)
            for index, command in enumerate(self._commands)
        ]
        selector.set_options(options, allow_blank=True)
        if selected is not None:
            selector.value = selected

    def _load_index_status(self, repository: str) -> None:
        self._index_status_token += 1
        token = self._index_status_token
        self.query_one("#index-context-strategy", Static).update("Context strategy: checking…")
        self.query_one("#index-status", Static).update("Index status: checking…")
        self.query_one("#index-metadata", Static).update("")
        self.query_one("#index-errors", Static).update("")
        self._set_index_buttons_disabled(True)
        self._fetch_index_status(repository, token)

    @work(thread=True, exclusive=True)
    def _fetch_index_status(self, repository: str, token: int) -> None:
        try:
            status = self.controller.repository_index_status(repository)
        except Exception as exc:
            status = RepositoryIndexStatus(
                strategy="qdrant",
                state=IndexLifecycleState.FAILED,
                error_message=f"Could not inspect the semantic index: {exc}",
            )
        self.app.call_from_thread(self._receive_index_status, repository, token, status)

    def _receive_index_status(
        self, repository: str, token: int, status: RepositoryIndexStatus
    ) -> None:
        if token != self._index_status_token or repository != self._editing_key:
            return
        self._render_index_status(status)

    def _render_index_status(self, status: RepositoryIndexStatus) -> None:
        strategy_label = status.strategy.replace("_", " ").title()
        state_label = status.state.value.replace("_", " ").upper()
        self.query_one("#index-context-strategy", Static).update(
            f"Context strategy: {strategy_label}"
        )
        self.query_one("#index-status", Static).update(f"Index status: {state_label}")
        details: list[str] = []
        if status.last_successful_indexed_at:
            details.append(f"Last successful index: {status.last_successful_indexed_at}")
        if status.last_indexed_commit:
            details.append(f"Commit: {status.last_indexed_commit}")
        if status.document_count is not None:
            details.append(f"Documents: {status.document_count}")
        if status.point_count is not None:
            details.append(f"Chunks: {status.point_count}")
        self.query_one("#index-metadata", Static).update(" • ".join(details))
        self.query_one("#index-errors", Static).update(status.error_message or "")
        self._set_index_buttons_disabled(
            self._editing_key is None
            or status.strategy != "qdrant"
            or status.state is IndexLifecycleState.INDEXING
        )

    def _set_index_buttons_disabled(self, disabled: bool) -> None:
        for button_id in ("#build-index", "#refresh-index", "#clear-index"):
            self.query_one(button_id, Button).disabled = disabled

    def _start_index_action(self, action: str) -> None:
        repository = self._editing_key
        if repository is None:
            self._error("Save this repository before managing an index.")
            return
        self._render_index_status(
            RepositoryIndexStatus(strategy="qdrant", state=IndexLifecycleState.INDEXING)
        )
        self._run_index_action(repository, action)

    @work(thread=True, exclusive=True)
    def _run_index_action(self, repository: str, action: str) -> None:
        try:
            methods = {
                "build": self.controller.build_repository_index,
                "refresh": self.controller.refresh_repository_index,
                "clear": self.controller.clear_repository_index,
            }
            status = methods[action](repository)
        except Exception as exc:
            self.app.call_from_thread(self._index_action_failed, repository, str(exc))
            return
        self.app.call_from_thread(self._index_action_finished, repository, status)

    def _index_action_finished(self, repository: str, status: RepositoryIndexStatus) -> None:
        if repository != self._editing_key:
            return
        self._render_index_status(status)
        self._status(f"Index operation completed for {repository!r}.")

    def _index_action_failed(self, repository: str, message: str) -> None:
        if repository != self._editing_key:
            return
        self._render_index_status(
            RepositoryIndexStatus(
                strategy="qdrant",
                state=IndexLifecycleState.FAILED,
                error_message=message or "Semantic index operation failed.",
            )
        )

    def _save_repository(self) -> None:
        if self._editing_key is None and not self._creating_repository:
            self._error("Select a repository or add one before saving.")
            return
        key = self.query_one("#repository-key", Input).value.strip()
        path = self.query_one("#repository-path", Input).value.strip()
        error = self._validate_repository(key, path)
        if error is not None:
            self._error(error)
            return
        review_valid, review_retries = self._budget_update("review")
        validation_valid, validation_retries = self._budget_update("validation")
        if not review_valid or not validation_valid:
            return
        try:
            saved = self.controller.save_repository(
                key,
                path,
                self.query_one("#repository-auto-detect", Checkbox).value,
                [list(command) for command in self._commands],
                original_key=self._editing_key,
                review_retries=review_retries,
                validation_retries=validation_retries,
            )
        except Exception as exc:
            self._error(f"Cannot save repository: {exc}")
            return
        previous = self._editing_key
        self._creating_repository = False
        self._editing_key = key
        self._refresh_repositories(key)
        action = "Created" if previous is None else "Saved"
        self._status(f"{action} repository {key!r} in {saved}.")

    def _set_budgets(
        self, review: tuple[int, bool, int], validation: tuple[int, bool, int]
    ) -> None:
        self._budget_original = {"review": review, "validation": validation}
        self._cleared_budgets.clear()
        for kind, (effective, _overridden, _inherited) in self._budget_original.items():
            self.query_one(f"#{kind}-budget", Input).value = str(effective)
            self._validate_budget(kind)

    def _clear_budget(self, kind: str) -> None:
        self._cleared_budgets.add(kind)
        inherited = self._budget_original[kind][2]
        self.query_one(f"#{kind}-budget", Input).value = str(inherited)
        self._validate_budget(kind)

    def _validate_budget(self, kind: str) -> bool:
        value = self.query_one(f"#{kind}-budget", Input).value
        error = self.query_one(f"#{kind}-budget-error", Static)
        source = self.query_one(f"#{kind}-budget-source", Static)
        label = "Review" if kind == "review" else "Validation"
        if re.fullmatch(r"[0-9]+", value) is None:
            error.update(f"{label} correction budget must be a non-negative integer.")
            return False
        try:
            parsed = int(value)
        except ValueError:
            error.update(f"{label} correction budget must be a non-negative integer.")
            return False
        error.update("")
        effective, overridden, inherited = self._budget_original[kind]
        if kind in self._cleared_budgets:
            source.update(f"Inherited from defaults: {inherited} (pending save)")
        elif value == str(effective):
            origin = "overridden for this repository" if overridden else "inherited from defaults"
            source.update(f"Effective: {effective} ({origin})")
        else:
            source.update(f"Pending repository override: {parsed}")
        return True

    def _budget_update(self, kind: str) -> tuple[bool, int | None | EllipsisType]:
        if not self._validate_budget(kind):
            return False, ...
        if kind in self._cleared_budgets:
            return True, None
        value = self.query_one(f"#{kind}-budget", Input).value
        if value == str(self._budget_original[kind][0]):
            return True, ...
        return True, int(value)

    def _validate_repository(self, key: str, path: str) -> str | None:
        if not key:
            return "Repository key is required."
        configured_keys = {repository.key for repository in self.controller.repositories()}
        if key != self._editing_key and key in configured_keys:
            return f"Repository key {key!r} is already configured."
        if not path:
            return "Local path is required."
        if "\0" in path:
            return "Local path contains an invalid null character."
        try:
            resolved = self.controller.resolve_repository_path(path)
        except (OSError, RuntimeError, ValueError) as exc:
            return f"Local path is not usable: {exc}"
        if resolved.exists() and not resolved.is_dir():
            return "Local path exists but is not a directory."
        return None

    def _update_path_warning(self, value: str) -> None:
        warning = self.query_one("#repository-path-warning", Static)
        normalized = value.strip()
        if not normalized:
            warning.update("")
            return
        try:
            resolved = self.controller.resolve_repository_path(normalized)
        except (OSError, RuntimeError, ValueError) as exc:
            warning.update(f"Path format is not usable: {exc}")
            return
        if resolved.is_dir():
            warning.update(f"Directory exists: {resolved}")
        elif resolved.exists():
            warning.update(f"Path exists but is not a directory: {resolved}")
        else:
            warning.update(f"Path does not currently exist: {resolved}")

    def _request_delete(self) -> None:
        if self._editing_key is None:
            self._error("Select an existing repository to remove.")
            return
        path = self.query_one("#repository-path", Input).value.strip()
        self.app.push_screen(
            DeleteRepositoryConfirmation(self._editing_key, path),
            self._delete_confirmed,
        )

    def _delete_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed or self._editing_key is None:
            self._status("Repository removal cancelled; configuration was not changed.")
            return
        repository = self._editing_key
        try:
            saved = self.controller.delete_repository(repository)
        except Exception as exc:
            self._error(f"Cannot remove repository: {exc}")
            return
        self._editing_key = None
        self._refresh_repositories(self.controller.selected_repository)
        self._status(
            f"Removed {repository!r} from {saved}. Its directory and files were not deleted."
        )

    def _error(self, message: str) -> None:
        self.query_one("#repository-errors", Static).update(message)

    def _status(self, message: str) -> None:
        self.query_one("#repository-status", Static).update(message)


__all__ = ["DeleteRepositoryConfirmation", "RepositorySettingsScreen"]
