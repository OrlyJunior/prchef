class ExternalClientError(RuntimeError):
    """Base error for an external integration."""


class CommandExecutionError(ExternalClientError):
    pass


class TrelloClientError(ExternalClientError):
    pass


class GitClientError(ExternalClientError):
    pass


class GitHubClientError(ExternalClientError):
    pass


class JulesClientError(ExternalClientError):
    def __init__(self, message: str, session_id: str | None = None) -> None:
        super().__init__(message)
        self.session_id = session_id


class JulesTransientPollError(JulesClientError):
    pass


class JulesSessionFailedError(JulesClientError):
    pass


class JulesSessionCancelledError(JulesClientError):
    pass


class JulesSessionNeedsHumanError(JulesClientError):
    """The Jules session is paused waiting for a human interaction."""


class JulesSessionTimeoutError(JulesClientError):
    pass
