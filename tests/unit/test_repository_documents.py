import subprocess
from pathlib import Path

from orchestrator.models.repository_context import RepositorySourceFile
from orchestrator.services.repository_documents import (
    RepositorySourcePipeline,
    chunk_source_file,
)


def test_source_pipeline_excludes_private_generated_binary_and_ignored_files(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text("def useful():\n    return 1\n")
    (tmp_path / "IMPLEMENTER.md").write_text("private")
    (tmp_path / "REVIEWER.md").write_text("private")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "PROJECT_CONTEXT.md").write_text("durable")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text("generated")
    (tmp_path / "app.min.js").write_text("generated")
    (tmp_path / "binary.py").write_bytes(b"prefix\0suffix")

    sources = RepositorySourcePipeline("repo", tmp_path).discover()

    assert [source.path for source in sources] == ["src/service.py"]
    assert sources[0].language == "python"


def test_chunking_is_bounded_deterministic_and_line_aware() -> None:
    text = "class Service:\n" + "    value = 'semantic context'\n" * 80
    source = RepositorySourceFile(
        repository="repo",
        path="src/service.py",
        language="python",
        content_hash="abc123",
        text=text,
    )

    first = chunk_source_file(source, source_root_id="root", chunk_size_chars=256, overlap_chars=40)
    second = chunk_source_file(
        source, source_root_id="root", chunk_size_chars=256, overlap_chars=40
    )

    assert first == second
    assert len(first) > 1
    assert all(len(chunk.text) <= 256 for chunk in first)
    assert all(chunk.start_line <= chunk.end_line for chunk in first)
    assert first[0].symbol == "Service"
    assert len({chunk.point_id for chunk in first}) == len(first)


def test_source_pipeline_honors_gitignore(tmp_path: Path) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("ignored semantic text", encoding="utf-8")
    (tmp_path / "included.py").write_text("included semantic text", encoding="utf-8")

    sources = RepositorySourcePipeline("repo", tmp_path).discover()

    assert [source.path for source in sources] == ["included.py"]
