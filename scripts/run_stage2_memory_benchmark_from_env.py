#!/usr/bin/env python3
"""Launch the Stage 2M matrix while keeping PostgreSQL credentials in memory."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from urllib.parse import quote_plus

import psycopg
from dotenv import dotenv_values
from psycopg import sql


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--project-directory", type=Path, required=True)
    value.add_argument("--output-directory", type=Path, required=True)
    value.add_argument("--database", required=True)
    value.add_argument("--experiment-id", required=True)
    value.add_argument(
        "--information-profile",
        choices=("visible_at_cutoff", "author_plan_conditioned"),
        required=True,
    )
    value.add_argument("--arms", choices=("A", "ABC"), default="A")
    value.add_argument("--checkpoints", default="20,40,60,80,95")
    value.add_argument("--model-max-output-tokens", default="4096")
    value.add_argument(
        "--initialize",
        action="store_true",
        help="create a new Canonical project instead of evaluating an existing one",
    )
    value.add_argument(
        "--resume",
        action="store_true",
        help="resume an initialized Canonical project from its durable checkpoint",
    )
    value.add_argument("--max-chapter", default="95")
    value.add_argument(
        "--create-database",
        action="store_true",
        help="create the isolated PostgreSQL database when it does not exist",
    )
    return value


def main() -> int:
    args = parser().parse_args()
    root = Path(__file__).resolve().parents[1]
    config = dotenv_values(root / ".env")
    database_url = "postgresql+psycopg://{}:{}@127.0.0.1:{}/{}".format(
        quote_plus(str(config["POSTGRES_USER"])),
        quote_plus(str(config["POSTGRES_PASSWORD"])),
        config["POSTGRES_PORT"],
        args.database,
    )
    environment = os.environ.copy()
    environment.update(
        {
            "ROOT": str(root),
            "SOURCE": str(root / "benchmarks/private/ztj_memory_pilot_v0.1"),
            "OUTPUT": str(args.output_directory.resolve()),
            "PROJECT_DIRECTORY": str(args.project_directory.resolve()),
            "STAGE2R_DATABASE_URL": database_url,
            "STAGE2R_EXPERIMENT_ID": args.experiment_id,
            "INFORMATION_PROFILE": args.information_profile,
            "ARMS": args.arms,
            "CHECKPOINTS": args.checkpoints,
            "MODEL_BASE_URL": "http://127.0.0.1:8002/v1",
            "MODEL": "qwen36-27b-nvfp4",
            "MODEL_MAX_OUTPUT_TOKENS": args.model_max_output_tokens,
            "PYTHON": str(root / ".conda-env/bin/python"),
        }
    )
    command = ["bash", str(root / "scripts/run_stage2_real_staged.sh")]
    if args.initialize:
        if args.project_directory.resolve() != args.output_directory.resolve():
            raise ValueError(
                "initial project run requires output-directory to equal project-directory"
            )
        command = [
            str(root / ".conda-env/bin/python"),
            str(root / "scripts/run_stage2_teacher_forced_e2e.py"),
            "--source",
            str(root / "benchmarks/private/ztj_memory_pilot_v0.1"),
            "--output-directory",
            str(args.project_directory.resolve()),
            "--information-profile",
            args.information_profile,
            "--arms",
            args.arms,
            "--semantic-backend",
            "local_openai",
            "--retrieval-backend",
            "real_hybrid",
            "--database-url",
            database_url,
            "--experiment-id",
            args.experiment_id,
            "--model-base-url",
            "http://127.0.0.1:8002/v1",
            "--model",
            "qwen36-27b-nvfp4",
            "--model-max-output-tokens",
            args.model_max_output_tokens,
            "--model-max-retries",
            "1",
            "--max-chapter",
            args.max_chapter,
        ]
        if args.resume:
            command.append("--resume")
        if args.create_database:
            maintenance_url = "postgresql://{}:{}@127.0.0.1:{}/postgres".format(
                quote_plus(str(config["POSTGRES_USER"])),
                quote_plus(str(config["POSTGRES_PASSWORD"])),
                config["POSTGRES_PORT"],
            )
            with psycopg.connect(maintenance_url, autocommit=True) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s",
                    (args.database,),
                ).fetchone()
                if exists is None:
                    connection.execute(
                        sql.SQL("CREATE DATABASE {}").format(sql.Identifier(args.database))
                    )
        migration_environment = environment.copy()
        migration_environment["NOVEL_AGENT_DATABASE_URL"] = database_url
        migrated = subprocess.run(
            [str(root / ".conda-env/bin/python"), "-m", "alembic", "upgrade", "head"],
            cwd=root,
            env=migration_environment,
            check=False,
        )
        if migrated.returncode != 0:
            return migrated.returncode
    completed = subprocess.run(
        command,
        cwd=root,
        env=environment,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
