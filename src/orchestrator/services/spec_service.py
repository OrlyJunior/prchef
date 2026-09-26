from collections.abc import Callable
from pathlib import Path

from orchestrator.models.agent import (
    AgentExecutionReference,
    AgentRequest,
    StructuredOutputExpectation,
)
from orchestrator.models.development_task import DevelopmentTask
from orchestrator.models.repository_spec import RepositorySpec
from orchestrator.providers.agent_provider import AgentProvider
from orchestrator.services.exceptions import StructuredOutputError
from orchestrator.services.project_context import (
    MAX_PROJECT_CONTEXT_CHARS,
    planner_instructions_prompt,
    project_context_prompt,
)
from orchestrator.services.repository_context import (
    RepositoryContextRequest,
    RepositoryContextStrategy,
)
from orchestrator.services.structured_output import parse_json_object, string_list

_SPEC_OUTPUT = StructuredOutputExpectation(
    name="repository-spec",
    schema={
        "type": "object",
        "properties": {
            "technical_context": {"type": "string"},
            "current_behavior": {"type": "string"},
            "involved_artifacts": {"type": "array", "items": {"type": "string"}},
            "implementation_proposal": {"type": "string"},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
            "expected_tests": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "ambiguities": {"type": "array", "items": {"type": "string"}},
            "safe_to_implement": {"type": "boolean"},
        },
        "required": [
            "technical_context",
            "current_behavior",
            "involved_artifacts",
            "implementation_proposal",
            "acceptance_criteria",
            "expected_tests",
            "risks",
            "ambiguities",
            "safe_to_implement",
        ],
        "additionalProperties": False,
    },
)


class SpecService:
    def __init__(
        self,
        provider: AgentProvider,
        repository_context_strategy: RepositoryContextStrategy,
        on_context_truncated: Callable[[Path, int, int], None] | None = None,
    ) -> None:
        self._provider = provider
        self._repository_context_strategy = repository_context_strategy
        self._on_context_truncated = on_context_truncated

    def generate(
        self,
        task: DevelopmentTask,
        repository_path: Path,
        repository_slug: str,
        base_branch: str,
        on_execution_started: Callable[[AgentExecutionReference], None],
        instructions_repository_path: Path | None = None,
    ) -> RepositorySpec:
        prompt = self._build_prompt(
            task, repository_path, repository_slug, base_branch, instructions_repository_path
        )
        result = self._provider.execute(
            AgentRequest(
                instructions=prompt,
                title="Orchestrator technical specification",
                workspace_path=repository_path,
                repository=repository_slug,
                revision=base_branch,
                structured_output=_SPEC_OUTPUT,
            ),
            on_started=on_execution_started,
        )
        if not result.success:
            raise RuntimeError(result.error_message or "Planner provider failed")
        raw = result.output
        data = (
            dict(result.structured_output)
            if result.structured_output is not None
            else parse_json_object(raw, "Planner output")
        )
        required_text = [
            "technical_context",
            "current_behavior",
            "implementation_proposal",
        ]
        for field in required_text:
            if not isinstance(data.get(field), str):
                raise StructuredOutputError(f"Planner output.{field} must be a string")
        safe = data.get("safe_to_implement")
        if not isinstance(safe, bool):
            raise StructuredOutputError("Planner output.safe_to_implement must be a boolean")
        return RepositorySpec(
            technical_context=data["technical_context"],
            current_behavior=data["current_behavior"],
            involved_artifacts=string_list(
                data.get("involved_artifacts"), "involved_artifacts", "spec"
            ),
            implementation_proposal=data["implementation_proposal"],
            acceptance_criteria=string_list(
                data.get("acceptance_criteria"), "acceptance_criteria", "spec"
            ),
            expected_tests=string_list(data.get("expected_tests"), "expected_tests", "spec"),
            risks=string_list(data.get("risks"), "risks", "spec"),
            ambiguities=string_list(data.get("ambiguities"), "ambiguities", "spec"),
            safe_to_implement=safe,
            raw_text=raw,
        )

    def _build_prompt(
        self,
        task: DevelopmentTask,
        repository_path: Path,
        repository_slug: str,
        base_branch: str,
        instructions_repository_path: Path | None = None,
    ) -> str:
        repository_context = self._repository_context_strategy.render(
            RepositoryContextRequest(
                role="planner",
                repository_path=repository_path,
                task=task,
                repository_slug=repository_slug,
                base_branch=base_branch,
            )
        )
        return f"""You are the read-only technical specification role.
Analyze the repository and the original task below.

STRICT SAFETY RULES:
- Do not edit, create, delete, or rename files.
- Do not create branches, commits, tags, pull requests, or remote changes.
- Do not invent requirements. Mark blocking ambiguity explicitly.

ORIGINAL TASK (preserve as authoritative):
{task.as_original_prompt()}

{self._comments_prompt(task)}{project_context_prompt(repository_path, self._on_context_truncated)}
{repository_context}
{planner_instructions_prompt(instructions_repository_path or repository_path)}
USER-FACING PLANNING:
- Apply the following only when the task or repository evidence affects a TUI, CLI, GUI, other
  user-facing interface, or user-facing messaging. Treat the TUI as a first-class product
  interface, and keep every UX consideration grounded in the task and current product behavior.
- Consider the interaction flow, action discoverability, layout complexity, sensible defaults,
  loading/error/empty states, and consistency with existing interface patterns.
- For terminal interfaces, also consider constrained-terminal behavior and, where relevant,
  keyboard navigation, focus management, and scrolling.
- Prefer a simple primary workflow over exposing internal configuration mechanics directly on its
  main screen. Identify when configuration belongs in a dedicated settings screen instead.
- Capture relevant UX decisions and interaction constraints in implementation_proposal and
  acceptance_criteria.
- Do not invent UX requirements for backend-only changes.
When writing implementation_proposal, direct the implementation agent to update the configured
durable project context file only if the change alters project purpose, operating model, layering,
an invariant, or a design decision. Do not request feature behavior, UI details, or per-change notes
in that file. Direct the agent to keep it within the 12,000-character prompt cap by condensing or
replacing existing content instead of only appending.

Return exactly one JSON object with this shape:
{{
  "technical_context": "string",
  "current_behavior": "string",
  "involved_artifacts": ["file, class, function, or module"],
  "implementation_proposal": "string",
  "acceptance_criteria": ["string"],
  "expected_tests": ["string"],
  "risks": ["string"],
  "ambiguities": ["string"],
  "safe_to_implement": true
}}
OUTPUT CONTRACT:
- Your final agent message itself must be that JSON object.
- Return plain valid JSON only: no Markdown fence, heading, explanation, status, or
  prose before or after it.
- Do not claim that JSON was returned in a tool result, plan, artifact, or completion
  note; include it directly in the final agent message.
Set safe_to_implement to false if an ambiguity prevents safe implementation.
"""

    @staticmethod
    def _comments_prompt(task: DevelopmentTask) -> str:
        comments = task.metadata.get("comments", [])
        if not comments:
            return ""
        content = "\n\n".join(comments)
        truncated = len(content) > MAX_PROJECT_CONTEXT_CHARS
        if truncated:
            content = content[-MAX_PROJECT_CONTEXT_CHARS:]
        notice = (
            "[Earlier task comments truncated; showing the most recent content.]\n"
            if truncated else ""
        )
        return (
            "TASK COMMENTS (supporting context; the original task remains authoritative; "
            "oldest to newest):\n"
            f"{notice}{content}\n\n"
        )
