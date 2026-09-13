from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from pytest import MonkeyPatch

from orchestrator.config.project_config import QdrantSettings
from orchestrator.services.qdrant_repository_context import create_qdrant_client


def test_client_factory_passes_local_url_and_host_configuration(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def client(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setitem(sys.modules, "qdrant_client", SimpleNamespace(QdrantClient=client))

    create_qdrant_client(QdrantSettings(mode="local", path=tmp_path / "index"))
    create_qdrant_client(QdrantSettings(mode="server", url="http://localhost:6333"))
    create_qdrant_client(QdrantSettings(mode="server", host="localhost", port=7000))

    assert calls == [
        {"path": str(tmp_path / "index")},
        {"url": "http://localhost:6333"},
        {"host": "localhost", "port": 7000},
    ]
