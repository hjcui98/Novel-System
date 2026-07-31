"""Versioned predicate-scoped canonical value and alias receipts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import ClassVar

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.canonical import CanonicalAliasReceipt
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes


@dataclass(frozen=True)
class CanonicalValueResolution:
    raw_value: str
    canonical_value_id: StableId
    canonicalizer_version: str


@dataclass(frozen=True)
class CanonicalAliasResolution:
    canonical_value_id: StableId
    canonicalizer_version: str
    receipt: CanonicalAliasReceipt | None
    receipt_ref: ArtifactRef | None


class CanonicalAliasRegistry:
    """Small trusted registry; entries are predicate-scoped, never benchmark-case scoped."""

    version = "canonical_alias_registry.v1"
    canonicalizer_version = "canonical_value.v1"
    _ALIASES: ClassVar[dict[str, dict[str, str]]] = {
        "attitude_toward_event": {
            "indifferent_to_ivy_feast": "indifferent_to_fame_from_ivy_feast",
            "indifferent_to_fame_from_ivy_feast": "indifferent_to_fame_from_ivy_feast",
        }
    }

    def resolve(self, predicate: str, raw_value: str) -> CanonicalValueResolution:
        normalized = self._normalize(raw_value)
        canonical = self._ALIASES.get(predicate, {}).get(normalized, normalized)
        predicate_digest = hashlib.sha256(predicate.encode("utf-8")).hexdigest()[:16]
        value_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        return CanonicalValueResolution(
            raw_value=raw_value,
            canonical_value_id=StableId(f"canonical-value.{predicate_digest}.{value_digest}"),
            canonicalizer_version=self.canonicalizer_version,
        )

    def equivalent(
        self,
        predicate: str,
        left_raw_value: str,
        right_raw_value: str,
    ) -> CanonicalAliasResolution | None:
        left = self.resolve(predicate, left_raw_value)
        right = self.resolve(predicate, right_raw_value)
        if (
            left.canonical_value_id != right.canonical_value_id
            or left.canonicalizer_version != right.canonicalizer_version
        ):
            return None
        if self._normalize(left_raw_value) == self._normalize(right_raw_value):
            return CanonicalAliasResolution(
                canonical_value_id=left.canonical_value_id,
                canonicalizer_version=left.canonicalizer_version,
                receipt=None,
                receipt_ref=None,
            )
        aliases = self._ALIASES.get(predicate, {})
        if (
            self._normalize(left_raw_value) not in aliases
            or self._normalize(right_raw_value) not in aliases
        ):
            return None
        ordered = tuple(sorted((left_raw_value, right_raw_value)))
        receipt_seed = "|".join((self.version, predicate, *ordered))
        receipt = CanonicalAliasReceipt(
            receipt_id=StableId(
                "canonical-alias-receipt."
                + hashlib.sha256(receipt_seed.encode("utf-8")).hexdigest()[:32]
            ),
            registry_version=self.version,
            predicate=predicate,
            raw_values=(ordered[0], ordered[1]),
            canonical_value_id=left.canonical_value_id,
            canonicalizer_version=left.canonicalizer_version,
        )
        receipt_ref = canonical_alias_receipt_ref(receipt)
        return CanonicalAliasResolution(
            canonical_value_id=left.canonical_value_id,
            canonicalizer_version=left.canonicalizer_version,
            receipt=receipt,
            receipt_ref=receipt_ref,
        )

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", value.casefold()).strip("_")


def canonical_alias_receipt_ref(receipt: CanonicalAliasReceipt) -> ArtifactRef:
    payload = canonical_json_bytes(receipt.model_dump(mode="json"))
    return ArtifactRef(
        artifact_id=sha256_id(payload),
        media_type="application/vnd.novel-agent.canonical-alias-receipt+json",
        byte_length=len(payload),
        schema_version=SchemaVersion("1.0.0"),
    )


__all__ = [
    "CanonicalAliasRegistry",
    "CanonicalAliasResolution",
    "CanonicalValueResolution",
    "canonical_alias_receipt_ref",
]
