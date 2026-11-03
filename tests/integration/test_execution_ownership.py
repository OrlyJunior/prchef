from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from typing import Any
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from prchef.config.database import build_engine, build_session_factory
from prchef.models.code_review import CodeReview
from prchef.models.development_task import DevelopmentTask
from prchef.models.implementation_result import ImplementationResult
from prchef.models.pull_request import PullRequest
from prchef.models.repository_spec import RepositorySpec
from prchef.models.validation_result import ValidationResult
from prchef.models.workflow_event import WorkflowEvent, WorkflowEventKind
from prchef.models.workflow_interruption import ExecutionOwnershipLostError
from prchef.models.workflow_status import (
    PolishOutcome,
    ReviewDecision,
    StepStatus,
    WorkflowStage,
    WorkflowStatus,
)
from prchef.repositories.workflow_repository import WorkflowRepository
from prchef.services.task_provider import TrelloTaskProvider
from prchef.services.workflow_service import WorkflowService
from prchef.tui.bridge import WorkflowController
from tests.unit.fakes import FakeClientFactory, FakeConfigLoader, FakeTrelloClient


@dataclass
class Clock:
    now: datetime = datetime(2026, 10, 31, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def migration_config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config()
    config.set_main_option("script_location", str(root / "src/prchef/migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def repositories(
    tmp_path: Path, clock: Clock,
) -> Iterator[tuple[WorkflowRepository, WorkflowRepository]]:
    url = f"sqlite:///{(tmp_path / 'owners.db').as_posix()}"
    command.upgrade(migration_config(url), "head")
    engines = [build_engine(url), build_engine(url)]
    try:
        yield (
            WorkflowRepository(build_session_factory(engines[0]), clock=clock),
            WorkflowRepository(build_session_factory(engines[1]), clock=clock),
        )
    finally:
        for engine in engines:
            engine.dispose()


@pytest.mark.parametrize("status", [WorkflowStatus.PENDING, WorkflowStatus.WAITING,
                                   WorkflowStatus.RUNNING])
@pytest.mark.parametrize("age", [299.999, 300, -60])
def test_live_owner_recovery_leaves_execution_and_steps_unchanged(
    repositories: tuple[WorkflowRepository, WorkflowRepository], clock: Clock,
    status: WorkflowStatus, age: float,
) -> None:
    first, second = repositories
    first.register_owner("first")
    execution = first.create_execution("live")
    first.update_status(execution.id, status)
    first.start_step(execution.id, WorkflowStage.GENERATE_SPEC)
    before = first.get_execution(execution.id)
    steps = first.list_steps(execution.id)
    clock.now += timedelta(seconds=age)

    assert second.recover_interrupted() == []
    assert second.live_owner_execution_ids == (execution.id,)
    assert second.get_execution(execution.id) == before
    assert second.list_steps(execution.id) == steps


@pytest.mark.parametrize("status", [WorkflowStatus.PENDING, WorkflowStatus.WAITING,
                                   WorkflowStatus.RUNNING])
@pytest.mark.parametrize("owner", ["stale", "ended", "missing", "legacy"])
def test_orphan_owners_recover_idempotently_with_checkpoint(
    repositories: tuple[WorkflowRepository, WorkflowRepository], clock: Clock,
    status: WorkflowStatus, owner: str,
) -> None:
    first, second = repositories
    if owner != "legacy":
        first.register_owner("first")
    execution = first.create_execution("orphan")
    first.update_status(execution.id, status)
    first.start_step(execution.id, WorkflowStage.GENERATE_SPEC)
    # Simulate broken ownership data as well as explicit end/crash records.
    with first._sessions() as session, session.begin():
        if owner == "ended":
            session.execute(text("UPDATE execution_owners SET ended_at = :now"), {"now": clock.now})
        elif owner == "missing":
            session.connection().exec_driver_sql("PRAGMA foreign_keys=OFF")
            session.execute(text("UPDATE workflow_executions SET owner_id = 'missing'"))
    if owner == "stale":
        clock.now += timedelta(seconds=300.001)

    assert [item.id for item in second.recover_interrupted()] == [execution.id]
    recovered = second.get_execution(execution.id)
    assert recovered.status is WorkflowStatus.INTERRUPTED
    assert recovered.owner_id is None
    assert recovered.interrupted_from_status is status
    assert recovered.resume_stage is WorkflowStage.GENERATE_SPEC
    assert second.list_steps(execution.id)[0].status is StepStatus.INTERRUPTED
    assert second.live_owner_execution_ids == ()
    assert second.recover_interrupted() == []
    assert second.get_execution(execution.id) == recovered


def test_recent_crash_is_recoverable_on_startup_after_expiry(
    repositories: tuple[WorkflowRepository, WorkflowRepository], clock: Clock,
) -> None:
    first, second = repositories
    first.register_owner("crashed")
    execution = first.create_execution("crash")
    assert second.recover_interrupted() == []
    clock.now += timedelta(seconds=301)
    assert [item.id for item in second.recover_interrupted()] == [execution.id]
    assert second.live_owner_execution_ids == ()


def test_recovery_samples_time_after_acquiring_write_reservation(
    repositories: tuple[WorkflowRepository, WorkflowRepository], clock: Clock,
) -> None:
    first, second = repositories
    first.register_owner("first")
    execution = first.create_execution("expires-while-waiting")
    attempted = Event()
    engine = second._sessions().get_bind()

    def observe_attempt(_connection: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if statement == "BEGIN IMMEDIATE":
            clock.now += timedelta(seconds=301)
            attempted.set()

    event.listen(engine, "before_cursor_execute", observe_attempt)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with first._sessions() as session, session.begin():
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                recovery = pool.submit(second.recover_interrupted)
                assert attempted.wait(5)
            assert [item.id for item in recovery.result(timeout=5)] == [execution.id]
    finally:
        event.remove(engine, "before_cursor_execute", observe_attempt)


def test_owner_read_failure_is_not_evidence_of_death(
    repositories: tuple[WorkflowRepository, WorkflowRepository],
) -> None:
    first, second = repositories
    first.register_owner("first")
    execution = first.create_execution("unreadable")
    engine = second._sessions().get_bind()

    def fail_owner_read(_connection: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if "FROM execution_owners" in statement:
            raise OperationalError(statement, {}, RuntimeError("read failed"))

    event.listen(engine, "before_cursor_execute", fail_owner_read)
    try:
        with pytest.raises(OperationalError, match="read failed"):
            second.recover_interrupted()
    finally:
        event.remove(engine, "before_cursor_execute", fail_owner_read)
    assert first.get_execution(execution.id).status is WorkflowStatus.PENDING


@pytest.mark.parametrize("admission", ["fresh", "interrupted", "human", "continuation"])
def test_admission_persists_new_owner_in_pending_transaction(
    repositories: tuple[WorkflowRepository, WorkflowRepository], admission: str,
) -> None:
    first, second = repositories
    first.register_owner("first")
    second.register_owner("second")
    if admission == "fresh":
        execution = second.create_execution("new")
    else:
        execution = first.create_execution("old")
        if admission == "interrupted":
            first.interrupt_owned_and_end()
            second.queue_interrupted(execution.id)
        elif admission == "human":
            first.request_human_input(execution.id, WorkflowStage.GENERATE_SPEC, "Which base?")
            first.finish(execution.id, WorkflowStatus.NEEDS_HUMAN)
            second.respond_to_human_input(execution.id, "main")
        else:
            first.finish(execution.id, WorkflowStatus.FAILED)
            second.queue_execution(execution.id)
    # An independent connection sees ownership with the first visible PENDING.
    persisted = first.get_execution(execution.id)
    assert persisted.status is WorkflowStatus.PENDING
    assert persisted.owner_id == "second"
    assert first.recover_interrupted() == []
    contender = WorkflowRepository(first._sessions)
    contender.register_owner("contender")
    with pytest.raises(ValueError, match="already active"):
        contender.queue_execution(execution.id)
    assert first.get_execution(execution.id).owner_id == "second"


def test_competing_interrupted_admissions_only_one_owner_wins(
    repositories: tuple[WorkflowRepository, WorkflowRepository],
) -> None:
    first, second = repositories
    execution = first.create_execution("competing")
    first.recover_interrupted()
    first.register_owner("first")
    second.register_owner("second")
    barrier = Barrier(2)

    def admit(repository: WorkflowRepository) -> str | None:
        barrier.wait(timeout=5)
        try:
            return repository.queue_interrupted(execution.id).owner_id
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(admit, repository) for repository in repositories]
        winners = [owner for future in futures if (owner := future.result(timeout=5)) is not None]
    assert len(winners) == 1
    assert first.get_execution(execution.id).owner_id == winners[0]


@pytest.mark.parametrize("write", ["live_completion", "stale_completion", "heartbeat"])
def test_startup_waits_for_prior_completion_or_heartbeat_transaction(
    repositories: tuple[WorkflowRepository, WorkflowRepository], clock: Clock, write: str,
) -> None:
    first, second = repositories
    first.register_owner("first")
    execution = first.create_execution("concurrent")
    first.update_status(execution.id, WorkflowStatus.RUNNING)
    if write != "live_completion":
        clock.now += timedelta(seconds=301)
    writer_ready = Event()
    recovery_attempted = Event()
    first_engine = first._sessions().get_bind()
    second_engine = second._sessions().get_bind()

    def hold_uncommitted_write(
        _connection: Any, _cursor: Any, statement: str, *_args: Any,
    ) -> None:
        if statement.startswith("UPDATE "):
            writer_ready.set()
            assert recovery_attempted.wait(5)

    def observe_reservation(
        _connection: Any, _cursor: Any, statement: str, *_args: Any,
    ) -> None:
        if statement == "BEGIN IMMEDIATE":
            recovery_attempted.set()

    event.listen(first_engine, "after_cursor_execute", hold_uncommitted_write)
    event.listen(second_engine, "before_cursor_execute", observe_reservation)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(
                first.refresh_owner if write == "heartbeat" else first.finish,
                "first" if write == "heartbeat" else execution.id,
                *(() if write == "heartbeat" else (WorkflowStatus.APPROVED,)),
            )
            assert writer_ready.wait(5)
            recovery = pool.submit(second.recover_interrupted)
            assert recovery.result(timeout=5) == []
            writer.result(timeout=5)
    finally:
        recovery_attempted.set()
        event.remove(first_engine, "after_cursor_execute", hold_uncommitted_write)
        event.remove(second_engine, "before_cursor_execute", observe_reservation)
    if write == "heartbeat":
        assert second.get_execution(execution.id).status is WorkflowStatus.RUNNING
        assert second.live_owner_execution_ids == (execution.id,)
    else:
        assert second.get_execution(execution.id).status is WorkflowStatus.APPROVED
        assert second.live_owner_execution_ids == ()


def test_upgrade_0012_retains_ownerless_rows_and_checkpoints(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    config = migration_config(url)
    command.upgrade(config, "0012")
    engine = build_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO workflow_executions "
                "(id, task_source, task_id, task_url, repository_ref, "
                "trello_card_id, trello_card_url, repository, base_branch, "
                "current_stage, status, created_at, updated_at) "
                "VALUES ('legacy', 'trello', 'task', '', '', "
                "'task', '', '', '', 'IMPLEMENT', 'RUNNING', :now, :now)"
            ), {"now": datetime(2026, 10, 31, tzinfo=UTC)})
            connection.execute(text(
                "INSERT INTO workflow_steps "
                "(workflow_execution_id, stage, status, started_at) "
                "VALUES ('legacy', 'IMPLEMENT', 'RUNNING', :now)"
            ), {"now": datetime(2026, 10, 31, tzinfo=UTC)})
        command.upgrade(config, "head")
        repository = WorkflowRepository(build_session_factory(engine))
        assert repository.get_execution("legacy").owner_id is None
        recovered = repository.recover_interrupted()
        assert len(recovered) == 1
        assert recovered[0].resume_stage is WorkflowStage.IMPLEMENT
        assert repository.list_steps("legacy")[0].status is StepStatus.INTERRUPTED
    finally:
        engine.dispose()


@pytest.mark.parametrize("second_has_work", [False, True])
def test_controller_shutdown_only_counts_and_interrupts_local_work(
    repositories: tuple[WorkflowRepository, WorkflowRepository],
    monkeypatch: pytest.MonkeyPatch, clock: Clock, second_has_work: bool,
) -> None:
    first, second = repositories
    monkeypatch.setattr("prchef.clients.command_client.interrupt_active_commands", Mock())
    first.register_owner("first")
    foreign = first.create_execution("foreign")
    first.start_step(foreign.id, WorkflowStage.IMPLEMENT)
    before = first.get_execution(foreign.id)
    steps = first.list_steps(foreign.id)
    second.recover_interrupted()
    second.register_owner("second")
    local = second.create_execution("local") if second_has_work else None
    controller = WorkflowController(
        workflow_repository=second, config_loader=Mock(), client_factory=Mock(),
        task_provider_factory=Mock(), repository_index_clients=Mock(),
    )
    assert controller.active_execution_count() == int(second_has_work)
    assert controller.other_instance_execution_count() == 1
    controller.shutdown_for_exit()
    controller.shutdown_for_exit()
    assert first.get_execution(foreign.id) == before
    assert first.list_steps(foreign.id) == steps
    if local is not None:
        assert first.get_execution(local.id).status is WorkflowStatus.INTERRUPTED
    assert controller.active_execution_count() == 0
    with first._sessions() as session:
        ended_before = session.execute(text(
            "SELECT last_heartbeat_at, ended_at FROM execution_owners WHERE id = 'second'"
        )).one()
    clock.now += timedelta(seconds=10)
    second.refresh_owner("second")
    with first._sessions() as session:
        assert session.execute(text(
            "SELECT last_heartbeat_at, ended_at FROM execution_owners WHERE id = 'second'"
        )).one() == ended_before
    assert ended_before.ended_at is not None
    # The other controller can still persist progress and finish normally.
    first.finish(foreign.id, WorkflowStatus.APPROVED)
    assert first.get_execution(foreign.id).status is WorkflowStatus.APPROVED


@pytest.mark.parametrize("takeover", [False, True])
@pytest.mark.parametrize(("method", "args"), [
    ("assert_owned", ()),
    ("update_status", (WorkflowStatus.RUNNING,)),
    ("update_stage", (WorkflowStage.COMMIT,)),
    ("update_task", (DevelopmentTask("changed", "trello", "Changed", "", ""),)),
    ("update_project", ("other/repo", "main")),
    ("start_step", (WorkflowStage.COMMIT,)),
    ("finish_step", (StepStatus.SUCCEEDED,)),
    ("record_base_commit", ("changed",)),
    ("record_submission_repository", ("changed",)),
    ("record_workspace", ("changed",)),
    ("record_spec", (RepositorySpec("changed", "changed"),)),
    ("record_spec_jules_session", ("changed", "url")),
    ("record_branch", ("changed",)),
    ("record_implementation", (ImplementationResult(success=True, summary="changed"),)),
    ("record_validation", (ValidationResult(success=True),)),
    ("record_commit", ("changed",)),
    ("record_pull_request", (PullRequest(9, "url", "title", "main", "branch"),)),
    ("record_review", (CodeReview(ReviewDecision.APPROVED),)),
    ("finish_review_publication", ()),
    ("record_polish_outcome", (PolishOutcome.APPLIED,)),
    ("record_polish_skip_comment_attempt", ()),
    ("record_review_jules_session", ("changed", "url")),
    ("record_error", ("changed",)),
    ("set_resume_mode", ("POLISH",)),
    ("set_resume_stage", (WorkflowStage.REVIEW,)),
    ("request_human_input", (WorkflowStage.REVIEW, "Changed?")),
    ("finish", (WorkflowStatus.APPROVED,)),
    ("reinterrupt", ("changed",)),
])
def test_returning_stalled_owner_cannot_write(
    repositories: tuple[WorkflowRepository, WorkflowRepository], clock: Clock,
    takeover: bool, method: str, args: tuple[Any, ...],
) -> None:
    first, second = repositories
    first.register_owner("first")
    execution = first.create_execution("stalled")
    step = first.start_step(execution.id, WorkflowStage.IMPLEMENT)
    clock.now += timedelta(seconds=301)
    second.recover_interrupted()
    assert second.get_execution(execution.id).owner_id is None
    if takeover:
        second.register_owner("second")
        second.queue_interrupted(execution.id)
        second.update_stage(execution.id, WorkflowStage.VALIDATE)
        second.record_implementation(execution.id, ImplementationResult(True, "new owner"))
        second.record_error(execution.id, "new owner's error")
        second.start_step(execution.id, WorkflowStage.VALIDATE)

    # Compare raw persisted columns, including every snapshot, timestamp and checkpoint.
    def state() -> list[list[Any]]:
        with second._sessions() as session:
            return [list(session.execute(text(f"SELECT * FROM {table}")).all()) for table in (
                "workflow_executions", "workflow_steps", "human_interactions",
            )]

    before = state()
    with pytest.raises(ExecutionOwnershipLostError):
        getattr(first, method)(step if method == "finish_step" else execution.id, *args)
    assert state() == before


def test_local_shutdown_retains_owner_and_allows_interrupted_step_completion(
    repositories: tuple[WorkflowRepository, WorkflowRepository],
) -> None:
    first, _ = repositories
    first.register_owner("first")
    execution = first.create_execution("local")
    step = first.start_step(execution.id, WorkflowStage.IMPLEMENT)
    first.interrupt_owned_and_end()
    assert first.get_execution(execution.id).owner_id == "first"
    first.finish_step(step, StepStatus.SUCCEEDED)
    assert first.list_steps(execution.id)[0].status is StepStatus.INTERRUPTED


def test_continuation_claims_resume_fields_atomically(
    repositories: tuple[WorkflowRepository, WorkflowRepository],
) -> None:
    first, second = repositories
    first.register_owner("first")
    second.register_owner("second")
    execution = first.create_execution("continue")
    first.finish(execution.id, WorkflowStatus.FAILED)
    second.queue_execution(
        execution.id, resume_mode="MANUAL_CONTINUATION", resume_stage=WorkflowStage.IMPLEMENT,
    )
    queued = first.get_execution(execution.id)
    assert queued.owner_id == "second"
    assert queued.status is WorkflowStatus.PENDING
    assert queued.resume_mode == "MANUAL_CONTINUATION"
    assert queued.resume_stage is WorkflowStage.IMPLEMENT


@pytest.mark.parametrize("boundary", [
    "admission", "step_exit", "commit", "push", "create_pr", "review", "provider", "validation",
])
def test_worker_stops_at_ownership_loss_without_finishing_or_external_writes(
    repositories: tuple[WorkflowRepository, WorkflowRepository], clock: Clock,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    boundary: str,
) -> None:
    first, second = repositories
    first.register_owner("first")
    factory = FakeClientFactory()
    notifications = Mock()
    service = WorkflowService(
        first, TrelloTaskProvider(FakeTrelloClient()), FakeConfigLoader(tmp_path), factory,
        notification_service=notifications,
    )
    events: list[WorkflowEvent] = []
    transferred: dict[str, Any] = {}

    def effects() -> tuple[Any, ...]:
        return (factory.git.commit_calls, factory.git.push_calls, factory.github.create_calls,
                list(factory.github.review_calls), list(factory.github.comment_calls),
                list(factory.github.assignment_calls))

    def transfer(execution_id: str) -> None:
        assert not transferred
        clock.now += timedelta(seconds=301)
        assert [item.id for item in second.recover_interrupted()] == [execution_id]
        second.register_owner("second")
        second.queue_interrupted(execution_id)
        second.update_stage(execution_id, WorkflowStage.IMPLEMENT)
        second.record_error(execution_id, "replacement owner checkpoint")
        transferred.update(
            execution=second.get_execution(execution_id), steps=second.list_steps(execution_id),
            effects=effects(),
        )

    original_step = service._stage_events._step

    @contextmanager
    def step(execution_id: str, stage: WorkflowStage) -> Iterator[None]:
        if (boundary, stage) in {
            ("commit", WorkflowStage.COMMIT), ("push", WorkflowStage.PUSH),
            ("create_pr", WorkflowStage.CREATE_PR),
        }:
            transfer(execution_id)
        with original_step(execution_id, stage):
            yield
            if boundary == "step_exit" and stage is WorkflowStage.IMPLEMENT:
                transfer(execution_id)

    monkeypatch.setattr(service._stage_events, "_step", step)
    if boundary == "admission":
        original_start = service._lifecycle._start_admitted

        def start(execution_id: str) -> None:
            transfer(execution_id)
            original_start(execution_id)

        monkeypatch.setattr(service._lifecycle, "_start_admitted", start)
    elif boundary == "review":
        original_review = first.record_review

        def record_review(execution_id: str, review: CodeReview) -> None:
            original_review(execution_id, review)
            transfer(execution_id)

        monkeypatch.setattr(first, "record_review", record_review)
    elif boundary == "provider":
        original_slot = service._provider_coordinator.slot

        @contextmanager
        def slot(provider: str, **kwargs: Any) -> Iterator[None]:
            with original_slot(provider, **kwargs):
                execution_id = kwargs["execution_id"]
                if first.get_execution(execution_id).current_stage is WorkflowStage.IMPLEMENT:
                    transfer(execution_id)
                yield

        monkeypatch.setattr(service._provider_coordinator, "slot", slot)
    elif boundary == "validation":
        original_check = service._stage_events._record_validation_check

        def check(execution_id: str, payload: dict[str, Any]) -> None:
            transfer(execution_id)
            original_check(execution_id, payload)

        monkeypatch.setattr(service._stage_events, "_record_validation_check", check)

    finish = Mock(wraps=service._lifecycle._finish)
    reinterrupt = Mock(wraps=first.reinterrupt)
    monkeypatch.setattr(service._lifecycle, "_finish", finish)
    monkeypatch.setattr(first, "reinterrupt", reinterrupt)
    try:
        result = service.run("returning-owner", event_listener=events.append)
    finally:
        service._dispatcher.shutdown(wait=True)

    assert transferred, "the test did not reach the intended takeover boundary"
    assert result == transferred["execution"] == second.get_execution(result.id)
    assert second.list_steps(result.id) == transferred["steps"]
    assert effects() == transferred["effects"]
    finish.assert_not_called()
    reinterrupt.assert_not_called()
    notifications.notify_outcome.assert_not_called()
    assert not any(event.kind is WorkflowEventKind.WORKFLOW_FINISHED for event in events)
    warnings = [event for event in events
                if (event.payload or {}).get("kind") == "EXECUTION_OWNERSHIP_LOST"]
    assert len(warnings) == 1
    assert warnings[0].payload["severity"] == "warning"
    diagnostics = [item for item in second.list_diagnostics(result.id)
                   if item.payload.get("kind") == "EXECUTION_OWNERSHIP_LOST"]
    assert len(diagnostics) == 1
    assert diagnostics[0].payload == warnings[0].payload
    assert "this instance stopped working on it" in caplog.text


@pytest.mark.parametrize("registered", [False, True])
def test_single_owner_workflow_is_unchanged(
    repositories: tuple[WorkflowRepository, WorkflowRepository], tmp_path: Path, registered: bool,
) -> None:
    first, _ = repositories
    if registered:
        first.register_owner("first")
    service = WorkflowService(
        first, TrelloTaskProvider(FakeTrelloClient()),
        FakeConfigLoader(tmp_path), FakeClientFactory(),
    )
    try:
        result = service.run("normal")
    finally:
        service._dispatcher.shutdown(wait=True)
    assert result.status is WorkflowStatus.APPROVED
    assert result.owner_id == ("first" if registered else None)
    assert all(step.status is StepStatus.SUCCEEDED for step in first.list_steps(result.id))
    assert [step.stage for step in first.list_steps(result.id)] == [
        WorkflowStage.LOAD_TASK, WorkflowStage.LOAD_CONFIG, WorkflowStage.GENERATE_SPEC,
        WorkflowStage.PREPARE_BRANCH, WorkflowStage.IMPLEMENT, WorkflowStage.VALIDATE,
        WorkflowStage.COMMIT, WorkflowStage.PUSH, WorkflowStage.CREATE_PR, WorkflowStage.REVIEW,
    ]
    assert not any(item.payload.get("kind") == "EXECUTION_OWNERSHIP_LOST"
                   for item in first.list_diagnostics(result.id))


def test_dropped_job_that_lost_ownership_does_not_finish(
    repositories: tuple[WorkflowRepository, WorkflowRepository], clock: Clock,
    tmp_path: Path,
) -> None:
    first, second = repositories
    first.register_owner("first")
    service = WorkflowService(
        first, TrelloTaskProvider(FakeTrelloClient()),
        FakeConfigLoader(tmp_path), FakeClientFactory(),
    )
    events: list[WorkflowEvent] = []
    try:
        handle = service.submit_execution("drop", admission_gate=lambda: False,
                                          event_listener=events.append)
        clock.now += timedelta(seconds=301)
        second.recover_interrupted()
        second.register_owner("second")
        expected = second.queue_interrupted(handle.execution_id)
        assert service._dispatcher.cancel(handle.execution_id)
        assert handle.result(timeout=5) == expected
        assert first.get_execution(handle.execution_id) == expected
    finally:
        service._dispatcher.shutdown(wait=True)
    assert len([event for event in events
                if (event.payload or {}).get("kind") == "EXECUTION_OWNERSHIP_LOST"]) == 1
    assert not any(event.kind is WorkflowEventKind.WORKFLOW_FINISHED for event in events)


@pytest.mark.parametrize("stage", [WorkflowStage.PUSH, WorkflowStage.CREATE_PR])
def test_different_instance_can_continue_terminal_execution(
    repositories: tuple[WorkflowRepository, WorkflowRepository], tmp_path: Path,
    stage: WorkflowStage,
) -> None:
    first, second = repositories
    first.register_owner("first")
    second.register_owner("second")
    factory = FakeClientFactory()
    if stage is WorkflowStage.PUSH:
        factory.git.push_error = RuntimeError("push unavailable")
    else:
        factory.github.error = RuntimeError("PR unavailable")
    original = WorkflowService(
        first, TrelloTaskProvider(FakeTrelloClient()), FakeConfigLoader(tmp_path), factory,
    )
    replacement = WorkflowService(
        second, TrelloTaskProvider(FakeTrelloClient()), FakeConfigLoader(tmp_path), factory,
    )
    try:
        failed = original.run("continue-elsewhere")
        assert failed.status is WorkflowStatus.FAILED
        assert failed.current_stage is stage
        factory.git.push_error = None
        factory.github.error = None
        continued = replacement.continue_execution(failed.id, "Retry publication")
        assert continued.status is WorkflowStatus.APPROVED
        assert continued.owner_id == "second"
        assert continued.commit_sha == failed.commit_sha
        assert factory.git.commit_calls == 1
        assert factory.git.push_calls == (2 if stage is WorkflowStage.PUSH else 1)
        assert factory.github.create_calls == (1 if stage is WorkflowStage.PUSH else 2)
    finally:
        original._dispatcher.shutdown(wait=True)
        replacement._dispatcher.shutdown(wait=True)


@pytest.mark.parametrize("stage", [WorkflowStage.PUSH, WorkflowStage.CREATE_PR])
def test_publication_retry_stops_when_another_instance_takes_ownership(
    repositories: tuple[WorkflowRepository, WorkflowRepository], clock: Clock,
    tmp_path: Path, stage: WorkflowStage,
) -> None:
    first, second = repositories
    first.register_owner("first")
    factory = FakeClientFactory()
    service = WorkflowService(
        first, TrelloTaskProvider(FakeTrelloClient()), FakeConfigLoader(tmp_path), factory,
    )
    if stage is WorkflowStage.PUSH:
        factory.git.push_error = RuntimeError("push unavailable")
    else:
        factory.github.error = RuntimeError("PR unavailable")
    events: list[WorkflowEvent] = []
    transferred: dict[str, Any] = {}

    def observe(event: WorkflowEvent) -> None:
        events.append(event)
        if event.kind is WorkflowEventKind.STAGE_STARTED and event.stage is stage:
            clock.now += timedelta(seconds=301)
            assert [item.id for item in second.recover_interrupted()] == [event.execution_id]
            second.register_owner("second")
            transferred["execution"] = second.queue_interrupted(event.execution_id)
            transferred["steps"] = second.list_steps(event.execution_id)

    try:
        failed = service.run("retry-takeover")
        assert failed.status is WorkflowStatus.FAILED
        assert failed.current_stage is stage
        effects = (factory.git.commit_calls, factory.git.push_calls, factory.github.create_calls)
        factory.git.push_error = None
        factory.github.error = None
        result = service.continue_execution(
            failed.id, "Retry publication", event_listener=observe,
        )
        assert result == transferred["execution"] == second.get_execution(failed.id)
        assert second.list_steps(failed.id) == transferred["steps"]
        assert (factory.git.commit_calls, factory.git.push_calls,
                factory.github.create_calls) == effects
        assert not factory.github.review_calls
        assert not any(event.kind is WorkflowEventKind.WORKFLOW_FINISHED for event in events)
        assert len([event for event in events
                    if (event.payload or {}).get("kind") == "EXECUTION_OWNERSHIP_LOST"]) == 1
    finally:
        service._dispatcher.shutdown(wait=True)
