"""Presentation helpers shared by single-task and batch progress views.

Both the launcher's live single-task timeline and the batch progress
projection translate the same workflow observer events and persisted
``WorkflowStep`` rows into user-facing stage/status wording. Keeping that
translation in one place means the two views cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from prchef.models.agent import AgentRole
from prchef.models.human_interaction import HumanInteraction
from prchef.models.workflow_event import WorkflowEvent, WorkflowEventKind
from prchef.models.workflow_status import StepStatus, WorkflowStage, WorkflowStatus
from prchef.models.workflow_step import WorkflowStep


def is_index_progress(event: WorkflowEvent) -> bool:
    return (
        event.kind is WorkflowEventKind.DIAGNOSTIC
        and (event.payload or {}).get("kind") == "REPOSITORY_INDEX_PROGRESS"
    )


def is_index_ready(event: WorkflowEvent) -> bool:
    return (
        is_index_progress(event)
        and (event.payload or {}).get("message") == "Semantic index ready"
    )


def is_active_index_line(line: str) -> bool:
    return line.startswith("Semantic index: ") and line != "Semantic index: Semantic index ready"


def clear_index_progress(lines: list[str]) -> None:
    # Keep stage line indices stable while removing the transient activity.
    for index, line in enumerate(lines):
        if is_active_index_line(line):
            lines[index] = ""

# The stages where an agent provider is actually invoked. Used to add optional
# provider/model context to a stage's timeline entry.
STAGE_ROLES: dict[WorkflowStage, AgentRole] = {
    WorkflowStage.GENERATE_SPEC: AgentRole.PLANNER,
    WorkflowStage.IMPLEMENT: AgentRole.IMPLEMENTER,
    WorkflowStage.POLISH: AgentRole.IMPLEMENTER,
    WorkflowStage.REVIEW: AgentRole.REVIEWER,
}

_RETRY_LABELS: dict[WorkflowStage, str] = {
    WorkflowStage.IMPLEMENT: "Implementing corrections",
    WorkflowStage.VALIDATE: "Re-validating changes",
    WorkflowStage.REVIEW: "Reviewing corrections",
}

_ACTIVITY_LABELS: dict[WorkflowStage, str] = {
    WorkflowStage.LOAD_TASK: "Loading task",
    WorkflowStage.LOAD_CONFIG: "Configuring repository",
    WorkflowStage.GENERATE_SPEC: "Generating specification",
    WorkflowStage.PREPARE_BRANCH: "Preparing branch",
    WorkflowStage.IMPLEMENT: "Implementing changes",
    WorkflowStage.POLISH: "Applying polish suggestions",
    WorkflowStage.VALIDATE: "Validating changes",
    WorkflowStage.COMMIT: "Committing changes",
    WorkflowStage.PUSH: "Pushing branch",
    WorkflowStage.CREATE_PR: "Creating pull request",
    WorkflowStage.REVIEW: "Reviewing pull request",
    WorkflowStage.COMPLETED: "Completing workflow",
}

_COMPLETE_LABELS: dict[WorkflowStage, str] = {
    WorkflowStage.LOAD_TASK: "Task loaded",
    WorkflowStage.LOAD_CONFIG: "Repository configured",
    WorkflowStage.GENERATE_SPEC: "Specification generated",
    WorkflowStage.PREPARE_BRANCH: "Branch prepared",
    WorkflowStage.IMPLEMENT: "Changes implemented",
    WorkflowStage.POLISH: "Polish pass completed",
    WorkflowStage.VALIDATE: "Validation passed",
    WorkflowStage.COMMIT: "Changes committed",
    WorkflowStage.PUSH: "Branch pushed",
    WorkflowStage.CREATE_PR: "Pull request created",
    WorkflowStage.REVIEW: "Review completed",
    WorkflowStage.COMPLETED: "Workflow completed",
}


def stage_activity(stage: WorkflowStage, attempt: int) -> str:
    if attempt > 1:
        return _RETRY_LABELS.get(stage, _ACTIVITY_LABELS[stage])
    return _ACTIVITY_LABELS[stage]


def stage_complete(stage: WorkflowStage) -> str:
    return _COMPLETE_LABELS[stage]


def human_waiting_line(stage: WorkflowStage) -> str:
    return f"! {stage_activity(stage, 1)} — waiting for human input"


def update_human_blocked_line(
    event: WorkflowEvent,
    lines: list[str],
    successful_stage_lines: dict[WorkflowStage, int],
) -> None:
    """Revise a successful attempt when its later outcome requests clarification."""

    if (
        event.kind not in {WorkflowEventKind.STATUS_CHANGED, WorkflowEventKind.WORKFLOW_FINISHED}
        or event.status is not WorkflowStatus.NEEDS_HUMAN
        or event.stage is None
    ):
        return
    index = successful_stage_lines.pop(event.stage, None)
    if index is None:
        return
    lines[index] = human_waiting_line(event.stage)


def _utc_timestamp(value: datetime) -> datetime:
    # SQLite may return naive UTC datetimes even when the domain wrote aware ones.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def status_message(status: WorkflowStatus | None, payload: dict[str, Any] | None = None) -> str:
    if status is None:
        return "Workflow status updated"
    provider = (payload or {}).get("provider")
    if status is WorkflowStatus.WAITING and isinstance(provider, str) and provider:
        return f"Waiting for {provider} capacity…"
    messages: dict[WorkflowStatus, str] = {
        WorkflowStatus.PENDING: "Queued for an execution slot…",
        WorkflowStatus.RUNNING: "Workflow running…",
        WorkflowStatus.WAITING: "Waiting for agent capacity…",
        WorkflowStatus.INTERRUPTED: "Workflow interrupted; choose when to resume",
        WorkflowStatus.APPROVED: "✓ Workflow approved",
        WorkflowStatus.CHANGES_REQUESTED: "• Review changes requested",
        WorkflowStatus.NEEDS_HUMAN: "! Waiting for human input",
        WorkflowStatus.CANCELLED: "! Workflow cancelled",
        WorkflowStatus.FAILED: "✗ Workflow failed",
    }
    return messages.get(status, "Workflow status updated")


def concise_error(message: str) -> str:
    """Keep failure summaries short while preserving fitting messages verbatim."""
    suffix = "… See detailed logs."
    first_line = message.split("\n", 1)[0].split("\r", 1)[0]
    if first_line == message and len(message) <= 160:
        return message
    return first_line[: 160 - len(suffix)] + suffix


def timeline_message(
    event: WorkflowEvent,
    stage_attempts: Mapping[WorkflowStage, int],
    *,
    agent_context: Callable[[WorkflowStage], str] | None = None,
) -> str | None:
    """Translate one observer event into a concise, user-facing progress line.

    ``stage_attempts`` is read, not mutated: callers own tracking each stage's
    attempt count so a caller-specific event (e.g. per batch position) does
    not disturb another caller's counters.
    """

    if event.kind == WorkflowEventKind.EXECUTION_CREATED:
        return "Preparing workflow…"
    if event.kind == WorkflowEventKind.STATUS_CHANGED:
        return status_message(event.status, event.payload)
    if event.kind == WorkflowEventKind.WORKFLOW_FINISHED:
        return status_message(event.status)
    if event.kind == WorkflowEventKind.AGENT_FALLBACK:
        return f"↳ {event.message or 'Agent fallback activated'}"
    if event.kind == WorkflowEventKind.DIAGNOSTIC:
        payload = event.payload or {}
        if payload.get("kind") == "REPOSITORY_INDEX_PROGRESS":
            return f"Semantic index: {payload.get('message', 'working')}"
        if payload.get("kind") == "REPOSITORY_CONTEXT_FALLBACK":
            return (
                f"! Semantic context unavailable: {payload.get('reason')} "
                f"{payload.get('remedy')}"
            )
        if payload.get("kind") == "PROJECT_CONTEXT_TRUNCATED":
            return (
                "! docs/PROJECT_CONTEXT.md exceeds prompt cap: "
                f"{payload.get('actual_chars')} / {payload.get('cap_chars')} characters"
            )
        if payload.get("kind") == "PROVIDER_QUOTA_EXHAUSTED":
            return (
                f"Provider {payload.get('provider', 'unknown')} quota exhausted — "
                "tasks that use it are held"
            )
    if event.stage is None:
        return None

    stage = event.stage
    attempt = stage_attempts.get(stage, 0) + (
        1 if event.kind == WorkflowEventKind.STAGE_STARTED else 0
    )
    context = agent_context(stage) if agent_context is not None else ""
    if event.kind == WorkflowEventKind.STAGE_STARTED:
        return f"● {stage_activity(stage, attempt)}{context}…"
    if event.kind == WorkflowEventKind.STAGE_FINISHED:
        if event.step_status is not None and event.step_status.value == "CANCELLED":
            return f"! {stage_activity(stage, attempt)} cancelled"
        return f"✓ {stage_complete(stage)}"
    if event.kind == WorkflowEventKind.STAGE_FAILED:
        detail = f": {concise_error(event.message)}" if event.message else ""
        return f"✗ {stage_activity(stage, attempt)} failed{detail}"
    return None


def step_timeline_lines(
    steps: list[WorkflowStep],
    human_interactions: Sequence[HumanInteraction] = (),
) -> list[str]:
    """Render persisted ``WorkflowStep`` rows with the same wording/glyphs.

    This is the persisted equivalent of ``timeline_message``: it lets a
    restarted or history-inspected batch row show the same concise timeline
    vocabulary as a live run, without a diagnostics read or a schema change.
    Human interaction timestamps identify successful attempts whose result
    requested clarification, including attempts before a later resume.
    """

    blocked_steps: set[int] = set()
    for interaction in human_interactions:
        # A request follows the stage attempt it is about. Select the latest
        # attempt, including failed ones, so an error-driven pause cannot
        # relabel an older successful attempt.
        candidates = [
            index
            for index, step in enumerate(steps)
            if step.stage is interaction.requesting_stage
            and step.finished_at is not None
            and _utc_timestamp(step.finished_at) <= _utc_timestamp(interaction.requested_at)
        ]
        if candidates and steps[candidates[-1]].status is StepStatus.SUCCEEDED:
            blocked_steps.add(candidates[-1])

    lines: list[str] = []
    attempts: dict[WorkflowStage, int] = {}
    previous_status: StepStatus | None = None
    for index, step in enumerate(steps):
        attempts[step.stage] = attempts.get(step.stage, 0) + 1
        attempt = attempts[step.stage]
        if step.status is StepStatus.RUNNING:
            lines.append(f"● {stage_activity(step.stage, attempt)}…")
        elif step.status is StepStatus.SUCCEEDED:
            if index in blocked_steps:
                lines.append(human_waiting_line(step.stage))
            elif attempt > 1 and previous_status is StepStatus.FAILED:
                lines.append(f"✓ {stage_activity(step.stage, attempt)}")
            else:
                lines.append(f"✓ {stage_complete(step.stage)}")
        elif step.status is StepStatus.CANCELLED:
            lines.append(f"! {stage_activity(step.stage, attempt)} cancelled")
        elif step.status is StepStatus.INTERRUPTED:
            lines.append(f"! {stage_activity(step.stage, attempt)} interrupted")
        elif step.status is StepStatus.FAILED:
            detail = f": {concise_error(step.error_message)}" if step.error_message else ""
            lines.append(f"✗ {stage_activity(step.stage, attempt)} failed{detail}")
        elif step.status is StepStatus.SKIPPED:
            lines.append(f"- {stage_activity(step.stage, attempt)} skipped")
        previous_status = step.status
    return lines
