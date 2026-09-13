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
- What the planting exemption is for: a `setup` entry is exempt because planting reaches nothing
  locked. The host does not judge the story, but it does refuse to apply the exemption to an
  entry whose own description announces a disclosure ("正式揭露"/"正式推进"/"实质揭露"/"兑现"/
  "揭晓"/"回收"): that entry is judged against the boundary it tried to step around. Judge the
  content; the host only refuses the label.
- An accepted obligation whose window the host cannot apply -- none declared, or none readable --
  is reported rather than passed. Say which it is instead of assuming the boundary was checked.
- Judge the stage by what its description does. A stage that discloses locked information
  without a `serves` handle is blocking: ask for the right responsibility there, not for a
  different label. Once the stage serves that responsibility, the host checks the boundary
  itself; a `setup`/`hint` wording dispute is not a blocking finding unless the description
  actually performs one of the disclosure actions above under a `setup` label.
