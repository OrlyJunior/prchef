from pathlib import Path

import pytest

from orchestrator.config.settings import Settings


@pytest.fixture(autouse=True)
def isolated_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Keep state outside tmp_path, which workflow tests also use as the control checkout.
    home = tmp_path.parent / f"{tmp_path.name}-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Settings captures its dotenv path at import time, before fixtures run.
    monkeypatch.setitem(Settings.model_config, "env_file", home / ".orchestrator" / ".env")
    return home
