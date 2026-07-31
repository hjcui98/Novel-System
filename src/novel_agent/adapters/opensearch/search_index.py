"""OpenSearch implementation of the derived SearchIndexPort."""

from typing import Any, cast

from opensearchpy import NotFoundError, OpenSearch


class OpenSearchIndex:
    def __init__(self, client: OpenSearch) -> None:
        self._client = client

    def ensure_index(self, index: str, mapping: dict[str, Any]) -> None:
        if not self._client.indices.exists(index=index):
            self._client.indices.create(index=index, body=mapping)

    def index_exists(self, index: str) -> bool:
        return bool(self._client.indices.exists(index=index))

    def index_document(self, index: str, document_id: str, document: dict[str, Any]) -> None:
        self._client.index(index=index, id=document_id, body=document, refresh="wait_for")

    def bulk_index(
        self,
        index: str,
        documents: tuple[tuple[str, dict[str, Any]], ...],
    ) -> None:
        if not documents:
            return
        body: list[dict[str, Any]] = []
        for document_id, document in documents:
            body.extend(({"index": {"_index": index, "_id": document_id}}, document))
        response = self._client.bulk(body=body, refresh=False)
        if response.get("errors") is True:
            failures = response.get("items")
            raise RuntimeError(f"OpenSearch bulk indexing failed: {failures!r}")

    def refresh(self, index: str) -> None:
        self._client.indices.refresh(index=index)

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
        self.publish_aliases(((index, alias),))

    def publish_aliases(self, bindings: tuple[tuple[str, str], ...]) -> None:
        """Switch a set of aliases in one OpenSearch update-aliases transaction."""

        if not bindings:
            raise ValueError("alias publication requires at least one binding")
        aliases = tuple(alias for _, alias in bindings)
        if any(not index or not alias for index, alias in bindings):
            raise ValueError("alias publication requires non-empty index and alias names")
        if len(aliases) != len(set(aliases)):
            raise ValueError("alias publication bindings must have unique aliases")
        actions: list[dict[str, dict[str, str]]] = []
        for index, alias in bindings:
            try:
                existing = self._client.indices.get_alias(name=alias)
            except NotFoundError:
                existing = {}
            actions.extend(
                {"remove": {"index": existing_index, "alias": alias}}
                for existing_index in existing
                if existing_index != index
            )
            actions.append({"add": {"index": index, "alias": alias}})
        self._client.indices.update_aliases(body={"actions": actions})

    def delete_index(self, index: str) -> None:
        if self._client.indices.exists(index=index):
            self._client.indices.delete(index=index)
