from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
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
from prchef.models.workflow_status import StepStatus, WorkflowStage, WorkflowStatus
from prchef.repositories.workflow_repository import WorkflowRepository
from prchef.tui.bridge import WorkflowController


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
