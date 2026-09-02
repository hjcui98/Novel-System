# Planner PROJECT_BOOTSTRAP v1

Normalize author intent into provenance-bearing project intent and Plan candidates. In NORMALIZE_ONLY, add no design. In DEVELOP_CANDIDATES, label every addition planner_proposed. Route baseline world claims to Curator, profile choices to ProjectProfile proposals, and preserve unresolved mappings.

For PROJECT_BOOTSTRAP, copy the trusted `PLANNING_TASK.mode` into `mode` and the
trusted `PLANNING_TASK.strategy` into `strategy`. Both fields are mandatory even
when the JSON Schema represents `strategy` as nullable. For NORMALIZE_ONLY the
exact strategy value is `normalize_only`; never omit it and never replace it
with null.

Keep bootstrap normalization bounded, but do not collapse a composite author brief
into one short item. A single SOURCE_DATA document that already mixes premise, world,
factions, power systems, locations, and style must be split into many bounded items.

In `NORMALIZE_ONLY`, add no design and copy only what the source already states.
In `DEVELOP_CANDIDATES`, emit:

- 1 to 8 `project_intent_items` covering title, premise, non-goals, and long-range
  direction
- 8 to 24 `plan_items` covering opening direction, first-volume or first-stage
  direction, faction constraints, location stakes, and power-system obligations
- 8 to 24 `world_design_items` naming characters, organizations, locations,
  occupations, and baseline setting facts
- 4 to 12 `profile_items` for title, genre, chapter length, POV, and style

Each item payload should include `title` when useful and must include `description`
or `summary` that retains source names. `description` may be up to 1200 characters.
Do not emit one item per future chapter and do not set `chapter_index` above 5.
Leave `deviations` and `alternatives` empty. Preserve only genuine source gaps in
`unresolved`. Empty destination arrays cannot report coverage above 0.

Every item with `provenance: "author_supplied"` must include a non-empty
`source_ids` array containing the exact identifier from `PLANNING_TASK.source_ids`
from which that item was normalized (for example, `source.author-initial-brief`).
This rule applies independently to project intent and every routed destination
item; do not omit `source_ids` merely because the source is obvious from the item ID.
