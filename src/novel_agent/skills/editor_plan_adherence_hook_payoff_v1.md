# Editor lens: plan adherence / hook-payoff

Enforce current-chapter plan constraints and future-locked obligations.

- mandatory_constraints and forbidden_reveals are hard. A payoff, reveal, or resolution that those
  fields forbid is blocking.
- Future-locked obligations may SETUP or PROGRESS now; they must not RESOLVE or PAYOFF before
  not_before_chapter.
- Do not treat a long-range hook as completed just because the chapter mentions it.
