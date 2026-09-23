import json
import re
from pathlib import Path

from orchestrator.clients.git_client import GitClient
from orchestrator.models.agent import AgentRequest
from orchestrator.models.code_review import CodeReview
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.implementation_result import ImplementationResult
from orchestrator.models.pull_request import PullRequest
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.models.validation_result import ValidationCheck, ValidationResult
from orchestrator.providers.agent_provider import AgentProvider
from orchestrator.runtime_logging import format_command, redact_text
from orchestrator.services.exceptions import StructuredOutputError
from orchestrator.services.project_context import (
    implementer_instructions_prompt,
    project_context_prompt,
)
from orchestrator.services.structured_output import parse_json_object

_CORRECTION_OUTPUT_LIMIT = 4_000
_TRUNCATED_OUTPUT_MARKER = "[... earlier output truncated ...]\n"
_PYTEST_FAILURES_HEADER = re.compile(r"^={2,} FAILURES ={2,}\r?$", re.MULTILINE)
# A traceback frame separator contains only underscores and spaces, even when it ends in "_".
_PYTEST_FAILURE_HEADER = re.compile(
    r"^_+ (?=[^\r\n]*[^_ \r\n])[^\r\n]+ _+\r?$", re.MULTILINE
)
_PYTEST_SUMMARY_HEADER = re.compile(
    r"^={2,} short test summary info ={2,}\r?$", re.MULTILINE
)
_PYTEST_SUMMARY_ENTRY = re.compile(
    r"^(?:FAILED|ERROR|SKIPPED|XFAIL|XPASS|PASSED)\s+\S+"
)
_PYTEST_SESSION_RESULT = re.compile(
    r"^={2,} (?:\d+ [^=\r\n]+|no tests ran[^=\r\n]*) ={2,}\r?$"
)


def _pytest_summary_end(output: str, header_end: int) -> int | None:
    """Find the end of a real pytest summary without swallowing later command output."""
    position = header_end
    if output[position : position + 1] == "\n":
        position += 1
    has_failure = False
    while position < len(output):
        line_end = output.find("\n", position)
        line_end = len(output) if line_end < 0 else line_end + 1
        line = output[position:line_end].rstrip("\r\n")
        if _PYTEST_SUMMARY_ENTRY.match(line):
            has_failure |= line.startswith("FAILED ") or line.startswith("ERROR ")
            position = line_end
            continue
        if has_failure and _PYTEST_SESSION_RESULT.fullmatch(line):
            return line_end
        return position if has_failure else None
    return position if has_failure else None


def _render_completed_failure_output(output: str) -> str:
    """Cap pytest failure details per test while retaining its complete failure summary."""
    failures = _PYTEST_FAILURES_HEADER.search(output)
    if failures is None:
        return output[:_CORRECTION_OUTPUT_LIMIT]
    summaries = [
        (match, end)
        for match in _PYTEST_SUMMARY_HEADER.finditer(output, failures.end())
        if (end := _pytest_summary_end(output, match.end())) is not None
    ]
    if not summaries:
        return output[:_CORRECTION_OUTPUT_LIMIT]
    # The last summary delimits the detailed blocks; earlier summaries may come
    # from nested pytest output or multiple pytest invocations in one command.
    summary, summary_end = summaries[-1]

    details_end = summary.start()
    headers = list(_PYTEST_FAILURE_HEADER.finditer(output, failures.end(), details_end))
    if not headers:
        return output[:_CORRECTION_OUTPUT_LIMIT]

    blocks = []
    for index, header in enumerate(headers):
        block_end = headers[index + 1].start() if index + 1 < len(headers) else details_end
        blocks.append(output[header.start() : block_end][:_CORRECTION_OUTPUT_LIMIT])
    parts = []
    if failures.start():
        parts.append(output[: failures.start()][:_CORRECTION_OUTPUT_LIMIT])
    parts.extend((failures.group(), *blocks))
    parts.extend(output[match.start() : end] for match, end in summaries)
    if summary_end < len(output):
        parts.append(output[summary_end : summary_end + _CORRECTION_OUTPUT_LIMIT])
    return "\n\n".join(parts)


def _bounded_tail(output: str, limit: int) -> str:
    """Keep the end of output from a killed process, where a hang is usually visible."""
    if len(output) <= limit:
        return output
    return _TRUNCATED_OUTPUT_MARKER + output[-limit:]


def _bounded_tails(stdout: str, stderr: str) -> tuple[str, str]:
    """Share one output budget between both streams of a killed process.

    A timed-out check contributes at most one output budget. A stream shorter than half the
    budget leaves its unused share to the other stream.
    """
    stderr_budget = min(
        len(stderr),
        max(_CORRECTION_OUTPUT_LIMIT // 2, _CORRECTION_OUTPUT_LIMIT - len(stdout)),
    )
    stdout_budget = _CORRECTION_OUTPUT_LIMIT - stderr_budget
    return _bounded_tail(stdout, stdout_budget), _bounded_tail(stderr, stderr_budget)


class ImplementationService:
    def __init__(self, provider: AgentProvider, git_client: GitClient) -> None:
        self._provider = provider
        self._git = git_client

    def implement(
        self,
        task: DevelopmentTask,
        spec: RepositorySpec,
        repository_path: Path,
        *,
        instructions_repository_path: Path | None = None,
    ) -> ImplementationResult:
        context = self._prompt_context(repository_path, instructions_repository_path)
        prompt = f"""ORIGINAL TASK:
{task.as_original_prompt()}

REPOSITORY SPEC:
{spec.as_prompt_context()}

{context}Keep docs/PROJECT_CONTEXT.md accurate when this task meaningfully
changes durable project knowledge:
architecture, workflows, domain behavior, invariants, configuration, persistence, integrations,
or important design decisions. Treat any necessary docs/PROJECT_CONTEXT.md update as part of
the normal diff.
Do not update it for trivial implementation details. The current repository is authoritative if it
conflicts with docs/PROJECT_CONTEXT.md.

Implement the original task.
Use the repository spec only as supporting technical context.
Do not invent requirements.
Preserve the conventions of the target repository.
Do not commit, push, create a pull request, or merge; the orchestrator owns those operations.
"""
        return self._run_prompt(prompt, repository_path)

    def implement_prompt(
        self,
        prompt: str,
        repository_path: Path,
        *,
        instructions_repository_path: Path | None = None,
    ) -> ImplementationResult:
        """Run explicit implementation input while retaining the safety contract."""
        if not prompt.strip():
            raise ValueError("Implementation prompt must not be empty")
        context = self._prompt_context(repository_path, instructions_repository_path)
        safe_prompt = f"""EXPLICIT IMPLEMENTATION INPUT:
{prompt}

STRICT SAFETY RULES:
Implement only the requested input in the configured workspace.
{context}Keep docs/PROJECT_CONTEXT.md accurate when the requested change
meaningfully changes durable project knowledge; include any needed update in the normal diff. Do
not update it for trivial details, and treat the repository as authoritative if it conflicts with
docs/PROJECT_CONTEXT.md.
Do not commit, push, create a pull request, or merge; the orchestrator owns those operations.
"""
        return self._run_prompt(safe_prompt, repository_path)

    def correct_validation_failures(
        self,
        task: DevelopmentTask,
        spec: RepositorySpec,
        validation: ValidationResult,
        repository_path: Path,
        *,
        instructions_repository_path: Path | None = None,
    ) -> ImplementationResult:
        """Ask the implementer to repair the existing workspace after validation failed."""
        failures = self._render_validation_failures(validation)
        context = self._prompt_context(repository_path, instructions_repository_path)
        prompt = f"""ORIGINAL TASK:
{task.as_original_prompt()}

REPOSITORY SPEC:
{spec.as_prompt_context()}

VALIDATION FAILURES FROM THE CURRENT IMPLEMENTATION:
{failures}

{context}Keep docs/PROJECT_CONTEXT.md accurate if correcting these failures also
changes durable project knowledge.
Do not update it for trivial details; current repository behavior is authoritative.

Correct the existing implementation in this workspace to address the validation failures.
Do not start over or revert unrelated existing changes.
Do not commit, push, create a pull request, or merge; the orchestrator owns those operations.
"""
        return self._run_prompt(prompt, repository_path)

    def correct_review_findings(
        self,
        task: DevelopmentTask,
        spec: RepositorySpec,
        review: CodeReview,
        repository_path: Path,
        *,
        instructions_repository_path: Path | None = None,
    ) -> ImplementationResult:
        """Ask the implementer to address a review in the existing workspace."""
        feedback = self.render_review_findings(review)
        context = self._prompt_context(repository_path, instructions_repository_path)
        prompt = f"""ORIGINAL TASK:
{task.as_original_prompt()}

REPOSITORY SPEC:
{spec.as_prompt_context()}

REVIEWER CHANGE REQUESTS FOR THE CURRENT IMPLEMENTATION:
{feedback}

{context}Keep docs/PROJECT_CONTEXT.md accurate if addressing these findings also
changes durable project knowledge.
Do not update it for trivial details; current repository behavior is authoritative.

Correct the existing implementation in this workspace to address every blocking review finding.
Use the findings, blocking findings, and suggestions above as review feedback; do not start over
or revert unrelated existing changes.
Do not commit, push, create a pull request, or merge; the orchestrator owns those operations.
"""
        return self._run_prompt(prompt, repository_path)

    def continue_implementation(
        self,
        task: DevelopmentTask,
        spec: RepositorySpec,
        feedback: str,
        repository_path: Path,
        *,
        instructions_repository_path: Path | None = None,
    ) -> ImplementationResult:
        """Start a fresh implementer invocation in a retained workflow workspace."""
        if not feedback.strip():
            raise ValueError("Continuation feedback must not be empty")
        context = self._prompt_context(repository_path, instructions_repository_path)
        prompt = f"""ORIGINAL TASK:
{task.as_original_prompt()}

REPOSITORY SPEC:
{spec.as_prompt_context()}

OPERATOR FEEDBACK FOR CONTINUING THE CURRENT IMPLEMENTATION:
{feedback}

{context}Continue the existing implementation in this workspace. Address the operator feedback
without starting over or reverting unrelated existing changes. The workspace may contain retained
changes from the prior terminal attempt; inspect its current state using your tools.
Keep docs/PROJECT_CONTEXT.md accurate if this correction changes durable project knowledge.
Do not commit, push, create a pull request, or merge; the orchestrator owns those operations.
"""
        return self._run_prompt(prompt, repository_path)

    @staticmethod
    def render_validation_failures(validation: ValidationResult) -> str:
        """Render the redacted validation context used for automatic corrections."""
        return ImplementationService._render_validation_failures(validation)

    @staticmethod
    def render_review_findings(review: CodeReview) -> str:
        """Render the full persisted review context used by review corrections."""
        return json.dumps(
            {
                "summary": review.summary,
                "findings": review.findings,
                "blocking_findings": review.blocking_findings,
                "suggestions": review.suggestions,
            },
            ensure_ascii=False,
            indent=2,
        )

    def run_polish_pass(
        self,
        task: DevelopmentTask,
        spec: RepositorySpec,
        suggestions: list[str],
        repository_path: Path,
        pull_request: PullRequest,
        *,
        instructions_repository_path: Path | None = None,
    ) -> ImplementationResult:
        """Offer approved-review suggestions as one optional, scoped polish pass."""
        feedback = json.dumps(suggestions, ensure_ascii=False, indent=2)
        pull_request_context = json.dumps(
            {
                "url": pull_request.url,
                "title": pull_request.title,
                "base_branch": pull_request.base_branch,
                "head_branch": pull_request.head_branch,
            },
            ensure_ascii=False,
            indent=2,
        )
        context = self._prompt_context(repository_path, instructions_repository_path)
        prompt = f"""ORIGINAL TASK:
{task.as_original_prompt()}

REPOSITORY SPEC:
{spec.as_prompt_context()}

CURRENT PULL REQUEST:
{pull_request_context}

NON-BLOCKING REVIEW SUGGESTIONS:
{feedback}

{context}The review is already APPROVED. Evaluate every suggestion
against the current implementation in this workspace, but apply only small, safe, clearly
actionable improvements that remain within the original task
scope. Decline or skip any suggestion that is outside the original task scope, disproportionately
large, requires an architectural redesign, is unrelated cleanup, or is otherwise not worthwhile.
If none should be applied,
make no modifications. These suggestions are non-blocking and must never be treated as required.

If you make no modifications, finish with exactly one JSON object in this shape:
{{
  "skip_reasons": [
    {{"suggestions": ["exact suggestion text"], "reason": "brief public-facing reason"}}
  ]
}}
You may group suggestions with the same reason. This will be used only for a short pull request
comment, so include concise final-facing reasons only. Do not include private reasoning, prompts,
commands, credentials, provider metadata, or raw tool output. If no concise reason is available,
return an empty `skip_reasons` list.

Keep docs/PROJECT_CONTEXT.md accurate only if an applied change meaningfully changes durable
project knowledge.
Do not commit, push, create a pull request, or merge; the orchestrator owns those operations.
"""
        result = self._run_prompt(prompt, repository_path, allow_no_changes=True)
        if result.success and not result.changed_files:
            result.polish_skip_reasons = self._parse_polish_skip_reasons(
                result.summary, suggestions
            )
        return result

    def _run_prompt(
        self, prompt: str, repository_path: Path, *, allow_no_changes: bool = False
    ) -> ImplementationResult:
        agent_result = self._provider.execute(
            AgentRequest(
                instructions=prompt,
                title="Orchestrator implementation",
                workspace_path=repository_path,
            )
        )
        result = ImplementationResult(
            success=agent_result.success,
            summary=agent_result.output,
            error_message=agent_result.error_message,
            workspace_changes_may_exist=agent_result.workspace_changes_may_exist,
        )
        if result.success or result.workspace_changes_may_exist:
            result.changed_files = self._git.changed_files()
        if result.workspace_changes_may_exist and not result.success:
            return result
        if result.success and not allow_no_changes:
            if not result.changed_files:
                result.success = False
                result.error_message = "Implementation provider completed without changing files"
        return result

    @staticmethod
    def _prompt_context(
        repository_path: Path, instructions_repository_path: Path | None
    ) -> str:
        return project_context_prompt(repository_path) + implementer_instructions_prompt(
            instructions_repository_path or repository_path,
            repository_path,
        )

    @staticmethod
    def _render_validation_failures(validation: ValidationResult) -> str:
        rendered: list[str] = []
        for check in validation.checks:
            if check.success:
                continue
            if check.timed_out:
                rendered.append(ImplementationService._render_timed_out_check(check))
                continue
            output = check.stderr or check.stdout or "no output"
            rendered.append(
                "\n".join(
                    (
                        f"Command: {format_command(check.command)}",
                        f"Exit code: {check.exit_code}",
                        "Output:",
                        _render_completed_failure_output(redact_text(output)),
                    )
                )
            )
        return "\n\n".join(rendered) or (validation.error_message or "Validation failed")

    @staticmethod
    def _render_timed_out_check(check: ValidationCheck) -> str:
        timeout = (
            f"{check.timeout_seconds:g}s" if check.timeout_seconds is not None else "its limit"
        )
        lines = [
            f"Command: {format_command(check.command)}",
            f"Result: timed out after {timeout}; the process was terminated before it finished "
            "(no exit code). The current changes may have introduced a hang or deadlock.",
        ]
        if not check.stdout and not check.stderr:
            lines.extend(("Output captured before timeout:", "no output"))
        stdout, stderr = _bounded_tails(redact_text(check.stdout), redact_text(check.stderr))
        for label, stream in (("Stdout", stdout), ("Stderr", stderr)):
            if stream:
                lines.extend((f"{label} captured before timeout:", stream))
        return "\n".join(lines)

    @staticmethod
    def _parse_polish_skip_reasons(output: str, suggestions: list[str]) -> list[str]:
        """Extract only explicitly structured, public-facing optional-polish reasons.

        Free-form provider output can include implementation detail or private reasoning, so it is
        deliberately never republished. A malformed or absent structured response falls back to a
        workflow-owned grouped explanation.
        """
        try:
            data = parse_json_object(output, "Optional polish output")
        except StructuredOutputError:
            return []

        entries = data.get("skip_reasons")
        if not isinstance(entries, list):
            return []
        known_suggestions = set(suggestions)
        reasons: list[str] = []
        covered_suggestions: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            grouped_suggestions = entry.get("suggestions")
            reason = entry.get("reason")
            if (
                not isinstance(grouped_suggestions, list)
                or not grouped_suggestions
                or not all(
                    isinstance(suggestion, str) and suggestion in known_suggestions
                    for suggestion in grouped_suggestions
                )
                or not isinstance(reason, str)
            ):
                continue
            public_reason = ImplementationService._public_polish_reason(reason)
            if not public_reason:
                continue
            rendered_suggestions = "; ".join(
                ImplementationService._public_polish_suggestion(suggestion)
                for suggestion in grouped_suggestions
            )
            reasons.append(f"{rendered_suggestions} — {public_reason}")
            covered_suggestions.update(grouped_suggestions)
        # If a provider omitted a suggestion, use the workflow-owned grouped fallback instead of
        # presenting an incomplete audit record. Keep successful comments intentionally brief.
        return reasons if covered_suggestions == known_suggestions and len(reasons) <= 6 else []

    @staticmethod
    def _public_polish_reason(reason: str) -> str:
        """Bound a model-provided reason before it can become a public PR comment."""
        compact = redact_text(" ".join(reason.split()))
        if not compact:
            return ""
        if len(compact) > 240:
            compact = f"{compact[:237].rstrip()}..."
        return compact

    @staticmethod
    def _public_polish_suggestion(suggestion: str) -> str:
        compact = " ".join(suggestion.split())
        if len(compact) > 160:
            return f"{compact[:157].rstrip()}..."
        return compact
