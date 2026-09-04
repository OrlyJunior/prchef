from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import OperationalError

import orchestrator
from orchestrator.clients.command_client import CommandClient
from orchestrator.clients.factory import ClientFactory
from orchestrator.clients.github_issue_client import GitHubIssueClient
from orchestrator.clients.trello_client import TrelloClient
from orchestrator.config.database import build_engine, build_session_factory
from orchestrator.config.project_config import ConfigurationError, ProjectConfigLoader
from orchestrator.config.settings import Settings
from orchestrator.models.pull_request import PullRequest
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.validation_result import ValidationCheck, ValidationResult
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import WorkflowStatus
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.runtime_logging import configure_logging, redact_text
from orchestrator.services.debug_service import (
    DebugExecutionError,
    DebugInputs,
    DebugService,
    DebugStage,
    parse_debug_stage,
)
from orchestrator.services.task_provider import (
    GitHubIssueTaskProvider,
    TaskProvider,
    TrelloTaskProvider,
)
from orchestrator.services.workflow_service import WorkflowService

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
    engine = build_engine(settings.database_url)
    return WorkflowRepository(build_session_factory(engine))


class MigrationError(RuntimeError):
    pass


def _apply_pending_migrations(settings: Settings) -> None:
    project_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(project_root / "migrations"))
    alembic_config.set_main_option("sqlalchemy.url", settings.database_url)
    try:
        command.upgrade(alembic_config, "head")
    except Exception as exc:
        raise MigrationError(f"Could not apply pending database migrations: {exc}") from exc


def _migrated_repository(settings: Settings) -> WorkflowRepository:
    try:
        _apply_pending_migrations(settings)
    except MigrationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    return _repository(settings)


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
    return selected_id, GitHubIssueTaskProvider(
        GitHubIssueClient(CommandClient()), repository
    )


def _debug_task_provider(
    settings: Settings, source: str, repository: str | None
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
            GitHubIssueClient(CommandClient()), repository.strip()
        )
    raise ConfigurationError(f"Unsupported task provider: {source}")


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
        project_config = ProjectConfigLoader(settings.config_file).load_defaults()
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    for name, provider in project_config.providers.items():
        typer.echo(f"{name}  {provider.type}")


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
    workflow_repository = _migrated_repository(settings)
    config_loader = ProjectConfigLoader(settings.config_file)
    selected_id, provider = _task_provider(
        settings=settings,
        config_loader=config_loader,
        trello_card=trello_card,
        github_issue=github_issue,
        task_id=task_id,
        repository=repository,
    )
    service = WorkflowService(
        workflow_repository=workflow_repository,
        task_provider=provider,
        config_loader=config_loader,
        client_factory=ClientFactory(settings.jules_api_key),
    )
    try:
        execution = service.run(selected_id, repository)
    except OperationalError as exc:
        typer.echo(f"Database access failed after migrations: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _print_execution(execution)
    _print_final_summary(execution)
    if execution.status not in {WorkflowStatus.APPROVED, WorkflowStatus.CHANGES_REQUESTED}:
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
    service = DebugService(
        config_loader=ProjectConfigLoader(settings.config_file),
        client_factory=ClientFactory(settings.jules_api_key),
        task_provider_factory=lambda source, repo: _debug_task_provider(settings, source, repo),
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


if __name__ == "__main__":
    app()
