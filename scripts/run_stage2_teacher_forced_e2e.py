#!/usr/bin/env python3
"""Run Stage 2 Genesis plus teacher-forced chapter replay on the human benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from novel_agent.adapters.model import OpenAICompatibleChatEndpoint
from novel_agent.domain.stage2 import BenchmarkInformationProfile
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.teacher_forced_benchmark_e2e import (
    TeacherForcedBenchmarkE2ERunner,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--source", type=Path, required=True)
    value.add_argument("--output-directory", type=Path, required=True)
    value.add_argument(
        "--information-profile",
        choices=tuple(item.value for item in BenchmarkInformationProfile),
        default=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED.value,
    )
    value.add_argument("--token-budget", type=int, default=4000)
    value.add_argument("--max-candidates", type=int, default=20)
    value.add_argument(
        "--semantic-backend",
        choices=("local_openai", "scripted"),
        default="local_openai",
    )
    value.add_argument("--model-base-url", default="http://127.0.0.1:8002/v1")
    value.add_argument("--model", default="qwen36-27b-nvfp4")
    value.add_argument("--model-max-output-tokens", type=int, default=8192)
    value.add_argument("--model-max-retries", type=int, default=0)
    value.add_argument("--stop-after-genesis", action="store_true")
    value.add_argument("--max-chapter", type=int, default=None)
    value.add_argument("--resume", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    bundle = HumanBenchmarkCompiler().compile(args.source)
    endpoint = (
        OpenAICompatibleChatEndpoint(
            base_url=args.model_base_url,
            model=args.model,
            max_output_tokens=args.model_max_output_tokens,
            max_retries=args.model_max_retries,
        )
        if args.semantic_backend == "local_openai"
        else None
    )
    try:
        summary = TeacherForcedBenchmarkE2ERunner(
            token_budget=args.token_budget,
            max_candidates=args.max_candidates,
            semantic_endpoint=endpoint,
        ).run(
            args.source,
            args.output_directory,
            bundle,
            information_profile=BenchmarkInformationProfile(args.information_profile),
            stop_after_genesis=args.stop_after_genesis,
            max_chapter=args.max_chapter,
            resume=args.resume,
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        if endpoint is not None:
            import asyncio

            asyncio.run(endpoint.aclose())


if __name__ == "__main__":
    raise SystemExit(main())
