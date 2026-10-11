from __future__ import annotations

import shlex

import pytest

from orchestrator.tui.screens.provider_settings import split_provider_command


@pytest.mark.parametrize("command,executable", [
    (r'C:\Tools\codex.cmd exec --output-last-message {output_file} -', r'C:\Tools\codex.cmd'),
    (r'C:\Tools\codex.exe exec --output-last-message {output_file} -', r'C:\Tools\codex.exe'),
    (r'"C:\Program Files\Codex\codex.cmd" exec --output-last-message {output_file} -',
     r'C:\Program Files\Codex\codex.cmd'),
    (r'"C:\Program Files\Codex\codex.exe" exec --output-last-message {output_file} -',
     r'C:\Program Files\Codex\codex.exe'),
    ('codex exec --output-last-message {output_file} -', 'codex'),
])
def test_windows_provider_command_preserves_paths_and_arguments(
    command: str, executable: str,
) -> None:
    assert split_provider_command(command, windows=True) == [
        executable, "exec", "--output-last-message", "{output_file}", "-",
    ]


@pytest.mark.parametrize("arguments", [
    [r"C:\Program Files\Codex\codex.exe", "exec", "{output_file}", "-"],
    [r"C:\Tools\codex.cmd", "exec", "-c", 'model_reasoning_effort="high"', "-"],
])
def test_windows_provider_command_round_trips_the_editor_display(arguments: list[str]) -> None:
    assert split_provider_command(shlex.join(arguments), windows=True) == arguments


@pytest.mark.parametrize("command", [r"codex exec some\ file -", 'codex exec "a b" -'])
def test_non_windows_provider_command_keeps_posix_behavior(command: str) -> None:
    assert split_provider_command(command, windows=False) == shlex.split(command)


@pytest.mark.parametrize("windows", [True, False])
def test_invalid_provider_command_quoting_fails(windows: bool) -> None:
    with pytest.raises(ValueError):
        split_provider_command('codex "unfinished', windows=windows)
