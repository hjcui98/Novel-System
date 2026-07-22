"""Replaceable derived search-index boundary."""

from typing import Any, Protocol


class SearchIndexPort(Protocol):
    def ensure_index(self, index: str, mapping: dict[str, Any]) -> None: ...

    def index_document(self, index: str, document_id: str, document: dict[str, Any]) -> None: ...

    def get_document(self, index: str, document_id: str) -> dict[str, Any] | None: ...

    def search(
        self, index: str, query: dict[str, Any], *, size: int
    ) -> tuple[dict[str, Any], ...]: ...
