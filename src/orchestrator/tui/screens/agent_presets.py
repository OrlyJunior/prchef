"""Dedicated editor for reusable role-local agent presets."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from orchestrator.config.project_config import AgentPresetConfig
from orchestrator.models.agent import AgentRole
from orchestrator.tui.bridge import WorkflowController
from orchestrator.tui.widgets import (
    OrchestratorSelect,
    RepositorySelect,
    is_orchestrator_select_widget,
)

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


_NEW_PRESET = object()


class AgentPresetsScreen(Screen[None]):
    """Manage persisted planner, implementer, and reviewer preset templates."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    DEFAULT_CSS = """
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
                yield OrchestratorSelect(
                    [(role.value.title(), role.value) for role in AgentRole],
                    allow_blank=False,
                    prompt="Choose a role",
                    id="agent-preset-role",
                )
                yield Static("Preset", classes="field-label")
                yield OrchestratorSelect(
                    [("Create new preset", _NEW_PRESET)],
                    prompt="Choose a preset",
                    id="agent-preset-select",
                )
                yield Static("Preset name", classes="field-label")
                yield Input(placeholder="Name", id="agent-preset-name")
                with Horizontal(classes="role-config-row"):
                    with Vertical(classes="role-field"):
                        yield Static("Provider", id="agent-preset-provider-label")
                        yield OrchestratorSelect(
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
        return cast("OrchestratorApp", self.app).controller

    def on_mount(self) -> None:
        try:
            repositories = self.controller.repositories()
        except Exception as exc:
            self._status(f"Cannot load configured repositories: {exc}")
            return
        selector = self.query_one("#agent-presets-repository", RepositorySelect)
        if not repositories:
            selector.set_repositories(repositories)
            self._status("No repositories are configured in orchestrator.yml.")
            return
        selector.set_repositories(
            repositories,
            selected_repository=self._initial_repository or self.controller.selected_repository,
        )
        selector.focus()

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        if is_orchestrator_select_widget(event.widget):
            return
        self.query_one("#agent-presets", VerticalScroll).scroll_to_widget(
            event.widget, animate=False, top=True
        )

    def on_select_changed(self, event: OrchestratorSelect.Changed) -> None:
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
            self._load_provider_controls(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "create-agent-preset": self._create,
            "update-agent-preset": self._update,
            "rename-agent-preset": self._rename,
            "delete-agent-preset": self._delete,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()

    def _load_repository(self, repository: str) -> None:
        try:
            providers = self.controller.configured_agent_providers(repository)
        except Exception as exc:
            self._status(f"Cannot load agent providers: {exc}")
            return
        provider = self.query_one("#agent-preset-provider", OrchestratorSelect)
        provider.set_options([(name, name) for name in providers])
        role_selector = self.query_one("#agent-preset-role", OrchestratorSelect)
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
            providers = self.controller.configured_agent_providers(repository)
        except Exception as exc:
            self._status(f"Cannot load {role.value} presets: {exc}")
            return
        selector = self.query_one("#agent-preset-select", OrchestratorSelect)
        self._suppressed_preset_changes.add(_NEW_PRESET)
        selector.set_options(
            [("Create new preset", _NEW_PRESET), *[(name, name) for name in presets]]
        )
        self._selected_preset = None
        selector.value = _NEW_PRESET
        self._clear_values()
        provider = self.query_one("#agent-preset-provider", OrchestratorSelect)
        provider.set_options([(name, name) for name in providers])
        provider.value = default_provider
        self._load_provider_controls(default_provider)
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
        provider = self.query_one("#agent-preset-provider", OrchestratorSelect)
        try:
            provider.value = preset.provider
            self._load_provider_controls(preset.provider)
        except Exception as exc:
            self._status(
                f"Preset {name!r} references unavailable provider {preset.provider!r}: {exc}"
            )
            return
        self.query_one("#agent-preset-model", Input).value = preset.model or ""
        self.query_one("#agent-preset-reasoning", Input).value = preset.reasoning_effort or ""
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
        self._status("Enter a name and settings, then create the preset.")

    def _load_provider_controls(self, provider: str) -> None:
        repository = self._repository()
        if repository is None:
            return
        try:
            capabilities = self.controller.provider_runtime_capabilities(repository, provider)
        except Exception as exc:
            self._status(f"Cannot load provider settings: {exc}")
            return
        self._set_visible("model", capabilities.supports_model)
        self._set_visible("reasoning", capabilities.supports_reasoning_effort)
        self._set_visible("temperature", capabilities.supports_generation)
        self._set_visible("max-output-tokens", capabilities.supports_generation)

    def _set_visible(self, name: str, visible: bool) -> None:
        self.query_one(f"#agent-preset-{name}-label", Static).display = visible
        self.query_one(f"#agent-preset-{name}", Input).display = visible

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

    def _reload_role(self, selected: str | None, message: str) -> None:
        role = self._role()
        if role is None:
            return
        self._load_role(role)
        if selected is not None:
            selector = self.query_one("#agent-preset-select", OrchestratorSelect)
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
        provider = self.query_one("#agent-preset-provider", OrchestratorSelect).value
        if not isinstance(provider, str):
            raise ValueError("Choose a provider first")
        generation = {
            "temperature": self._optional("temperature"),
            "max_output_tokens": self._optional("max-output-tokens"),
        }
        return AgentPresetConfig(
            provider=provider,
            model=self._optional("model"),
            reasoning_effort=self._optional("reasoning"),
            generation={key: value for key, value in generation.items() if value is not None},
        )

    def _clear_values(self) -> None:
        for name in ("model", "reasoning", "temperature", "max-output-tokens"):
            self.query_one(f"#agent-preset-{name}", Input).value = ""

    def _optional(self, name: str) -> str | None:
        return self.query_one(f"#agent-preset-{name}", Input).value.strip() or None

    def _repository(self) -> str | None:
        value = self.query_one("#agent-presets-repository", OrchestratorSelect).value
        return value if isinstance(value, str) else None

    def _role(self) -> AgentRole | None:
        value = self.query_one("#agent-preset-role", OrchestratorSelect).value
        return AgentRole(value) if isinstance(value, str) else None

    @staticmethod
    def _text(value: Any) -> str:
        return "" if value is None else str(value)

    def _status(self, message: str) -> None:
        self.query_one("#agent-presets-status", Static).update(message)
