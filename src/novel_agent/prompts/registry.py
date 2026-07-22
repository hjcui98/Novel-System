"""Content-addressed prompt loading and deterministic layered rendering."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.stage2 import PromptContractRef


class PromptRegistryError(ValueError):
    pass


def content_hash(content: bytes) -> ArtifactId:
    return ArtifactId(f"sha256:{hashlib.sha256(content).hexdigest()}")


@dataclass(frozen=True)
class PromptTemplate:
    prompt_id: StableId
    version: SchemaVersion
    path: Path
    expected_hash: ArtifactId


class PromptRegistry:
    def __init__(self, templates: Iterable[PromptTemplate]) -> None:
        indexed: dict[tuple[StableId, SchemaVersion], PromptTemplate] = {}
        for template in templates:
            key = (template.prompt_id, template.version)
            if key in indexed:
                raise PromptRegistryError(f"duplicate prompt contract: {key}")
            indexed[key] = template
        self._templates = indexed

    def read(self, prompt_id: StableId, version: SchemaVersion) -> str:
        try:
            template = self._templates[(prompt_id, version)]
        except KeyError as error:
            raise PromptRegistryError("prompt version is not explicitly registered") from error
        content = template.path.read_bytes()
        if content_hash(content) != template.expected_hash:
            raise PromptRegistryError(f"prompt content hash mismatch: {template.path}")
        return content.decode("utf-8")

    def render(
        self,
        layers: tuple[tuple[StableId, SchemaVersion], ...],
        task_payload: str,
        source_hashes: tuple[ArtifactId, ...] = (),
    ) -> tuple[str, tuple[PromptContractRef, ...]]:
        rendered_layers: list[str] = []
        refs: list[PromptContractRef] = []
        for prompt_id, version in layers:
            text = self.read(prompt_id, version)
            layer_hash = content_hash(text.encode("utf-8"))
            rendered_layers.append(text)
            refs.append(
                PromptContractRef(
                    contract_id=prompt_id,
                    version=version,
                    content_hash=layer_hash,
                    render_fingerprint=layer_hash,
                )
            )
        source_manifest = "\n".join(sorted(source.root for source in source_hashes))
        rendered = (
            "\n\n".join(rendered_layers)
            + '\n\n<TASK_PAYLOAD trusted="false">\n'
            + task_payload
            + "\n</TASK_PAYLOAD>\n<SOURCE_HASHES>\n"
            + source_manifest
            + "\n</SOURCE_HASHES>"
        )
        fingerprint = content_hash(rendered.encode("utf-8"))
        return rendered, tuple(
            ref.model_copy(update={"render_fingerprint": fingerprint}) for ref in refs
        )
