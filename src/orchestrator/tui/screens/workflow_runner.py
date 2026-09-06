"""Workflow launch and live-progress screen."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus, Resize
from textual.screen import Screen
from textual.widgets import Button, Input, Select, Static

from orchestrator.models.agent import AgentRole
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.workflow_event import WorkflowEvent
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.tui.bridge import WorkflowController, WorkflowLaunch
from orchestrator.tui.screens.task_selector import TaskSelector

if TYPE_CHECKING:
    from orchestrator.tui.app import OrchestratorApp


class WorkflowRunnerScreen(Screen[None]):
    """Collect compact launch selections and observe the workflow service."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self._event_lines: list[str] = []
        self._role_providers: dict[AgentRole, str] = {}
        self._role_default_providers: dict[AgentRole, str] = {}
        self._runtime_inputs: dict[tuple[AgentRole, str], dict[str, Any]] = {}
        self._runtime_defaults: dict[tuple[AgentRole, str], dict[str, Any]] = {}
        self._advanced_open: dict[AgentRole, bool] = {}
        self._task_load_token = 0
        self._suppressed_task_provider_change: tuple[str, str] | None = None

    def compose(self) -> ComposeResult:
        # One scroll context keeps a keyboard-focused control visible on small terminals.
        with VerticalScroll(id="workflow-runner"):
            yield Static("Start workflow", classes="screen-title")
            with Vertical(classes="workflow-form"):
                yield Static("Repository", classes="field-label")
                yield Select([], prompt="Configured repository", id="repository")
                yield Static("Task provider", classes="field-label")
                yield Select([], prompt="Task provider", id="task-provider")
                yield TaskSelector(id="task-selector")
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
                            yield Button("Configure", id=f"{prefix}-configure")
                        with Vertical(classes="role-config", id=f"{prefix}-config"):
                            with Horizontal(classes="role-config-row"):
                                with Vertical(classes="role-field"):
                                    yield Static("Provider", id=f"{prefix}-provider-label")
                                    yield Select(
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
                yield Static("", id="github-app-summary")
                with Horizontal(classes="screen-actions"):
                    yield Button("Start workflow", variant="success", id="start-workflow")
                    yield Button("Execution history", id="workflow-history")
                    yield Button("GitHub App settings", id="workflow-github-apps")
            yield Static("No workflow running.", id="workflow-progress")
            yield Static("", id="workflow-events")

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
        repository_select = self.query_one("#repository", Select)
        repository_select.set_options(
            [(f"{item.key} — {item.path}", item.key) for item in repositories]
        )
        repository_select.focus()
        if repositories:
            repository_select.value = repositories[0].key
        else:
            self.query_one("#workflow-progress", Static).update(
                "No repositories are configured. Add entries under repositories "
                "in orchestrator.yml."
            )

    def on_resize(self, event: Resize) -> None:
        self._set_layout_mode(event.size.width)

    def _set_layout_mode(self, width: int) -> None:
        self.set_class(width <= 80, "-narrow")
        self.set_class(width > 80, "-wide")

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        self.query_one("#workflow-runner", VerticalScroll).scroll_to_widget(
            event.widget, animate=False, top=True
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "repository" and isinstance(event.value, str):
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
            if event.select.id == f"{prefix}-provider" and isinstance(event.value, str):
                self._load_role_runtime_controls(role, prefix, event.value)
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
            self._update_role_summary(role, prefix, provider, values, capabilities)
            self._update_advanced_indicators(role, prefix, provider, values)
            return

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
        task_provider_select = self.query_one("#task-provider", Select)
        task_provider_select.set_options([(provider, provider) for provider in providers])
        self._suppressed_task_provider_change = (repository, providers[0])
        task_provider_select.value = providers[0]
        self.query_one("#task-selector", TaskSelector).clear_tasks()
        self._role_providers.clear()
        self._role_default_providers = selected_agents.copy()
        self._runtime_inputs.clear()
        self._runtime_defaults.clear()
        self._advanced_open.clear()
        for role, prefix in self._role_fields():
            select = self.query_one(f"#{prefix}-provider", Select)
            select.set_options([(provider, provider) for provider in configured_agents])
            select.value = selected_agents[role]
            self._load_role_runtime_controls(role, prefix, selected_agents[role])
        self._load_github_app_summary(repository)
        self._request_task_load(repository, providers[0])

    @staticmethod
    def _role_fields() -> tuple[tuple[AgentRole, str], ...]:
        return (
            (AgentRole.PLANNER, "planner"),
            (AgentRole.IMPLEMENTER, "implementer"),
            (AgentRole.REVIEWER, "reviewer"),
        )

    def _load_role_runtime_controls(self, role: AgentRole, prefix: str, provider: str) -> None:
        repository = self._selected("#repository")
        if repository is None:
            return
        previous = self._role_providers.get(role)
        if previous is not None:
            self._runtime_inputs[(role, previous)] = self._runtime_values_from_controls(prefix)
        try:
            capabilities = self.controller.provider_runtime_capabilities(repository, provider)
            values = self._runtime_inputs.get((role, provider))
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
        provider_note = (
            "Provider/YAML default"
            if self._role_default_providers.get(role) == provider
            else "this-run provider override"
        )
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

        def status(settings: Any) -> str:
            return (
                f"configured (App ID {settings.app_id})"
                if settings.is_configured
                else "not configured"
            )

        self.query_one("#github-app-summary", Static).update(
            "GitHub App defaults (persisted separately): "
            f"Implementer {status(implementer)}; Reviewer {status(reviewer)}."
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
        elif event.button.id == "workflow-history":
            self._show_history()
        elif event.button.id == "workflow-github-apps":
            from orchestrator.tui.screens.github_app_settings import GitHubAppSettingsScreen

            self.app.push_screen(GitHubAppSettingsScreen())
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
        self.query_one("#workflow-events", Static).update("")
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
        stage = event.stage.value.replace("_", " ").title() if event.stage else ""
        message = event.message or event.status or event.step_status or ""
        self.query_one("#workflow-progress", Static).update(
            f"Execution {event.execution_id}: {stage or event.kind.value} {message}".strip()
        )
        self._event_lines.append(f"{event.kind.value}: {stage or message}")
        self.query_one("#workflow-events", Static).update("\n".join(self._event_lines[-8:]))

    def _workflow_finished(self, execution: WorkflowExecution) -> None:
        self.query_one("#start-workflow", Button).disabled = False
        self.query_one("#workflow-progress", Static).update(
            f"Finished {execution.id}: {execution.status.value} ({execution.current_stage.value})"
        )
        self.notify(f"Workflow finished: {execution.status.value}")

    def _workflow_failed(self, message: str) -> None:
        self.query_one("#start-workflow", Button).disabled = False
        self.query_one("#workflow-progress", Static).update(f"Could not start workflow: {message}")
        self.notify(f"Could not start workflow: {message}", severity="error")

    def _selected(self, selector: str) -> str | None:
        value = self.query_one(selector, Select).value
        return value if isinstance(value, str) else None
