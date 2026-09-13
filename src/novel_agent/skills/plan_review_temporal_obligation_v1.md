# Plan-review lens: temporal obligation windows

Fail closed on long-range timing.

- PROMISE and FORESHADOWING must declare not_before_chapter. If the window is missing and the
  author must choose the volume or phase, return HUMAN_REQUIRED.
- A proposal that RESOLVES or PAYS OFF an obligation before not_before_chapter is blocking.
  Return REVISE: keep SETUP/PROGRESS only.
- target_chapter_start/end must be complete, not reversed, and not start before not_before_chapter.
- Volume narrative stages declare `window`, `role` (setup/hint/progression/payoff) and,
  when they disclose or advance something locked, the host-accepted `serves` handle. The cited
  responsibility is authoritative: the stage may not start before that responsibility's
  `not_before_chapter`, whatever its role label says, and another responsibility's boundary
  never blocks it. Planting (`setup`) may reference a lock and still happen earlier. An unknown
  `serves` handle is a blocking fabrication.
- Judge the stage by what its description does, not by its label: a stage that discloses
  locked information is a hint/payoff even when it is labelled `setup`.
