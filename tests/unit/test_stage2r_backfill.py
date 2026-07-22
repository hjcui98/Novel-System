from scripts.backfill_stage2_derived_snapshots import _safe_database_descriptor


def test_backfill_report_redacts_database_credentials() -> None:
    descriptor = _safe_database_descriptor(
        "postgresql+psycopg://user:secret@127.0.0.1:5432/novel_agent"
    )

    assert descriptor == "postgresql+psycopg://127.0.0.1:5432/novel_agent"
    assert "user" not in descriptor
    assert "secret" not in descriptor
