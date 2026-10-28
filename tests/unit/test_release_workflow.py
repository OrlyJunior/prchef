from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"


def release_steps() -> list[dict]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["tag-and-release"]["steps"]


def run_preflight(
    tmp_path: Path, tags: list[str], metadata: str | None, bump: str = "patch"
) -> tuple[subprocess.CompletedProcess[str], str]:
    step = next(step for step in release_steps() if step.get("id") == "version")
    script = step["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    if metadata is not None:
        (tmp_path / "pyproject.toml").write_text(metadata, encoding="utf-8")
    output = tmp_path / "output"
    # Replace only the Git tag inventory; execute the exact script from the workflow.
    wrapper = (
        "import json, os, subprocess\n"
        "subprocess.check_output = lambda *args, **kwargs: json.loads(os.environ['TAGS'])\n"
        "exec(" + repr(textwrap.dedent(script)) + ")\n"
    )
    env = os.environ | {
        "BUMP": bump,
        "GITHUB_OUTPUT": str(output),
        "TAGS": json.dumps("\n".join(tags)),
    }
    result = subprocess.run(
        [sys.executable, "-c", wrapper], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    return result, output.read_text(encoding="utf-8") if output.exists() else ""


def metadata(version: str) -> str:
    return f'[project]\nversion = "{version}"\n'


@pytest.mark.parametrize(
    ("tags", "version", "bump", "expected"),
    [
        ([], "0.1.0", "major", "v0.1.0"),
        (["v01.0.0", "v1.0.0-rc1", "other"], "0.1.0", "patch", "v0.1.0"),
        (["v1.2.3"], "1.2.4", "patch", "v1.2.4"),
        (["v1.2.3"], "1.3.0", "minor", "v1.3.0"),
        (["v1.2.3"], "2.0.0", "major", "v2.0.0"),
        (["v0.9.0", "v0.10.0", "v01.99.0", "v9.0.0-rc1", "other"], "0.10.1", "patch", "v0.10.1"),
    ],
)
def test_valid_release(
    tmp_path: Path, tags: list[str], version: str, bump: str, expected: str
) -> None:
    result, output = run_preflight(tmp_path, tags, metadata(version), bump)
    assert result.returncode == 0, result.stderr
    assert output == f"tag={expected}\n"


@pytest.mark.parametrize(
    ("tags", "version", "messages"),
    [
        ([], "0.2.0", ["0.2.0", "0.1.0"]),
        (["v0.1.0"], "0.1.0", ["0.1.0", "0.1.1", "already has tag v0.1.0"]),
        (["v0.1.0", "v0.3.0"], "0.1.0", ["already has tag v0.1.0", "v0.3.0", "0.3.1"]),
        (["v0.3.0"], "0.2.0", ["0.2.0", "not newer", "v0.3.0", "0.3.1"]),
        (["v0.1.0"], "0.2.0", ["0.2.0", "0.1.1", "v0.1.0"]),
    ],
)
def test_invalid_release_does_not_emit_tag(
    tmp_path: Path, tags: list[str], version: str, messages: list[str]
) -> None:
    result, output = run_preflight(tmp_path, tags, metadata(version))
    assert result.returncode != 0
    assert output == ""
    assert all(message in result.stderr for message in messages)
    assert "pyproject.toml" in result.stderr
    assert "increment" in result.stderr


@pytest.mark.parametrize(
    "content",
    [None, "[project]\n", '[project]\nversion = "01.2.3"\n', "[project]\nversion = 123\n", "["],
)
def test_invalid_metadata_does_not_emit_tag(tmp_path: Path, content: str | None) -> None:
    result, output = run_preflight(tmp_path, [], content)
    assert result.returncode != 0
    assert output == ""
    assert "project.version" in result.stderr
    assert "pyproject.toml" in result.stderr
    assert "0.1.0" in result.stderr


def test_version_checks_precede_tag_creation_and_push() -> None:
    steps = release_steps()
    names = [step.get("name") for step in steps]
    assert names.index("Fetch release tags") < names.index("Validate release version")
    assert names.index("Validate release version") < names.index("Ensure tag does not exist")
    assert names.index("Ensure tag does not exist") < names.index("Tag checked-out develop commit")
    guard = steps[names.index("Ensure tag does not exist")]["run"]
    creation = steps[names.index("Tag checked-out develop commit")]["run"]
    assert 'git show-ref --verify --quiet "refs/tags/$TAG"' in guard
    assert 'git tag -a "$TAG"' in creation
    assert 'git push origin "refs/tags/$TAG"' in creation
