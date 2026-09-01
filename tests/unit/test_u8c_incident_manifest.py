from __future__ import annotations

import math

import pytest
from scripts.preregister_u8c_incident_manifest import (
    IncidentSpec,
    build_manifest,
    parse_incident_spec,
)

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


def _incident(split: str, suffix: str, *, chain: str | None = None) -> IncidentSpec:
    return parse_incident_spec(
        "|".join(
            (
                split,
                f"incident.u8c.{suffix}",
                f"project.u8c.{suffix}",
                f"run.u8c.{suffix}",
                f"postgresql+psycopg://127.0.0.1:5432/na_u8c_{suffix}",
                SHA_B,
                chain or f"crash-chain.u8c.{suffix}",
            )
        )
    )


def _stream_root(tmp_path):
    root = tmp_path / "stream"
    root.mkdir()
    (root / "000_prologue_and_frontmatter.txt").touch()
    for index in range(1, 301):
        (root / f"{index:03d}.txt").touch()
    return root


def _build(tmp_path, incidents):
    return build_manifest(
        campaign_id="u8c-campaign-20260830",
        preregistered_at="2026-08-30T12:00:00+08:00",
        source_project_id="project.u8b.natural.20260830.zrun31",
        source_commit=SHA_A,
        source_text_root=SHA_A,
        benchmark_stream_root=_stream_root(tmp_path),
        code_source_fingerprint=SHA_A,
        configuration_fingerprint=SHA_A,
        model_profile="qwen38_27b_fp8_8005",
        cutoff_chapter=95,
        target_chapter=96,
        access_scope="writer_safe",
        incidents=tuple(incidents),
    )


def _problem_identity() -> dict[str, object]:
    return {
        "need_id": "need.u8c.preregistered.betrothed",
        "question_id": "question.u8l2.r2",
        "need_query": "徐有容与陈长生的婚约关系是什么?",
        "semantic_question": "预注册: 徐有容与陈长生的婚约关系是什么?",
        "facet": "relation_state",
        "source_commit": SHA_A,
        "source_text_root": SHA_A,
        "cutoff_chapter": 95,
    }


def test_manifest_freezes_both_splits_and_full_stream(tmp_path):
    payload = _build(tmp_path, (_incident("development", "dev"), _incident("held_out", "hold")))

    assert payload["status"] == "PREREGISTERED"
    assert payload["source"]["benchmark_stream_file_count"] == 301
    assert [item["split"] for item in payload["identity_split"]] == ["development", "held_out"]
    assert payload["fixed_runtime"]["budget"]["runtime_parallelism"] == 1


def test_manifest_freezes_campaign_local_budget_tranche(tmp_path):
    payload = build_manifest(
        campaign_id="u8c-campaign-budget",
        preregistered_at="2026-08-31T12:00:00+08:00",
        source_project_id="project.u8b.natural.20260830.zrun31",
        source_commit=SHA_A,
        source_text_root=SHA_A,
        benchmark_stream_root=_stream_root(tmp_path),
        code_source_fingerprint=SHA_A,
        configuration_fingerprint=SHA_A,
        model_profile="qwen38_27b_fp8_8005",
        cutoff_chapter=95,
        target_chapter=96,
        access_scope="writer_safe",
        incidents=(_incident("development", "dev"), _incident("held_out", "hold")),
        chapter_settlement_timeout_seconds=360.0,
        settlement_output_tokens=4096,
        settlement_token_budget=192_000,
        settlement_max_total_model_calls=16,
    )

    assert payload["fixed_runtime"]["budget"] == {
        "chapter_settlement_timeout_seconds": 360.0,
        "settlement_output_tokens": 4096,
        "settlement_token_budget": 192_000,
        "settlement_max_total_model_calls": 16,
        "max_tasks": 1,
        "runtime_parallelism": 1,
        "memory_write_validation_only": False,
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("chapter_settlement_timeout_seconds", 0.0, "timeout_seconds"),
        ("chapter_settlement_timeout_seconds", 901.0, "timeout_seconds"),
        ("chapter_settlement_timeout_seconds", math.nan, "timeout_seconds"),
        ("chapter_settlement_timeout_seconds", math.inf, "timeout_seconds"),
        ("settlement_output_tokens", 0, "output_tokens"),
        ("settlement_output_tokens", 131_073, "output_tokens"),
        ("settlement_token_budget", 0, "token_budget"),
        ("settlement_max_total_model_calls", 0, "model_calls"),
    ),
)
def test_manifest_rejects_unbounded_or_empty_budget_tranche(tmp_path, field, value, message):
    kwargs = {field: value}
    with pytest.raises(ValueError, match=message):
        build_manifest(
            campaign_id="u8c-campaign-invalid-budget",
            preregistered_at="2026-08-31T12:00:00+08:00",
            source_project_id="project.u8b.natural.20260830.zrun31",
            source_commit=SHA_A,
            source_text_root=SHA_A,
            benchmark_stream_root=_stream_root(tmp_path),
            code_source_fingerprint=SHA_A,
            configuration_fingerprint=SHA_A,
            model_profile="qwen38_27b_fp8_8005",
            cutoff_chapter=95,
            target_chapter=96,
            access_scope="writer_safe",
            incidents=(_incident("development", "dev"), _incident("held_out", "hold")),
            **kwargs,
        )


def test_manifest_requires_both_splits(tmp_path):
    with pytest.raises(ValueError, match="development and one held_out"):
        _build(tmp_path, (_incident("development", "dev"),))


def test_manifest_does_not_split_a_crash_retry_chain(tmp_path):
    with pytest.raises(ValueError, match="crash_chain_id values must be unique"):
        _build(
            tmp_path,
            (
                _incident("development", "dev", chain="crash-chain.shared"),
                _incident("held_out", "hold", chain="crash-chain.shared"),
            ),
        )


def test_parse_incident_rejects_invalid_basis():
    with pytest.raises(ValueError, match="basis_commit"):
        parse_incident_spec("development|i|p|r|db|not-a-hash|chain")


def test_manifest_freezes_optional_problem_identity(tmp_path):
    payload = build_manifest(
        campaign_id="u8c-campaign-20260830",
        preregistered_at="2026-08-30T12:00:00+08:00",
        source_project_id="project.u8b.natural.20260830.zrun31",
        source_commit=SHA_A,
        source_text_root=SHA_A,
        benchmark_stream_root=_stream_root(tmp_path),
        code_source_fingerprint=SHA_A,
        configuration_fingerprint=SHA_A,
        model_profile="qwen38_27b_fp8_8005",
        cutoff_chapter=95,
        target_chapter=96,
        access_scope="writer_safe",
        incidents=(_incident("development", "dev"), _incident("held_out", "hold")),
        problem_identity=_problem_identity(),
    )

    assert payload["problem_identity"] == _problem_identity()


def test_manifest_rejects_problem_identity_from_another_source(tmp_path):
    problem = _problem_identity()
    problem["source_text_root"] = SHA_B
    with pytest.raises(ValueError, match="source_text_root"):
        build_manifest(
            campaign_id="u8c-campaign-20260830",
            preregistered_at="2026-08-30T12:00:00+08:00",
            source_project_id="project.u8b.natural.20260830.zrun31",
            source_commit=SHA_A,
            source_text_root=SHA_A,
            benchmark_stream_root=_stream_root(tmp_path),
            code_source_fingerprint=SHA_A,
            configuration_fingerprint=SHA_A,
            model_profile="qwen38_27b_fp8_8005",
            cutoff_chapter=95,
            target_chapter=96,
            access_scope="writer_safe",
            incidents=(_incident("development", "dev"), _incident("held_out", "hold")),
            problem_identity=problem,
        )
