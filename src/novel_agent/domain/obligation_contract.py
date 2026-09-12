"""Shared obligation-declaration and action contract for planning candidates.

Planning candidates were written with two different shapes over time:

* the current ``obligation_declarations`` form, and
* the legacy ``obligation_plan`` responsibility table whose entries carry
  free-text ``summary`` plus ``setup_window``/``progress_windows``/
  ``payoff_window`` strings.

The legacy form was never compiled: the materializer only looked for
``obligation_declarations``-style keys, so an upper-layer responsibility table
silently produced zero World obligations and every lower-layer reference then
had nothing to bind to.  Host review also accepted ``obligation_actions`` that
were plain natural-language strings, so the failure only surfaced at commit.

This module is the single normalization owner shared by host review and the
candidate materializer.  A declaration or action that cannot be interpreted is
reported as an explicit discrepancy instead of being dropped, guessed by name
similarity, or silently ignored.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from novel_agent.domain.memory import ObligationKind

_CHAPTER_WINDOW = re.compile(r"^(?P<start>[1-9][0-9]*)(?:\s*-\s*(?P<end>[1-9][0-9]*))?$")
_ACTION_ALIASES = {
    "setup": "SETUP",
    "progress": "PROGRESS",
    "payoff": "PAYOFF",
    "defer": "DEFER",
}


class ObligationAction(StrEnum):
    """Unified execution semantics for one declared obligation action."""

    SETUP = "SETUP"
    PROGRESS = "PROGRESS"
    PAYOFF = "PAYOFF"
    DEFER = "DEFER"


class ObligationWindow(StrEnum):
    """Which stage window of a responsibility one chapter action serves."""

    SETUP = "setup"
    PROGRESS = "progress"
    PAYOFF = "payoff"
    FORBIDDEN_REVEAL = "forbidden_reveal"


class ObligationContractError(ValueError):
    """A declaration or action that cannot be normalized unambiguously."""


@dataclass(frozen=True, slots=True)
class ChapterWindow:
    """One inclusive chapter range parsed from a declared window value."""

    start: int
    end: int

    def as_text(self) -> str:
        return f"{self.start}-{self.end}"


@dataclass(frozen=True, slots=True)
class ObligationDeclarationInput:
    """One mutable declaration item handed to the identity-owning binder."""

    kind: ObligationKind
    description: str
    not_before_chapter: int | None
    source_form: str
    source_ordinal: int
    setup_window: ChapterWindow | None
    progress_windows: tuple[ChapterWindow, ...]
    payoff_window: ChapterWindow | None

    def as_binder_payload(self) -> dict[str, Any]:
        """Return the declaration mapping the existing binder consumes."""

        return {
            "kind": self.kind.value,
            "summary": self.description,
            "not_before_chapter": self.not_before_chapter,
        }

    def as_source_record(self) -> dict[str, Any]:
        """Return the untouched legacy source values for lineage."""

        return {
            "source_form": self.source_form,
            "source_ordinal": self.source_ordinal,
            "setup_window": self.setup_window.as_text() if self.setup_window else None,
            "progress_windows": [window.as_text() for window in self.progress_windows],
            "payoff_window": self.payoff_window.as_text() if self.payoff_window else None,
        }


@dataclass(frozen=True, slots=True)
class ObligationDeclarationCompilation:
    """Normalized declarations plus every item that could not be interpreted."""

    declarations: tuple[ObligationDeclarationInput, ...]
    discrepancies: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.discrepancies


@dataclass(frozen=True, slots=True)
class ObligationActionInput:
    """One normalized chapter-scope action referencing an accepted obligation."""

    obligation_id: str
    action: ObligationAction
    expected_delta: str
    window: ObligationWindow | None = None

    @property
    def resolves_obligation(self) -> bool:
        return self.action is ObligationAction.PAYOFF

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "obligation_id": self.obligation_id,
            "action": self.action.value,
            "expected_delta": self.expected_delta,
        }
        if self.window is not None:
            payload["window"] = self.window.value
        return payload


@dataclass(frozen=True, slots=True)
class ObligationActionCompilation:
    """Normalized chapter actions plus every action that could not be read."""

    actions: tuple[ObligationActionInput, ...]
    discrepancies: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.discrepancies


def parse_chapter_window(value: object, *, field: str) -> ChapterWindow:
    """Parse one inclusive chapter window, rejecting anything ambiguous."""

    if isinstance(value, bool):
        raise ObligationContractError(f"{field} must be a chapter number or range")
    if isinstance(value, int):
        if value < 1:
            raise ObligationContractError(f"{field} must be a positive chapter number")
        return ChapterWindow(start=value, end=value)
    if not isinstance(value, str):
        raise ObligationContractError(f"{field} must be a chapter number or range")
    match = _CHAPTER_WINDOW.match(value.strip())
    if match is None:
        raise ObligationContractError(f"{field} is not a chapter number or range: {value!r}")
    start = int(match.group("start"))
    end = int(match.group("end")) if match.group("end") else start
    if end < start:
        raise ObligationContractError(f"{field} is reversed: {value!r}")
    return ChapterWindow(start=start, end=end)


def is_legacy_obligation_plan(value: object) -> bool:
    """Report whether a payload value is a legacy responsibility table."""

    if not isinstance(value, (list, tuple)) or not value:
        return False
    return any(
        isinstance(entry, Mapping)
        and any(
            key in entry for key in ("setup_window", "progress_windows", "payoff_window", "summary")
        )
        and not any(key in entry for key in ("obligation_id", "id"))
        for entry in value
    )


def _legacy_text(entry: Mapping[str, object], ordinal: int) -> str:
    for key in ("summary", "description", "goal", "text", "objective"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    raise ObligationContractError(
        f"obligation_plan entry {ordinal} has no usable summary/description"
    )


def _progress_windows(raw: object, *, ordinal: int) -> tuple[ChapterWindow, ...]:
    if raw is None:
        return ()
    values: Sequence[object] = raw if isinstance(raw, (list, tuple)) else (raw,)
    windows: list[ChapterWindow] = []
    for index, value in enumerate(values):
        windows.append(
            parse_chapter_window(
                value, field=f"obligation_plan[{ordinal}].progress_windows[{index}]"
            )
        )
    return tuple(windows)


def compile_legacy_obligation_plan(value: object) -> ObligationDeclarationCompilation:
    """Compile a legacy responsibility table into binder declarations.

    Every window is parsed or reported; nothing is dropped by name similarity.
    An entry that cannot be interpreted contributes a discrepancy and no
    declaration.
    """

    if not isinstance(value, (list, tuple)):
        return ObligationDeclarationCompilation(
            declarations=(),
            discrepancies=("obligation_plan must be a list of responsibility objects",),
        )
    declarations: list[ObligationDeclarationInput] = []
    discrepancies: list[str] = []
    for ordinal, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            discrepancies.append(f"obligation_plan[{ordinal}] is not an object")
            continue
        try:
            description = _legacy_text(entry, ordinal)
            kind_raw = entry.get("kind") or entry.get("obligation_kind") or entry.get("type")
            if not isinstance(kind_raw, str) or not kind_raw.strip():
                raise ObligationContractError(f"obligation_plan[{ordinal}] has no obligation kind")
            try:
                kind = ObligationKind(kind_raw.strip().lower())
            except ValueError as error:
                raise ObligationContractError(
                    f"obligation_plan[{ordinal}] has an unknown kind: {kind_raw!r}"
                ) from error
            setup = (
                parse_chapter_window(
                    entry["setup_window"], field=f"obligation_plan[{ordinal}].setup_window"
                )
                if entry.get("setup_window") is not None
                else None
            )
            payoff = (
                parse_chapter_window(
                    entry["payoff_window"], field=f"obligation_plan[{ordinal}].payoff_window"
                )
                if entry.get("payoff_window") is not None
                else None
            )
            progress = _progress_windows(entry.get("progress_windows"), ordinal=ordinal)
            not_before_raw = entry.get("not_before_chapter")
            not_before: int | None = None
            if not_before_raw is not None:
                not_before = parse_chapter_window(
                    not_before_raw, field=f"obligation_plan[{ordinal}].not_before_chapter"
                ).start
            if setup is None and payoff is None and not progress:
                raise ObligationContractError(
                    f"obligation_plan[{ordinal}] declares no setup, progress or payoff window"
                )
            if payoff is None:
                # A responsibility with no payoff/reveal window has no observable
                # completion boundary, so its later payoff could never be checked.
                raise ObligationContractError(
                    f"obligation_plan[{ordinal}] declares no payoff_window, so the "
                    "responsibility has no observable completion boundary"
                )
            # A declared setup/progress window may legitimately precede the reveal
            # boundary: ``not_before`` constrains payoff/reveal, not setup or progress.
        except ObligationContractError as error:
            discrepancies.append(str(error))
            continue
        declarations.append(
            ObligationDeclarationInput(
                kind=kind,
                description=description,
                not_before_chapter=not_before,
                source_form="obligation_plan",
                source_ordinal=ordinal,
                setup_window=setup,
                progress_windows=progress,
                payoff_window=payoff,
            )
        )
    return ObligationDeclarationCompilation(
        declarations=tuple(declarations), discrepancies=tuple(discrepancies)
    )


def normalize_obligation_action(action: object, *, index: int) -> ObligationActionInput:
    """Normalize one declared chapter action, rejecting legacy free text."""

    if isinstance(action, str):
        raise ObligationContractError(
            f"obligation_actions[{index}] is a free-text string; chapters may only reference "
            "an accepted obligation id with an explicit action and expected_delta"
        )
    if not isinstance(action, Mapping):
        raise ObligationContractError(
            f"obligation_actions[{index}] must be an object with "
            "obligation_id/action/expected_delta"
        )
    raw_id = action.get("obligation_id") or action.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ObligationContractError(
            f"obligation_actions[{index}] requires a string obligation_id"
        )
    raw_action = action.get("action") or action.get("operation")
    if not isinstance(raw_action, str) or not raw_action.strip():
        raise ObligationContractError(f"obligation_actions[{index}] requires an explicit action")
    normalized = _ACTION_ALIASES.get(raw_action.strip().lower())
    if normalized is None:
        raise ObligationContractError(
            f"obligation_actions[{index}] has an unsupported action: {raw_action!r}"
        )
    raw_delta = action.get("expected_delta") or action.get("delta")
    if not isinstance(raw_delta, str) or not raw_delta.strip():
        raise ObligationContractError(
            f"obligation_actions[{index}] requires a non-empty expected_delta"
        )
    window: ObligationWindow | None = None
    raw_window = action.get("window")
    if raw_window is not None:
        if not isinstance(raw_window, str):
            raise ObligationContractError(
                f"obligation_actions[{index}].window must be a stage window name"
            )
        try:
            window = ObligationWindow(raw_window.strip().lower())
        except ValueError as error:
            raise ObligationContractError(
                f"obligation_actions[{index}] has an unknown window: {raw_window!r}"
            ) from error
    return ObligationActionInput(
        obligation_id=raw_id.strip(),
        action=ObligationAction(normalized),
        expected_delta=raw_delta.strip(),
        window=window,
    )


def compile_obligation_actions(value: object) -> ObligationActionCompilation:
    """Normalize a chapter-scope action list, reporting every unreadable action."""

    if value is None:
        return ObligationActionCompilation(actions=(), discrepancies=())
    if not isinstance(value, (list, tuple)):
        return ObligationActionCompilation(
            actions=(), discrepancies=("obligation_actions must be a list",)
        )
    actions: list[ObligationActionInput] = []
    discrepancies: list[str] = []
    for index, raw in enumerate(value):
        try:
            actions.append(normalize_obligation_action(raw, index=index))
        except ObligationContractError as error:
            discrepancies.append(str(error))
    return ObligationActionCompilation(actions=tuple(actions), discrepancies=tuple(discrepancies))


def legacy_obligation_plan_source_records(payload: Mapping[str, object]) -> list[dict[str, Any]]:
    """Return lineage records for a legacy responsibility table, if present."""

    value = payload.get("obligation_plan")
    if not is_legacy_obligation_plan(value):
        return []
    compilation = compile_legacy_obligation_plan(value)
    return [declaration.as_source_record() for declaration in compilation.declarations]
