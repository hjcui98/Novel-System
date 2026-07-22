from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest
from opensearchpy import NotFoundError, OpenSearch

from novel_agent.adapters.opensearch import OpenSearchIndex


def not_found() -> NotFoundError:
    return NotFoundError(404, "missing", {})


def test_ensure_index_creates_only_when_missing() -> None:
    client = MagicMock(spec=OpenSearch)
    client.indices = MagicMock()
    adapter = OpenSearchIndex(cast(OpenSearch, client))
    mapping = {"mappings": {"properties": {"text": {"type": "text"}}}}

    client.indices.exists.return_value = False
    adapter.ensure_index("evidence", mapping)
    client.indices.create.assert_called_once_with(index="evidence", body=mapping)

    client.indices.reset_mock()
    client.indices.exists.return_value = True
    adapter.ensure_index("evidence", mapping)
    client.indices.create.assert_not_called()


def test_index_get_and_missing_document() -> None:
    client = MagicMock(spec=OpenSearch)
    adapter = OpenSearchIndex(cast(OpenSearch, client))
    document = {"text": "证据"}

    adapter.index_document("evidence", "doc-1", document)
    client.index.assert_called_once_with(
        index="evidence", id="doc-1", body=document, refresh="wait_for"
    )

    client.get.return_value = {"_source": document}
    assert adapter.get_document("evidence", "doc-1") == document

    client.get.side_effect = not_found()
    assert adapter.get_document("evidence", "missing") is None


def test_get_rejects_non_object_source() -> None:
    client = MagicMock(spec=OpenSearch)
    client.get.return_value = {"_source": "invalid"}
    adapter = OpenSearchIndex(cast(OpenSearch, client))

    with pytest.raises(TypeError, match="must be an object"):
        adapter.get_document("evidence", "doc-1")


def test_search_preserves_raw_ranked_hits() -> None:
    client = MagicMock(spec=OpenSearch)
    hits = [{"_id": "one", "_score": 2.0}, {"_id": "two", "_score": 1.0}]
    client.search.return_value = {"hits": {"hits": hits}}
    adapter = OpenSearchIndex(cast(OpenSearch, client))

    result = adapter.search("evidence", {"match": {"text": "query"}}, size=20)

    assert result == tuple(hits)
    client.search.assert_called_once_with(
        index="evidence", body={"query": {"match": {"text": "query"}}, "size": 20}
    )

    client.search.return_value = {"hits": {"hits": hits, "total": {"value": 7}}}
    raw, total = adapter.search_with_total("evidence", {"match_all": {}}, size=2)
    assert raw == tuple(hits) and total == 7
    client.search.return_value = {"hits": {"hits": hits, "total": "invalid"}}
    with pytest.raises(TypeError, match=r"hits\.total"):
        adapter.search_with_total("evidence", {"match_all": {}}, size=2)


def test_alias_publication_and_index_deletion_are_idempotent() -> None:
    client = MagicMock(spec=OpenSearch)
    client.indices = MagicMock()
    adapter = OpenSearchIndex(cast(OpenSearch, client))
    client.indices.get_alias.return_value = {"old-index": {}, "new-index": {}}

    adapter.publish_alias("new-index", "project-anchor")
    client.indices.update_aliases.assert_called_once_with(
        body={
            "actions": [
                {"remove": {"index": "old-index", "alias": "project-anchor"}},
                {"add": {"index": "new-index", "alias": "project-anchor"}},
            ]
        }
    )

    client.indices.get_alias.side_effect = not_found()
    adapter.publish_alias("first-index", "project-grounded")
    assert client.indices.update_aliases.call_args_list[-1].kwargs == {
        "body": {"actions": [{"add": {"index": "first-index", "alias": "project-grounded"}}]}
    }

    client.indices.exists.return_value = True
    adapter.delete_index("old-index")
    client.indices.delete.assert_called_once_with(index="old-index")
    client.indices.exists.return_value = False
    adapter.delete_index("missing-index")
    client.indices.delete.assert_called_once()
