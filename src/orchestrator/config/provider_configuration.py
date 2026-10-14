"""Provider editor validation and source-preserving global configuration writes."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from copy import deepcopy
from math import isfinite
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import yaml
from pydantic import BaseModel, ValidationError
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken

from orchestrator.config.project_config import (
    AgentPresetConfig,
    ClaudeCodeProviderSettings,
    CodexCliProviderSettings,
    ConfigurationError,
    LiteLLMProviderSettings,
    _deep_merge,
)

PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "codex-cli": {
        "command": ["codex", "exec", "--sandbox", "workspace-write",
                    "--output-last-message", "{output_file}", "-"],
        "timeout_seconds": 3600, "max_concurrency": 1,
    },
    "claude-code": {"executable": "claude", "permission_mode": "dontAsk",
                    "timeout_seconds": 1800, "max_concurrency": 1},
    "litellm": {"model": "", "max_concurrency": 1},
}
_SCHEMAS: dict[str, type[BaseModel]] = {
    "codex-cli": CodexCliProviderSettings,
    "claude-code": ClaudeCodeProviderSettings,
    "litellm": LiteLLMProviderSettings,
}
_CREDENTIAL = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|authorization|password|"
    r"secret|credential|private[_-]?key|(?:^|[_-])token$|"
    r"(?:^|[\W_])(?:auth|bearer)(?=$|[\W_]))", re.I,
)
_BEARER_CREDENTIAL = re.compile(r"(?<!\w)bearer\s+\S+", re.I)


def _reject_credentials(value: Any, *, root: bool = False) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _CREDENTIAL.search(str(key)) and not (root and key == "api_key_env"):
                raise ConfigurationError("Credentials are forbidden; use api_key_env references.")
            _reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _reject_credentials(item)
    elif isinstance(value, str) and _BEARER_CREDENTIAL.search(value):
        raise ConfigurationError("Credentials are forbidden; use api_key_env references.")


def reject_provider_credentials(values: Mapping[str, Any]) -> None:
    """Also used before displaying persisted values in the editor."""
    _reject_credentials(values, root=True)
    env = values.get("api_key_env")
    if env is not None and (not isinstance(env, str) or not re.fullmatch(
        r"[A-Za-z_]\w*", env, flags=re.ASCII,
    )):
        raise ConfigurationError("api_key_env must be an environment variable name.")
    base = values.get("api_base")
    if isinstance(base, str):
        try:
            query = parse_qsl(urlsplit(base).query, keep_blank_values=True)
        except ValueError:
            raise ConfigurationError("api_base must be a valid URL without credentials.") from None
        if "@" in base or any(
            _CREDENTIAL.search(name) or re.search(r"key|token|secret", name, flags=re.I)
            for name, _ in query
        ):
            raise ConfigurationError("api_base must not contain credentials.")
    command = values.get("command", [])
    if isinstance(command, str) and _CREDENTIAL.search(command):
        raise ConfigurationError("command must not contain credential flags or assignments.")
    if isinstance(command, list) and any(
        isinstance(arg, str) and any(_CREDENTIAL.search(part) for part in (
            arg.split("=")[:-1] if "=" in arg else [arg]
        ))
        for arg in command if isinstance(arg, str) and (
            "=" in arg or arg.startswith("--") or arg.casefold() == "bearer"
        )
    ):
        raise ConfigurationError("command must not contain credential flags or assignments.")
    executable = values.get("executable")
    if isinstance(executable, str) and "=" in executable and _CREDENTIAL.search(executable):
        raise ConfigurationError("executable must not contain credential assignments.")


def validate_provider(settings: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate without constructing clients, resolving secrets, or executing commands."""
    values = deepcopy(dict(settings))
    reject_provider_credentials(values)
    kind = values.get("type")
    if not isinstance(kind, str) or kind not in _SCHEMAS:
        raise ConfigurationError("Choose a supported provider type.")
    for field in ("model", "executable"):
        if field in values and (not isinstance(values[field], str) or not values[field].strip()):
            raise ConfigurationError(f"{field} must be nonblank.")
    concurrency = values.get("max_concurrency", 1)
    if type(concurrency) is not int or concurrency <= 0:
        raise ConfigurationError("max_concurrency must be a positive integer.")
    timeout = values.get("timeout_seconds")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
        not isfinite(timeout) or timeout <= 0
    ):
        raise ConfigurationError("timeout_seconds must be a positive number.")
    executable = None
    if kind == "codex-cli":
        command = values.get("command")
        if not isinstance(command, list) or not command or any(
            not isinstance(arg, str) or not arg.strip() or any(c in arg for c in "\r\n\x00")
            for arg in command
        ):
            raise ConfigurationError("command must be a nonempty argument list.")
        if command[0].startswith("-"):
            raise ConfigurationError("command must start with an executable.")
        executable = command[0]
    elif kind == "claude-code":
        executable = values.get("executable", "claude")
        if isinstance(executable, str) and re.search(r"\s--|=", executable):
            raise ConfigurationError("executable must be one executable without CLI arguments.")
    try:
        _SCHEMAS[kind].model_validate(values)
    except ValidationError as exc:
        # Pydantic's formatted exception includes input values. Never expose it.
        for error in exc.errors(include_input=False):
            message = error["msg"]
            for marker, explanation in (
                ("conflicting model flag", "model conflicts with the command's model flag."),
                ("conflicting model_reasoning_effort", "reasoning_effort conflicts with command."),
                ("requires the workspace-write",
                 "approval_policy requires workspace-write sandbox."),
                ("without a value", "command has a model or reasoning flag without a value."),
            ):
                if marker in message:
                    raise ConfigurationError(explanation) from None
        fields = sorted({".".join(map(str, error["loc"])) or "provider options"
                         for error in exc.errors(include_input=False)})
        raise ConfigurationError("Invalid " + ", ".join(fields) +
                                 "; check required fields and backend constraints.") from None
    warnings = []
    if executable and shutil.which(executable) is None:
        warnings.append("Executable not found on PATH; install it before running this provider.")
    return values, warnings


def _read(path: Path) -> tuple[bytes, str, dict[str, Any], MappingNode]:
    source = path.read_bytes()
    try:
        text = source.decode("utf-8-sig")
        if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(text)):
            raise ConfigurationError("Cannot safely edit YAML with anchors or aliases.")
        # Ambiguous duplicate keys cannot safely be edited by source locations.
        node = yaml.compose(text)
        data = yaml.safe_load(text)
        if not isinstance(node, MappingNode) or not isinstance(data, dict):
            raise ConfigurationError("Global configuration must be a YAML mapping.")
        _check_keys(node)
        return source, text, data, node
    except (UnicodeError, yaml.YAMLError):
        raise ConfigurationError("Cannot read provider settings: invalid UTF-8 YAML.") from None


def _check_keys(node: Node, visited: set[int] | None = None) -> None:
    if visited is None:
        visited = set()
    if id(node) in visited:
        return
    visited.add(id(node))
    if isinstance(node, MappingNode):
        keys = []
        for key, value in node.value:
            if not isinstance(key, ScalarNode) or key.value in keys:
                raise ConfigurationError("Provider editing requires unique scalar YAML keys.")
            keys.append(key.value)
            _check_keys(value, visited)
    elif isinstance(node, SequenceNode):
        for child in node.value:
            _check_keys(child, visited)


def _providers(data: dict[str, Any]) -> dict[str, Any]:
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigurationError("defaults must be a mapping.")
    providers = defaults.get("providers", {})
    if not isinstance(providers, dict):
        raise ConfigurationError("defaults.providers must be a mapping.")
    return providers


def list_providers(path: Path) -> dict[str, dict[str, Any]]:
    providers = _providers(_read(path)[2])
    if any(not isinstance(name, str) or not isinstance(value, dict)
           for name, value in providers.items()):
        raise ConfigurationError("Each provider must have a name and a mapping of options.")
    return deepcopy(providers)


def provider_repository_overrides(path: Path, name: str) -> list[str]:
    """List repository entries overriding this global provider, without loading a checkout."""
    repositories = _read(path)[2].get("repositories", {})
    if not isinstance(repositories, dict):
        raise ConfigurationError("repositories must be a mapping.")
    result = []
    for key, entry in repositories.items():
        if not isinstance(entry, dict):
            continue
        providers = entry.get("providers", {})
        override = providers.get(name) if isinstance(providers, dict) else None
        if isinstance(override, dict) and override:
            result.append(str(key))
    return sorted(result)


def provider_references(data: dict[str, Any], name: str) -> list[str]:
    """Inspect roles and presets, including inherited legacy implementer selection."""
    result: set[str] = set()
    defaults = data.get("defaults", {})
    entries = [("defaults", defaults)]
    repositories = data.get("repositories", {})
    if isinstance(repositories, dict):
        entries.extend((f"repositories.{key}", _deep_merge(defaults, entry))
                       for key, entry in repositories.items() if isinstance(entry, dict))
    for location, entry in entries:
        if not isinstance(entry, dict):
            continue
        agents = entry.get("agents", {})
        for role, settings in agents.items() if isinstance(agents, dict) else []:
            if isinstance(settings, dict) and settings.get("provider") == name:
                result.add(f"{location}.agents.{role}.provider")
        if "agents" not in entry:
            roles = entry.get("roles", {})
            if isinstance(roles, dict):
                for role in ("spec", "implementation", "review"):
                    if roles.get(role, "codex" if role == "implementation" else None) == name:
                        result.add(f"{location}.roles.{role}")
        presets = entry.get("agent_presets", {})
        if isinstance(presets, dict):
            for role, choices in presets.items():
                for preset, settings in choices.items() if isinstance(choices, dict) else []:
                    if isinstance(settings, dict) and settings.get("provider") == name:
                        result.add(f"{location}.agent_presets.{role}.{preset}.provider")
    return sorted(result)


def _validate_presets(data: dict[str, Any], name: str, values: dict[str, Any]) -> None:
    defaults = data.get("defaults", {})
    entries = [("defaults", defaults)]
    overrides: dict[str, dict[str, Any]] = {"defaults": {}}
    repositories = data.get("repositories", {})
    if not isinstance(repositories, dict):
        raise ConfigurationError("repositories must be a mapping.")
    for key, raw in repositories.items():
        if not isinstance(raw, dict):
            raise ConfigurationError("Repository settings must be mappings.")
        location = f"repositories.{key}"
        entries.append((location, _deep_merge(defaults, raw)))
        providers = raw.get("providers", {})
        if not isinstance(providers, dict) or not isinstance(providers.get(name, {}), dict):
            raise ConfigurationError("Repository provider overrides must be mappings.")
        overrides[location] = providers.get(name, {})
    for location, entry in entries:
        effective = _deep_merge(values, overrides[location])
        try:
            validate_provider(effective)
        except ConfigurationError:
            raise ConfigurationError(
                f"Provider options are incompatible with {location}.providers.{name}."
            ) from None
        role_presets = entry.get("agent_presets", {})
        if not isinstance(role_presets, dict):
            raise ConfigurationError(f"{location}.agent_presets must be a mapping.")
        for role, presets in role_presets.items():
            if not isinstance(presets, dict):
                raise ConfigurationError(f"{location}.agent_presets.{role} must be a mapping.")
            for preset_name, raw in presets.items():
                if not isinstance(raw, dict) or raw.get("provider") != name:
                    continue
                reference = f"{location}.agent_presets.{role}.{preset_name}"
                try:
                    preset = AgentPresetConfig.model_validate(raw)
                    # Reuse the runtime option boundary without loading a repository or adapter.
                    from orchestrator.config.project_config import ProjectConfig

                    config = ProjectConfig.model_validate({
                        "path": Path("."), "providers": {name: effective},
                        "agents": {r: {"provider": name}
                                   for r in ("planner", "implementer", "reviewer")},
                    })
                    config.with_agent_runtime_overrides({role: name},
                                                        {role: preset.runtime_options()})
                except (ValueError, ConfigurationError):
                    raise ConfigurationError(
                        f"Provider options are incompatible with {reference}."
                    ) from None


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
    return len(text) if end == -1 else end + 1


def _dump(value: dict[str, Any], indent: int, newline: str, *, flow: bool = False) -> str:
    rendered = yaml.safe_dump(value, sort_keys=False, allow_unicode=True,
                              default_flow_style=flow, width=100_000)
    if flow:
        return rendered.strip()
    return "".join(" " * indent + line + newline for line in rendered.splitlines())


def _splice(text: str, node: MappingNode, keys: list[str], value: Any,
            newline: str) -> str:
    key = keys[0]
    match = next(((k, v) for k, v in node.value if k.value == key), None)
    if len(keys) > 1 and match:
        if not isinstance(match[1], MappingNode):
            raise ConfigurationError("Provider parent containers must be mappings.")
        return _splice(text, match[1], keys[1:], value, newline)
    replacement = value
    for parent in reversed(keys[1:]):
        replacement = {parent: replacement}
    if node.flow_style:
        if match:
            k, v = match
            start, end = k.start_mark.index, v.end_mark.index
            if value is None:
                index = node.value.index(match)
                if index + 1 < len(node.value):
                    end = node.value[index + 1][0].start_mark.index
                elif index:
                    start = node.value[index - 1][1].end_mark.index
                fragment = ""
            else:
                fragment = _dump({key: replacement}, 0, newline, flow=True)[1:-1]
        else:
            start = end = node.end_mark.index - 1
            fragment = (", " if node.value else "") + \
                _dump({key: replacement}, 0, newline, flow=True)[1:-1]
        return text[:start] + fragment + text[end:]
    indent = node.value[0][0].start_mark.column if node.value else node.start_mark.column
    if match:
        k, v = match
        start = k.start_mark.index - k.start_mark.column
        end = _line_end(text, _last_end(v))
        if value is None and len(node.value) == 1:
            # Keep the now-empty providers mapping valid.
            fragment = " " * indent + "{}" + newline
        else:
            fragment = "" if value is None else _dump({key: replacement}, indent, newline)
    else:
        start = end = _line_end(text, _last_end(node))
        fragment = (newline if start and text[start - 1] != "\n" else "") + \
            _dump({key: replacement}, indent, newline)
    return text[:start] + fragment + text[end:]


def save_provider(path: Path, name: str, values: Mapping[str, Any] | None,
                  *, create: bool = False, expected_source: bytes | None = None) -> list[str]:
    """Apply one entry edit; validation or IO failure leaves the source intact."""
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ConfigurationError("Provider name must be nonblank with no surrounding whitespace.")
    source, text, data, node = _read(path)
    if expected_source is not None and source != expected_source:
        raise ConfigurationError("Configuration changed externally; reload before saving.")
    providers = _providers(data)
    if create and name in providers:
        raise ConfigurationError("Provider name already exists.")
    if not create and name not in providers:
        raise ConfigurationError("Provider no longer exists; reload before saving.")
    if re.search(r"(?m)^\s*<<\s*:", text):
        raise ConfigurationError("Cannot safely edit YAML merge keys.")
    warnings: list[str] = []
    if values is None:
        references = provider_references(data, name)
        if references:
            raise ConfigurationError("Provider is used by: " + ", ".join(references))
        definition = None
    else:
        definition, warnings = validate_provider(values)
        _validate_presets(data, name, definition)
    newline = "\r\n" if "\r\n" in text else "\n"
    try:
        candidate = _splice(text, node, ["defaults", "providers", name], definition, newline)
    except (yaml.YAMLError, ValueError, TypeError, OSError):
        raise ConfigurationError("Cannot serialize provider options; no changes saved.") from None
    expected = deepcopy(data)
    target = expected.setdefault("defaults", {}).setdefault("providers", {})
    if definition is None:
        del target[name]
    else:
        target[name] = definition
    try:
        if yaml.safe_load(candidate) != expected:
            raise ConfigurationError("Cannot preserve unrelated YAML values; no changes saved.")
    except yaml.YAMLError:
        raise ConfigurationError("Candidate YAML is invalid; no changes saved.") from None
    encoded = candidate.encode("utf-8")
    if source.startswith(b"\xef\xbb\xbf"):
        encoded = b"\xef\xbb\xbf" + encoded
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(path.stat().st_mode)
        if path.read_bytes() != source:
            raise ConfigurationError("Configuration changed externally; reload before saving.")
        os.replace(temporary, path)
    except OSError:
        raise ConfigurationError(
            "Could not write provider configuration; no changes saved."
        ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return warnings
