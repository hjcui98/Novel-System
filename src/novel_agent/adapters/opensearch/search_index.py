"""OpenSearch implementation of the derived SearchIndexPort."""

from typing import Any, cast

from opensearchpy import NotFoundError, OpenSearch


class OpenSearchIndex:
    def __init__(self, client: OpenSearch) -> None:
        self._client = client

    def ensure_index(self, index: str, mapping: dict[str, Any]) -> None:
        if not self._client.indices.exists(index=index):
            self._client.indices.create(index=index, body=mapping)

    def index_document(self, index: str, document_id: str, document: dict[str, Any]) -> None:
        self._client.index(index=index, id=document_id, body=document, refresh="wait_for")

    def get_document(self, index: str, document_id: str) -> dict[str, Any] | None:
        try:
            response = self._client.get(index=index, id=document_id)
        except NotFoundError:
            return None
        source = response.get("_source")
        if not isinstance(source, dict):
            raise TypeError("OpenSearch document _source must be an object")
        return cast(dict[str, Any], source)

    def search(self, index: str, query: dict[str, Any], *, size: int) -> tuple[dict[str, Any], ...]:
        hits, _ = self.search_with_total(index, query, size=size)
        return hits

    def search_with_total(
        self, index: str, query: dict[str, Any], *, size: int
    ) -> tuple[tuple[dict[str, Any], ...], int]:
        response = self._client.search(index=index, body={"query": query, "size": size})
        hits = response["hits"]["hits"]
        total_value = response["hits"].get("total", len(hits))
        total = total_value.get("value", 0) if isinstance(total_value, dict) else total_value
        if not isinstance(total, int):
            raise TypeError("OpenSearch hits.total must resolve to an integer")
        return tuple(cast(dict[str, Any], hit) for hit in hits), total

    def publish_alias(self, index: str, alias: str) -> None:
        try:
            existing = self._client.indices.get_alias(name=alias)
        except NotFoundError:
            existing = {}
        actions = [
            {"remove": {"index": existing_index, "alias": alias}}
            for existing_index in existing
            if existing_index != index
        ]
        actions.append({"add": {"index": index, "alias": alias}})
        self._client.indices.update_aliases(body={"actions": actions})

    def delete_index(self, index: str) -> None:
        if self._client.indices.exists(index=index):
            self._client.indices.delete(index=index)
