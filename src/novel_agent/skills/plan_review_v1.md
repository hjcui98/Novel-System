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
blocking content issue must cite the affected item ids, the exact slot field path,
verbatim candidate text, and the unmet condition so the host can verify it.
If the same repetition appears in more than one narrative slot, emit a separate issue
for each slot, with one exact quote from that slot and only the item ids where that
quote occurs. Do not mention an unquoted slot only in `unmet_condition`. Every named
item must contain the quote in its cited field. A quote must be copied verbatim from
the cited candidate field: never paraphrase it, normalize place names, or insert
placeholders such as `[地点]` or `[目标]`. If full sentences differ, cite only a
short exact substring shared by every named field. If you cannot copy and verify the
exact text in every named field, omit the blocking issue or make it advisory. Before
finalizing each issue, compare its quote literally against the cited field for every
affected item, remove any non-matching item id, and omit a cross-item blocking issue if
fewer than two exact matches remain. Semantic similarity is not a substitute for this
check. Those are
citations, not permissions: omit or leave empty the host-owned
`authorized_operations`, `actual`, `expected`, `host_issued`, and
`verification_failures` fields. Never put strings such as `MODIFY vol-x.field` in
`authorized_operations`; the host derives authorization. Use one parseable dotted
field path per issue, not a comma-separated list of paths.

A narrative stage window only has to fall inside its volume's declared
`chapter_start`/`chapter_end`. A volume may contain intentional gaps, transitions, or
parallel material that no single stage key covers. Do not invent a blocking
`TARGET_WINDOW_OUTSIDE_PARENT_SCOPE` or continuity finding merely because the stage
windows do not continuously fill the whole volume; report it only when a window
actually crosses the parent boundary.

Treat the host overlay as the only source of `field_path`, `constraint_id`,
`actual`, `expected`, `authorized_operations`, and finding identity. The model
cannot self-issue those fields or impersonate a host finding. Authorize only
the exact item and operation supported by a verified quote and condition;
unknown IDs, duplicate operations, unexplained removal/closure, and advisory
text are not write permission. Unresolved items use explicit ADD/MODIFY/CLOSE
operations and retain operation history; the host owns stable IDs and closure
proof.
