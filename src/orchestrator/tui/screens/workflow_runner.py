"""Workflow launch and live-progress screen."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from textual import work
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
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
    """Collect launch selections and observe the existing workflow service."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self._event_lines: list[str] = []
        self._role_providers: dict[AgentRole, str] = {}
        self._runtime_inputs: dict[tuple[AgentRole, str], dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        with Container(id="workflow-runner"):
            yield Static("Start workflow", classes="screen-title")
            with Vertical(classes="workflow-form"):
                yield Static("Repository", classes="field-label")
                yield Select([], prompt="Configured repository", id="repository")
                yield Static("Task provider", classes="field-label")
                yield Select([], prompt="Task provider", id="task-provider")
                yield TaskSelector(id="task-selector")
                yield Static(
                    "Agent settings — temporary for this run; YAML defaults are unchanged.",
                    id="runtime-settings-note",
                )
                for role, prefix in self._role_fields():
                    yield Static(role.value.title(), classes="field-label", id=f"{prefix}-title")
                    yield Static("Provider", id=f"{prefix}-provider-label")
                    yield Select(
                        [], prompt=f"{role.value.title()} provider", id=f"{prefix}-provider"
                    )
                    yield Static("Model (free-form)", id=f"{prefix}-model-label")
                    yield Input(placeholder="Model identifier", id=f"{prefix}-model")
                    yield Static("Reasoning effort", id=f"{prefix}-reasoning-label")
                    yield Input(placeholder="Reasoning effort", id=f"{prefix}-reasoning")
                    yield Static("Temperature", id=f"{prefix}-temperature-label")
                    yield Input(placeholder="0.0–2.0", id=f"{prefix}-temperature")
                    yield Static("Max output tokens", id=f"{prefix}-max-output-tokens-label")
                    yield Input(placeholder="Provider default", id=f"{prefix}-max-output-tokens")
                    yield Static("", id=f"{prefix}-effective")
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
        try:
            repositories = self.controller.repositories()
        except Exception as exc:
            self.query_one("#workflow-progress", Static).update(
                f"Cannot load configured repositories: {exc}"
            )
            return
        options = [(f"{item.key} — {item.path}", item.key) for item in repositories]
        self.query_one("#repository", Select).set_options(options)
        if not repositories:
            self.query_one("#workflow-progress", Static).update(
                "No repositories are configured. Add entries under repositories "
                "in orchestrator.yml."
            )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "repository" and isinstance(event.value, str):
            self._load_repository_options(event.value)
            return
        for role, prefix in self._role_fields():
            if event.select.id == f"{prefix}-provider" and isinstance(event.value, str):
                self._load_role_runtime_controls(role, prefix, event.value)
                return

    def on_input_changed(self, event: Input.Changed) -> None:
        """Keep the effective temporary-override summary current while typing."""

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
            self._update_effective_summary(
                role,
                prefix,
                provider,
                self._runtime_values_from_controls(prefix),
                capabilities.supports_generation,
            )
            return

    def _load_repository_options(self, repository: str) -> None:
        try:
            providers = self.controller.task_providers(repository)
            configured_agents = self.controller.configured_agent_providers(repository)
            selected_agents = self.controller.agent_providers(repository)
        except Exception as exc:
            self.notify(f"Could not resolve repository configuration: {exc}", severity="error")
            return

        self.query_one("#task-provider", Select).set_options(
            [(provider, provider) for provider in providers]
        )
        self.query_one("#task-provider", Select).value = providers[0]
        self.query_one("#task-selector", TaskSelector).clear_tasks()
        self._role_providers.clear()
        self._runtime_inputs.clear()
        for role, prefix in self._role_fields():
            select = self.query_one(f"#{prefix}-provider", Select)
            select.set_options([(provider, provider) for provider in configured_agents])
            select.value = selected_agents[role]
            self._load_role_runtime_controls(role, prefix, selected_agents[role])
        self._load_github_app_summary(repository)

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
        self._set_runtime_control(prefix, "model", capabilities.supports_model, values.get("model"))
        self._set_runtime_control(
            prefix,
            "reasoning",
            capabilities.supports_reasoning_effort,
            values.get("reasoning_effort"),
        )
        generation = values.get("generation", {})
        if not isinstance(generation, dict):
            generation = {}
        self._set_runtime_control(
            prefix, "temperature", capabilities.supports_generation, generation.get("temperature")
        )
        self._set_runtime_control(
            prefix,
            "max-output-tokens",
            capabilities.supports_generation,
            generation.get("max_output_tokens"),
        )
        self._update_effective_summary(
            role, prefix, provider, values, capabilities.supports_generation
        )

    def _set_runtime_control(
        self, prefix: str, name: str, visible: bool, value: Any
    ) -> None:
        self.query_one(f"#{prefix}-{name}-label", Static).display = visible
        input_widget = self.query_one(f"#{prefix}-{name}", Input)
        input_widget.display = visible
        if visible:
            input_widget.value = "" if value is None else str(value)

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

    def _update_effective_summary(
        self,
        role: AgentRole,
        prefix: str,
        provider: str,
        values: dict[str, Any],
        has_generation: bool,
    ) -> None:
        lines = [f"{role.value.title()} — this run only", f"Provider: {provider}"]
        if "model" in values:
            lines.append(f"Model: {values['model'] or 'provider default'}")
        if "reasoning_effort" in values:
            lines.append(f"Reasoning: {values['reasoning_effort'] or 'provider default'}")
        if has_generation and isinstance(values.get("generation"), dict):
            generation = values["generation"]
            lines.append(f"Temperature: {generation.get('temperature', 'provider default')}")
            lines.append(
                "Max output tokens: "
                f"{generation.get('max_output_tokens') or 'provider default'}"
            )
        self.query_one(f"#{prefix}-effective", Static).update("\n".join(lines))

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
        if event.button.id == "load-tasks":
            repository = self._selected("#repository")
            provider = self._selected("#task-provider")
            if repository is None or provider is None:
                self.notify("Choose a repository and task provider first.", severity="warning")
                return
            self._load_tasks(repository, provider)
        elif event.button.id == "start-workflow":
            self._start_selected_workflow()
        elif event.button.id == "workflow-history":
            self._show_history()
        elif event.button.id == "workflow-github-apps":
            from orchestrator.tui.screens.github_app_settings import GitHubAppSettingsScreen

            self.app.push_screen(GitHubAppSettingsScreen())

    def _show_history(self) -> None:
        from orchestrator.tui.screens.execution_history import ExecutionHistoryScreen

        self.app.push_screen(ExecutionHistoryScreen())

    @work(thread=True, exclusive=True)
    def _load_tasks(self, repository: str, provider: str) -> None:
        try:
            tasks = self.controller.list_tasks(repository=repository, task_provider=provider)
        except Exception as exc:
            self.app.call_from_thread(self._task_loading_failed, str(exc))
            return
        self.app.call_from_thread(self._set_available_tasks, tasks, provider)

    def _set_available_tasks(self, tasks: list[DevelopmentTask], provider: str) -> None:
        self.query_one("#task-selector", TaskSelector).set_tasks(tasks, provider=provider)

    def _task_loading_failed(self, message: str) -> None:
        self.notify(f"Could not load tasks: {message}", severity="error")

    def _start_selected_workflow(self) -> None:
        repository = self._selected("#repository")
        task_provider = self._selected("#task-provider")
        task_id = self.query_one("#task-id", Input).value
        provider_selections = {
            role: self._selected(f"#{prefix}-provider") for role, prefix in self._role_fields()
        }
        if repository is None or task_provider is None or not task_id.strip():
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
