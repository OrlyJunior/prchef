from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy.exc import OperationalError

from orchestrator.clients.factory import ClientFactory
from orchestrator.clients.trello_client import TrelloClient
from orchestrator.config.database import build_engine, build_session_factory
from orchestrator.config.project_config import ProjectConfigLoader
from orchestrator.config.settings import Settings
from orchestrator.models.workflow_execution import WorkflowExecution
from orchestrator.models.workflow_status import WorkflowStatus
from orchestrator.repositories.workflow_repository import WorkflowRepository
from orchestrator.services.workflow_service import WorkflowService

app = typer.Typer(
    name="orchestrator",
    help="Autonomous development workflow: Trello -> Jules -> Codex -> GitHub.",
    no_args_is_help=True,
)


def _json_default(value: Any) -> str:
    return str(value)


def _print_execution(execution: WorkflowExecution) -> None:
    typer.echo(json.dumps(asdict(execution), ensure_ascii=False, indent=2, default=_json_default))


def _repository(settings: Settings) -> WorkflowRepository:
    engine = build_engine(settings.database_url)
    return WorkflowRepository(build_session_factory(engine))


@app.command()
def run(
    trello_card: Annotated[str, typer.Option("--trello-card", help="Trello card ID")],
    repository: Annotated[
        str | None,
        typer.Option("--repository", help="Configured repository key or absolute path"),
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", help="Global YAML configuration")
    ] = None,
) -> None:
    settings = Settings()
    if config is not None:
        settings.config_file = config
    workflow_repository = _repository(settings)
    service = WorkflowService(
        workflow_repository=workflow_repository,
        trello_client=TrelloClient(settings.trello_api_key, settings.trello_token),
        config_loader=ProjectConfigLoader(settings.config_file),
        client_factory=ClientFactory(settings.jules_api_key),
    )
    try:
        execution = service.run(trello_card, repository)
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
