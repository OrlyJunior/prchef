"""Dedicated editor for reusable role-local agent presets."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from prchef.config.project_config import AgentPresetConfig
from prchef.models.agent import AgentRole
from prchef.tui.bridge import WorkflowController
from prchef.tui.widgets import (
    PrchefSelect,
    RepositorySelect,
    is_prchef_select_widget,
)

if TYPE_CHECKING:
    from prchef.tui.app import PrchefApp


_NEW_PRESET = object()
_NO_FALLBACK = object()
_PROVIDER_DEFAULT_PERMISSION = object()
_PERMISSION_MODES = ("dontAsk", "acceptEdits", "auto")
_PermissionMode = Literal["dontAsk", "acceptEdits", "auto"]


class AgentPresetsScreen(Screen[None]):
    """Manage persisted planner, implementer, and reviewer preset templates."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    DEFAULT_CSS = """
    /* This ID is intentionally local to the editor. The home-screen
       navigation button uses the same destination name, so sizing the
       editor here prevents a content-area rule from leaking into navigation. */
    #agent-presets {
        width: 100%;
        height: 1fr;
        padding: 1 2;
        overflow-y: auto;
    }
    #agent-preset-role {
        width: 32;
        max-width: 100%;
    }
    """

    def __init__(self, repository: str | None = None) -> None:
        super().__init__()
        self._initial_repository = repository
        self._selected_preset: str | None = None
        self._suppressed_preset_changes: set[object] = set()
        # Select changes queued during role loading can arrive after an
        # invalid provider default has been reported.
        self._provider_controls_failed = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="agent-presets"):
            yield Static("Agent Presets", classes="screen-title")
            yield Static(
                "Create reusable role-local provider and runtime settings. "
                "Selecting a preset in Start workflow applies it only to that run."
            )
            with Vertical(classes="workflow-form"):
                yield Static("Repository", classes="field-label")
                yield RepositorySelect(id="agent-presets-repository")
                yield Static("Role", classes="field-label")
                yield PrchefSelect(
                    [(role.value.title(), role.value) for role in AgentRole],
                    allow_blank=False,
                    prompt="Choose a role",
                    id="agent-preset-role",
                )
                yield Static("Fallback preset", classes="field-label")
                with Horizontal(classes="role-config-row"):
                    yield PrchefSelect(
                        [("Disabled", _NO_FALLBACK)],
                        allow_blank=False,
                        prompt="Disabled",
                        id="agent-fallback-preset",
                    )
                    yield Button("Save fallback", id="save-agent-fallback")
                yield Static("Preset", classes="field-label")
                yield PrchefSelect(
                    [("Create new preset", _NEW_PRESET)],
                    prompt="Choose a preset",
                    id="agent-preset-select",
                )
                yield Static("Preset name", classes="field-label")
                yield Input(placeholder="Name", id="agent-preset-name")
                with Horizontal(classes="role-config-row"):
                    with Vertical(classes="role-field"):
                        yield Static("Provider", id="agent-preset-provider-label")
                        yield PrchefSelect(
                            [], prompt="Choose a provider", id="agent-preset-provider"
                        )
                    with Vertical(classes="role-field"):
                        yield Static("Model (free-form)", id="agent-preset-model-label")
                        yield Input(placeholder="Provider/YAML default", id="agent-preset-model")
                with Horizontal(classes="role-config-row"):
                    with Vertical(classes="role-field"):
                        yield Static(
                            "Reasoning effort", id="agent-preset-reasoning-label"
                        )
                        yield Input(
                            placeholder="Provider/YAML default", id="agent-preset-reasoning"
                        )
                    with Vertical(classes="role-field"):
                        yield Static("Permission mode", id="agent-preset-permission-label")
                        yield PrchefSelect(
                            [],
                            prompt="Provider/YAML default",
                            id="agent-preset-permission",
                        )
                    with Vertical(classes="role-field"):
                        yield Static("Temperature", id="agent-preset-temperature-label")
                        yield Input(
                            placeholder="Provider default", id="agent-preset-temperature"
                        )
                with Vertical(classes="role-field"):
                    yield Static("Max output tokens", id="agent-preset-max-output-tokens-label")
                    yield Input(
                        placeholder="Provider default", id="agent-preset-max-output-tokens"
                    )
                with Horizontal(classes="screen-actions"):
                    yield Button("Create", variant="success", id="create-agent-preset")
                    yield Button("Update", id="update-agent-preset")
                    yield Button("Rename", id="rename-agent-preset")
                    yield Button("Delete", variant="error", id="delete-agent-preset")
            yield Static(
                "Choose a repository, role, and preset to edit.", id="agent-presets-status"
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
        selector = self.query_one("#agent-presets-repository", RepositorySelect)
        if not repositories:
            selector.set_repositories(repositories)
            self._status("No repositories are configured in prchef.yml.")
            return
        selector.set_repositories(
            repositories,
            selected_repository=self._initial_repository or self.controller.selected_repository,
        )
        selector.focus()

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        if is_prchef_select_widget(event.widget):
            return
        self.query_one("#agent-presets", VerticalScroll).scroll_to_widget(
            event.widget, animate=False, top=True
        )

    def on_select_changed(self, event: PrchefSelect.Changed) -> None:
        if event.select.id == "agent-presets-repository" and isinstance(event.value, str):
            self.controller.select_repository(event.value)
            self._load_repository(event.value)
        elif event.select.id == "agent-preset-role" and isinstance(event.value, str):
            self._load_role(AgentRole(event.value))
        elif event.select.id == "agent-preset-select":
            if event.value in self._suppressed_preset_changes:
                self._suppressed_preset_changes.discard(event.value)
                return
            if isinstance(event.value, str):
                self._load_preset(event.value)
            elif event.value is _NEW_PRESET:
                self._new_preset()
        elif event.select.id == "agent-preset-provider" and isinstance(event.value, str):
            # Provider changes can be posted after a preset or role switch has
            # already changed the control again. Applying an older event here
            # would reload permission controls for a provider the selector no
            # longer names, dropping or misapplying the preset's override.
            if event.select.value != event.value:
                return
            preset = self._selected_agent_preset()
            self._load_provider_controls(
                event.value,
                permission_mode=(
                    preset.permission_mode
                    if preset is not None and preset.provider == event.value
                    else None
                ),
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "create-agent-preset": self._create,
            "update-agent-preset": self._update,
            "rename-agent-preset": self._rename,
            "delete-agent-preset": self._delete,
            "save-agent-fallback": self._save_fallback,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()

    def _load_repository(self, repository: str) -> None:
        try:
            provider_options = self.controller.configured_agent_provider_options(repository)
        except Exception as exc:
            self._status(f"Cannot load agent providers: {exc}")
            return
        provider = self.query_one("#agent-preset-provider", PrchefSelect)
        provider.set_options(provider_options, allow_blank=False)
        role_selector = self.query_one("#agent-preset-role", PrchefSelect)
        active_role = self._role() or AgentRole.PLANNER
        # Keep the role-local editor active when navigating between repositories.
        # Assigning the existing value does not emit a change event, so load the
        # role directly instead of relying on Textual to do it for us.
        role_selector.value = active_role.value
        self._load_role(active_role)

    def _load_role(self, role: AgentRole) -> None:
        repository = self._repository()
        if repository is None:
            return
        try:
            presets = self.controller.agent_presets(repository, role)
            default_provider = self.controller.agent_providers(repository)[role]
            provider_options = self.controller.configured_agent_provider_options(repository)
            fallback_preset = self.controller.agent_fallback_preset(repository, role)
        except Exception as exc:
            self._status(f"Cannot load {role.value} presets: {exc}")
            return
        selector = self.query_one("#agent-preset-select", PrchefSelect)
        self._suppressed_preset_changes.add(_NEW_PRESET)
        selector.set_options(
            [("Create new preset", _NEW_PRESET), *[(name, name) for name in presets]]
        )
        fallback = self.query_one("#agent-fallback-preset", PrchefSelect)
        fallback.set_options(
            [("Disabled", _NO_FALLBACK), *[(name, name) for name in presets]],
            allow_blank=False,
        )
        fallback.value = fallback_preset if fallback_preset is not None else _NO_FALLBACK
        self._selected_preset = None
        selector.value = _NEW_PRESET
        self._clear_values()
        provider = self.query_one("#agent-preset-provider", PrchefSelect)
        provider.set_options(provider_options, allow_blank=False)
        provider.value = default_provider
        if not self._load_provider_controls(default_provider):
            return
        self._status(
            f"{role.value.title()} presets loaded. Create new or select an existing preset."
        )

    def _load_preset(self, name: str) -> None:
        role = self._role()
        repository = self._repository()
        if role is None or repository is None:
            return
        preset = self.controller.agent_presets(repository, role).get(name)
        if preset is None:
            self._status(f"Preset {name!r} no longer exists. Reload the role to continue.")
            return
        self._selected_preset = name
        self.query_one("#agent-preset-name", Input).value = name
        provider = self.query_one("#agent-preset-provider", PrchefSelect)
        try:
            provider.value = preset.provider
            if not self._load_provider_controls(
                preset.provider, permission_mode=preset.permission_mode
            ):
                return
        except Exception as exc:
            self._status(
                f"Preset {name!r} references unavailable provider {preset.provider!r}: {exc}"
            )
            return
        self.query_one("#agent-preset-model", Input).value = preset.model or ""
        self.query_one("#agent-preset-reasoning", Input).value = preset.reasoning_effort or ""
        permission = self.query_one("#agent-preset-permission", PrchefSelect)
        if permission.display:
            permission.value = (
                preset.permission_mode
                if preset.permission_mode is not None
                else _PROVIDER_DEFAULT_PERMISSION
            )
        self.query_one("#agent-preset-temperature", Input).value = self._text(
            preset.generation.get("temperature")
        )
        self.query_one("#agent-preset-max-output-tokens", Input).value = self._text(
            preset.generation.get("max_output_tokens")
        )
        self._status(f"Editing {role.value} preset {name!r}.")

    def _new_preset(self) -> None:
        self._selected_preset = None
        self.query_one("#agent-preset-name", Input).value = ""
        self._clear_values()
        if self._provider_controls_failed:
            return
        self._status("Enter a name and settings, then create the preset.")

    def _load_provider_controls(
        self, provider: str, *, permission_mode: _PermissionMode | None = None
    ) -> bool:
        self._provider_controls_failed = False
        repository = self._repository()
        if repository is None:
            return False
        try:
            capabilities = self.controller.provider_runtime_capabilities(repository, provider)
        except Exception as exc:
            self._provider_controls_failed = True
            self._status(f"Cannot load provider settings: {exc}")
            return False
        self._set_visible("model", capabilities.supports_model)
        self._set_visible("reasoning", capabilities.supports_reasoning_effort)
        if not self._set_permission_visible(
            provider, capabilities.supports_permission_mode, permission_mode
        ):
            self._provider_controls_failed = True
            return False
        self._set_visible("temperature", capabilities.supports_generation)
        self._set_visible("max-output-tokens", capabilities.supports_generation)
        return True

    def _set_visible(self, name: str, visible: bool) -> None:
        self.query_one(f"#agent-preset-{name}-label", Static).display = visible
        self.query_one(f"#agent-preset-{name}").display = visible

    def _set_permission_visible(
        self, provider: str, visible: bool, permission_mode: _PermissionMode | None
    ) -> bool:
        label = self.query_one("#agent-preset-permission-label", Static)
        selector = self.query_one("#agent-preset-permission", PrchefSelect)
        label.display = visible
        selector.display = visible
        if not visible:
            return True
        repository = self._repository()
        if repository is None:
            return False
        try:
            values = self.controller.provider_runtime_values(repository, provider)
        except Exception as exc:
            # Provider schemas are deliberately revalidated when their defaults
            # are exposed.  Keep an invalid deferred setting from escaping this
            # event handler, and do not leave a stale selection from another
            # provider visible.  Do not replace its options with an empty
            # non-blank Select: Textual rejects that invalid widget state while
            # this screen is mounting.
            selector.display = False
            label.display = False
            self._status(f"Cannot load permission default for provider {provider!r}: {exc}")
            return False
        default = values.get("permission_mode", "dontAsk")
        selector.set_options(
            [
                (f"Provider/YAML default ({default})", _PROVIDER_DEFAULT_PERMISSION),
                *[(mode, mode) for mode in _PERMISSION_MODES],
            ],
            allow_blank=False,
        )
        selector.value = (
            permission_mode
            if permission_mode is not None
            else _PROVIDER_DEFAULT_PERMISSION
        )
        return True

    def _create(self) -> None:
        context = self._preset_context(require_selected=False)
        if context is None:
            return
        repository, role, name = context
        try:
            self.controller.save_agent_preset(repository, role, name, self._preset())
        except Exception as exc:
            self._status(f"Could not create preset: {exc}")
            return
        self._reload_role(name, f"Created {role.value} preset {name!r}.")

    def _update(self) -> None:
        context = self._preset_context(require_selected=True, require_name=False)
        if context is None:
            return
        repository, role, _ = context
        assert self._selected_preset is not None
        try:
            self.controller.save_agent_preset(
                repository, role, self._selected_preset, self._preset(), overwrite=True
            )
        except Exception as exc:
            self._status(f"Could not update preset: {exc}")
            return
        self._reload_role(
            self._selected_preset,
            f"Updated {role.value} preset {self._selected_preset!r}.",
        )

    def _rename(self) -> None:
        context = self._preset_context(require_selected=True)
        if context is None:
            return
        repository, role, name = context
        assert self._selected_preset is not None
        try:
            self.controller.rename_agent_preset(repository, role, self._selected_preset, name)
        except Exception as exc:
            self._status(f"Could not rename preset: {exc}")
            return
        self._reload_role(name, f"Renamed {role.value} preset to {name!r}.")

    def _delete(self) -> None:
        context = self._preset_context(require_selected=True, require_name=False)
        if context is None:
            return
        repository, role, _ = context
        assert self._selected_preset is not None
        name = self._selected_preset
        try:
            self.controller.delete_agent_preset(repository, role, name)
        except Exception as exc:
            self._status(f"Could not delete preset: {exc}")
            return
        self._reload_role(None, f"Deleted {role.value} preset {name!r}.")

    def _save_fallback(self) -> None:
        repository = self._repository()
        role = self._role()
        value = self.query_one("#agent-fallback-preset", PrchefSelect).value
        if repository is None or role is None:
            self._status("Choose a repository and role first.")
            return
        preset_name = value if isinstance(value, str) else None
        try:
            self.controller.save_agent_fallback_preset(repository, role, preset_name)
        except Exception as exc:
            self._status(f"Could not save fallback preset: {exc}")
            return
        self._status(
            f"{role.value.title()} fallback set to {preset_name!r}."
            if preset_name is not None
            else f"{role.value.title()} fallback disabled."
        )

    def _reload_role(self, selected: str | None, message: str) -> None:
        role = self._role()
        if role is None:
            return
        self._load_role(role)
        if selected is not None:
            selector = self.query_one("#agent-preset-select", PrchefSelect)
            self._suppressed_preset_changes.add(selected)
            selector.value = selected
            self._load_preset(selected)
        self._status(message)

    def _preset_context(
        self, *, require_selected: bool, require_name: bool = True
    ) -> tuple[str, AgentRole, str] | None:
        repository = self._repository()
        role = self._role()
        name = self.query_one("#agent-preset-name", Input).value.strip()
        if repository is None or role is None:
            self._status("Choose a repository and role first.")
            return None
        if require_selected and self._selected_preset is None:
            self._status("Select an existing preset first.")
            return None
        if require_name and not name:
            self._status("Enter a preset name first.")
            return None
        return repository, role, name

    def _preset(self) -> AgentPresetConfig:
        provider = self.query_one("#agent-preset-provider", PrchefSelect).value
        if not isinstance(provider, str):
            raise ValueError("Choose a provider first")
        original = self._selected_agent_preset()
        generation = dict(original.generation) if original is not None else {}
        for key, control in (("temperature", "temperature"),
                             ("max_output_tokens", "max-output-tokens")):
            if not self.query_one(f"#agent-preset-{control}", Input).display:
                continue
            value = self._optional(control)
            if value is None:
                generation.pop(key, None)
            else:
                generation[key] = value
        return AgentPresetConfig(
            provider=provider,
            model=self._optional("model"),
            reasoning_effort=self._optional("reasoning"),
            permission_mode=self._permission_mode(),
            generation=generation,
        )

    def _clear_values(self) -> None:
        for name in ("model", "reasoning", "temperature", "max-output-tokens"):
            self.query_one(f"#agent-preset-{name}", Input).value = ""
        selector = self.query_one("#agent-preset-permission", PrchefSelect)
        if _PROVIDER_DEFAULT_PERMISSION in selector._legal_values:
            selector.value = _PROVIDER_DEFAULT_PERMISSION

    def _selected_agent_preset(self) -> AgentPresetConfig | None:
        """Return the currently edited preset, if it still exists."""

        role = self._role()
        repository = self._repository()
        if self._selected_preset is None or role is None or repository is None:
            return None
        return self.controller.agent_presets(repository, role).get(self._selected_preset)

    def _optional(self, name: str) -> str | None:
        widget = self.query_one(f"#agent-preset-{name}", Input)
        if not widget.display:
            return None
        return widget.value.strip() or None

    def _permission_mode(self) -> _PermissionMode | None:
        selector = self.query_one("#agent-preset-permission", PrchefSelect)
        if not selector.display:
            return None
        value = selector.value
        return cast(_PermissionMode, value) if value in _PERMISSION_MODES else None

    def _repository(self) -> str | None:
        value = self.query_one("#agent-presets-repository", PrchefSelect).value
        return value if isinstance(value, str) else None

    def _role(self) -> AgentRole | None:
        value = self.query_one("#agent-preset-role", PrchefSelect).value
        return AgentRole(value) if isinstance(value, str) else None

    @staticmethod
    def _text(value: Any) -> str:
        return "" if value is None else str(value)

    def _status(self, message: str) -> None:
        self.query_one("#agent-presets-status", Static).update(message)
