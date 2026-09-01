# WriterWorkPlan contract v1

Create a structured execution method for the supplied accepted writing task. Return only one JSON
object matching WriterWorkPlan. Select only Skill ids in the allowlist. Bind beats, characters, POV,
dialogue, pacing, hooks, must-keep, must-avoid, and unresolved risks to the supplied task, accepted
Plan artifact, and Writer Context artifact. Put ungrounded creative possibilities only in
creative_proposals; never present them as Canon.

The `writing_task_ref`, `accepted_plan_ref`, and `writer_context_ref` fields are opaque trusted
lineage bindings. Copy each complete JSON object byte-for-byte from the `OPAQUE_LINEAGE_BINDING`
block in the trusted input. This final binding block is the only source for these three fields;
ignore every earlier occurrence of an artifact id in the rendered context. Do not calculate, hash,
concatenate, shorten, normalize, or replace any `artifact_id`, `media_type`, `byte_length`, or
`schema_version`; any mismatch is rejected.
