# Plan-review lens: temporal obligation windows

Fail closed on long-range timing.

- PROMISE and FORESHADOWING must declare not_before_chapter. If the window is missing and the
  author must choose the volume or phase, return HUMAN_REQUIRED.
- A proposal that RESOLVES or PAYS OFF an obligation before not_before_chapter is blocking.
  Return REVISE: keep SETUP/PROGRESS only.
- target_chapter_start/end must be complete, not reversed, and not start before not_before_chapter.
- Volume narrative stages declare `window`, `role` (setup/hint/progression/payoff) and the
  host-accepted `serves` handle. A hint/progression/payoff may not start before the
  `not_before_chapter` of the responsibility it serves; a boundary that belongs to another
  responsibility never blocks it. An unknown `serves` handle is a blocking fabrication.
- Judge the stage by what its description does, not by its label: a stage that discloses
  locked information is a hint/payoff even when it is labelled `setup`.
