from prchef.models.workflow_interruption import WorkflowInterruptedError


class ExternalClientError(RuntimeError):
    """Base error for an external integration."""


class CommandExecutionError(ExternalClientError):
    pass


class CommandInterruptedError(WorkflowInterruptedError, CommandExecutionError):
    """The application is stopping an owned command tree."""

    stdout: str = ""
    stderr: str = ""


class CommandProcessTreeUnconfirmedError(CommandExecutionError):
    """A timed-out command may still have a running process or descendant."""

    stdout: str = ""
    stderr: str = ""


class CommandTimeoutError(CommandExecutionError):
    """A command exceeded its configured timeout and was terminated.

    Carries whatever output the process produced before it was killed so callers can report it.
    """

    def __init__(
        self,
        message: str,
        *,
        timeout_seconds: float,
        stdout: str = "",
        stderr: str = "",
        duration_seconds: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.timeout_seconds = timeout_seconds
        self.stdout = stdout
        self.stderr = stderr
        self.duration_seconds = duration_seconds


class TrelloClientError(ExternalClientError):
    pass


class GitClientError(ExternalClientError):
    pass


class MissingRemoteBaseBranchError(GitClientError):
    def __init__(self, branch: str) -> None:
        self.branch = branch
        super().__init__(f"Remote base branch {branch!r} does not exist on origin.")


class GitHubClientError(ExternalClientError):
    pass
