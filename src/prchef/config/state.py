from pathlib import Path


def default_state_directory(home: Path | None = None) -> Path:
    """Return the per-user directory shared by persistent prchef state."""
    return (home or Path.home()).expanduser() / ".prchef"
