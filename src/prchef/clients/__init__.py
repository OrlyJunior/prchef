from prchef.clients.claude_code_client import ClaudeCodeClient
from prchef.clients.codex_client import CodexClient
from prchef.clients.command_client import CommandClient, CommandResult
from prchef.clients.git_client import GitClient
from prchef.clients.github_client import GitHubClient
from prchef.clients.github_issue_client import GitHubIssueClient
from prchef.clients.trello_client import TrelloClient

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
        from prchef.clients.factory import ClientFactory, WorkflowClients

        return {"ClientFactory": ClientFactory, "WorkflowClients": WorkflowClients}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
