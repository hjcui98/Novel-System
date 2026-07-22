SHELL := /bin/bash

ENV_PREFIX := .conda-env
PYTHON := $(ENV_PREFIX)/bin/python
UV := $(ENV_PREFIX)/bin/uv
UV_CACHE_DIR := $(CURDIR)/tmp/uv-cache
INFRA_BACKEND ?= native

-include .env
export

.PHONY: bootstrap quality infra-up infra-health migrate demo integration stage0 stage1-smoke infra-down \
	models-bootstrap models-up models-health model-smoke models-down \
	model-benchmark-smoke stage1-native-benchmark stage2-benchmark-read-pilot \
	stage2-teacher-forced-e2e stage2r-backfill stage2r-gate stage2r-diagnose

bootstrap:
	@if test -x "$(UV)"; then \
		:; \
	elif test -d "$(ENV_PREFIX)/conda-meta"; then \
		conda env update --prefix "$(CURDIR)/$(ENV_PREFIX)" --file environment.yml --prune; \
	else \
		conda env create --prefix "$(CURDIR)/$(ENV_PREFIX)" --file environment.yml; \
	fi
	@mkdir -p "$(UV_CACHE_DIR)"
	@UV_CACHE_DIR="$(UV_CACHE_DIR)" UV_PROJECT_ENVIRONMENT="$(ENV_PREFIX)" \
		"$(UV)" sync --frozen --inexact --extra dev
	@test -f .env || cp .env.example .env
	@if test "$(INFRA_BACKEND)" = "native"; then \
		"$(PYTHON)" scripts/native_infra.py bootstrap; \
	elif test "$(INFRA_BACKEND)" != "docker"; then \
		echo "unsupported INFRA_BACKEND=$(INFRA_BACKEND)" >&2; exit 2; \
	fi

quality:
	@"$(ENV_PREFIX)/bin/ruff" check .
	@"$(ENV_PREFIX)/bin/ruff" format --check .
	@"$(ENV_PREFIX)/bin/mypy"
	@NOVEL_AGENT_FORBID_MODEL_CALLS=true "$(ENV_PREFIX)/bin/pytest" \
		-m "not model_required and not integration"

infra-up:
	@if test "$(INFRA_BACKEND)" = "native"; then \
		"$(PYTHON)" scripts/native_infra.py up; \
	elif test "$(INFRA_BACKEND)" = "docker"; then \
		docker compose up -d --wait; \
	else echo "unsupported INFRA_BACKEND=$(INFRA_BACKEND)" >&2; exit 2; fi

infra-health:
	@if test "$(INFRA_BACKEND)" = "native"; then \
		"$(PYTHON)" scripts/native_infra.py health; \
	elif test "$(INFRA_BACKEND)" = "docker"; then \
		"$(PYTHON)" scripts/check_infra.py; \
	else echo "unsupported INFRA_BACKEND=$(INFRA_BACKEND)" >&2; exit 2; fi

migrate:
	@NOVEL_AGENT_DATABASE_URL="postgresql+psycopg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@127.0.0.1:$(POSTGRES_PORT)/$(POSTGRES_DB)" \
		"$(PYTHON)" -m alembic upgrade head

demo:
	@LANGGRAPH_STRICT_MSGPACK=true "$(PYTHON)" scripts/run_stage0_demo.py

integration: bootstrap
	@INFRA_BACKEND="$(INFRA_BACKEND)" NOVEL_AGENT_FORBID_MODEL_CALLS=true \
		"$(ENV_PREFIX)/bin/pytest" -m integration --no-cov

stage0: bootstrap
	@$(MAKE) infra-up
	@$(MAKE) infra-health
	@$(MAKE) migrate
	@$(MAKE) demo

stage1-smoke:
	@NOVEL_AGENT_FORBID_MODEL_CALLS=true "$(ENV_PREFIX)/bin/pytest" \
		tests/contract/test_stage1_benchmark_contract.py \
		tests/contract/test_retrieval_model_service.py \
		tests/contract/test_retrieval_models_contract.py \
		tests/contract/test_stage1_native_benchmark_cli.py \
		tests/unit/test_benchmark_workspace.py \
		tests/unit/test_event_log.py \
		tests/unit/test_stage1_retrieval.py \
		tests/unit/test_stage1_memory_pipeline.py \
		tests/unit/test_stage1_benchmark_runner.py \
		tests/unit/test_stage1_evaluation.py \
		tests/unit/test_stage1_write_side.py \
		tests/unit/test_projection.py \
		tests/unit/test_r1_repository.py \
		tests/unit/test_search_retrieval.py \
		tests/unit/test_http_inference.py \
		tests/unit/test_model_memory.py \
		tests/unit/test_model_curation.py \
		tests/unit/test_model_validation.py \
		tests/unit/test_stage1_gate.py \
		tests/unit/test_stage1_replay.py --no-cov

models-bootstrap: bootstrap
	@UV_CACHE_DIR="$(UV_CACHE_DIR)" UV_PROJECT_ENVIRONMENT="$(ENV_PREFIX)" \
		"$(UV)" sync --frozen --inexact --extra dev --extra retrieval-models
	@"$(PYTHON)" scripts/native_models.py bootstrap

models-up:
	@"$(PYTHON)" scripts/native_models.py up

models-health:
	@"$(PYTHON)" scripts/native_models.py health

model-smoke:
	@NOVEL_AGENT_FORBID_MODEL_CALLS=false "$(ENV_PREFIX)/bin/pytest" \
		tests/model/test_retrieval_models_live.py --no-cov

model-benchmark-smoke:
	@$(MAKE) infra-health
	@$(MAKE) models-health
	@NOVEL_AGENT_FORBID_MODEL_CALLS=false "$(ENV_PREFIX)/bin/pytest" \
		tests/model/test_native_bge_benchmark_live.py --no-cov

models-down:
	@"$(PYTHON)" scripts/native_models.py down

stage1-native-benchmark:
	@test -n "$(BUNDLE)" || { echo "BUNDLE is required" >&2; exit 2; }
	@test -n "$(CASE_ID)" || { echo "CASE_ID is required" >&2; exit 2; }
	@test -n "$(OUTPUT)" || { echo "OUTPUT is required" >&2; exit 2; }
	@$(MAKE) infra-health
	@$(MAKE) models-health
	@"$(PYTHON)" scripts/run_stage1_benchmark.py "$(BUNDLE)" \
		--case-id "$(CASE_ID)" --track oracle --retrieval-backend native-bge \
		--output "$(OUTPUT)"

stage2-benchmark-read-pilot:
	@test -n "$(SOURCE)" || { echo "SOURCE is required" >&2; exit 2; }
	@test -n "$(OUTPUT)" || { echo "OUTPUT is required" >&2; exit 2; }
	@"$(PYTHON)" scripts/run_stage2_benchmark_flow.py \
		--source "$(SOURCE)" --output-directory "$(OUTPUT)"

stage2-teacher-forced-e2e:
	@test -n "$(SOURCE)" || { echo "SOURCE is required" >&2; exit 2; }
	@test -n "$(OUTPUT)" || { echo "OUTPUT is required" >&2; exit 2; }
	@test -n "$(PROJECT_DIRECTORY)" || { echo "PROJECT_DIRECTORY is required" >&2; exit 2; }
	@test -n "$(STAGE2R_DATABASE_URL)" || { echo "STAGE2R_DATABASE_URL is required" >&2; exit 2; }
	@"$(PYTHON)" scripts/run_stage2_teacher_forced_e2e.py \
		--source "$(SOURCE)" --output-directory "$(OUTPUT)" \
		--resume-project "$(PROJECT_DIRECTORY)" \
		--database-url "$(STAGE2R_DATABASE_URL)" \
		--information-profile "$${PROFILE:-author_plan_conditioned}" \
		--semantic-backend "$${SEMANTIC_BACKEND:-local_openai}" \
		--retrieval-backend "$${RETRIEVAL_BACKEND:-real_hybrid}" \
		--model-base-url "$${MODEL_BASE_URL:-http://127.0.0.1:8002/v1}" \
		--model "$${MODEL:-qwen36-27b-nvfp4}"

stage2r-backfill:
	@test -n "$(PROJECT_DIRECTORY)" || { echo "PROJECT_DIRECTORY is required" >&2; exit 2; }
	@$(MAKE) infra-health
	@$(MAKE) models-health
	@"$(PYTHON)" scripts/backfill_stage2_derived_snapshots.py \
		--project-directory "$(PROJECT_DIRECTORY)" --retrieval-backend real_hybrid \
		--build-profile "$${BUILD_PROFILE:-stage2r-hybrid-v0.1}" $${RESUME:+--resume}

stage2r-gate:
	@test -n "$(PROJECT_DIRECTORY)" || { echo "PROJECT_DIRECTORY is required" >&2; exit 2; }
	@"$(PYTHON)" scripts/run_stage2_retrieval_gate.py \
		--project-directory "$(PROJECT_DIRECTORY)" \
		--checkpoints "$${CHECKPOINTS:-20,40,60,80,95}"

stage2r-diagnose:
	@test -n "$(SOURCE)" || { echo "SOURCE is required" >&2; exit 2; }
	@test -n "$(PROJECT_DIRECTORY)" || { echo "PROJECT_DIRECTORY is required" >&2; exit 2; }
	@test -n "$(CASE_ID)" || { echo "CASE_ID is required" >&2; exit 2; }
	@test -n "$(CHECKPOINT)" || { echo "CHECKPOINT is required" >&2; exit 2; }
	@"$(PYTHON)" scripts/diagnose_stage2_retrieval_case.py \
		--source "$(SOURCE)" --project-directory "$(PROJECT_DIRECTORY)" \
		--case-id "$(CASE_ID)" --checkpoint "$(CHECKPOINT)" \
		--query-condition "$${QUERY_CONDITION:-oracle}"

infra-down:
	@if test "$(INFRA_BACKEND)" = "native"; then \
		"$(PYTHON)" scripts/native_infra.py down; \
	elif test "$(INFRA_BACKEND)" = "docker"; then \
		docker compose down; \
	else echo "unsupported INFRA_BACKEND=$(INFRA_BACKEND)" >&2; exit 2; fi
