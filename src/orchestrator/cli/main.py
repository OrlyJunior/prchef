from __future__ import annotations

import json
import logging
from dataclasses import asdict
from importlib.resources import as_file, files
from pathlib import Path
from typing import Annotated, Any

import typer
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import OperationalError

import orchestrator
from orchestrator.clients.command_client import CommandClient
from orchestrator.clients.exceptions import GitClientError
from orchestrator.clients.factory import ClientFactory
from orchestrator.clients.git_client import GitClient
from orchestrator.clients.github_issue_client import GitHubIssueClient
from orchestrator.clients.trello_client import TrelloClient
from orchestrator.config.database import build_engine, build_session_factory, resolve_database_url
from orchestrator.config.project_config import (
    ConfigurationError,
    GitHubAppSettings,
    ProjectConfigLoader,
)
from orchestrator.config.settings import Settings
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.pull_request import PullRequest
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.validation_result import ValidationCheck, ValidationResult
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import WorkflowStatus
from orchestrator.providers.concurrency import ProviderConcurrencyCoordinator
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.runtime_logging import configure_logging, redact_text
from orchestrator.services.debug_service import (
    DebugExecutionError,
    DebugInputs,
    DebugService,
    DebugStage,
    parse_debug_stage,
)
from orchestrator.services.execution_dispatcher import ExecutionDispatcher
from orchestrator.services.qdrant_repository_context import (
    QdrantClientManager,
    QdrantRepositoryContextStrategy,
)
from orchestrator.services.task_provider import (
    GitHubIssueTaskProvider,
    TaskProvider,
    TrelloTaskProvider,
)
from orchestrator.services.workflow_service import WorkflowService
from orchestrator.services.workspace_cleaner import WorkspaceCleanupService
from orchestrator.tui.bridge import WorkflowController
from orchestrator.workspace.manager import WorkspaceManager

# Keep non-interactive commands independent from the terminal UI at import
# time. Textual is still a required project dependency, but this prevents an
# incomplete environment from breaking commands such as ``--version`` before
# the user has asked to launch the UI.
OrchestratorApp: type[Any] | None = None


class TextualDependencyError(RuntimeError):
    """Raised when the interactive UI dependency is unavailable."""


app = typer.Typer(
    name="orchestrator",
    help="Autonomous development workflow from Trello cards or GitHub Issues.",
    no_args_is_help=True,
)


logger = logging.getLogger(__name__)


def version_callback(value: bool) -> None:
    if value:
        typer.echo(orchestrator.__version__)
        raise typer.Exit()


@app.callback()
def callback(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=version_callback,
            is_eager=True,
            help="Show the package version and exit.",
        ),
    ] = None,
) -> None:
    pass


def _json_default(value: Any) -> str:
    return str(value)


def _print_execution(execution: WorkflowExecution) -> None:
    typer.echo(json.dumps(asdict(execution), ensure_ascii=False, indent=2, default=_json_default))


def _print_final_summary(execution: WorkflowExecution) -> None:
    logger.info("[workflow] Finished")
    logger.info("Status: %s", execution.status)
    logger.info("Stage: %s", execution.current_stage)
    logger.info("Execution ID: %s", execution.id)
    if execution.error_message:
        logger.info("Reason: %s", redact_text(execution.error_message))


def _repository(settings: Settings) -> WorkflowRepository:
    engine = build_engine(_database_url(settings))
    return WorkflowRepository(build_session_factory(engine))


class MigrationError(RuntimeError):
    pass


def _database_url(settings: Settings) -> str:
    return resolve_database_url(
        database_url=settings.database_url,
        database_path=settings.database_path,
        config_file=settings.config_file,
    )


def _apply_pending_migrations(settings: Settings) -> None:
    try:
        # Migration scripts are package resources so normal wheels and pipx
        # editable installations do not depend on a repository checkout.
        with as_file(files("orchestrator.migrations")) as migrations_directory:
            alembic_config = Config()
            alembic_config.set_main_option("script_location", str(migrations_directory))
            # Alembic stores options in ConfigParser, where % introduces interpolation.
            alembic_config.set_main_option(
                "sqlalchemy.url", _database_url(settings).replace("%", "%%")
            )
            command.upgrade(alembic_config, "head")
    except Exception as exc:
        raise MigrationError(f"Could not apply pending database migrations: {exc}") from exc


def _migrated_repository(settings: Settings) -> WorkflowRepository:
    try:
        _apply_pending_migrations(settings)
    except MigrationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    repository = _repository(settings)
    recover = getattr(repository, "recover_interrupted", None)
    if callable(recover):
        recover()
    return repository


def _provider_coordinator(
    config_loader: ProjectConfigLoader,
) -> ProviderConcurrencyCoordinator:
    """Fix every configured provider's concurrency ceiling for this process.

    Ceilings are read once at startup: changing ``max_concurrency`` requires a
    restart, so a running process never has to reconcile slots held under an
    older limit. Providers only defined per repository register lazily at their
    first use with the conservative default.
    """

    try:
        defaults = config_loader.load_defaults()
    except ConfigurationError:
        return ProviderConcurrencyCoordinator()
    return ProviderConcurrencyCoordinator(
        {name: settings.max_concurrency for name, settings in defaults.providers.items()}
    )


def _execution_dispatcher(coordinator: ProviderConcurrencyCoordinator) -> ExecutionDispatcher:
    """Size the worker pool from the configured ceilings, not from user input."""

    return ExecutionDispatcher(max_workers=coordinator.total_capacity())


def _config_loader(config_file: Path | None) -> ProjectConfigLoader:
    try:
        return ProjectConfigLoader(config_file)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


def _task_provider(
    *,
    settings: Settings,
    config_loader: ProjectConfigLoader,
    trello_card: str | None,
    github_issue: int | None,
    task_id: str | None,
    repository: str | None,
) -> tuple[str, TaskProvider]:
    supplied = sum(value is not None for value in (trello_card, github_issue, task_id))
    if supplied != 1:
        raise typer.BadParameter(
            "Provide exactly one of --trello-card, --github-issue, or --task-id"
        )

    if trello_card is not None:
        return trello_card, TrelloTaskProvider(
            TrelloClient(settings.trello_api_key, settings.trello_token)
        )

    provider_name: str | None
    if github_issue is not None:
        provider_name = "github"
        selected_id = str(github_issue)
    else:
        try:
            provider_name = config_loader.default_task_provider()
        except ConfigurationError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if provider_name is None:
            raise typer.BadParameter(
                "--task-id requires task.provider in the global configuration"
            )
        selected_id = str(task_id)

    if provider_name == "trello":
        return selected_id, TrelloTaskProvider(
            TrelloClient(settings.trello_api_key, settings.trello_token)
        )
    if not repository:
        raise typer.BadParameter(
            "GitHub Issues require --repository in the form owner/repository"
        )
    try:
        planner_app = _planner_app_for_slug(config_loader, repository)
    except ConfigurationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return selected_id, GitHubIssueTaskProvider(
        GitHubIssueClient(CommandClient(), planner_app=planner_app), repository
    )


def _planner_app_for_slug(
    config_loader: ProjectConfigLoader, repository_slug: str
) -> GitHubAppSettings | None:
    """Resolve credentials only when a slug identifies one Planner identity."""
    if not config_loader.has_planner_github_app():
        return None
    matches: list[tuple[str, GitHubAppSettings | None]] = []
    for entry in config_loader.list_repositories():
        if not entry.path.is_dir():
            continue
        try:
            configured_slug = GitClient(CommandClient(), entry.path).github_repository()
        except GitClientError:
            continue
        if configured_slug.casefold() != repository_slug.strip().casefold():
            continue
        selection = DevelopmentTask(
            id="issue-selection", source="github", title="", description="",
            url="", repository=entry.key,
        )
        project = config_loader.load(selection, entry.key)
        planner_app = project.integrations.github.planner_app
        matches.append((entry.key, planner_app if planner_app.is_configured else None))
    if not matches:
        return None
    selected_app = matches[0][1]
    if any(planner_app != selected_app for _, planner_app in matches[1:]):
        keys = ", ".join(key for key, _ in matches)
        raise ConfigurationError(
            f"GitHub repository {repository_slug} matches multiple configured repositories "
            f"with different Planner App identities ({keys}); the --repository slug cannot "
            "select one identity. Use the TUI repository selection or make their Planner "
            "App settings identical"
        )
    return selected_app


def _debug_task_provider(
    settings: Settings, source: str, repository: str | None,
    config_loader: ProjectConfigLoader | None = None,
) -> TaskProvider:
    source = source.casefold()
    if source == "trello":
        return TrelloTaskProvider(TrelloClient(settings.trello_api_key, settings.trello_token))
    if source == "github":
        if not isinstance(repository, str) or not repository.strip():
            raise ConfigurationError(
                "GitHub task input requires --repository owner/repository"
            )
        return GitHubIssueTaskProvider(
            GitHubIssueClient(
                CommandClient(),
                planner_app=_planner_app_for_slug(config_loader, repository)
                if config_loader is not None else None,
            ), repository.strip()
        )
    raise ConfigurationError(f"Unsupported task provider: {source}")


def _tui_task_provider(
    settings: Settings,
    config_loader: ProjectConfigLoader,
    source: str,
    repository: str,
) -> TaskProvider:
    """Build a task provider for a selected configured repository.

    GitHub's issue API needs an ``owner/repository`` slug, which remains an
    integration detail derived from the selected repository's existing Git
    client rather than a UI-specific configuration field.
    """

    normalized = source.casefold()
    if normalized == "trello":
        return TrelloTaskProvider(TrelloClient(settings.trello_api_key, settings.trello_token))
    if normalized != "github":
        raise ConfigurationError(f"Unsupported task provider: {source}")
    selection = DevelopmentTask(
        id="tui-selection",
        source="system",
        title="Interactive workflow selection",
        description="Resolve the selected repository for GitHub issue browsing.",
        url="",
        repository=repository,
    )
    project_config = config_loader.load(selection, repository)
    repository_slug = GitClient(CommandClient(), project_config.path).github_repository()
    return GitHubIssueTaskProvider(
        GitHubIssueClient(
            CommandClient(), planner_app=project_config.integrations.github.planner_app
        ), repository_slug
    )


def _orchestrator_app_type() -> type[Any]:
    """Load the Textual application only when the interactive command runs."""

    global OrchestratorApp
    if OrchestratorApp is not None:
        return OrchestratorApp
    try:
        from orchestrator.tui.app import OrchestratorApp as textual_app
    except ModuleNotFoundError as exc:
        if exc.name != "textual":
            raise
        raise TextualDependencyError(
            "The terminal UI requires the Textual dependency. "
            "Reinstall the project dependencies and try again."
        ) from exc
    OrchestratorApp = textual_app
    return OrchestratorApp


def _read_debug_text(path: Path, label: str) -> str:
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise typer.BadParameter(f"Cannot read {label} file {path}: {exc}") from exc
    if not value.strip():
        raise typer.BadParameter(f"{label} file must not be empty: {path}")
    return value


def _read_debug_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise typer.BadParameter(f"Cannot parse {label} JSON file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter(f"{label} JSON file must contain an object: {path}")
    return payload


def _read_debug_spec(path: Path) -> RepositorySpec:
    try:
        return RepositorySpec(**_read_debug_json(path, "spec"))
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(f"Invalid spec JSON in {path}: {exc}") from exc


def _read_debug_validation(path: Path) -> ValidationResult:
    payload = _read_debug_json(path, "validation")
    raw_checks = payload.get("checks", [])
    if not isinstance(raw_checks, list) or not isinstance(payload.get("success"), bool):
        raise typer.BadParameter(
            f"Invalid validation JSON in {path}: expected boolean success and checks list"
        )
    try:
        checks = [ValidationCheck(**check) for check in raw_checks]
        return ValidationResult(
            success=payload["success"],
            checks=checks,
            error_message=payload.get("error_message"),
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise typer.BadParameter(f"Invalid validation JSON in {path}: {exc}") from exc


def _read_debug_pull_request(path: Path) -> PullRequest:
    try:
        return PullRequest(**_read_debug_json(path, "pull-request"))
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(f"Invalid pull-request JSON in {path}: {exc}") from exc


@app.command()
def providers(
    config: Annotated[
        Path | None, typer.Option("--config", help="Global YAML configuration")
    ] = None,
) -> None:
    """List the agent providers configured in the global defaults."""
    settings = Settings()
    if config is not None:
        settings.config_file = config
    try:
        project_config = _config_loader(settings.config_file).load_defaults()
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    for name, provider in project_config.providers.items():
        typer.echo(f"{name}  {provider.type}")


@app.command()
def tui(
    config: Annotated[
        Path | None, typer.Option("--config", help="Global YAML configuration")
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show additional runtime details")
    ] = False,
) -> None:
    """Launch the interactive terminal UI for workflow control."""

    settings = Settings()
    if config is not None:
        settings.config_file = config
    configure_logging(verbose=verbose, configured_level=settings.log_level)
    config_loader = _config_loader(settings.config_file)
    try:
        config_loader.load_defaults()
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    try:
        application_type = _orchestrator_app_type()
    except TextualDependencyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    workflow_repository = _migrated_repository(settings)
    coordinator = _provider_coordinator(config_loader)
    dispatcher = _execution_dispatcher(coordinator)
    # One long-lived index client per configured store: local Qdrant takes an
    # exclusive lock, so concurrent executions must share it.
    index_clients = QdrantClientManager()
    controller = WorkflowController(
        workflow_repository=workflow_repository,
        config_loader=config_loader,
        client_factory=ClientFactory(),
        task_provider_factory=lambda source, repository: _tui_task_provider(
            settings, config_loader, source, repository
        ),
        verbose=verbose,
        dispatcher=dispatcher,
        provider_coordinator=coordinator,
        repository_index_clients=index_clients,
    )
    try:
        application_type(controller).run()
    finally:
        controller.shutdown_for_exit()
        index_clients.close()


@app.command()
def run(
    trello_card: Annotated[
        str | None, typer.Option("--trello-card", help="Trello card ID")
    ] = None,
    github_issue: Annotated[
        int | None, typer.Option("--github-issue", help="GitHub issue number")
    ] = None,
    task_id: Annotated[
        str | None,
        typer.Option("--task-id", help="Task ID using task.provider from configuration"),
    ] = None,
    repository: Annotated[
        str | None,
        typer.Option(
            "--repository",
            help="Repository key/path; GitHub Issues require owner/repository",
        ),
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", help="Global YAML configuration")
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show additional subprocess and retry details")
    ] = False,
) -> None:
    settings = Settings()
    if config is not None:
        settings.config_file = config
    configure_logging(verbose=verbose, configured_level=settings.log_level)
    config_loader = _config_loader(settings.config_file)
    try:
        config_loader.load_defaults()
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    workflow_repository = _migrated_repository(settings)
    selected_id, provider = _task_provider(
        settings=settings,
        config_loader=config_loader,
        trello_card=trello_card,
        github_issue=github_issue,
        task_id=task_id,
        repository=repository,
    )
    coordinator = _provider_coordinator(config_loader)
    dispatcher = _execution_dispatcher(coordinator)
    index_clients = QdrantClientManager()
    service = WorkflowService(
        workflow_repository=workflow_repository,
        task_provider=provider,
        config_loader=config_loader,
        client_factory=ClientFactory(),
        verbose=verbose,
        dispatcher=dispatcher,
        provider_coordinator=coordinator,
        repository_index_clients=index_clients,
    )
    try:
        execution = service.run(selected_id, repository)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except OperationalError as exc:
        typer.echo(f"Database access failed after migrations: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        dispatcher.shutdown(wait=False)
        index_clients.close()
    _print_execution(execution)
    _print_final_summary(execution)
    if execution.status not in {WorkflowStatus.APPROVED, WorkflowStatus.CHANGES_REQUESTED}:
        raise typer.Exit(code=1)


@app.command()
def respond(
    execution_id: str,
    message: Annotated[
        str, typer.Option("--message", "-m", help="Human clarification")
    ],
    config: Annotated[
        Path | None, typer.Option("--config", help="Global YAML configuration")
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show additional subprocess and retry details")
    ] = False,
) -> None:
    """Persist a clarification and resume an existing human-blocked execution."""
    if not message.strip():
        raise typer.BadParameter("--message must not be empty")
    settings = Settings()
    if config is not None:
        settings.config_file = config
    configure_logging(verbose=verbose, configured_level=settings.log_level)
    workflow_repository = _migrated_repository(settings)
    try:
        execution = workflow_repository.get_execution(execution_id)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if execution.status is not WorkflowStatus.NEEDS_HUMAN:
        raise typer.BadParameter(f"Execution {execution_id} is not awaiting human input")
    if not execution.task_snapshot:
        raise typer.BadParameter(
            "Execution has no persisted task snapshot and cannot be resumed"
        )
    task = DevelopmentTask(**execution.task_snapshot)
    config_loader = _config_loader(settings.config_file)
    if execution.task_source == "trello":
        provider: TaskProvider = TrelloTaskProvider(
            TrelloClient(settings.trello_api_key, settings.trello_token)
        )
    elif execution.task_source == "github":
        project_config = config_loader.load(
            task, execution.repository_ref or execution.repository or None
        )
        repository_slug = GitClient(CommandClient(), project_config.path).github_repository()
        provider = GitHubIssueTaskProvider(
            GitHubIssueClient(
                CommandClient(), planner_app=project_config.integrations.github.planner_app
            ), repository_slug
        )
    else:
        raise typer.BadParameter(
            f"Unsupported persisted task provider: {execution.task_source}"
        )
    coordinator = _provider_coordinator(config_loader)
    dispatcher = _execution_dispatcher(coordinator)
    index_clients = QdrantClientManager()
    service = WorkflowService(
        workflow_repository=workflow_repository,
        task_provider=provider,
        config_loader=config_loader,
        client_factory=ClientFactory(),
        verbose=verbose,
        dispatcher=dispatcher,
        provider_coordinator=coordinator,
        repository_index_clients=index_clients,
    )
    try:
        result = service.respond_and_resume(execution_id, message)
    finally:
        dispatcher.shutdown(wait=False)
        index_clients.close()
    _print_execution(result)
    _print_final_summary(result)
    if result.status not in {WorkflowStatus.APPROVED, WorkflowStatus.CHANGES_REQUESTED}:
        raise typer.Exit(code=1)


@app.command()
def debug(
    stage: Annotated[
        str,
        typer.Option(
            "--stage",
            "-s",
            help="Stage: task-loading, spec-generation, implementation, validation, or review",
        ),
    ],
    from_execution: Annotated[
        str | None,
        typer.Option(
            "--from-execution",
            "--execution-id",
            "-e",
            help="Optional persisted execution used as read-only input defaults",
        ),
    ] = None,
    trello_card: Annotated[
        str | None, typer.Option("--trello-card", help="Trello card ID for task loading")
    ] = None,
    github_issue: Annotated[
        int | None, typer.Option("--github-issue", help="GitHub issue number for task loading")
    ] = None,
    task_id: Annotated[
        str | None, typer.Option("--task-id", help="Task ID using configured task.provider")
    ] = None,
    task_source: Annotated[
        str | None, typer.Option("--task-source", help="Task provider: trello or github")
    ] = None,
    repository: Annotated[
        str | None,
        typer.Option(
            "--repository",
            help="Registered repository key/path; required for direct GitHub task loading",
        ),
    ] = None,
    base_branch: Annotated[
        str | None, typer.Option("--base-branch", help="Base branch context for spec/review")
    ] = None,
    feature_branch: Annotated[
        str | None, typer.Option("--feature-branch", help="Feature branch context for review")
    ] = None,
    prompt: Annotated[
        str | None, typer.Option("--prompt", help="Explicit implementation input")
    ] = None,
    prompt_file: Annotated[
        Path | None, typer.Option("--prompt-file", help="Read explicit implementation input")
    ] = None,
    spec_file: Annotated[
        Path | None, typer.Option("--spec-file", help="JSON RepositorySpec input")
    ] = None,
    validation_file: Annotated[
        Path | None, typer.Option("--validation-file", help="JSON ValidationResult input")
    ] = None,
    pull_request_file: Annotated[
        Path | None, typer.Option("--pull-request-file", help="JSON PullRequest input")
    ] = None,
    diff_file: Annotated[
        Path | None, typer.Option("--diff-file", help="Read implementation diff for review")
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", help="Global YAML configuration")
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show additional subprocess and retry details")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Validate stage inputs and report the planned operation without executing it",
        ),
    ] = False,
) -> None:
    try:
        debug_stage = parse_debug_stage(stage)
    except DebugExecutionError as exc:
        raise typer.BadParameter(str(exc), param_hint="--stage") from exc

    settings = Settings()
    if config is not None:
        settings.config_file = config
    configure_logging(verbose=verbose, configured_level=settings.log_level)

    origins = sum(value is not None for value in (trello_card, github_issue, task_id))
    if origins > 1:
        raise typer.BadParameter(
            "Provide at most one of --trello-card, --github-issue, or --task-id"
        )
    if task_source is not None and task_source.casefold() not in {"trello", "github"}:
        raise typer.BadParameter("--task-source must be 'trello' or 'github'")
    if github_issue is not None:
        task_source = "github"
        task_id = str(github_issue)
    elif trello_card is not None:
        task_source = "trello"
        task_id = trello_card

    if prompt is not None and prompt_file is not None:
        raise typer.BadParameter("Use only one of --prompt or --prompt-file")
    if (
        (prompt is not None or prompt_file is not None)
        and debug_stage is not DebugStage.IMPLEMENTATION
    ):
        raise typer.BadParameter("--prompt/--prompt-file is only valid for implementation debug")
    if spec_file is not None and debug_stage not in {
        DebugStage.IMPLEMENTATION,
        DebugStage.REVIEW,
    }:
        raise typer.BadParameter("--spec-file is only valid for implementation or review debug")
    if validation_file is not None and debug_stage is not DebugStage.REVIEW:
        raise typer.BadParameter("--validation-file is only valid for review debug")
    if pull_request_file is not None and debug_stage is not DebugStage.REVIEW:
        raise typer.BadParameter("--pull-request-file is only valid for review debug")
    if diff_file is not None and debug_stage is not DebugStage.REVIEW:
        raise typer.BadParameter("--diff-file is only valid for review debug")

    prompt_value = prompt
    if prompt_file is not None:
        prompt_value = _read_debug_text(prompt_file, "prompt")
    explicit_spec = _read_debug_spec(spec_file) if spec_file is not None else None
    explicit_validation = (
        _read_debug_validation(validation_file) if validation_file is not None else None
    )
    explicit_pull_request = (
        _read_debug_pull_request(pull_request_file)
        if pull_request_file is not None
        else None
    )
    explicit_diff = _read_debug_text(diff_file, "diff") if diff_file is not None else None

    execution: WorkflowExecution | None = None
    if from_execution is not None:
        workflow_repository = _migrated_repository(settings)
        try:
            execution = workflow_repository.get_execution(from_execution)
        except KeyError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

    logger.info(
        "[debug] %s stage%s%s",
        debug_stage.value,
        f" from execution {execution.id}" if execution is not None else "",
        " (dry-run)" if dry_run else "",
    )
    explicit_inputs = DebugInputs(
        task_source=task_source,
        task_id=task_id,
        repository=repository,
        base_branch=base_branch,
        feature_branch=feature_branch,
        prompt=prompt_value,
        spec=explicit_spec,
        validation=explicit_validation,
        pull_request=explicit_pull_request,
        diff=explicit_diff,
    )
    index_clients = QdrantClientManager()
    service = DebugService(
        config_loader=_config_loader(settings.config_file),
        client_factory=ClientFactory(),
        task_provider_factory=lambda source, repo: _debug_task_provider(
            settings, source, repo, _config_loader(settings.config_file)
        ),
        repository_index_clients=index_clients,
    )
    try:
        result = service.execute(execution, debug_stage, inputs=explicit_inputs, dry_run=dry_run)
    except Exception as exc:
        message = redact_text(str(exc))
        logger.error("[debug] %s failed: %s", debug_stage.value, message)
        typer.echo(
            json.dumps(
                {
                    "execution_id": execution.id if execution is not None else None,
                    "stage": debug_stage.value,
                    "dry_run": dry_run,
                    "execution_mutated": False,
                    "error": message,
                },
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            )
        )
        raise typer.Exit(code=1) from exc
    finally:
        index_clients.close()
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))


@app.command()
def show(
    execution_id: str,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    settings = Settings()
    if config is not None:
        settings.config_file = config
    configure_logging(configured_level=settings.log_level)
    try:
        execution = _migrated_repository(settings).get_execution(execution_id)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _print_execution(execution)


@app.command("cleanup-workspaces")
def cleanup_workspaces(
    repository: Annotated[
        str,
        typer.Option(
            "--repository",
            help="Registered repository key or local control-checkout path to clean",
        ),
    ],
    config: Annotated[
        Path | None, typer.Option("--config", help="Global YAML configuration")
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="List retention decisions without deleting worktrees"),
    ] = False,
    include_failed: Annotated[
        bool,
        typer.Option(
            "--include-failed",
            help="Also clean FAILED and NEEDS_HUMAN workspaces; active runs remain protected",
        ),
    ] = False,
    all_workspaces: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Alias for --include-failed; active and unmanaged workspaces remain protected",
        ),
    ] = False,
) -> None:
    """Inspect or remove retained execution worktrees for one configured repository."""
    settings = Settings()
    if config is not None:
        settings.config_file = config
    configure_logging(configured_level=settings.log_level)
    config_loader = _config_loader(settings.config_file)
    cleanup_task = DevelopmentTask(
        id="workspace-cleanup",
        source="system",
        title="Workspace cleanup",
        description="Operational cleanup of retained execution worktrees.",
        url="",
        repository=repository,
    )
    try:
        project_config = config_loader.load(cleanup_task, repository)
        workspace_root = WorkspaceManager(
            GitClient(CommandClient(), project_config.path),
            project_config.path,
            project_config.workspace,
        ).workspace_root()
    except (ConfigurationError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    delete_index_points = None
    if (
        project_config.repository_context.strategy == "qdrant"
        and project_config.configured_repository_key is not None
    ):
        index_strategy = QdrantRepositoryContextStrategy(
            project_config.repository_context,
            project_config.configured_repository_key,
            project_config.path,
        )
        delete_index_points = index_strategy.delete_worktree_points
    service = WorkspaceCleanupService(
        _migrated_repository(settings),
        GitClient(CommandClient(), project_config.path),
        project_config.path,
        workspace_root,
        delete_index_points=delete_index_points,
    )
    report = service.cleanup(
        dry_run=dry_run,
        include_failed=include_failed or all_workspaces,
    )
    typer.echo(
        json.dumps(
            {
                "dry_run": report.dry_run,
                "include_failed": include_failed or all_workspaces,
                "workspaces": [asdict(item) for item in report.workspaces],
                "summary": {
                    "eligible": report.eligible_count,
                    "removed": report.removed_count,
                    "errors": report.error_count,
                },
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )
    if report.error_count:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
