class ExternalClientError(RuntimeError):
    """Base error for an external integration."""


class CommandExecutionError(ExternalClientError):
    pass


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


class GitHubClientError(ExternalClientError):
    pass
