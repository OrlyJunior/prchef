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

from orchestrator.clients.command_client import CommandClient
from orchestrator.clients.factory import ClientFactory
from orchestrator.clients.github_issue_client import GitHubIssueClient
from orchestrator.clients.trello_client import TrelloClient
from orchestrator.config.database import build_engine, build_session_factory
from orchestrator.config.project_config import ConfigurationError, ProjectConfigLoader
from orchestrator.config.settings import Settings
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import WorkflowStatus
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.runtime_logging import configure_logging, redact_text
from orchestrator.services.debug_service import (
    DebugExecutionError,
    DebugService,
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


def _debug_task_provider(settings: Settings, execution: WorkflowExecution) -> TaskProvider:
    source = execution.task_source.casefold()
    if source == "trello":
        return TrelloTaskProvider(TrelloClient(settings.trello_api_key, settings.trello_token))
    if source == "github":
        snapshot = execution.task_snapshot or {}
        repository = snapshot.get("repository")
        metadata = snapshot.get("metadata")
        if not isinstance(repository, str) or not repository.strip():
            repository = metadata.get("repository") if isinstance(metadata, dict) else None
        if not isinstance(repository, str) or not repository.strip():
            raise ConfigurationError(
                "task-loading debug for GitHub requires repository in persisted task_snapshot"
            )
        return GitHubIssueTaskProvider(
            GitHubIssueClient(CommandClient()), repository.strip()
        )
    raise ConfigurationError(f"Unsupported persisted task source: {execution.task_source}")


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
    execution_id: Annotated[
        str, typer.Option("--execution-id", "-e", help="Persisted execution to inspect")
    ],
    stage: Annotated[
        str,
        typer.Option(
            "--stage",
            "-s",
            help="Stage: task-loading, spec-generation, implementation, validation, or review",
        ),
    ],
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
    workflow_repository = _migrated_repository(settings)
    try:
        execution = workflow_repository.get_execution(execution_id)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    logger.info(
        "[debug] %s stage for execution %s%s",
        debug_stage.value,
        execution.id,
        " (dry-run)" if dry_run else "",
    )
    service = DebugService(
        config_loader=ProjectConfigLoader(settings.config_file),
        client_factory=ClientFactory(settings.jules_api_key),
        task_provider_factory=lambda item: _debug_task_provider(settings, item),
    )
    try:
        result = service.execute(execution, debug_stage, dry_run=dry_run)
    except Exception as exc:
        message = redact_text(str(exc))
        logger.error("[debug] %s failed: %s", debug_stage.value, message)
        typer.echo(
            json.dumps(
                {
                    "execution_id": execution.id,
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
