from orchestrator.clients.codex_client import CodexClient
from orchestrator.models.agent import AgentRequest, AgentResult, AgentStartedCallback


class CodexAgentProvider:
    def __init__(self, client: CodexClient) -> None:
        self._client = client

    def execute(
        self,
        request: AgentRequest,
        *,
        on_started: AgentStartedCallback | None = None,
    ) -> AgentResult:
        del on_started
        if request.workspace_path is None:
            raise ValueError("Codex CLI provider requires a workspace path")
        result = self._client.implement(request.instructions, request.workspace_path)
        return AgentResult(
            output=result.summary,
            success=result.success,
            error_message=result.error_message,
        )
