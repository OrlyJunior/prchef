from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer
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


def _json_default(value: Any) -> str:
    return str(value)


def _print_execution(execution: WorkflowExecution) -> None:
    typer.echo(json.dumps(asdict(execution), ensure_ascii=False, indent=2, default=_json_default))


def _repository(settings: Settings) -> WorkflowRepository:
    engine = build_engine(settings.database_url)
    return WorkflowRepository(build_session_factory(engine))


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
) -> None:
    settings = Settings()
    if config is not None:
        settings.config_file = config
    config_loader = ProjectConfigLoader(settings.config_file)
    selected_id, provider = _task_provider(
        settings=settings,
        config_loader=config_loader,
        trello_card=trello_card,
        github_issue=github_issue,
        task_id=task_id,
        repository=repository,
    )
    workflow_repository = _repository(settings)
    service = WorkflowService(
        workflow_repository=workflow_repository,
        task_provider=provider,
        config_loader=config_loader,
        client_factory=ClientFactory(settings.jules_api_key),
    )
    try:
        execution = service.run(selected_id, repository)
    except OperationalError as exc:
        typer.echo(f"Database is not migrated; run 'alembic upgrade head'. Detail: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _print_execution(execution)
    if execution.status not in {WorkflowStatus.APPROVED, WorkflowStatus.CHANGES_REQUESTED}:
        raise typer.Exit(code=1)


@app.command()
def show(
    execution_id: str,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    settings = Settings()
    if config is not None:
        settings.config_file = config
    try:
        execution = _repository(settings).get_execution(execution_id)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _print_execution(execution)


if __name__ == "__main__":
    app()
