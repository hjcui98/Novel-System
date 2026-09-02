# Plan-review lens: parent scope

A child plan may not exceed its parent.

- ARC_VOLUME and CHAPTER_SET nodes must be bounded (chapter_start and chapter_end).
- If a parent has a chapter range, the child range must sit inside it.
- STORY may be unbounded. REPLAN is an action, not a level; inspect the target PlanLevel instead.
- One post-Genesis candidate still produces only one PlanLevel.
