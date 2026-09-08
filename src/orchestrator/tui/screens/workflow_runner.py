"""Workflow launch and live-progress screen."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus, Resize
from textual.screen import Screen
from textual.widgets import Button, Input, RichLog, Static, TextArea

from orchestrator.config.project_config import AgentPresetConfig
from orchestrator.models.agent import AgentRole
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.workflow_event import WorkflowEvent, WorkflowEventKind
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import WorkflowStage, WorkflowStatus
from orchestrator.runtime_logging import redact_payload, redact_text
from orchestrator.tui.bridge import WorkflowController, WorkflowLaunch
from orchestrator.tui.screens.task_selector import TaskSelector
from orchestrator.tui.widgets import (
    OrchestratorSelect,
    RepositorySelect,
    is_orchestrator_select_widget,
)

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


_MANUAL_PRESET = object()


class WorkflowRunnerScreen(Screen[None]):
    """Collect compact launch selections and observe the workflow service."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self._event_lines: list[str] = []
        self._raw_event_lines: list[str] = []
        self._active_timeline_stages: dict[WorkflowStage, int] = {}
        self._stage_attempts: dict[WorkflowStage, int] = {}
        self._show_raw_events = False
        self._role_providers: dict[AgentRole, str] = {}
        self._role_default_providers: dict[AgentRole, str] = {}
        self._role_presets: dict[AgentRole, dict[str, AgentPresetConfig]] = {}
        self._selected_presets: dict[AgentRole, str] = {}
        self._suppressed_preset_changes: set[tuple[AgentRole, str]] = set()
        self._suppressed_manual_preset_changes: set[AgentRole] = set()
        self._runtime_inputs: dict[tuple[AgentRole, str], dict[str, Any]] = {}
        self._runtime_defaults: dict[tuple[AgentRole, str], dict[str, Any]] = {}
        self._advanced_open: dict[AgentRole, bool] = {}
        self._task_load_token = 0
        self._suppressed_task_provider_change: tuple[str, str] | None = None
        self._active_execution_id: str | None = None

    def compose(self) -> ComposeResult:
        # One scroll context keeps a keyboard-focused control visible on small terminals.
        with VerticalScroll(id="workflow-runner"):
            yield Static("Start workflow", classes="screen-title")
            with Vertical(classes="workflow-form"):
                yield Static("Repository", classes="field-label")
                yield RepositorySelect(id="repository")
                yield Static("Task provider", classes="field-label")
                yield OrchestratorSelect([], prompt="Task provider", id="task-provider")
                yield TaskSelector(id="task-selector")
                yield Static("", id="github-app-summary")
                # Launch and navigation remain usable before the optional role
                # configuration sections, even on a short terminal.
                with Horizontal(classes="screen-actions navigation-actions"):
                    yield Button(
                        "Start workflow",
                        variant="success",
                        id="start-workflow",
                        classes="navigation-control",
                    )
                    yield Button(
                        "Execution history",
                        id="workflow-history",
                        classes="navigation-control",
                    )
                    yield Button(
                        "Agent presets",
                        id="workflow-agent-presets",
                        classes="navigation-control",
                    )
                    yield Button(
                        "GitHub Apps",
                        id="workflow-github-apps",
                        classes="navigation-control",
                    )
                    yield Button(
                        "Agent instructions",
                        id="workflow-agent-instructions",
                        classes="navigation-control",
                    )
                yield Static(
                    "Agent settings are temporary for this run; "
                    "Provider/YAML defaults are unchanged.",
                    id="runtime-settings-note",
                )
                for role, prefix in self._role_fields():
                    with Vertical(classes="role-section", id=f"{prefix}-section"):
                        yield Static(role.value.title(), classes="role-title", id=f"{prefix}-title")
                        with Horizontal(classes="role-summary-row"):
                            yield Static("", id=f"{prefix}-summary")
                            yield OrchestratorSelect(
                                [],
                                prompt="Provider/YAML default",
                                id=f"{prefix}-preset",
                                classes="role-preset-select",
                            )
                            yield Button("Configure", id=f"{prefix}-configure")
                        with Vertical(classes="role-config", id=f"{prefix}-config"):
                            yield Static(
                                "Provider/YAML default",
                                classes="default-indicator",
                                id=f"{prefix}-preset-source",
                            )
                            with Horizontal(classes="role-config-row"):
                                with Vertical(classes="role-field"):
                                    yield Static("Provider", id=f"{prefix}-provider-label")
                                    yield OrchestratorSelect(
                                        [],
                                        prompt=f"{role.value.title()} provider",
                                        id=f"{prefix}-provider",
                                    )
                                with Vertical(classes="role-field"):
                                    yield Static("Model (free-form)", id=f"{prefix}-model-label")
                                    yield Input(
                                        placeholder="Provider/YAML default",
                                        id=f"{prefix}-model",
                                    )
                            with Horizontal(classes="role-config-row"):
                                with Vertical(classes="role-field"):
                                    yield Static("Reasoning effort", id=f"{prefix}-reasoning-label")
                                    yield Input(
                                        placeholder="Provider/YAML default",
                                        id=f"{prefix}-reasoning",
                                    )
                                with Vertical(classes="role-field role-advanced-action"):
                                    yield Button("Advanced ▸", id=f"{prefix}-advanced-toggle")
                            with Vertical(classes="role-advanced", id=f"{prefix}-advanced"):
                                with Horizontal(classes="role-config-row"):
                                    with Vertical(classes="role-field"):
                                        yield Static(
                                            "Temperature", id=f"{prefix}-temperature-label"
                                        )
                                        yield Input(
                                            placeholder="Provider default",
                                            id=f"{prefix}-temperature",
                                        )
                                        yield Static(
                                            "",
                                            classes="default-indicator",
                                            id=f"{prefix}-temperature-source",
                                        )
                                    with Vertical(classes="role-field"):
                                        yield Static(
                                            "Max output tokens",
                                            id=f"{prefix}-max-output-tokens-label",
                                        )
                                        yield Input(
                                            placeholder="Provider default",
                                            id=f"{prefix}-max-output-tokens",
                                        )
                                        yield Static(
                                            "",
                                            classes="default-indicator",
                                            id=f"{prefix}-max-output-tokens-source",
                                        )
            yield Static("No workflow running.", id="workflow-progress")
            with Vertical(id="human-input-panel"):
                yield Static("Human input required", id="human-input-title")
                yield Static("", id="human-input-prompt")
                yield TextArea("", id="human-response")
                with Horizontal(classes="screen-actions"):
                    yield Button(
                        "Resume workflow",
                        variant="primary",
                        id="resume-workflow",
                        disabled=True,
                    )
                    yield Button("Return later", id="return-later")
            yield Static("", id="workflow-events")
            with Horizontal(classes="workflow-log-actions"):
                yield Button("Show detailed logs", id="toggle-workflow-logs")
            yield RichLog(id="workflow-raw-events", wrap=True, markup=False)

    @property
    def controller(self) -> WorkflowController:
        return cast("OrchestratorApp", self.app).controller

    def on_mount(self) -> None:
        self._set_layout_mode(self.size.width)
        try:
            repositories = self.controller.repositories()
        except Exception as exc:
            self.query_one("#workflow-progress", Static).update(
                f"Cannot load configured repositories: {exc}"
            )
            return
        repository_select = self.query_one("#repository", RepositorySelect)
        repository_select.focus()
        if repositories:
            repository_select.set_repositories(
                repositories, selected_repository=self.controller.selected_repository
            )
        else:
            # Textual cannot create a non-blank Select without choices. Keep the
            # empty-state selector usable while reporting the configuration issue.
            repository_select.set_options([], allow_blank=True)
            self.query_one("#workflow-progress", Static).update(
                "No repositories are configured. Add entries under repositories "
                "in orchestrator.yml."
            )

    def on_resize(self, event: Resize) -> None:
        self._set_layout_mode(event.size.width)

    def _set_layout_mode(self, width: int) -> None:
        self.set_class(width <= 80, "-narrow")
        self.set_class(width > 80, "-wide")
        self.set_class(width <= 96, "-compact-navigation")

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        # A mouse press focuses a button before Textual dispatches its matching
        # release/click. Scrolling here can move that button out from under the
        # pointer, which turns the first click into a focus-only click. Selects
        # and inputs still scroll into view so their keyboard
        # interaction remain usable on short terminals.
        if isinstance(event.widget, Button) or is_orchestrator_select_widget(event.widget):
            return
        self.query_one("#workflow-runner", VerticalScroll).scroll_to_widget(
            event.widget, animate=False, top=True
        )

    def on_select_changed(self, event: OrchestratorSelect.Changed) -> None:
        if event.select.id == "repository" and isinstance(event.value, str):
            self.controller.select_repository(event.value)
            self._load_repository_options(event.value)
            return
        if event.select.id == "task-provider" and isinstance(event.value, str):
            repository = self._selected("#repository")
            if self._suppressed_task_provider_change is not None:
                suppressed_change = self._suppressed_task_provider_change
                self._suppressed_task_provider_change = None
                if suppressed_change == (repository, event.value):
                    return
            if repository is not None:
                self._request_task_load(repository, event.value)
            return
        for role, prefix in self._role_fields():
            if event.select.id == f"{prefix}-preset":
                if isinstance(event.value, str):
                    suppressed_change = (role, event.value)
                    if suppressed_change in self._suppressed_preset_changes:
                        self._suppressed_preset_changes.remove(suppressed_change)
                        return
                    self._select_role_preset(role, prefix, event.value)
                elif event.value is _MANUAL_PRESET:
                    if role in self._suppressed_manual_preset_changes:
                        self._suppressed_manual_preset_changes.remove(role)
                        return
                    self._restore_role_defaults(role, prefix)
                return
            if event.select.id == f"{prefix}-provider" and isinstance(event.value, str):
                # Provider changes can be posted after a preset selection has
                # already changed the control again. Applying an older event
                # here would load that provider's defaults into the current
                # role, even though its selector now names a different one.
                if event.select.value != event.value:
                    return
                self._load_role_runtime_controls(role, prefix, event.value)
                self._clear_preset_if_provider_changed(role, prefix, event.value)
                self._update_preset_source(role, prefix)
                return

    def on_input_changed(self, event: Input.Changed) -> None:
        """Keep the compact summary and advanced override indicators current."""

        input_id = event.input.id
        for role, prefix in self._role_fields():
            if not input_id or not input_id.startswith(f"{prefix}-"):
                continue
            provider = self._selected(f"#{prefix}-provider")
            repository = self._selected("#repository")
            if provider is None or repository is None:
                return
            try:
                capabilities = self.controller.provider_runtime_capabilities(repository, provider)
            except Exception:
                return
            values = self._runtime_values_from_controls(prefix)
            self._clear_preset_if_runtime_changed(role, prefix)
            self._update_role_summary(role, prefix, provider, values, capabilities)
            self._update_advanced_indicators(role, prefix, provider, values)
            self._update_preset_source(role, prefix)
            return

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "human-response":
            self.query_one("#resume-workflow", Button).disabled = not event.text_area.text.strip()

    def _load_repository_options(self, repository: str) -> None:
        try:
            providers = self.controller.task_providers(repository)
            configured_agents = self.controller.configured_agent_providers(repository)
            selected_agents = self.controller.agent_providers(repository)
        except Exception as exc:
            self.notify(f"Could not resolve repository configuration: {exc}", severity="error")
            return
        if not providers:
            self.query_one("#task-selector", TaskSelector).clear_tasks()
            return
        task_provider_select = self.query_one("#task-provider", OrchestratorSelect)
        task_provider_select.set_options([(provider, provider) for provider in providers])
        self._suppressed_task_provider_change = (repository, providers[0])
        task_provider_select.value = providers[0]
        self.query_one("#task-selector", TaskSelector).clear_tasks()
        self._role_providers.clear()
        self._role_default_providers = selected_agents.copy()
        self._role_presets.clear()
        self._selected_presets.clear()
        self._runtime_inputs.clear()
        self._runtime_defaults.clear()
        self._advanced_open.clear()
        for role, prefix in self._role_fields():
            select = self.query_one(f"#{prefix}-provider", OrchestratorSelect)
            select.set_options([(provider, provider) for provider in configured_agents])
            select.value = selected_agents[role]
            self._load_role_runtime_controls(role, prefix, selected_agents[role])
            self._load_role_presets(role, prefix)
        self._load_github_app_summary(repository)
        self._request_task_load(repository, providers[0])

    @staticmethod
    def _role_fields() -> tuple[tuple[AgentRole, str], ...]:
        return (
            (AgentRole.PLANNER, "planner"),
            (AgentRole.IMPLEMENTER, "implementer"),
            (AgentRole.REVIEWER, "reviewer"),
        )

    def _load_role_presets(
        self, role: AgentRole, prefix: str, selected_name: str | None = None
    ) -> None:
        """Load only presets for this role and keep manual use available."""

        repository = self._selected("#repository")
        if repository is None:
            return
        try:
            presets = self.controller.agent_presets(repository, role)
        except Exception as exc:
            self._role_presets[role] = {}
            self.notify(f"Could not load {role.value} presets: {exc}", severity="error")
            return
        self._role_presets[role] = presets
        selector = self.query_one(f"#{prefix}-preset", OrchestratorSelect)
        # The explicit Manual option is the only fallback this selector needs.
        # Remove Textual's synthetic blank choice so the first option remains
        # the role-local manual reset and cannot be mistaken for a separate
        # preset selection.
        if selector.value is not _MANUAL_PRESET:
            self._suppressed_manual_preset_changes.add(role)
        selector.set_options(
            [
                ("Provider/YAML default (manual)", _MANUAL_PRESET),
                *[(name, name) for name in presets],
            ],
            allow_blank=False,
        )
        target = selected_name or self._selected_presets.get(role)
        if target is None:
            # A preset which exactly describes the configured role is the
            # useful default selection. Otherwise the explicit default choice
            # represents the existing Provider/YAML configuration.
            target = next(
                (
                    name
                    for name, preset in presets.items()
                    if self._preset_matches_controls(role, prefix, preset)
                ),
                None,
            )
        if target is not None and target in presets:
            # ``Select.value`` posts its Changed message asynchronously. This
            # is a programmatic refresh, so let the state established below
            # stand rather than allowing its delayed event to overwrite a name
            # the user has started entering for a rename.
            self._suppressed_preset_changes.add((role, target))
            selector.value = target
            self._selected_presets[role] = target
        else:
            selector.value = _MANUAL_PRESET
            self._selected_presets.pop(role, None)
        self._update_preset_source(role, prefix)

    def _select_role_preset(self, role: AgentRole, prefix: str, name: str) -> None:
        preset = self._role_presets.get(role, {}).get(name)
        if preset is None:
            self.notify(f"Unknown {role.value} preset: {name}", severity="warning")
            return
        self._selected_presets[role] = name
        repository = self._selected("#repository")
        if repository is None:
            return
        try:
            self.controller.provider_runtime_capabilities(repository, preset.provider)
        except Exception as exc:
            self.query_one(f"#{prefix}-preset-source", Static).update(
                f"Selected preset {name!r} references unavailable provider "
                f"{preset.provider!r}: {exc}"
            )
            return
        provider_select = self.query_one(f"#{prefix}-provider", OrchestratorSelect)
        provider_select.value = preset.provider
        self._load_role_runtime_controls(role, prefix, preset.provider)
        self._set_preset_runtime_controls(prefix, preset)
        self._update_preset_source(role, prefix)

    def _restore_role_defaults(self, role: AgentRole, prefix: str) -> None:
        """Return one role to its configured provider and runtime defaults."""

        self._selected_presets.pop(role, None)
        default_provider = self._role_default_providers.get(role)
        if default_provider is None:
            self.notify(
                f"Could not restore {role.value} defaults: no default provider is configured.",
                severity="warning",
            )
            return
        for key in [key for key in self._runtime_inputs if key[0] is role]:
            del self._runtime_inputs[key]
        for name in ("model", "reasoning", "temperature", "max-output-tokens"):
            self.query_one(f"#{prefix}-{name}", Input).value = ""
        provider_select = self.query_one(f"#{prefix}-provider", OrchestratorSelect)
        provider_select.value = default_provider
        self._load_role_runtime_controls(
            role, prefix, default_provider, reset_to_defaults=True
        )
        self._update_preset_source(role, prefix)

    def _clear_preset_if_provider_changed(
        self, role: AgentRole, prefix: str, provider: str
    ) -> None:
        selected = self._selected_presets.get(role)
        preset = self._role_presets.get(role, {}).get(selected) if selected else None
        if preset is not None and preset.provider != provider:
            self._select_manual_without_reset(role, prefix)

    def _clear_preset_if_runtime_changed(self, role: AgentRole, prefix: str) -> None:
        selected = self._selected_presets.get(role)
        preset = self._role_presets.get(role, {}).get(selected) if selected else None
        if preset is not None and not self._preset_matches_controls(role, prefix, preset):
            self._select_manual_without_reset(role, prefix)

    def _select_manual_without_reset(self, role: AgentRole, prefix: str) -> None:
        """Reflect an edit in the selector while retaining that edit for this run."""

        self._selected_presets.pop(role, None)
        selector = self.query_one(f"#{prefix}-preset", OrchestratorSelect)
        if selector.value is not _MANUAL_PRESET:
            self._suppressed_manual_preset_changes.add(role)
            selector.value = _MANUAL_PRESET

    def _set_preset_runtime_controls(self, prefix: str, preset: AgentPresetConfig) -> None:
        self.query_one(f"#{prefix}-model", Input).value = preset.model or ""
        self.query_one(f"#{prefix}-reasoning", Input).value = preset.reasoning_effort or ""
        generation = dict(preset.generation)
        for name in ("temperature", "max-output-tokens"):
            value = generation.get(name.replace("-", "_"))
            self.query_one(f"#{prefix}-{name}", Input).value = "" if value is None else str(value)

    def _preset_from_controls(self, role: AgentRole, prefix: str) -> AgentPresetConfig:
        provider = self._selected(f"#{prefix}-provider")
        if provider is None:
            raise ValueError(f"Select a provider for {role.value} first")
        options = self._runtime_options_for_role(role, prefix)
        generation = options.get("generation", {})
        if isinstance(generation, dict):
            generation = {key: value for key, value in generation.items() if value is not None}
        return AgentPresetConfig(
            provider=provider,
            model=options.get("model"),
            reasoning_effort=options.get("reasoning_effort"),
            generation=generation,
        )

    def _preset_matches_controls(
        self, role: AgentRole, prefix: str, preset: AgentPresetConfig
    ) -> bool:
        if self._selected(f"#{prefix}-provider") != preset.provider:
            return False
        current = self._preset_from_controls(role, prefix).model_dump(exclude_none=True)
        expected = preset.model_dump(exclude_none=True)
        return bool(
            self._preset_comparison_value(current)
            == self._preset_comparison_value(expected)
        )

    @staticmethod
    def _preset_comparison_value(value: Any) -> Any:
        """Compare persisted numeric generation values with their text inputs."""

        if isinstance(value, dict):
            return {
                key: WorkflowRunnerScreen._preset_comparison_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [WorkflowRunnerScreen._preset_comparison_value(item) for item in value]
        return None if value is None else str(value)

    def _update_preset_source(self, role: AgentRole, prefix: str) -> None:
        selected = self._selected_presets.get(role)
        source = "Provider/YAML default (manual)"
        if selected is not None:
            preset = self._role_presets.get(role, {}).get(selected)
            if preset is not None:
                repository = self._selected("#repository")
                if repository is not None:
                    try:
                        self.controller.provider_runtime_capabilities(repository, preset.provider)
                    except Exception as exc:
                        self.query_one(f"#{prefix}-preset-source", Static).update(
                            f"Selected preset {selected!r} references unavailable provider "
                            f"{preset.provider!r}: {exc}"
                        )
                        return
                try:
                    matches = self._preset_matches_controls(role, prefix, preset)
                except Exception:
                    matches = False
                source = (
                    f"Selected preset: {selected}"
                    if matches
                    else f"Selected preset: {selected}; temporary this-run overrides"
                )
        elif self._manual_runtime_overrides(role, prefix):
            source = "Manual configuration; temporary this-run overrides"
        self.query_one(f"#{prefix}-preset-source", Static).update(source)

    def _manual_runtime_overrides(self, role: AgentRole, prefix: str) -> bool:
        provider = self._selected(f"#{prefix}-provider")
        if provider is None:
            return False
        defaults = self._runtime_defaults.get((role, provider))
        if defaults is None:
            return False
        generation = defaults.get("generation", {})
        if not isinstance(generation, dict):
            generation = {}
        baseline = AgentPresetConfig(
            provider=provider,
            model=defaults.get("model"),
            reasoning_effort=defaults.get("reasoning_effort"),
            generation={key: value for key, value in generation.items() if value is not None},
        )
        return not self._preset_matches_controls(role, prefix, baseline)

    def _load_role_runtime_controls(
        self,
        role: AgentRole,
        prefix: str,
        provider: str,
        *,
        reset_to_defaults: bool = False,
    ) -> None:
        repository = self._selected("#repository")
        if repository is None:
            return
        previous = self._role_providers.get(role)
        if previous is not None and not reset_to_defaults:
            self._runtime_inputs[(role, previous)] = self._runtime_values_from_controls(prefix)
        try:
            capabilities = self.controller.provider_runtime_capabilities(repository, provider)
            values = None if reset_to_defaults else self._runtime_inputs.get((role, provider))
            if values is None:
                values = self.controller.provider_runtime_values(repository, provider)
        except Exception as exc:
            self.notify(f"Could not load provider runtime settings: {exc}", severity="error")
            return
        self._role_providers[role] = provider
        self._runtime_defaults.setdefault((role, provider), self._copy_runtime_values(values))
        self._set_runtime_control(prefix, "model", capabilities.supports_model, values.get("model"))
        self._set_runtime_control(
            prefix,
            "reasoning",
            capabilities.supports_reasoning_effort,
            values.get("reasoning_effort"),
        )
        generation = values.get("generation", {})
        generation = generation if isinstance(generation, dict) else {}
        self._set_runtime_control(
            prefix, "temperature", capabilities.supports_generation, generation.get("temperature")
        )
        self._set_runtime_control(
            prefix,
            "max-output-tokens",
            capabilities.supports_generation,
            generation.get("max_output_tokens"),
        )
        self.query_one(f"#{prefix}-advanced-toggle", Button).display = (
            capabilities.supports_generation
        )
        self._set_advanced_visibility(role, prefix, capabilities.supports_generation)
        control_values = self._runtime_values_from_controls(prefix)
        self._update_role_summary(role, prefix, provider, control_values, capabilities)
        self._update_advanced_indicators(role, prefix, provider, control_values)

    def _set_runtime_control(self, prefix: str, name: str, visible: bool, value: Any) -> None:
        self.query_one(f"#{prefix}-{name}-label", Static).display = visible
        input_widget = self.query_one(f"#{prefix}-{name}", Input)
        input_widget.display = visible
        if visible:
            input_widget.value = "" if value is None else str(value)

    @staticmethod
    def _copy_runtime_values(values: dict[str, Any]) -> dict[str, Any]:
        generation = values.get("generation", {})
        return {
            "model": values.get("model"),
            "reasoning_effort": values.get("reasoning_effort"),
            "generation": dict(generation) if isinstance(generation, dict) else {},
        }

    def _runtime_values_from_controls(self, prefix: str) -> dict[str, Any]:
        return {
            "model": self.query_one(f"#{prefix}-model", Input).value.strip() or None,
            "reasoning_effort": self.query_one(f"#{prefix}-reasoning", Input).value.strip() or None,
            "generation": {
                "temperature": self.query_one(f"#{prefix}-temperature", Input).value.strip()
                or None,
                "max_output_tokens": self.query_one(
                    f"#{prefix}-max-output-tokens", Input
                ).value.strip()
                or None,
            },
        }

    def _runtime_options_for_role(self, role: AgentRole, prefix: str) -> dict[str, Any]:
        provider = self._selected(f"#{prefix}-provider")
        repository = self._selected("#repository")
        if provider is None or repository is None:
            return {}
        capabilities = self.controller.provider_runtime_capabilities(repository, provider)
        values = self._runtime_values_from_controls(prefix)
        options: dict[str, Any] = {}
        if capabilities.supports_model:
            options["model"] = values["model"]
        if capabilities.supports_reasoning_effort:
            options["reasoning_effort"] = values["reasoning_effort"]
        if capabilities.supports_generation:
            options["generation"] = values["generation"]
        return options

    def _update_role_summary(
        self, role: AgentRole, prefix: str, provider: str, values: dict[str, Any], capabilities: Any
    ) -> None:
        selected_preset = self._selected_presets.get(role)
        if selected_preset is not None:
            preset = self._role_presets.get(role, {}).get(selected_preset)
            provider_note = (
                f"preset: {selected_preset}"
                if preset is not None and self._preset_matches_controls(role, prefix, preset)
                else f"preset: {selected_preset}; this-run override"
            )
        else:
            if self._role_default_providers.get(role) != provider:
                provider_note = "this-run provider override"
            elif self._manual_runtime_overrides(role, prefix):
                provider_note = "this-run runtime override"
            else:
                provider_note = "Provider/YAML default"
        parts = [f"{provider} ({provider_note})"]
        if capabilities.supports_model:
            parts.append(str(values.get("model") or "Provider default"))
        if capabilities.supports_reasoning_effort:
            parts.append(str(values.get("reasoning_effort") or "Provider default"))
        self.query_one(f"#{prefix}-summary", Static).update(" · ".join(parts))

    def _update_advanced_indicators(
        self, role: AgentRole, prefix: str, provider: str, values: dict[str, Any]
    ) -> None:
        defaults = self._runtime_defaults.get((role, provider), {})
        default_generation = defaults.get("generation", {})
        generation = values.get("generation", {})
        if not isinstance(default_generation, dict) or not isinstance(generation, dict):
            return
        for name, control_name in (
            ("temperature", "temperature"),
            ("max_output_tokens", "max-output-tokens"),
        ):
            current = generation.get(name)
            configured = default_generation.get(name)
            source = "Provider/YAML default"
            is_default = (current is None and configured is None) or (
                current is not None and configured is not None and str(current) == str(configured)
            )
            if not is_default:
                default_text = configured if configured is not None else "Provider default"
                source = f"This-run override (default: {default_text})"
            self.query_one(f"#{prefix}-{control_name}-source", Static).update(source)

    def _set_advanced_visibility(self, role: AgentRole, prefix: str, supported: bool) -> None:
        advanced = self.query_one(f"#{prefix}-advanced", Vertical)
        expanded = supported and self._advanced_open.get(role, False)
        advanced.display = expanded
        toggle = self.query_one(f"#{prefix}-advanced-toggle", Button)
        toggle.label = "Advanced ▾" if expanded else "Advanced ▸"

    def _load_github_app_summary(self, repository: str) -> None:
        try:
            implementer, reviewer = self.controller.github_app_settings(repository)
        except Exception as exc:
            self.query_one("#github-app-summary", Static).update(
                f"GitHub Apps: could not inspect configured defaults ({exc})"
            )
            return

        self.query_one("#github-app-summary", Static).update(
            self.github_app_summary(implementer, reviewer)
        )

    @staticmethod
    def github_app_summary(implementer: Any, reviewer: Any) -> str:
        """Return a launcher-safe summary without exposing App identifiers."""

        implementer_status = "✓" if implementer.is_configured else "—"
        reviewer_status = "✓" if reviewer.is_configured else "—"
        return f"GitHub Apps: Implementer {implementer_status} · Reviewer {reviewer_status}"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh-tasks":
            repository = self._selected("#repository")
            provider = self._selected("#task-provider")
            if repository is None or provider is None:
                self.notify("Choose a repository and task provider first.", severity="warning")
                return
            self._request_task_load(repository, provider)
        elif event.button.id == "start-workflow":
            self._start_selected_workflow()
        elif event.button.id == "resume-workflow":
            self._resume_active_workflow()
        elif event.button.id == "return-later":
            self._hide_human_panel()
            self.notify("The execution remains available in execution history.")
        elif event.button.id == "workflow-history":
            self._show_history()
        elif event.button.id == "workflow-agent-presets":
            from orchestrator.tui.screens.agent_presets import AgentPresetsScreen

            self.app.push_screen(AgentPresetsScreen(self._selected("#repository")))
        elif event.button.id == "workflow-github-apps":
            from orchestrator.tui.screens.github_app_settings import GitHubAppSettingsScreen

            self.app.push_screen(GitHubAppSettingsScreen())
        elif event.button.id == "workflow-agent-instructions":
            from orchestrator.tui.screens.agent_instructions import AgentInstructionsScreen

            self.app.push_screen(
                AgentInstructionsScreen(self._selected("#repository"))
            )
        elif event.button.id == "toggle-workflow-logs":
            self._show_raw_events = not self._show_raw_events
            raw_events = self.query_one("#workflow-raw-events", RichLog)
            raw_events.display = self._show_raw_events
            if self._show_raw_events:
                # A hidden RichLog has no usable width. Replay after it is
                # visible so Textual can lay out retained diagnostics.
                self.call_after_refresh(self._render_raw_events)
            event.button.label = (
                "Hide detailed logs" if self._show_raw_events else "Show detailed logs"
            )
        else:
            for role, prefix in self._role_fields():
                if event.button.id == f"{prefix}-configure":
                    config = self.query_one(f"#{prefix}-config", Vertical)
                    config.display = not config.display
                    event.button.label = "Hide configuration" if config.display else "Configure"
                    return
                if event.button.id == f"{prefix}-advanced-toggle":
                    self._advanced_open[role] = not self._advanced_open.get(role, False)
                    provider = self._selected(f"#{prefix}-provider")
                    repository = self._selected("#repository")
                    if provider is not None and repository is not None:
                        capabilities = self.controller.provider_runtime_capabilities(
                            repository, provider
                        )
                        self._set_advanced_visibility(
                            role, prefix, capabilities.supports_generation
                        )
                    return

    def _show_history(self) -> None:
        from orchestrator.tui.screens.execution_history import ExecutionHistoryScreen

        self.app.push_screen(ExecutionHistoryScreen())

    def _request_task_load(self, repository: str, provider: str) -> None:
        self._task_load_token += 1
        token = self._task_load_token
        self.query_one("#task-selector", TaskSelector).set_tasks_loading(provider=provider)
        self._load_tasks(repository, provider, token)

    @work(thread=True, exclusive=True)
    def _load_tasks(self, repository: str, provider: str, token: int) -> None:
        try:
            tasks = self.controller.list_tasks(repository=repository, task_provider=provider)
        except Exception as exc:
            self.app.call_from_thread(self._task_loading_failed, str(exc), token)
            return
        self.app.call_from_thread(self._set_available_tasks, tasks, provider, token)

    def _set_available_tasks(self, tasks: list[DevelopmentTask], provider: str, token: int) -> None:
        if token == self._task_load_token:
            self.query_one("#task-selector", TaskSelector).set_tasks(tasks, provider=provider)

    def _task_loading_failed(self, message: str, token: int) -> None:
        if token == self._task_load_token:
            self.query_one("#task-selector", TaskSelector).set_error(message)

    def _start_selected_workflow(self) -> None:
        repository = self._selected("#repository")
        task_provider = self._selected("#task-provider")
        task_id = self.query_one("#task-selector", TaskSelector).selected_task_id
        provider_selections = {
            role: self._selected(f"#{prefix}-provider") for role, prefix in self._role_fields()
        }
        if repository is None or task_provider is None or task_id is None:
            self.notify(
                "Select a repository, provider, and task before starting.", severity="warning"
            )
            return
        if any(value is None for value in provider_selections.values()):
            self.notify("Select an agent provider for every workflow role.", severity="warning")
            return
        launch = WorkflowLaunch(
            repository=repository,
            task_provider=task_provider,
            task_id=task_id,
            agent_providers={
                role: value for role, value in provider_selections.items() if value is not None
            },
            agent_runtime_options={
                role: self._runtime_options_for_role(role, prefix)
                for role, prefix in self._role_fields()
            },
        )
        self.query_one("#start-workflow", Button).disabled = True
        self.query_one("#workflow-progress", Static).update("Starting workflow…")
        self._event_lines.clear()
        self._raw_event_lines.clear()
        self._active_timeline_stages.clear()
        self._stage_attempts.clear()
        self.query_one("#workflow-events", Static).update("")
        self.query_one("#workflow-raw-events", RichLog).clear()
        self._hide_human_panel()
        self._run_workflow(launch)

    @work(thread=True, exclusive=True)
    def _run_workflow(self, launch: WorkflowLaunch) -> None:
        try:
            execution = self.controller.start_workflow(
                launch, event_listener=self._on_workflow_event
            )
        except Exception as exc:
            self.app.call_from_thread(self._workflow_failed, str(exc))
            return
        self.app.call_from_thread(self._workflow_finished, execution)

    def _on_workflow_event(self, event: WorkflowEvent) -> None:
        self.app.call_from_thread(self._render_workflow_event, event)

    def _render_workflow_event(self, event: WorkflowEvent) -> None:
        self._append_raw_event(event)
        message = self._timeline_message(event)
        if message is None:
            return
        self.query_one("#workflow-progress", Static).update(message)

        if event.kind == WorkflowEventKind.STAGE_STARTED and event.stage is not None:
            self._stage_attempts[event.stage] = self._stage_attempts.get(event.stage, 0) + 1
            self._active_timeline_stages[event.stage] = len(self._event_lines)
            self._event_lines.append(message)
        elif event.kind in {
            WorkflowEventKind.STAGE_FINISHED,
            WorkflowEventKind.STAGE_FAILED,
        } and event.stage is not None:
            active_line = self._active_timeline_stages.pop(event.stage, None)
            if active_line is None:
                self._event_lines.append(message)
            else:
                self._event_lines[active_line] = message
        elif event.kind == WorkflowEventKind.WORKFLOW_FINISHED:
            self._event_lines.append(message)

        self.query_one("#workflow-events", Static).update("\n".join(self._event_lines[-10:]))

    def _append_raw_event(self, event: WorkflowEvent) -> None:
        """Retain observer diagnostics without putting them in the primary timeline."""

        details = [event.kind.value]
        if event.category:
            details.append(f"[{event.category}]")
        if event.stage is not None:
            details.append(f"stage={event.stage.value}")
        if event.status is not None:
            details.append(f"status={event.status.value}")
        if event.step_status is not None:
            details.append(f"step={event.step_status.value}")
        if event.message:
            details.append(redact_text(event.message))
        if event.payload is not None:
            details.append(
                json.dumps(redact_payload(event.payload), ensure_ascii=False, indent=2, default=str)
            )
        rendered_event = " | ".join(details)
        self._raw_event_lines.append(rendered_event)
        raw_events = self.query_one("#workflow-raw-events", RichLog)
        if self._show_raw_events:
            raw_events.write(rendered_event)

    def _render_raw_events(self) -> None:
        """Render retained diagnostics after the optional log becomes visible."""

        raw_events = self.query_one("#workflow-raw-events", RichLog)
        if not self._show_raw_events:
            return
        raw_events.clear()
        for line in self._raw_event_lines:
            raw_events.write(line)

    def _timeline_message(self, event: WorkflowEvent) -> str | None:
        """Translate persisted observer events into concise, user-facing progress."""

        if event.kind == WorkflowEventKind.EXECUTION_CREATED:
            return "Preparing workflow…"
        if event.kind == WorkflowEventKind.STATUS_CHANGED:
            return self._status_message(event.status)
        if event.kind == WorkflowEventKind.WORKFLOW_FINISHED:
            return self._status_message(event.status)
        if event.stage is None:
            return None

        stage = event.stage
        attempt = self._stage_attempts.get(stage, 0) + (
            1 if event.kind == WorkflowEventKind.STAGE_STARTED else 0
        )
        if event.kind == WorkflowEventKind.STAGE_STARTED:
            return f"● {self._stage_activity(stage, attempt)}{self._agent_stage_context(stage)}…"
        if event.kind == WorkflowEventKind.STAGE_FINISHED:
            return f"✓ {self._stage_complete(stage)}"
        if event.kind == WorkflowEventKind.STAGE_FAILED:
            detail = f": {event.message}" if event.message else ""
            return f"✗ {self._stage_activity(stage, attempt)} failed{detail}"
        return None

    def _agent_stage_context(self, stage: WorkflowStage) -> str:
        """Add selected agent runtime context only where an agent is active."""

        roles = {
            WorkflowStage.GENERATE_SPEC: AgentRole.PLANNER,
            WorkflowStage.IMPLEMENT: AgentRole.IMPLEMENTER,
            WorkflowStage.POLISH: AgentRole.IMPLEMENTER,
            WorkflowStage.REVIEW: AgentRole.REVIEWER,
        }
        role = roles.get(stage)
        if role is None:
            return ""
        provider = self._role_providers.get(role)
        if provider is None:
            return ""
        prefix = dict(self._role_fields())[role]
        model = self.query_one(f"#{prefix}-model", Input).value.strip()
        details = [provider]
        if model:
            details.append(model)
        return f" ({' · '.join(details)})"

    @staticmethod
    def _status_message(status: WorkflowStatus | None) -> str:
        if status is None:
            return "Workflow status updated"
        messages: dict[WorkflowStatus, str] = {
            WorkflowStatus.PENDING: "Preparing workflow…",
            WorkflowStatus.RUNNING: "Workflow running…",
            WorkflowStatus.APPROVED: "✓ Workflow approved",
            WorkflowStatus.CHANGES_REQUESTED: "• Review changes requested",
            WorkflowStatus.NEEDS_HUMAN: "! Waiting for human input",
            WorkflowStatus.FAILED: "✗ Workflow failed",
        }
        return messages.get(status, "Workflow status updated")

    @staticmethod
    def _stage_activity(stage: WorkflowStage, attempt: int) -> str:
        retry_labels = {
            WorkflowStage.IMPLEMENT: "Implementing corrections",
            WorkflowStage.VALIDATE: "Re-validating changes",
            WorkflowStage.REVIEW: "Reviewing corrections",
        }
        activities = {
            WorkflowStage.LOAD_TASK: "Loading task",
            WorkflowStage.LOAD_CONFIG: "Configuring repository",
            WorkflowStage.GENERATE_SPEC: "Generating specification",
            WorkflowStage.PREPARE_BRANCH: "Preparing branch",
            WorkflowStage.IMPLEMENT: "Implementing changes",
            WorkflowStage.POLISH: "Applying polish suggestions",
            WorkflowStage.VALIDATE: "Validating changes",
            WorkflowStage.COMMIT: "Committing changes",
            WorkflowStage.PUSH: "Pushing branch",
            WorkflowStage.CREATE_PR: "Creating pull request",
            WorkflowStage.REVIEW: "Reviewing pull request",
            WorkflowStage.COMPLETED: "Completing workflow",
        }
        if attempt > 1:
            return retry_labels.get(stage, activities[stage])
        return activities[stage]

    @staticmethod
    def _stage_complete(stage: WorkflowStage) -> str:
        return {
            WorkflowStage.LOAD_TASK: "Task loaded",
            WorkflowStage.LOAD_CONFIG: "Repository configured",
            WorkflowStage.GENERATE_SPEC: "Specification generated",
            WorkflowStage.PREPARE_BRANCH: "Branch prepared",
            WorkflowStage.IMPLEMENT: "Changes implemented",
            WorkflowStage.POLISH: "Polish pass completed",
            WorkflowStage.VALIDATE: "Validation passed",
            WorkflowStage.COMMIT: "Changes committed",
            WorkflowStage.PUSH: "Branch pushed",
            WorkflowStage.CREATE_PR: "Pull request created",
            WorkflowStage.REVIEW: "Review completed",
            WorkflowStage.COMPLETED: "Workflow completed",
        }[stage]

    def _workflow_finished(self, execution: WorkflowExecution) -> None:
        self._active_execution_id = execution.id
        self.query_one("#start-workflow", Button).disabled = False
        self.query_one("#workflow-progress", Static).update(
            self._status_message(execution.status)
        )
        self.notify(self._status_message(execution.status))
        if execution.status is WorkflowStatus.NEEDS_HUMAN:
            self._show_human_panel(execution)
        else:
            self._hide_human_panel()

    def _show_human_panel(self, execution: WorkflowExecution) -> None:
        interaction = next(
            (item for item in reversed(execution.human_interactions) if item.awaiting_response),
            None,
        )
        if interaction is None:
            return
        role = {
            WorkflowStage.GENERATE_SPEC: "Planner",
            WorkflowStage.REVIEW: "Reviewer",
        }.get(
            interaction.requesting_stage,
            interaction.requesting_stage.value.replace("_", " ").title(),
        )
        self.query_one("#human-input-title", Static).update(f"{role} needs clarification")
        self.query_one("#human-input-prompt", Static).update(interaction.prompt)
        response = self.query_one("#human-response", TextArea)
        response.text = ""
        self.query_one("#resume-workflow", Button).disabled = True
        self.query_one("#human-input-panel", Vertical).display = True
        response.focus()

    def _hide_human_panel(self) -> None:
        self.query_one("#human-input-panel", Vertical).display = False

    def _resume_active_workflow(self) -> None:
        if self._active_execution_id is None:
            return
        response = self.query_one("#human-response", TextArea).text.strip()
        if not response:
            return
        self.query_one("#resume-workflow", Button).disabled = True
        self.query_one("#workflow-progress", Static).update("Resuming workflow…")
        self._resume_workflow(self._active_execution_id, response)

    @work(thread=True, exclusive=True)
    def _resume_workflow(self, execution_id: str, response: str) -> None:
        try:
            execution = self.controller.respond_and_resume(
                execution_id, response, event_listener=self._on_workflow_event
            )
        except Exception as exc:
            self.app.call_from_thread(self._workflow_failed, str(exc))
            return
        self.app.call_from_thread(self._workflow_finished, execution)

    def _workflow_failed(self, message: str) -> None:
        self.query_one("#start-workflow", Button).disabled = False
        self.query_one("#workflow-progress", Static).update(f"Could not start workflow: {message}")
        self.notify(f"Could not start workflow: {message}", severity="error")

    def _selected(self, selector: str) -> str | None:
        value = self.query_one(selector, OrchestratorSelect).value
        return value if isinstance(value, str) else None
