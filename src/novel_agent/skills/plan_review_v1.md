# Independent plan review

Check coverage, contradiction, feasibility, obligations, pacing, alternative
comparison, provenance, and unresolved Memory gaps. Preserve sound decisions.
Issue at most one bounded revision direction and escalate material author
tradeoffs as HUMAN_REQUIRED.

For ARC_VOLUME, perform a cross-item repetition audit. Compare same-named narrative
slots (especially `midpoint_reversal`, `volume_climax`, and `ending_state`) across
volumes. If the same reveal, event result, capability jump, antagonist mechanism,
or narrative consequence is reproduced without a new causal consequence, cost,
information state, or other discernible progress, report one blocking content issue
covering the affected items. Do not reject intentional recurrence that creates a
new state; do not accept merely because each volume is locally coherent. Each
 blocking content issue must propose comparison evidence in `affected_item_ids`,
 the exact slot field path, verbatim candidate text, and the unmet condition so the
 host can verify it. It must separately propose the items it believes need
 modification in `proposed_target_item_ids`; a comparison/baseline item is not
 automatically a write target.
If the same repetition appears in more than one narrative slot, emit a separate issue
for each slot. Every comparison row must carry the exact field path and an exact quote
from its own cited candidate field; use the structured per-item citation rows when
the wording differs across items. The legacy single `quote` remains valid only when
the same literal quote occurs in every named field. Do not mention an unquoted slot
only in `unmet_condition`. Never paraphrase a quote, normalize place names, or insert
placeholders such as `[地点]` or `[目标]`. Semantic equivalence is a review claim
that the host must inspect; it is not proved by forcing all fields to share one
substring. Before finalizing each issue, compare every per-item quote literally
against its cited field and remove any unmatched item. Those are citations and model
proposals, not permissions: omit or leave empty
the host-owned `authorized_operations`, `authorized_target_item_ids`, `actual`,
`expected`, `host_issued`, and `verification_failures` fields. Never put strings such
as `MODIFY vol-x.field` in `authorized_operations`; the host verifies the proposed
targets and derives authorization. Use one parseable dotted field path per issue, not
a comma-separated list of paths.

The top-level JSON must always include `target_kind`, `decision`, `issues`, and
`revision_instruction`. When `decision` is `REVISE`, `revision_instruction` must be a
non-empty bounded instruction that names only the verified blocking findings and their
proposed targets. When `decision` is `ACCEPT` or `HUMAN_REQUIRED`, set
`revision_instruction` explicitly to `null`. Do not omit this key or rely on `issues` to
imply the instruction; if no bounded revision can be stated, return `HUMAN_REQUIRED`.
For a blocking content issue, the minimum legacy shape is
`{"affected_item_ids":["comparison-id","target-id"],"proposed_target_item_ids":["target-id"],"field_path":"slot.description","quote":"exact candidate text","unmet_condition":"specific unmet condition"}`.
For differently worded comparisons, add
`"citations":[{"item_id":"comparison-id","field_path":"slot.description","quote":"exact text from this item"}, {"item_id":"target-id","field_path":"slot.description","quote":"exact text from that item"}]`
and keep `affected_item_ids` as the comparison set. Replace every example value with actual values from the full candidate; never copy the example IDs or prose.

A narrative stage window only has to fall inside its volume's declared
`chapter_start`/`chapter_end`. A volume may contain intentional gaps, transitions, or
parallel material that no single stage key covers. Do not invent a blocking
`TARGET_WINDOW_OUTSIDE_PARENT_SCOPE` or continuity finding merely because the stage
windows do not continuously fill the whole volume; report it only when a window
actually crosses the parent boundary.

The comparison projection is a read-only extraction of the same candidate, not a
second candidate. Never report an apparent projection/candidate mismatch as a
finding; use the full candidate as the only authority for item IDs and field values.

Treat `field_path`, `constraint_id`, `quote`, `citations`, `unmet_condition`, and the two item-id
lists as the model's proposed citation/target data. The host must re-resolve every
comparison row and target against the reviewed candidate; it owns the resulting
`actual`, `expected`, `authorized_operations`, `authorized_target_item_ids`, and
finding identity. The model cannot impersonate a host finding. Authorize only the
exact proposed target and operation supported by a verified quote and condition;
unknown IDs, duplicate operations, unexplained removal/closure, and advisory
text are not write permission. Unresolved items use explicit ADD/MODIFY/CLOSE
operations and retain operation history; the host owns stable IDs and closure
proof.
