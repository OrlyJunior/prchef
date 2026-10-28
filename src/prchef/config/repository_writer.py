"""Source-preserving writes for a single global repository entry."""

from __future__ import annotations

import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, SequenceNode
from yaml.tokens import AliasToken, AnchorToken

from prchef.config.project_config import ConfigurationError


def _last_end(node: Node) -> int:
    if isinstance(node, MappingNode) and not node.flow_style and node.value:
        return _last_end(node.value[-1][1])
    if isinstance(node, SequenceNode) and not node.flow_style and node.value:
        return _last_end(node.value[-1])
    return node.end_mark.index


def _line_end(text: str, index: int) -> int:
    if index and text[index - 1] == "\n":
        return index
    end = text.find("\n", index)
    return len(text) if end < 0 else end + 1


def _check_keys(node: Node) -> None:
    if isinstance(node, MappingNode):
        seen: set[str] = set()
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode):
                raise ConfigurationError("Cannot safely edit YAML with complex keys.")
            if key.value == "<<":
                raise ConfigurationError("Cannot safely edit YAML with merge keys.")
            if key.value in seen:
                raise ConfigurationError(
                    f"Cannot safely edit YAML with duplicate key {key.value!r}."
                )
            seen.add(key.value)
            _check_keys(value)
    elif isinstance(node, SequenceNode):
        for value in node.value:
            _check_keys(value)


def write_repository(
    path: Path, source: bytes, old_key: str, new_key: str, entry: dict[str, Any]
) -> Path:
    """Replace or insert one entry, validating semantics before atomic replacement."""
    try:
        text = source.decode("utf-8-sig")
        if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(text)):
            raise ConfigurationError("Cannot safely edit YAML with anchors or aliases.")
        root = yaml.compose(text)
        if not isinstance(root, MappingNode):
            raise ConfigurationError("Global configuration must be a YAML mapping.")
        _check_keys(root)
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ConfigurationError("Global configuration must be a YAML mapping.")
        newline = "\r\n" if "\r\n" in text else "\n"
        match = next(((k, v) for k, v in root.value if k.value == "repositories"), None)
        if match is None:
            if root.flow_style:
                fragment = yaml.safe_dump(
                    {"repositories": {new_key: entry}},
                    sort_keys=False,
                    allow_unicode=True,
                    default_flow_style=True,
                    width=100000,
                ).strip()[1:-1]
                offset = root.end_mark.index - 1
                candidate = text[:offset] + (", " if root.value else "") + fragment + text[offset:]
            else:
                fragment = yaml.safe_dump(
                    {"repositories": {new_key: entry}},
                    sort_keys=False,
                    allow_unicode=True,
                    width=100000,
                )
                separator = newline if text and not text.endswith("\n") else ""
                candidate = text + separator + fragment.replace("\n", newline)
        else:
            node = match[1]
            if not isinstance(node, MappingNode):
                raise ConfigurationError("'repositories' must be a mapping")
            found = next(((k, v) for k, v in node.value if k.value == old_key), None)
            if node.flow_style:
                rendered = yaml.safe_dump(
                    {new_key: entry},
                    sort_keys=False,
                    allow_unicode=True,
                    default_flow_style=True,
                    width=100000,
                ).strip()[1:-1]
                if found:
                    start, end = found[0].start_mark.index, found[1].end_mark.index
                else:
                    start = end = node.end_mark.index - 1
                    rendered = (", " if node.value else "") + rendered
            else:
                indent = (
                    node.value[0][0].start_mark.column
                    if node.value
                    else match[0].start_mark.column + 2
                )
                rendered = yaml.safe_dump(
                    {new_key: entry},
                    sort_keys=False,
                    allow_unicode=True,
                    default_flow_style=False,
                    width=100000,
                )
                rendered = "".join(" " * indent + line + newline for line in rendered.splitlines())
                if found:
                    start = found[0].start_mark.index - found[0].start_mark.column
                    end = _line_end(text, _last_end(found[1]))
                else:
                    start = end = _line_end(text, _last_end(node))
                    if start and text[start - 1] != "\n":
                        rendered = newline + rendered
            candidate = text[:start] + rendered + text[end:]
        expected = deepcopy(data)
        repositories = expected.setdefault("repositories", {})
        if old_key != new_key:
            repositories.pop(old_key, None)
        repositories[new_key] = entry
        if yaml.safe_load(candidate) != expected:
            raise ConfigurationError("Cannot preserve unrelated YAML values; no changes saved.")
        bom = b"\xef\xbb\xbf" if source.startswith(b"\xef\xbb\xbf") else b""
        encoded = bom + candidate.encode("utf-8")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=f".{path.name}.", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(path.stat().st_mode)
            if path.read_bytes() != source:
                raise ConfigurationError("Configuration changed externally; reload before saving.")
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except (UnicodeError, yaml.YAMLError, OSError, ValueError, TypeError) as exc:
        raise ConfigurationError(f"Cannot safely write repository configuration: {exc}") from exc
    return path
