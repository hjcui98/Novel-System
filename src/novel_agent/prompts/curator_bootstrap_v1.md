# Memory Curator BOOTSTRAP v1

Extract Entity, baseline State, Relation, prehistory Event, and unresolved candidates
from approved setting sources. Preserve origin and truth class. Never invent narrative
evidence or promote future Plan content to observed World facts.

A composite author brief is one source that already names a world, cities, factions,
occupations, ranks, and opening characters. Extract those named facts as many bounded
items. Do not return `items: []` for a source that names specific places, groups, or
people. `extraction_coverage` must be 0 when `items` is empty; never report high
coverage for an empty extraction.

Emit 16 to 48 `items`. Prefer these `kind` values:

- `character` for named people
- `organization` for factions, councils, houses, and occupational orders
- `location` for continents, cities, walls, stations, and dungeons
- `occupation` for named professions and rank ladders
- `baseline_state` for opening world facts and energy/power baselines

Each item payload must include `label` or `name`, `entity_type`, and `description`
or `fact` copied from the source. Keep each description under 800 characters.
Copy exact source names (do not translate them away). Every `author_supplied` item
must include the exact `source_ids`. Put only missing names or unspecified later
plot in `unresolved_claims`.
