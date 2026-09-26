"""Workflow launch and live-progress screen."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, cast

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus, Resize
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, Input, Static, TextArea

from orchestrator.config.project_config import AgentPresetConfig
from orchestrator.models.agent import AgentRole
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.execution_diagnostic import DiagnosticCategory, ExecutionDiagnostic
from orchestrator.models.workflow_batch import WorkflowBatch
from orchestrator.models.workflow_event import WorkflowEvent, WorkflowEventKind
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import (
    ACTIVE_WORKFLOW_STATUSES,
    StepStatus,
    WorkflowStage,
    WorkflowStatus,
)
from orchestrator.runtime_logging import redact_payload, redact_text
from orchestrator.tui.batch_progress import LiveBatchProgress, build_persisted_rows, summarize
from orchestrator.tui.bridge import WorkflowBatchLaunch, WorkflowController, WorkflowLaunch
from orchestrator.tui.diagnostics import format_diagnostics_for_export
from orchestrator.tui.screens.task_selector import TaskSelector
from orchestrator.tui.widgets import (
    BatchItemRow,
    BatchProgressView,
    OrchestratorSelect,
    RepositorySelect,
    is_orchestrator_select_widget,
)
from orchestrator.tui.workflow_presentation import (
    STAGE_ROLES,
    stage_activity,
    stage_complete,
    status_message,
    step_timeline_lines,
    timeline_message,
    update_human_blocked_line,
)

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


_MANUAL_PRESET = object()
_PROVIDER_DEFAULT_PERMISSION = object()
_PERMISSION_MODES = ("dontAsk", "acceptEdits", "auto")
_PermissionMode = Literal["dontAsk", "acceptEdits", "auto"]
# The advanced controls expose exactly these generation options. Any other
# generation key a provider configures (`num_ctx`, `vertex_project`, ...) is
# inherited unchanged by every run, so it cannot distinguish one selection
# from another.
_CONTROLLED_GENERATION_OPTIONS = ("temperature", "max_output_tokens")
# Non-terminal states. All of them can still be stopped by the operator.
_CANCELLABLE_STATUSES = ACTIVE_WORKFLOW_STATUSES


class StopWorkflowConfirmation(ModalScreen[bool]):
    """Small guard against stopping a workflow by accident."""

    CSS = """
    StopWorkflowConfirmation { align: center middle; }
    #stop-workflow-confirmation { width: 60; height: auto; padding: 1 2; border: round $warning; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="stop-workflow-confirmation"):
            yield Static("Stop this workflow?", classes="screen-title")
            yield Static("The current worktree, branch, commits, and pull request will be kept.")
            with Horizontal(classes="screen-actions"):
                yield Button("Stop workflow", variant="error", id="confirm-stop-workflow")
                yield Button("Keep running", id="dismiss-stop-workflow")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-stop-workflow")


class FreshRunConfirmation(ModalScreen[tuple[str, ...] | None]):
    """Choose which previously run tasks get separate executions."""

    CSS = """
    FreshRunConfirmation { align: center middle; }
    #fresh-run-confirmation {
        width: 100%; max-width: 78; height: 80%;
        padding: 1 1; border: round $warning;
    }
    #fresh-run-content { width: 100%; height: 1fr; overflow-y: auto; }
    #fresh-run-actions { width: 100%; height: auto; }
    #fresh-run-actions Button { width: 100%; }
    """

    def __init__(
        self,
        previous: tuple[tuple[DevelopmentTask, WorkflowExecution], ...],
        new_count: int,
    ) -> None:
        super().__init__()
        self.previous = previous
        self.new_count = new_count

    def compose(self) -> ComposeResult:
        with Vertical(id="fresh-run-confirmation"):
            with VerticalScroll(id="fresh-run-content"):
                yield Static("Start fresh runs?", classes="screen-title")
                yield Static(
                    "Each checked task starts a new, separate execution. Earlier runs remain "
                    "in Execution history."
                )
                if any(
                    execution.status == WorkflowStatus.NEEDS_HUMAN
                    for _, execution in self.previous
                ):
                    yield Static(
                        "An earlier execution still has a pending question and remains open "
                        "for an answer. Continuing it could lead to a second PR."
                    )
                if any(
                    execution.status == WorkflowStatus.CHANGES_REQUESTED
                    and (execution.pr_url or execution.pull_request_snapshot)
                    for _, execution in self.previous
                ):
                    yield Static(
                        "An earlier changes-requested PR may still be open. Continuing it "
                        "could lead to a second PR."
                    )
                for index, (task, execution) in enumerate(self.previous):
                    yield Checkbox(
                        f"{task.id} [{execution.status.value}]: {task.title or '(untitled)'}",
                        value=True,
                        id=f"fresh-run-{index}",
                    )
                if self.new_count:
                    yield Static(
                        f"{self.new_count} task(s) with no earlier execution will also start."
                    )
            with Vertical(id="fresh-run-actions"):
                yield Button("Launch selected", variant="primary", id="confirm-fresh-run")
                yield Button("Back", id="dismiss-fresh-run")

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        self.query_one("#confirm-fresh-run", Button).disabled = not (
            self.new_count or any(
                self.query_one(f"#fresh-run-{index}", Checkbox).value
                for index in range(len(self.previous))
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dismiss-fresh-run":
            self.dismiss(None)
        elif event.button.id == "confirm-fresh-run":
            self.dismiss(tuple(
                task.id for index, (task, _) in enumerate(self.previous)
                if self.query_one(f"#fresh-run-{index}", Checkbox).value
            ))


class WorkflowRunnerScreen(Screen[None]):
    """Collect compact launch selections and observe the workflow service."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self._event_lines: list[str] = []
        self._raw_event_lines: list[str] = []
        self._live_diagnostics: list[ExecutionDiagnostic] = []
        self._active_timeline_stages: dict[WorkflowStage, int] = {}
        self._successful_timeline_stages: dict[WorkflowStage, int] = {}
        self._stage_attempts: dict[WorkflowStage, int] = {}
        self._show_raw_events = False
        self._role_providers: dict[AgentRole, str] = {}
        self._role_default_providers: dict[AgentRole, str] = {}
        self._role_presets: dict[AgentRole, dict[str, AgentPresetConfig]] = {}
        self._role_fallback_presets: dict[AgentRole, str] = {}
        self._selected_presets: dict[AgentRole, str] = {}
        self._suppressed_preset_changes: set[tuple[AgentRole, str]] = set()
        self._suppressed_manual_preset_changes: set[AgentRole] = set()
        self._runtime_inputs: dict[tuple[AgentRole, str], dict[str, Any]] = {}
        self._runtime_defaults: dict[tuple[AgentRole, str], dict[str, Any]] = {}
        self._advanced_open: dict[AgentRole, bool] = {}
        self._task_load_token = 0
        self._task_context: tuple[str, str] | None = None
        self._suppressed_task_provider_change: tuple[str, str] | None = None
        self._active_execution_id: str | None = None
        self._outcome_visible = False
        self._human_input_dismissed = False
        self._batch_tasks: tuple[DevelopmentTask, ...] = ()
        self._batch_running = False
        self._batch_progress: LiveBatchProgress | None = None

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
                        "Repositories",
                        id="workflow-repositories",
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
                                with Vertical(classes="role-field"):
                                    yield Static(
                                        "Permission mode", id=f"{prefix}-permission-label"
                                    )
                                    yield OrchestratorSelect(
                                        [],
                                        prompt="Provider/YAML default",
                                        id=f"{prefix}-permission",
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
            yield BatchProgressView(id="batch-progress")
            with Horizontal(classes="screen-actions", id="workflow-stop-actions"):
                yield Button("Stop workflow", variant="error", id="stop-workflow")
            yield Static("", id="workflow-events")
            with Vertical(id="workflow-outcome"):
                yield Static("", id="workflow-outcome-title")
                yield Static("", id="workflow-outcome-message")
                with Horizontal(classes="screen-actions", id="workflow-outcome-actions"):
                    yield Button("Answer…", id="outcome-answer-later")
                    yield Button("Continue…", variant="primary", id="outcome-continue")
                with Vertical(id="human-input-panel"):
                    yield Static("Human input required", id="human-input-title")
                    yield Static("", id="human-input-prompt")
                    yield TextArea("", id="human-response")
                    with Horizontal(classes="screen-actions"):
                        yield Button(
                            "Answer and Resume",
                            variant="primary",
                            id="resume-workflow",
                            disabled=True,
                        )
                        yield Button("Return later", id="return-later")
            with Horizontal(classes="workflow-log-actions", id="workflow-log-actions"):
                yield Button("Show detailed logs", id="toggle-workflow-logs")
                yield Button(
                    "Copy detailed logs", id="copy-detailed-logs-button", disabled=True
                )
            yield Static("Detailed logs", id="workflow-raw-events-title")
            # A Static grows with its rendered content, leaving the surrounding
            # VerticalScroll as the sole vertical scroll context. RichLog owns
            # a fixed viewport and its own scrollbar, which makes diagnostics
            # difficult to inspect in an execution screen.
            yield Static("", id="workflow-raw-events", markup=False, expand=True)

    @property
    def controller(self) -> WorkflowController:
        return cast("OrchestratorApp", self.app).controller

    def on_mount(self) -> None:
        self._set_layout_mode(self.size.width)
        self.query_one("#workflow-stop-actions", Horizontal).display = False
        self.query_one("#batch-progress", BatchProgressView).display = False
        self._clear_workflow_outcome()
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

    def on_screen_resume(self) -> None:
        """Reload repository choices and reconcile execution state on return."""

        try:
            repositories = self.controller.repositories()
        except Exception as exc:
            self.query_one("#workflow-progress", Static).update(
                f"Cannot reload configured repositories: {exc}"
            )
            return
        self.query_one("#repository", RepositorySelect).set_repositories(
            repositories,
            selected_repository=self.controller.selected_repository,
        )
        if self._batch_running and self._batch_progress is not None:
            self._reconcile_live_batch()
        elif self._batch_progress is None and self._active_execution_id is not None:
            self._reconcile_active_execution()
        repository = self._selected("#repository")
        provider = self._selected("#task-provider")
        if (
            repository is not None
            and provider is not None
            and not self._batch_running
            and not self.query_one("#start-workflow", Button).disabled
        ):
            self._request_task_load(repository, provider)

    def on_task_selector_selection_changed(
        self, event: TaskSelector.SelectionChanged
    ) -> None:
        count = len(event.task_ids)
        self.query_one("#start-workflow", Button).label = (
            f"Start workflow ({count})" if count else "Start workflow"
        )

    def _set_layout_mode(self, width: int) -> None:
        self.set_class(width <= 80, "-narrow")
        self.set_class(width > 80, "-wide")
        self.set_class(width <= 96, "-compact-navigation")

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        # A mouse press focuses a clickable control before its release/click.
        # Scrolling it under the pointer turns that first click into focus only.
        # Textual still scrolls controls into view during keyboard navigation.
        if isinstance(event.widget, (Button, Checkbox)) or is_orchestrator_select_widget(
            event.widget
        ):
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
            if event.select.id == f"{prefix}-permission":
                provider = self._selected(f"#{prefix}-provider")
                repository = self._selected("#repository")
                if provider is None or repository is None:
                    return
                capabilities = self.controller.provider_runtime_capabilities(repository, provider)
                values = self._runtime_values_from_controls(prefix)
                self._clear_preset_if_runtime_changed(role, prefix)
                self._update_role_summary(role, prefix, provider, values, capabilities)
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
            configured_agent_options = self.controller.configured_agent_provider_options(repository)
            selected_agents = self.controller.agent_providers(repository)
        except Exception as exc:
            self.notify(f"Could not resolve repository configuration: {exc}", severity="error")
            return
        if self._task_context is not None and self._task_context[0] != repository:
            self._task_context = None
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
        self._role_fallback_presets = {
            role: fallback
            for role, _ in self._role_fields()
            if (fallback := self.controller.agent_fallback_preset(repository, role)) is not None
        }
        self._selected_presets.clear()
        self._runtime_inputs.clear()
        self._runtime_defaults.clear()
        self._advanced_open.clear()
        for role, prefix in self._role_fields():
            select = self.query_one(f"#{prefix}-provider", OrchestratorSelect)
            select.set_options(configured_agent_options, allow_blank=False)
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
        provider = self._selected(f"#{prefix}-provider")
        if provider is not None:
            capabilities = self.controller.provider_runtime_capabilities(repository, provider)
            self._update_role_summary(
                role,
                prefix,
                provider,
                self._runtime_values_from_controls(prefix),
                capabilities,
            )

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
        self._load_role_runtime_controls(
            role, prefix, preset.provider, reset_to_defaults=True
        )
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
        # Omitted preset fields inherit the selected provider's YAML defaults,
        # which `_load_role_runtime_controls` has already populated.
        if preset.model is not None:
            self.query_one(f"#{prefix}-model", Input).value = preset.model
        if preset.reasoning_effort is not None:
            self.query_one(f"#{prefix}-reasoning", Input).value = preset.reasoning_effort
        permission = self.query_one(f"#{prefix}-permission", OrchestratorSelect)
        if permission.display:
            permission.value = (
                preset.permission_mode
                if preset.permission_mode is not None
                else _PROVIDER_DEFAULT_PERMISSION
            )
        generation = dict(preset.generation)
        for name in ("temperature", "max-output-tokens"):
            value = generation.get(name.replace("-", "_"))
            if value is not None:
                self.query_one(f"#{prefix}-{name}", Input).value = str(value)

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
            permission_mode=options.get("permission_mode"),
            generation=generation,
        )

    def _preset_matches_controls(
        self, role: AgentRole, prefix: str, preset: AgentPresetConfig
    ) -> bool:
        if self._selected(f"#{prefix}-provider") != preset.provider:
            return False
        current = self._preset_from_controls(role, prefix).model_dump(exclude_none=True)
        expected = self._preset_with_provider_defaults(preset).model_dump(exclude_none=True)
        return bool(
            self._preset_comparison_value(current)
            == self._preset_comparison_value(expected)
        )

    def _preset_with_provider_defaults(self, preset: AgentPresetConfig) -> AgentPresetConfig:
        """Materialize omitted preset values as the selected provider's defaults.

        Only the generation options the launcher can edit are retained: the
        controls can never reproduce a provider's remaining generation keys,
        so keeping them here would make every preset on such a provider look
        like a this-run override.
        """

        repository = self._selected("#repository")
        defaults = (
            self.controller.provider_runtime_values(repository, preset.provider)
            if repository is not None
            else {}
        )
        default_generation = defaults.get("generation", {})
        generation = (
            dict(default_generation) if isinstance(default_generation, dict) else {}
        )
        generation.update(preset.generation)
        return AgentPresetConfig(
            provider=preset.provider,
            model=preset.model if preset.model is not None else defaults.get("model"),
            reasoning_effort=(
                preset.reasoning_effort
                if preset.reasoning_effort is not None
                else defaults.get("reasoning_effort")
            ),
            permission_mode=preset.permission_mode,
            generation={
                key: value
                for key, value in generation.items()
                if value is not None and key in _CONTROLLED_GENERATION_OPTIONS
            },
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
            permission_mode=None,
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
            provider_values = self.controller.provider_runtime_values(repository, provider)
            values = None if reset_to_defaults else self._runtime_inputs.get((role, provider))
            if values is None:
                values = provider_values
                permission_value = None
            else:
                permission_value = values.get("permission_mode")
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
        self._set_permission_control(
            prefix,
            capabilities.supports_permission_mode,
            provider_values.get("permission_mode"),
            permission_value,
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

    def _set_permission_control(
        self, prefix: str, visible: bool, default: Any, value: Any
    ) -> None:
        label = self.query_one(f"#{prefix}-permission-label", Static)
        selector = self.query_one(f"#{prefix}-permission", OrchestratorSelect)
        label.display = visible
        selector.display = visible
        if not visible:
            return
        selector.set_options(
            [
                (f"Provider/YAML default ({default or 'dontAsk'})", _PROVIDER_DEFAULT_PERMISSION),
                *[(mode, mode) for mode in _PERMISSION_MODES],
            ],
            allow_blank=False,
        )
        selector.value = value if value in _PERMISSION_MODES else _PROVIDER_DEFAULT_PERMISSION

    @staticmethod
    def _copy_runtime_values(values: dict[str, Any]) -> dict[str, Any]:
        generation = values.get("generation", {})
        return {
            "model": values.get("model"),
            "reasoning_effort": values.get("reasoning_effort"),
            "permission_mode": values.get("permission_mode"),
            "generation": dict(generation) if isinstance(generation, dict) else {},
        }

    def _runtime_values_from_controls(self, prefix: str) -> dict[str, Any]:
        return {
            "model": self.query_one(f"#{prefix}-model", Input).value.strip() or None,
            "reasoning_effort": self.query_one(f"#{prefix}-reasoning", Input).value.strip() or None,
            "permission_mode": self._permission_mode(prefix),
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
        if capabilities.supports_permission_mode and values["permission_mode"] is not None:
            options["permission_mode"] = values["permission_mode"]
        if capabilities.supports_generation:
            options["generation"] = values["generation"]
        return options

    def _update_role_summary(
        self, role: AgentRole, prefix: str, provider: str, values: dict[str, Any], capabilities: Any
    ) -> None:
        primary_overridden = self._role_default_providers.get(role) != provider
        if not primary_overridden:
            primary_overridden = self._manual_runtime_overrides(role, prefix)
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
        fallback_preset = self._role_fallback_presets.get(role)
        if fallback_preset is not None:
            fallback = self._role_presets.get(role, {}).get(fallback_preset)
            if fallback is not None:
                fallback_note = f"fallback: {fallback.provider}/{fallback_preset}"
                if primary_overridden:
                    fallback_note += " (repository setting; unchanged)"
                parts.append(fallback_note)
        if capabilities.supports_model:
            parts.append(str(values.get("model") or "Provider default"))
        if capabilities.supports_reasoning_effort:
            parts.append(str(values.get("reasoning_effort") or "Provider default"))
        if capabilities.supports_permission_mode:
            default = self._runtime_defaults.get((role, provider), {}).get("permission_mode")
            permission = values.get("permission_mode")
            parts.append(
                str(permission)
                if permission is not None
                else f"Provider/YAML default ({default or 'dontAsk'})"
            )
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

    def _permission_mode(self, prefix: str) -> _PermissionMode | None:
        selector = self.query_one(f"#{prefix}-permission", OrchestratorSelect)
        if not selector.display:
            return None
        value = selector.value
        return cast(_PermissionMode, value) if value in _PERMISSION_MODES else None

    def _set_advanced_visibility(self, role: AgentRole, prefix: str, supported: bool) -> None:
        advanced = self.query_one(f"#{prefix}-advanced", Vertical)
        expanded = supported and self._advanced_open.get(role, False)
        advanced.display = expanded
        toggle = self.query_one(f"#{prefix}-advanced-toggle", Button)
        toggle.label = "Advanced ▾" if expanded else "Advanced ▸"

    def _load_github_app_summary(self, repository: str) -> None:
        try:
            planner, implementer, reviewer = self.controller.github_app_settings(repository)
        except Exception as exc:
            self.query_one("#github-app-summary", Static).update(
                f"GitHub Apps: could not inspect configured defaults ({exc})"
            )
            return

        self.query_one("#github-app-summary", Static).update(
            self.github_app_summary(planner, implementer, reviewer)
        )

    @staticmethod
    def github_app_summary(planner: Any, implementer: Any, reviewer: Any) -> str:
        """Return a launcher-safe summary without exposing App identifiers."""

        planner_status = "✓" if planner.is_configured else "—"
        implementer_status = "✓" if implementer.is_configured else "—"
        reviewer_status = "✓" if reviewer.is_configured else "—"
        return (
            f"GitHub Apps: Planner {planner_status} · Implementer {implementer_status}"
            f" · Reviewer {reviewer_status}"
        )

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
        elif event.button.id == "stop-workflow":
            self._request_stop_workflow()
        elif event.button.id == "resume-workflow":
            self._resume_active_workflow()
        elif event.button.id == "return-later":
            self._human_input_dismissed = True
            self._hide_human_panel()
            self.query_one("#workflow-outcome-message", Static).update(
                "The execution remains stopped until you answer. Select Answer to open the "
                "response form, or return through execution history later."
            )
            self.query_one("#outcome-answer-later", Button).display = True
            self.query_one("#workflow-outcome-actions", Horizontal).display = True
            self.query_one("#outcome-answer-later", Button).focus()
            self.notify("The execution remains available in execution history.")
        elif event.button.id == "outcome-answer-later":
            self._human_input_dismissed = False
            self._open_active_execution_detail()
        elif event.button.id == "outcome-continue":
            self._open_active_execution_detail()
        elif event.button.id == "workflow-history":
            self._show_history()
        elif event.button.id == "workflow-agent-presets":
            from orchestrator.tui.screens.agent_presets import AgentPresetsScreen

            self.app.push_screen(AgentPresetsScreen(self._selected("#repository")))
        elif event.button.id == "workflow-repositories":
            from orchestrator.tui.screens.repository_settings import RepositorySettingsScreen

            self.app.push_screen(RepositorySettingsScreen(self._selected("#repository")))
        elif event.button.id == "workflow-github-apps":
            from orchestrator.tui.screens.github_app_settings import GitHubAppSettingsScreen

            self.app.push_screen(GitHubAppSettingsScreen())
        elif event.button.id == "workflow-agent-instructions":
            from orchestrator.tui.screens.agent_instructions import AgentInstructionsScreen

            self.app.push_screen(
                AgentInstructionsScreen(self._selected("#repository"))
            )
        elif event.button.id == "copy-detailed-logs-button":
            self._copy_detailed_logs()
        elif event.button.id == "toggle-workflow-logs":
            self._show_raw_events = not self._show_raw_events
            raw_events = self.query_one("#workflow-raw-events", Static)
            raw_events_title = self.query_one("#workflow-raw-events-title", Static)
            raw_events.display = self._show_raw_events
            raw_events_title.display = self._show_raw_events
            # Static sizes itself from its renderable. Make it visible before
            # changing that renderable so the layout pass measures the full
            # diagnostic content, then refresh the owning scroll region.
            raw_events.update(self._rendered_raw_events() if self._show_raw_events else "")
            self.query_one("#workflow-runner", VerticalScroll).refresh(layout=True)
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

    def _open_active_execution_detail(self) -> None:
        if self._active_execution_id is None:
            return
        from orchestrator.tui.screens.execution_detail import ExecutionDetailScreen

        self.app.push_screen(ExecutionDetailScreen(self._active_execution_id))

    def _request_task_load(self, repository: str, provider: str) -> None:
        self._task_load_token += 1
        token = self._task_load_token
        self.query_one("#task-selector", TaskSelector).set_tasks_loading(
            provider=provider,
            preserve_selection=self._task_context == (repository, provider),
        )
        self._load_tasks(repository, provider, token)

    @work(thread=True, exclusive=True)
    def _load_tasks(self, repository: str, provider: str, token: int) -> None:
        try:
            tasks = self.controller.list_tasks(repository=repository, task_provider=provider)
        except Exception as exc:
            self.app.call_from_thread(self._task_loading_failed, str(exc), token)
            return
        # Labels share the task request token, so a stale catalog is discarded
        # with its tasks. A failed lookup is reported rather than treated as
        # a provider without label support.
        labels: tuple[str, ...] | None = None
        label_error = ""
        try:
            labels = self.controller.list_task_labels(
                repository=repository, task_provider=provider
            )
        except Exception as exc:
            label_error = str(exc) or type(exc).__name__
        self.app.call_from_thread(
            self._set_available_tasks, tasks, provider, token, labels, label_error
        )

    def _set_available_tasks(
        self,
        tasks: list[DevelopmentTask],
        provider: str,
        token: int,
        labels: tuple[str, ...] | None = None,
        label_error: str = "",
    ) -> None:
        if token == self._task_load_token:
            repository = self._selected("#repository")
            try:
                executions = (
                    self.controller.task_execution_status(repository, provider, tasks)
                    if repository is not None
                    else {}
                )
            except Exception as exc:
                executions = {}
                self.notify(
                    f"Tasks loaded, but execution statuses are unavailable: {exc}",
                    severity="warning",
                )
            self.query_one("#task-selector", TaskSelector).set_tasks(
                tasks,
                provider=provider,
                executions=executions,
                labels=labels,
                label_error=label_error,
            )
            if repository is not None:
                self._task_context = (repository, provider)

    def _task_loading_failed(self, message: str, token: int) -> None:
        if token == self._task_load_token:
            self._task_context = None
            self.query_one("#task-selector", TaskSelector).set_error(message)

    def _start_selected_workflow(
        self, confirmed_previous_ids: set[str] | None = None
    ) -> None:
        repository = self._selected("#repository")
        task_provider = self._selected("#task-provider")
        task_selector = self.query_one("#task-selector", TaskSelector)
        task_ids = task_selector.selected_task_ids
        provider_selections = {
            role: self._selected(f"#{prefix}-provider") for role, prefix in self._role_fields()
        }
        if repository is None or task_provider is None:
            self.notify(
                "Select a repository and provider before starting.", severity="warning"
            )
            return
        if not task_ids:
            self.notify("Select at least one task to start.", severity="warning")
            return
        if any(value is None for value in provider_selections.values()):
            self.notify("Select an agent provider for every workflow role.", severity="warning")
            return
        agent_providers = {
            role: value for role, value in provider_selections.items() if value is not None
        }
        runtime_options = {
            role: self._runtime_options_for_role(role, prefix)
            for role, prefix in self._role_fields()
        }
        previous = task_selector.selected_previous_executions
        if confirmed_previous_ids is None and previous:
            self.app.push_screen(
                FreshRunConfirmation(
                    tuple(
                        (task, previous[task.id])
                        for task in task_selector.selected_tasks if task.id in previous
                    ),
                    len(task_ids) - len(previous),
                ),
                lambda selected: self._start_selected_workflow(set(selected))
                if selected is not None else None,
            )
            return
        if confirmed_previous_ids is not None:
            task_ids = tuple(
                task_id for task_id in task_ids
                if task_id not in previous or task_id in confirmed_previous_ids
            )
            if not task_ids:
                return
        self.query_one("#start-workflow", Button).disabled = True
        self.query_one("#workflow-stop-actions", Horizontal).display = False
        self.query_one("#workflow-progress", Static).update("Starting workflow…")
        self._human_input_dismissed = False
        self._event_lines.clear()
        self._raw_event_lines.clear()
        self._live_diagnostics.clear()
        self._active_execution_id = None
        self._clear_workflow_outcome()
        self.query_one("#copy-detailed-logs-button", Button).disabled = True
        self._active_timeline_stages.clear()
        self._successful_timeline_stages.clear()
        self._stage_attempts.clear()
        self.query_one("#workflow-events", Static).update("")
        self.query_one("#workflow-raw-events", Static).update("")
        self._hide_human_panel()
        self._batch_progress = None
        self.query_one("#batch-progress", BatchProgressView).display = False
        if len(task_ids) == 1:
            self._batch_running = False
            self._batch_tasks = ()
            self._set_single_task_controls_visible(True)
            launch = WorkflowLaunch(
                repository=repository,
                task_provider=task_provider,
                task_id=task_ids[0],
                agent_providers=agent_providers,
                agent_runtime_options=runtime_options,
            )
            self._run_workflow(launch)
            return
        self._batch_running = True
        self._batch_tasks = tuple(
            task for task in task_selector.selected_tasks if task.id in task_ids
        )
        self._set_single_task_controls_visible(False)
        self.query_one("#workflow-progress", Static).update(
            f"Starting batch of {len(task_ids)} tasks…"
        )
        batch_launch = WorkflowBatchLaunch(
            repository=repository,
            task_provider=task_provider,
            task_ids=task_ids,
            agent_providers=agent_providers,
            agent_runtime_options=runtime_options,
        )
        self._run_batch(batch_launch)

    def _set_single_task_controls_visible(self, visible: bool) -> None:
        """Hide single-task timeline/outcome/log controls while a batch is active.

        A batch renders its own per-task timeline and logs through
        ``BatchProgressView``; showing both would duplicate the same
        information under two different widgets.
        """

        self.query_one("#workflow-events", Static).display = visible
        self.query_one("#workflow-outcome", Vertical).display = visible and self._outcome_visible
        self.query_one("#workflow-log-actions", Horizontal).display = visible
        self.query_one("#workflow-raw-events-title", Static).display = (
            visible and self._show_raw_events
        )
        self.query_one("#workflow-raw-events", Static).display = visible and self._show_raw_events

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

    @work(thread=True, exclusive=True)
    def _run_batch(self, launch: WorkflowBatchLaunch) -> None:
        try:
            batch = self.controller.start_batch(
                launch,
                event_listener=self._on_workflow_event,
                on_batch_created=lambda created_batch: self.app.call_from_thread(
                    self._batch_created, created_batch, launch
                ),
            )
        except Exception as exc:
            self.app.call_from_thread(self._workflow_failed, str(exc))
            return
        self.app.call_from_thread(self._batch_finished, batch)

    def _batch_created(self, batch: WorkflowBatch, launch: WorkflowBatchLaunch) -> None:
        """Render every queued item as soon as the durable batch row exists.

        This runs before the first item is submitted, so the batch identity
        and its stable task order are visible immediately rather than only
        after the first execution starts.
        """

        role_models = {
            role: options.get("model") for role, options in launch.agent_runtime_options.items()
        }
        self._batch_progress = LiveBatchProgress(
            batch, role_providers=launch.agent_providers, role_models=role_models,
            required_providers=self.controller.batch_required_providers(batch.id),
        )
        view = self.query_one("#batch-progress", BatchProgressView)
        view.set_rows(self._batch_progress.rows())
        view.set_provider_availability({})
        view.display = True
        self.query_one("#workflow-stop-actions", Horizontal).display = True
        stop = self.query_one("#stop-workflow", Button)
        stop.disabled = False
        stop.label = "Stop batch"
        self.query_one("#workflow-progress", Static).update(
            f"Batch running: {len(batch.items)} task(s)."
        )

    def _on_workflow_event(self, event: WorkflowEvent) -> None:
        self.app.call_from_thread(self._render_workflow_event, event)

    def _render_workflow_event(self, event: WorkflowEvent) -> None:
        if self._batch_running and self._batch_progress is not None:
            self._render_batch_event(event)
            return
        if event.kind is WorkflowEventKind.EXECUTION_CREATED:
            self._active_execution_id = event.execution_id
        elif self._active_execution_id is None:
            self._active_execution_id = event.execution_id
        self._ensure_stop_controls_visible(event)
        if (
            event.kind is WorkflowEventKind.WORKFLOW_FINISHED
            and event.execution_id == self._active_execution_id
        ):
            self._reset_active_workflow_controls()
        self._append_raw_event(event)
        self._append_live_diagnostic(event)
        update_human_blocked_line(
            event, self._event_lines, self._successful_timeline_stages
        )
        message = self._timeline_message(event)
        if message is None:
            return
        self.query_one("#workflow-progress", Static).update(message)

        if event.kind == WorkflowEventKind.STAGE_STARTED and event.stage is not None:
            self._successful_timeline_stages.pop(event.stage, None)
            self._stage_attempts[event.stage] = self._stage_attempts.get(event.stage, 0) + 1
            self._active_timeline_stages[event.stage] = len(self._event_lines)
            self._event_lines.append(message)
        elif event.kind in {
            WorkflowEventKind.STAGE_FINISHED,
            WorkflowEventKind.STAGE_FAILED,
        } and event.stage is not None:
            active_line = self._active_timeline_stages.pop(event.stage, None)
            if active_line is None:
                active_line = len(self._event_lines)
                self._event_lines.append(message)
            else:
                self._event_lines[active_line] = message
            if (
                event.kind is WorkflowEventKind.STAGE_FINISHED
                and event.step_status is StepStatus.SUCCEEDED
            ):
                self._successful_timeline_stages[event.stage] = active_line
            else:
                self._successful_timeline_stages.pop(event.stage, None)
        elif event.kind in {WorkflowEventKind.AGENT_FALLBACK, WorkflowEventKind.DIAGNOSTIC}:
            self._event_lines.append(message)

        self.query_one("#workflow-events", Static).update("\n".join(self._event_lines[-10:]))

    def _ensure_stop_controls_visible(self, event: WorkflowEvent) -> None:
        """Show the stop control as soon as cancellable work exists.

        Queued and provider-blocked work is cancellable too, so this must run
        before a stage is genuinely running.
        """

        if event.execution_id == self._active_execution_id and (
            event.kind is WorkflowEventKind.EXECUTION_CREATED
            or (
                event.kind is WorkflowEventKind.STATUS_CHANGED
                and event.status in _CANCELLABLE_STATUSES
            )
        ):
            self.query_one("#workflow-stop-actions", Horizontal).display = True
            stop = self.query_one("#stop-workflow", Button)
            stop.disabled = False
            stop.label = "Stop workflow"

    def _render_batch_event(self, event: WorkflowEvent) -> None:
        progress = self._batch_progress
        assert progress is not None
        if event.kind is WorkflowEventKind.EXECUTION_CREATED:
            self._active_execution_id = event.execution_id
        position = progress.apply(event)
        if position is None:
            return
        if (
            event.kind is WorkflowEventKind.WORKFLOW_FINISHED
            and event.status is WorkflowStatus.FAILED
        ):
            try:
                execution, _ = self.controller.execution_detail(event.execution_id)
            except Exception:
                progress.set_continuation_eligible(position, False)
            else:
                progress.set_continuation_eligible(
                    position, self.controller.continuation_stage(execution) is not None
                )
        view = self.query_one("#batch-progress", BatchProgressView)
        progress.set_unavailable_providers(
            set(self.controller.batch_unavailable_providers(progress.batch_id))
        )
        rows = progress.rows()
        for row in rows:
            view.update_row(row)
        counts = summarize(rows)
        view.update_summary(counts)
        unavailable = self.controller.batch_unavailable_providers(progress.batch_id)
        newly_unavailable = set(unavailable) - view.unavailable_providers
        view.set_provider_availability({
            provider: progress.held_count(provider) for provider in unavailable
        })
        # The availability controls sit below the launch form, usually out of
        # view while a batch runs, so say when a provider starts holding tasks.
        for provider in sorted(newly_unavailable):
            self.notify(
                f"Provider {provider} quota exhausted; queued tasks that use it are held. "
                "Mark it available in the batch progress once its quota resets.",
                severity="warning",
            )
        self.query_one("#workflow-progress", Static).update(
            f"Batch running: {len(rows)} task(s) — {counts['NEEDS_HUMAN']} waiting for you."
        )

    def _reconcile_live_batch(self) -> None:
        """Refresh the live projection after a detail screen changes an item."""

        progress = self._batch_progress
        if progress is None:
            return
        try:
            batch = self.controller.batch_detail(progress.batch_id)
        except Exception as exc:
            self.notify(f"Could not refresh batch progress: {exc}", severity="error")
            return
        for item in batch.items:
            if item.execution_id is None:
                continue
            try:
                execution, _ = self.controller.execution_detail(item.execution_id)
            except Exception as exc:
                self.notify(f"Could not refresh batch task: {exc}", severity="error")
                continue
            if progress.reconcile_execution(item.position, execution):
                eligible = False
                if execution.status is WorkflowStatus.FAILED:
                    try:
                        eligible = self.controller.continuation_stage(execution) is not None
                    except Exception:
                        eligible = False
                progress.set_continuation_eligible(
                    item.position, eligible,
                )
        view = self.query_one("#batch-progress", BatchProgressView)
        rows = progress.rows()
        for row in rows:
            view.update_row(row)
        counts = summarize(rows)
        view.update_summary(counts)
        self.query_one("#workflow-progress", Static).update(
            f"Batch running: {len(rows)} task(s) — {counts['NEEDS_HUMAN']} waiting for you."
        )

    def on_batch_progress_view_provider_available_requested(
        self, message: BatchProgressView.ProviderAvailableRequested
    ) -> None:
        progress = self._batch_progress
        if progress is None:
            return
        try:
            self.controller.mark_batch_provider_available(progress.batch_id, message.provider)
        except Exception as exc:
            self.notify(f"Could not mark provider available: {exc}", severity="error")
            return
        progress.set_unavailable_providers(
            set(self.controller.batch_unavailable_providers(progress.batch_id))
        )
        view = self.query_one("#batch-progress", BatchProgressView)
        for row in progress.rows():
            view.update_row(row)
        view.set_provider_availability({
            provider: progress.held_count(provider)
            for provider in self.controller.batch_unavailable_providers(progress.batch_id)
        })

    def on_batch_item_row_action_requested(self, message: BatchItemRow.ActionRequested) -> None:
        self.on_batch_item_row_open_execution_requested(
            BatchItemRow.OpenExecutionRequested(message.position)
        )

    def on_batch_item_row_cancel_requested(self, message: BatchItemRow.CancelRequested) -> None:
        progress = self._batch_progress
        if progress is None:
            self.notify("That batch task has no execution to cancel.", severity="warning")
            return
        execution_id = progress.execution_id_for(message.position)
        if execution_id is None:
            self.notify("That batch task has no execution to cancel.", severity="warning")
            return
        try:
            accepted = self.controller.cancel_workflow(execution_id)
            execution, _ = self.controller.execution_detail(execution_id)
        except Exception as exc:
            self.notify(f"Could not cancel batch task: {exc}", severity="error")
            return
        if (
            execution.status not in ACTIVE_WORKFLOW_STATUSES
            and progress.rows()[message.position].category != execution.status.value
        ):
            self._render_batch_event(
                WorkflowEvent(
                    execution_id=execution_id,
                    kind=WorkflowEventKind.WORKFLOW_FINISHED,
                    status=execution.status,
                    payload={"batch_position": message.position},
                )
            )
        if not accepted:
            self.notify("This batch task has already finished.", severity="warning")
            return
        if execution.status in ACTIVE_WORKFLOW_STATUSES:
            progress.mark_cancellation_requested(execution_id)
            view = self.query_one("#batch-progress", BatchProgressView)
            view.update_row(progress.rows()[message.position])
        if execution.status is WorkflowStatus.CANCELLED:
            self.notify("Batch task cancelled.")
        elif execution.status in ACTIVE_WORKFLOW_STATUSES:
            self.notify("Batch task cancellation requested.")
        else:
            self.notify("Batch task finished before cancellation settled.", severity="warning")

    def on_batch_item_row_show_logs_requested(
        self, message: BatchItemRow.ShowLogsRequested
    ) -> None:
        progress = self._batch_progress
        if progress is None:
            return
        view = self.query_one("#batch-progress", BatchProgressView)
        row = view.row(message.position)
        if row is None:
            return
        content = format_diagnostics_for_export(progress.diagnostics_for(message.position))
        row.set_detailed_logs(content)

    def on_batch_item_row_copy_logs_requested(
        self, message: BatchItemRow.CopyLogsRequested
    ) -> None:
        progress = self._batch_progress
        if progress is None:
            return
        content = format_diagnostics_for_export(progress.diagnostics_for(message.position))
        if not content:
            self.notify("No detailed logs are available to copy yet.", severity="warning")
            return
        try:
            self.app.copy_to_clipboard(content)
        except Exception as exc:
            self.notify(f"Could not copy detailed logs: {exc}", severity="error")
            return
        self.notify("Detailed logs copied to clipboard.")

    def on_batch_item_row_open_execution_requested(
        self, message: BatchItemRow.OpenExecutionRequested
    ) -> None:
        progress = self._batch_progress
        execution_id = progress.execution_id_for(message.position) if progress else None
        if execution_id is None:
            self.notify("That batch task has no execution to inspect yet.", severity="warning")
            return
        from orchestrator.tui.screens.execution_detail import ExecutionDetailScreen

        self.app.push_screen(ExecutionDetailScreen(execution_id))

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
        raw_events = self.query_one("#workflow-raw-events", Static)
        if self._show_raw_events:
            raw_events.update(self._rendered_raw_events())
            self.query_one("#workflow-runner", VerticalScroll).refresh(layout=True)

    def _append_live_diagnostic(self, event: WorkflowEvent) -> None:
        """Retain diagnostics while persistence is still being updated in a live run."""

        if event.kind is WorkflowEventKind.DIAGNOSTIC:
            try:
                category = DiagnosticCategory(event.category or "")
            except ValueError:
                category = DiagnosticCategory.WORKFLOW_EVENT
            payload = event.payload or {}
        else:
            category = DiagnosticCategory.WORKFLOW_EVENT
            payload = {
                "kind": event.kind.value,
                "status": event.status.value if event.status else None,
                "step_status": event.step_status.value if event.step_status else None,
                "message": event.message,
            }
        self._live_diagnostics.append(
            ExecutionDiagnostic(
                execution_id=event.execution_id,
                category=category,
                payload=cast(dict[str, Any], redact_payload(payload)),
                stage=event.stage,
                occurred_at=event.occurred_at,
            )
        )
        self.query_one("#copy-detailed-logs-button", Button).disabled = False

    def _copy_detailed_logs(self) -> None:
        diagnostics = self._live_diagnostics
        if self._active_execution_id is not None:
            try:
                persisted = self.controller.execution_diagnostics(self._active_execution_id)
            except Exception:
                persisted = []
            if persisted:
                diagnostics = persisted
        content = format_diagnostics_for_export(diagnostics)
        if not content:
            self.notify("No detailed logs are available to copy yet.", severity="warning")
            return
        try:
            self.app.copy_to_clipboard(content)
        except Exception as exc:
            self.notify(f"Could not copy detailed logs: {exc}", severity="error")
            return
        self.notify("Detailed logs copied to clipboard.")

    def _rendered_raw_events(self) -> str:
        """Keep individual diagnostics visually distinct in the page log."""

        return "\n\n".join(self._raw_event_lines)

    def _timeline_message(self, event: WorkflowEvent) -> str | None:
        """Translate persisted observer events into concise, user-facing progress."""

        return timeline_message(
            event, self._stage_attempts, agent_context=self._agent_stage_context
        )

    def _agent_stage_context(self, stage: WorkflowStage) -> str:
        """Add selected agent runtime context only where an agent is active."""

        role = STAGE_ROLES.get(stage)
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
    def _status_message(
        status: WorkflowStatus | None, payload: dict[str, Any] | None = None
    ) -> str:
        return status_message(status, payload)

    @staticmethod
    def _stage_activity(stage: WorkflowStage, attempt: int) -> str:
        return stage_activity(stage, attempt)

    @staticmethod
    def _stage_complete(stage: WorkflowStage) -> str:
        return stage_complete(stage)

    def _reset_active_workflow_controls(self) -> None:
        """Restore idle-run controls; safe to call more than once per execution."""

        self.query_one("#start-workflow", Button).disabled = False
        self.query_one("#workflow-stop-actions", Horizontal).display = False

    def _workflow_finished(self, execution: WorkflowExecution) -> None:
        self._active_execution_id = execution.id
        self._reset_active_workflow_controls()
        progress = self.query_one("#workflow-progress", Static)
        progress.update(self._status_message(execution.status))
        progress.set_class(execution.status is WorkflowStatus.CANCELLED, "cancelled")
        self.notify(self._status_message(execution.status))
        self._render_workflow_outcome(execution)
        self._refresh_task_statuses()

    def _render_workflow_outcome(self, execution: WorkflowExecution) -> None:
        """Render stopped single-task outcomes separately from stage attempts."""

        self._clear_workflow_outcome()
        status = execution.status
        title = "Workflow stopped"
        message = "No further action is available."
        outcome_class = "stopped"

        pending_interaction = next(
            (item for item in reversed(execution.human_interactions) if item.awaiting_response),
            None,
        )
        continuation_stage = None
        if status in {WorkflowStatus.CHANGES_REQUESTED, WorkflowStatus.FAILED}:
            try:
                continuation_stage = self.controller.continuation_stage(execution)
            except Exception:
                continuation_stage = None

        if status is WorkflowStatus.NEEDS_HUMAN:
            outcome_class = "awaiting-user"
            if pending_interaction is not None:
                title = "Workflow stopped — waiting for your response"
                if self._human_input_dismissed:
                    message = (
                        "The workflow remains paused until you answer. Select Answer to "
                        "open the response form."
                    )
                    self.query_one("#outcome-answer-later", Button).display = True
                else:
                    message = "The workflow is paused until you answer the pending question."
                    self._show_human_panel(execution)
            else:
                title = "Workflow stopped — needs human input"
                message = (
                    "No pending response was found. Open execution history to inspect this run."
                )
        elif status is WorkflowStatus.CHANGES_REQUESTED:
            outcome_class = "awaiting-user"
            title = "Workflow stopped — changes requested"
            if continuation_stage is not None:
                message = "Review the requested changes, then continue the execution when ready."
                self.query_one("#outcome-continue", Button).display = True
            else:
                message = "No continuation is available for this execution."
        elif status is WorkflowStatus.FAILED:
            outcome_class = "failed"
            title = "Workflow stopped — failed"
            if continuation_stage is not None:
                message = "A continuation is available for this execution."
                self.query_one("#outcome-continue", Button).display = True
            else:
                message = "No continuation is available for this failure."
        elif status is WorkflowStatus.APPROVED:
            outcome_class = "completed"
            title = "Workflow completed"
            message = "Execution approved. No further action is available."
        elif status is WorkflowStatus.CANCELLED:
            outcome_class = "cancelled"
            title = "Workflow stopped — cancelled"
            message = "No further action is available."
        elif status in ACTIVE_WORKFLOW_STATUSES:
            return

        panel = self.query_one("#workflow-outcome", Vertical)
        panel.set_class(False, "awaiting-user")
        panel.set_class(False, "failed")
        panel.set_class(False, "completed")
        panel.set_class(False, "cancelled")
        panel.set_class(False, "stopped")
        panel.add_class(outcome_class)
        self.query_one("#workflow-outcome-title", Static).update(title)
        self.query_one("#workflow-outcome-message", Static).update(message)
        has_action = any(
            self.query_one(f"#{widget_id}", Button).display
            for widget_id in ("outcome-answer-later", "outcome-continue")
        )
        self.query_one("#workflow-outcome-actions", Horizontal).display = has_action
        self._outcome_visible = True
        panel.display = not self._batch_running
        if panel.display:
            self.query_one("#workflow-runner", VerticalScroll).scroll_to_widget(
                panel, animate=False, top=True
            )

    def _clear_workflow_outcome(self) -> None:
        """Hide and reset terminal controls before new work or a live resume."""

        self._outcome_visible = False
        self.query_one("#workflow-outcome", Vertical).display = False
        self.query_one("#workflow-outcome-actions", Horizontal).display = False
        self.query_one("#workflow-outcome-title", Static).update("")
        self.query_one("#workflow-outcome-message", Static).update("")
        self.query_one("#outcome-answer-later", Button).display = False
        self.query_one("#outcome-continue", Button).display = False
        self.query_one("#human-input-panel", Vertical).display = False

    def _reconcile_active_execution(self) -> None:
        """Refresh the timeline and terminal controls from persisted execution state."""

        assert self._active_execution_id is not None
        try:
            execution, steps = self.controller.execution_detail(self._active_execution_id)
        except Exception:
            return

        self._event_lines = step_timeline_lines(steps, execution.human_interactions)
        self._active_timeline_stages.clear()
        self._successful_timeline_stages.clear()
        self._stage_attempts.clear()
        for index, step in enumerate(steps):
            self._stage_attempts[step.stage] = self._stage_attempts.get(step.stage, 0) + 1
            if step.status is StepStatus.RUNNING:
                self._active_timeline_stages[step.stage] = index
            elif (
                step.status is StepStatus.SUCCEEDED
                and "waiting for human input" not in self._event_lines[index]
            ):
                self._successful_timeline_stages[step.stage] = index
        self.query_one("#workflow-events", Static).update("\n".join(self._event_lines[-10:]))

        progress = self.query_one("#workflow-progress", Static)
        stop = self.query_one("#stop-workflow", Button)
        cancellation_pending = execution.status in ACTIVE_WORKFLOW_STATUSES and (
            execution.cancellation_requested_at is not None
            or (stop.disabled and str(stop.label) == "Cancellation requested")
        )
        progress.update(
            "Cancellation requested; stopping after the current operation..."
            if cancellation_pending
            else self._status_message(execution.status)
        )
        progress.set_class(
            cancellation_pending or execution.status is WorkflowStatus.CANCELLED, "cancelled"
        )
        if execution.status in ACTIVE_WORKFLOW_STATUSES:
            self._clear_workflow_outcome()
            self.query_one("#start-workflow", Button).disabled = True
            self.query_one("#workflow-stop-actions", Horizontal).display = True
            stop.disabled = cancellation_pending
            stop.label = "Cancellation requested" if cancellation_pending else "Stop workflow"
        else:
            self._reset_active_workflow_controls()
            self._render_workflow_outcome(execution)

    def _batch_finished(self, batch: WorkflowBatch | None) -> None:
        task_count = len(self._batch_tasks)
        linked_count = sum(item.execution_id is not None for item in batch.items) if batch else 0
        self._batch_running = False
        self._batch_tasks = ()
        self._reset_active_workflow_controls()
        self.query_one("#workflow-progress", Static).update(
            f"Batch finished: {linked_count} of {task_count} task(s) submitted."
        )
        self.query_one("#workflow-stop-actions", Horizontal).display = False
        self.query_one("#batch-progress", BatchProgressView).set_provider_availability({})
        # Reconcile with durable storage once the batch stops running.
        # Existing rows are updated in place rather than rebuilt so an
        # operator's expanded rows and visible detailed logs are not disturbed.
        if batch is not None:
            try:
                rows = build_persisted_rows(batch, self.controller)
            except Exception:
                rows = None
            if rows is not None:
                view = self.query_one("#batch-progress", BatchProgressView)
                for row in rows:
                    view.update_row(row)
                view.update_summary(summarize(rows))
        self.notify("Batch finished.")
        self._refresh_task_statuses()

    def _refresh_task_statuses(self) -> None:
        repository = self._selected("#repository")
        provider = self._selected("#task-provider")
        if repository is not None and provider is not None:
            self._request_task_load(repository, provider)

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

    def _hide_human_panel(self) -> None:
        self.query_one("#human-input-panel", Vertical).display = False

    def _request_stop_workflow(self) -> None:
        if self._active_execution_id is None and not self._batch_running:
            self.notify("No active workflow is available to stop.", severity="warning")
            return
        self.app.push_screen(StopWorkflowConfirmation(), self._confirm_stop_workflow)

    def _confirm_stop_workflow(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        if not self._batch_running and self._active_execution_id is None:
            return
        stop = self.query_one("#stop-workflow", Button)
        stop.disabled = True
        stop.label = "Cancellation requested"
        progress = self.query_one("#workflow-progress", Static)
        progress.update(
            "Batch cancellation requested; no further tasks will start."
            if self._batch_running
            else "Cancellation requested; stopping after the current operation..."
        )
        progress.add_class("cancelled")
        try:
            if self._batch_running and self._batch_progress is not None:
                accepted = self.controller.cancel_batch(self._batch_progress.batch_id)
            else:
                assert self._active_execution_id is not None
                accepted = self.controller.cancel_workflow(self._active_execution_id)
        except Exception as exc:
            stop.disabled = False
            stop.label = "Stop workflow"
            progress.remove_class("cancelled")
            self.notify(f"Could not request cancellation: {exc}", severity="error")
            return
        if not accepted:
            self.query_one("#workflow-stop-actions", Horizontal).display = False
            self.notify("This workflow has already finished.", severity="warning")
            return
        if self._batch_running and self._batch_progress is not None:
            view = self.query_one("#batch-progress", BatchProgressView)
            for row in self._batch_progress.rows():
                view.update_row(row)

    def _resume_active_workflow(self) -> None:
        if self._active_execution_id is None:
            return
        response = self.query_one("#human-response", TextArea).text.strip()
        if not response:
            return
        self.query_one("#resume-workflow", Button).disabled = True
        self._human_input_dismissed = False
        self._clear_workflow_outcome()
        self.query_one("#start-workflow", Button).disabled = True
        self.query_one("#workflow-stop-actions", Horizontal).display = True
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
        was_batch = self._batch_running
        self._batch_running = False
        self._batch_tasks = ()
        self.query_one("#start-workflow", Button).disabled = False
        self.query_one("#workflow-stop-actions", Horizontal).display = False
        if was_batch and self._batch_progress is None:
            # Nothing was persisted (validation failed before the batch row
            # was created): restore the single-task controls this screen
            # otherwise leaves hidden while a batch is active.
            self._set_single_task_controls_visible(True)
        self.query_one("#workflow-progress", Static).update(f"Could not start workflow: {message}")
        self.notify(f"Could not start workflow: {message}", severity="error")
        if not was_batch and self._active_execution_id is not None:
            self._reconcile_active_execution()
        self._refresh_task_statuses()

    def _selected(self, selector: str) -> str | None:
        value = self.query_one(selector, OrchestratorSelect).value
        return value if isinstance(value, str) else None
