from pathlib import Path

import pytest
import yaml

from prchef.clients.command_client import CommandClient, CommandResult
from prchef.clients.exceptions import (
    CommandExecutionError,
    CommandTimeoutError,
    GitClientError,
    MissingRemoteBaseBranchError,
)
from prchef.clients.git_client import GitClient
from prchef.config.project_config import ConfigurationError, ProjectConfigLoader
from prchef.config.repository_writer import write_repository


def test_base_branch_inheritance_override_reset_and_source_preservation(tmp_path: Path) -> None:
    path = tmp_path / "custom.yml"
    before = (
        b"\xef\xbb\xbf# heading\r\ndefaults: {repository: {base_branch: main}, "
        b"roles: {spec: codex, review: codex}}\r\n"
        b'repositories:\r\n  "one":\r\n    path: checkout\r\n'
        b"    repository: {base_branch: main, protected_branches: [release], branch_prefix: x}\r\n"
        b"  # other entry stays byte-for-byte\r\n  two: {path: elsewhere}\r\n"
        b'after: "untouched" # comment\r\n'
    )
    path.write_bytes(before)
    loader = ProjectConfigLoader(path)
    settings = loader.repository_settings("one")
    assert (
        settings.base_branch,
        settings.base_branch_overridden,
        settings.inherited_base_branch,
    ) == ("main", True, "main")
    loader.save_repository("renamed", "checkout", True, [], original_key="one", base_branch=None)
    after = path.read_bytes()
    assert after.startswith(
        b"\xef\xbb\xbf# heading\r\ndefaults: {repository: {base_branch: main}, "
        b"roles: {spec: codex, review: codex}}\r\n"
    )
    assert after.endswith(
        b'  # other entry stays byte-for-byte\r\n  two: {path: elsewhere}\r\n'
        b'after: "untouched" # comment\r\n'
    )
    assert b"\n" not in after.replace(b"\r\n", b"")
    data = yaml.safe_load(after.decode("utf-8-sig"))
    assert list(data["repositories"]) == ["renamed", "two"]
    assert data["repositories"]["renamed"]["repository"] == {
        "protected_branches": ["release"],
        "branch_prefix": "x",
    }
    assert not loader.repository_settings("renamed").base_branch_overridden
    loader.save_repository("renamed", "checkout", True, [], base_branch="feature/á")
    assert loader.repository_settings("renamed").base_branch == "feature/á"


@pytest.mark.parametrize(
    "branch", ["", " ", "HEAD", "-option", "a..b", "a@{b", "a~1", "a.lock", "/a", "a/"]
)
def test_invalid_base_branch_does_not_write(tmp_path: Path, branch: str) -> None:
    path = tmp_path / "config.yml"
    original = b"repositories:\n  one: {path: checkout}\n"
    path.write_bytes(original)
    with pytest.raises(ConfigurationError, match="Base branch"):
        ProjectConfigLoader(path).save_repository("one", "checkout", True, [], base_branch=branch)
    assert path.read_bytes() == original


class Commands(CommandClient):
    def __init__(self, results: list[CommandResult]) -> None:
        self.results = results
        self.calls: list[list[str]] = []
        self.options: list[dict] = []

    def run(self, command, **kwargs):  # type: ignore[no-untyped-def,override]
        self.calls.append(command)
        self.options.append(kwargs)
        return self.results.pop(0)


def result(code: int, output: str = "") -> CommandResult:
    return CommandResult([], code, output, "failed" if code else "", 0)


def test_probe_matches_full_ref_and_classifies_fetch(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    commands = Commands([result(0, "abc\trefs/heads/other/main\n"), result(1), result(0)])
    git = GitClient(commands, tmp_path)
    assert git.probe_remote_branch("main") == "missing"
    with pytest.raises(MissingRemoteBaseBranchError, match="main"):
        git.fetch_base("main")
    assert commands.calls[0] == ["git", "ls-remote", "--heads", "origin", "refs/heads/main"]
    assert commands.options[0]["timeout_seconds"] == 15
    assert commands.options[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    commands = Commands([result(1), result(1)])
    with pytest.raises(GitClientError) as exc:
        GitClient(commands, tmp_path).fetch_base("main")
    assert not isinstance(exc.value, MissingRemoteBaseBranchError)


def test_probe_found_linked_worktree_and_unavailable_paths(tmp_path: Path) -> None:
    commands = Commands([result(0, "abc\trefs/heads/main\n")])
    git = GitClient(commands, tmp_path)
    assert git.probe_remote_branch("main") == "unavailable"
    assert not commands.calls
    (tmp_path / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    assert git.probe_remote_branch("main") == "found"
    assert len(commands.calls) == 1


@pytest.mark.parametrize(
    "failure",
    [CommandExecutionError("missing git"), CommandTimeoutError("timeout", timeout_seconds=15)],
)
def test_probe_unavailable_on_command_error(tmp_path: Path, failure: Exception) -> None:
    class BrokenCommands(CommandClient):
        def run(self, command, **kwargs):  # type: ignore[no-untyped-def,override]
            raise failure

    (tmp_path / ".git").mkdir()
    assert GitClient(BrokenCommands(), tmp_path).probe_remote_branch("main") == "unavailable"


def test_repository_writer_rejects_unsafe_yaml_and_failed_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yml"
    source = b"repositories: {one: {path: checkout}}\nother: &anchor value\n"
    path.write_bytes(source)
    with pytest.raises(ConfigurationError, match="anchors"):
        write_repository(path, source, "one", "one", {"path": "new"})
    assert path.read_bytes() == source

    source = b"repositories: {one: {path: checkout}, two: {path: other}}\n"
    path.write_bytes(source)

    def fail_replace(*args: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("prchef.config.repository_writer.os.replace", fail_replace)
    with pytest.raises(ConfigurationError, match="Cannot safely write"):
        write_repository(path, source, "one", "one", {"path": "new"})
    assert path.read_bytes() == source


def test_repository_writer_detects_concurrent_change(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    source = b"repositories: {one: {path: checkout}}\n"
    changed = source + b"# external change\n"
    path.write_bytes(changed)
    with pytest.raises(ConfigurationError, match="changed externally"):
        write_repository(path, source, "one", "one", {"path": "new"})
    assert path.read_bytes() == changed


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (b"repositories: {}\nother: 1\nother: 2\n", "duplicate key 'other'"),
        (b"repositories: {}\nother: {<<: {value: 1}}\n", "merge keys"),
        (b"repositories: {}\n? [complex, key]\n: value\n", "complex keys"),
    ],
)
def test_repository_writer_identifies_unsafe_keys(
    tmp_path: Path, source: bytes, message: str
) -> None:
    path = tmp_path / "config.yml"
    path.write_bytes(source)
    with pytest.raises(ConfigurationError, match=message):
        write_repository(path, source, "one", "one", {"path": "new"})
    assert path.read_bytes() == source


def test_repository_writer_preserves_flow_mapping_siblings(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    prefix = b"before: 'keep' # comment\nrepositories: {"
    suffix = b", 'other key': {path: 'other'}} # keep\nafter: [one, two]\n"
    source = prefix + b'"one key": {path: old}' + suffix
    path.write_bytes(source)

    write_repository(path, source, "one key", "one key", {"path": "new"})

    actual = path.read_bytes()
    assert actual.startswith(prefix)
    assert actual.endswith(suffix)
    assert yaml.safe_load(actual)["repositories"]["one key"] == {"path": "new"}


def test_repository_writer_rejects_invalid_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yml"
    source = b"repositories: {one: {path: checkout}, two: {path: other}}\n"
    path.write_bytes(source)
    monkeypatch.setattr(
        "prchef.config.repository_writer.yaml.safe_dump",
        lambda *args, **kw: "{one: {path: wrong}}",
    )
    with pytest.raises(ConfigurationError, match="Cannot preserve unrelated"):
        write_repository(path, source, "one", "one", {"path": "new"})
    assert path.read_bytes() == source
