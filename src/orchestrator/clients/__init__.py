from orchestrator.clients.claude_code_client import ClaudeCodeClient
from orchestrator.clients.codex_client import CodexClient
from orchestrator.clients.command_client import CommandClient, CommandResult
from orchestrator.clients.git_client import GitClient
from orchestrator.clients.github_client import GitHubClient
from orchestrator.clients.github_issue_client import GitHubIssueClient
from orchestrator.clients.trello_client import TrelloClient

__all__ = [
    "ClaudeCodeClient",
    "CodexClient",
    "CommandClient",
    "CommandResult",
    "ClientFactory",
    "GitClient",
    "GitHubClient",
    "GitHubIssueClient",
    "TrelloClient",
    "WorkflowClients",
]


def __getattr__(name: str) -> object:
    """Lazily expose factory types without creating a provider import cycle."""
    if name in {"ClientFactory", "WorkflowClients"}:
        from orchestrator.clients.factory import ClientFactory, WorkflowClients

        return {"ClientFactory": ClientFactory, "WorkflowClients": WorkflowClients}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
