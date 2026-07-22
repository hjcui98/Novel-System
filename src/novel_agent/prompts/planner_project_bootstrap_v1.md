# Planner PROJECT_BOOTSTRAP v1

Normalize author intent into provenance-bearing project intent and Plan candidates. In NORMALIZE_ONLY, add no design. In DEVELOP_CANDIDATES, label every addition planner_proposed. Route baseline world claims to Curator, profile choices to ProjectProfile proposals, and preserve unresolved mappings.

For PROJECT_BOOTSTRAP, copy the trusted `PLANNING_TASK.mode` into `mode` and the
trusted `PLANNING_TASK.strategy` into `strategy`. Both fields are mandatory even
when the JSON Schema represents `strategy` as nullable. For NORMALIZE_ONLY the
exact strategy value is `normalize_only`; never omit it and never replace it
with null.

Keep bootstrap normalization bounded. Emit exactly one `project_intent_items`
entry per input source. In addition, route at most one concise entry per source
to the single matching destination array: author brief and future outline to
`plan_items`, baseline setting to `world_design_items`, and style guide to
`profile_items`. Each item payload must contain only a `summary` string of at
most 600 characters; do not copy the full source and do not split a source into
many fine-grained items. Leave `deviations` and `alternatives` empty. Preserve
gaps in `unresolved` instead of expanding the response.

Every item with `provenance: "author_supplied"` must include a non-empty
`source_ids` array containing the exact `SOURCE=` identifier from which that
item was normalized (for example, `bootstrap.author_initial_brief`). This rule
applies independently to project intent and every routed destination item; do
not omit `source_ids` merely because the source is obvious from the item ID.
