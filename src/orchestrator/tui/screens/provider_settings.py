"""Repository-independent editor for global named providers."""

from __future__ import annotations

import os
import shlex
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus, Resize
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Static

from orchestrator.config.project_config import ConfigurationError
from orchestrator.config.provider_configuration import (
    PROVIDER_DEFAULTS,
    reject_provider_credentials,
)
from orchestrator.tui.bridge import WorkflowController
from orchestrator.tui.widgets import OrchestratorSelect, is_orchestrator_select_widget

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp

_FIELDS = {
    "codex-cli": ("command", "model", "reasoning_effort", "approval_policy",
                  "timeout_seconds", "max_concurrency"),
    "claude-code": ("executable", "model", "effort", "permission_mode",
                    "timeout_seconds", "max_concurrency"),
    "litellm": ("model", "api_base", "api_key_env", "timeout_seconds", "max_concurrency"),
}
_LABELS = {
    "command": "Command (quoted arguments)", "model": "Model (free-form)",
    "api_key_env": "API key environment variable name (never its value)",
    "approval_policy": "Approval policy (optional: approve-for-me)",
    "effort": "Effort (optional: low, medium, high, xhigh, max)",
    "permission_mode": "Permission mode (dontAsk, acceptEdits, auto)",
}


def split_provider_command(value: str, *, windows: bool | None = None) -> list[str]:
    """Keep Windows path separators while retaining the editor's argument quoting."""
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return shlex.split(value)
    lexer = shlex.shlex(value, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)


class RemoveProviderConfirmation(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    RemoveProviderConfirmation { align: center middle; }
    #remove-provider-dialog { width: 70; max-width: 95%; height: auto;
        padding: 1 2; border: round $error; }
    #remove-provider-dialog .screen-actions { layout: vertical; }
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self._provider_name = name

    def compose(self) -> ComposeResult:
        with Vertical(id="remove-provider-dialog"):
            yield Static(f"Remove provider {self._provider_name!r}?")
            with Horizontal(classes="screen-actions"):
                yield Button("Remove", variant="error", id="confirm-remove-provider")
                yield Button("Cancel", id="cancel-remove-provider")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-remove-provider")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ProviderSettingsScreen(Screen[None]):
    BINDINGS = [("escape", "app.pop_screen", "Back")]
    DEFAULT_CSS = """
    #provider-settings { width: 100%; height: 1fr; padding: 1 2; }
    #provider-form, .provider-field { height: auto; }
    #provider-status, #provider-auth, #provider-restart { height: auto; margin-top: 1; }
    #provider-status.-error { color: $error; }
    ProviderSettingsScreen.-narrow .screen-actions { layout: vertical; }
    ProviderSettingsScreen.-narrow .screen-actions Button { width: 100%; margin-bottom: 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._providers: dict[str, dict[str, Any]] = {}
        self._editing: str | None = None
        self._original: dict[str, Any] = {}
        self._kind = "codex-cli"
        self._source: bytes | None = None

    @property
    def controller(self) -> WorkflowController:
        return cast("OrchestratorApp", self.app).controller

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="provider-settings"):
            yield Static("Providers", classes="screen-title")
            yield Static("Manage defaults.providers in the existing global orchestrator.yml.")
            yield Static(
                "Saved settings apply to subsequent workflow submissions. Active runs keep their "
                "settings. Restart the TUI after changing an existing max_concurrency limit; "
                "restart to expand startup dispatcher capacity.", id="provider-restart",
            )
            yield Static("Provider", classes="field-label")
            yield OrchestratorSelect([], prompt="No providers configured", id="provider-select")
            with Horizontal(classes="screen-actions"):
                yield Button("Add provider", id="add-provider", variant="success")
                yield Button("Reload", id="reload-providers")
            with Vertical(id="provider-form", classes="workflow-form"):
                yield Static("Name", classes="field-label")
                yield Input(id="provider-name")
                yield Static("Type", classes="field-label")
                yield OrchestratorSelect(
                    [("Codex CLI", "codex-cli"), ("Claude Code", "claude-code"),
                     ("API model (LiteLLM)", "litellm")],
                    allow_blank=False, id="provider-type",
                )
                for field in dict.fromkeys(
                    field for fields in _FIELDS.values() for field in fields
                ):
                    with Vertical(classes="provider-field", id=f"provider-field-{field}"):
                        yield Static(_LABELS.get(field, field), classes="field-label")
                        yield Input(id=f"provider-{field}")
                yield Static("", id="provider-auth")
                with Horizontal(classes="screen-actions"):
                    yield Button("Save provider", id="save-provider", variant="primary")
                    yield Button("Remove provider", id="remove-provider", variant="error")
            yield Static("", id="provider-status")

    def on_mount(self) -> None:
        self.set_class(self.size.width <= 60, "-narrow")
        self._reload()

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.width <= 60, "-narrow")

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        if not is_orchestrator_select_widget(event.widget):
            self.query_one("#provider-settings", VerticalScroll).scroll_to_widget(
                event.widget, animate=False, top=True,
            )

    def _status(self, message: str, *, error: bool = False) -> None:
        status = self.query_one("#provider-status", Static)
        status.set_class(error, "-error")
        status.update(message)

    def _reload(self, selected: str | None = None) -> None:
        try:
            # Verify listing and source came from the same revision.
            source = self.controller.provider_configuration_source()
            providers = self.controller.provider_settings()
            if source != self.controller.provider_configuration_source():
                raise ConfigurationError("Configuration changed externally; reload.")
        except Exception as exc:
            self._source = None
            self._providers = {}
            self._editing = None
            self._original = {}
            selector = self.query_one("#provider-select", OrchestratorSelect)
            selector.set_options([], allow_blank=True)
            selector.disabled = True
            for action in ("save-provider", "remove-provider", "add-provider"):
                self.query_one(f"#{action}", Button).disabled = True
            message = str(exc) if isinstance(exc, ConfigurationError) else \
                "Cannot load providers from the global configuration. Reload to retry."
            self._status(message, error=True)
            return
        self._source = source
        self._providers = providers
        selector = self.query_one("#provider-select", OrchestratorSelect)
        selector.disabled = False
        selector.set_options([(f"{name} ({value.get('type', 'unknown')})", name)
                              for name, value in providers.items()], allow_blank=True)
        self.query_one("#add-provider", Button).disabled = False
        self.query_one("#save-provider", Button).disabled = False
        self._status("Select a provider to edit or add a new provider." if providers else
                     "No providers configured. Enter a name and save your first provider.")
        if selected in providers:
            selector.value = selected
            self._edit(selected)
        else:
            self._new()

    def _new(self) -> None:
        self._editing = None
        self._original = {}
        self.query_one("#save-provider", Button).disabled = self._source is None
        self.query_one("#provider-select", OrchestratorSelect).clear()
        self.query_one("#provider-name", Input).disabled = False
        self.query_one("#provider-name", Input).value = ""
        self.query_one("#remove-provider", Button).disabled = True
        self.query_one("#provider-type", OrchestratorSelect).value = "codex-cli"
        self._fill("codex-cli", PROVIDER_DEFAULTS["codex-cli"])

    def _edit(self, name: str) -> None:
        if self._source is None or name not in self._providers:
            return
        self._editing = name
        self._original = deepcopy(self._providers[name])
        self.query_one("#save-provider", Button).disabled = self._source is None
        self.query_one("#provider-name", Input).value = name
        self.query_one("#provider-name", Input).disabled = True
        try:
            reject_provider_credentials(self._original)
        except ConfigurationError:
            self._fill(self._kind, {})
            self.query_one("#save-provider", Button).disabled = True
            self.query_one("#remove-provider", Button).disabled = False
            self._status("Provider contains forbidden credentials. Remove them outside the editor "
                         "and reload; credential values cannot be displayed or saved.", error=True)
            return
        kind = self._original.get("type")
        if not isinstance(kind, str) or kind not in _FIELDS:
            self.query_one("#save-provider", Button).disabled = True
            self._status("Unsupported provider type; select a supported type to replace it.",
                         error=True)
        else:
            self.query_one("#provider-type", OrchestratorSelect).value = kind
            self._fill(kind, self._original)
        self.query_one("#remove-provider", Button).disabled = False

    def _fill(self, kind: str, values: dict[str, Any]) -> None:
        self._kind = kind
        for field in dict.fromkeys(field for fields in _FIELDS.values() for field in fields):
            self.query_one(f"#provider-field-{field}").display = field in _FIELDS[kind]
            value = values.get(field)
            rendered = shlex.join(value) if (
                field == "command" and isinstance(value, list) and
                all(isinstance(argument, str) for argument in value)
            ) else \
                ("" if value is None else str(value))
            self.query_one(f"#provider-{field}", Input).value = rendered
        guidance = {
            "litellm": "API keys are environment variable references only. Set the value outside "
                       "this screen; it is never read or saved here.",
        }.get(kind, "Authenticate using the CLI outside this screen. Never embed credentials "
                    "in commands or YAML.")
        self.query_one("#provider-auth", Static).update(guidance)

    def on_select_changed(self, event: OrchestratorSelect.Changed) -> None:
        if event.select.value != event.value or not isinstance(event.value, str):
            return
        if event.select.id == "provider-select" and event.value in self._providers:
            self._edit(event.value)
        elif event.select.id == "provider-type" and event.value != self._kind:
            self._fill(event.value, PROVIDER_DEFAULTS[event.value])
            self.query_one("#save-provider", Button).disabled = self._source is None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-provider":
            self._new()
            self.query_one("#provider-name", Input).focus()
        elif event.button.id == "reload-providers":
            self._reload(self._editing)
        elif event.button.id == "save-provider":
            self._save()
        elif (event.button.id == "remove-provider" and self._editing is not None
              and self._source is not None):
            name = self._editing
            self.app.push_screen(RemoveProviderConfirmation(name),
                                 lambda confirmed: self._remove(name, confirmed))

    def _save(self) -> None:
        if self._source is None:
            return
        values = deepcopy(self._original) if self._original.get("type") == self._kind else {}
        values["type"] = self._kind
        try:
            for field in _FIELDS[self._kind]:
                value = self.query_one(f"#provider-{field}", Input).value.strip()
                if not value:
                    values.pop(field, None)
                    if field in ("command", "max_concurrency", "executable") or (
                        field == "model" and self._kind == "litellm"
                    ):
                        raise ConfigurationError(f"{field} is required.")
                    continue
                if field == "command":
                    try:
                        values[field] = split_provider_command(value)
                    except ValueError:
                        raise ConfigurationError("command has invalid argument quoting.") from None
                elif field in ("max_concurrency", "timeout_seconds"):
                    try:
                        values[field] = float(value) if (
                            field == "timeout_seconds" and self._kind == "litellm"
                        ) else int(value)
                    except ValueError:
                        raise ConfigurationError(f"{field} must be a positive number.") from None
                else:
                    values[field] = value
            name = self._editing or self.query_one("#provider-name", Input).value.strip()
            warnings = self.controller.save_provider(name, values, create=self._editing is None,
                                                     expected_source=self._source)
        except ConfigurationError as exc:
            self._status(str(exc), error=True)
            return
        except Exception:
            self._status("Cannot save provider; no changes saved. Reload or retry.", error=True)
            return
        self._reload(name)
        self._status("Provider saved. " + " ".join(warnings))

    def _remove(self, name: str, confirmed: bool | None) -> None:
        if not confirmed:
            self._status("Provider removal cancelled.")
            return
        if self._source is None:
            self._status("Reload provider configuration before removing a provider.", error=True)
            return
        try:
            self.controller.delete_provider(name, expected_source=self._source)
        except ConfigurationError as exc:
            self._status(str(exc), error=True)
            return
        except Exception:
            self._status("Cannot remove provider; no changes saved.", error=True)
            return
        self._reload()
        self._status("Provider removed.")
