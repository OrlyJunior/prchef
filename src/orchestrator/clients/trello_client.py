from __future__ import annotations

from typing import Any

import httpx

from orchestrator.clients.exceptions import TrelloClientError
from orchestrator.models.development_task import DevelopmentTask


class TrelloClient:
    def __init__(
        self,
        api_key: str | None,
        token: str | None,
        *,
        base_url: str = "https://api.trello.com/1",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def get_card(self, card_id: str) -> DevelopmentTask:
        if not self._api_key or not self._token:
            raise TrelloClientError("Trello credentials are not configured")
        params: dict[str, str] = {
            "key": self._api_key,
            "token": self._token,
            "fields": "id,name,desc,url,shortUrl,idBoard,idList,labels,dateLastActivity",
            "customFieldItems": "true",
        }
        card = self._get_json(f"/cards/{card_id}", params)
        definitions = self._get_json(f"/boards/{card['idBoard']}/customFields", params)
        custom_fields = self._map_custom_fields(
            card.get("customFieldItems", []), definitions if isinstance(definitions, list) else []
        )
        metadata = {
            "board_id": card.get("idBoard"),
            "list_id": card.get("idList"),
            "last_activity": card.get("dateLastActivity"),
            "labels": card.get("labels", []),
            "short_url": card.get("shortUrl"),
            "custom_fields": custom_fields,
        }
        repository = self._repository_from_custom_fields(custom_fields)
        return DevelopmentTask(
            id=str(card["id"]),
            source="trello",
            title=str(card.get("name", "")),
            description=str(card.get("desc", "")),
            url=str(card.get("url") or card.get("shortUrl") or ""),
            repository=repository,
            metadata=metadata,
        )

    def _get_json(self, path: str, params: dict[str, str]) -> Any:
        try:
            response = httpx.get(
                f"{self._base_url}{path}", params=params, timeout=self._timeout_seconds
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise TrelloClientError(f"Trello request failed for {path}: {exc}") from exc

    @staticmethod
    def _map_custom_fields(
        items: list[dict[str, Any]], definitions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        by_id = {str(definition.get("id")): definition for definition in definitions}
        mapped: dict[str, Any] = {}
        for item in items:
            definition = by_id.get(str(item.get("idCustomField")))
            if not definition:
                continue
            name = str(definition.get("name", item.get("idCustomField", "unknown")))
            value = item.get("value")
            if isinstance(value, dict) and value:
                mapped[name] = next(iter(value.values()))
                continue
            selected_id = item.get("idValue")
            options = definition.get("options", [])
            selected = next(
                (option for option in options if str(option.get("id")) == str(selected_id)), None
            )
            if selected:
                mapped[name] = selected.get("value", {}).get("text")
        return mapped

    @staticmethod
    def _repository_from_custom_fields(custom_fields: dict[str, Any]) -> str | None:
        lowered = {str(key).casefold(): value for key, value in custom_fields.items()}
        for key in ("repository", "repo"):
            value = lowered.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None
