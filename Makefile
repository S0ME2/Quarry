.PHONY: install format lint typecheck test test-unit test-browser migration-check check build schema-check integrity matcher-benchmark real-matcher-benchmark live-matcher live-real-matcher frontend-install frontend-check frontend-test frontend-build api-contract-check api-contract-update test-api test-product-e2e

install:
	uv sync --locked

format:
	uv run --locked ruff format .

lint:
	uv run --locked ruff format --check .
	uv run --locked ruff check .

typecheck:
	uv run --locked mypy

test: frontend-build
	uv run --locked pytest

test-unit:
	uv run --locked pytest -m 'not database and not browser'

test-browser: frontend-build
	uv run --locked pytest -m browser

migration-check:
	uv run --locked pytest tests/integration/test_migrations.py

check: lint typecheck frontend-check frontend-test api-contract-check test

frontend-install:
	cd frontend && npm ci --no-audit --no-fund

frontend-check:
	cd frontend && npm run check

frontend-test:
	cd frontend && npm test

frontend-build:
	cd frontend && npm run build

.PHONY: frontend-dev
frontend-dev:
	cd frontend && npm run dev

api-contract-update:
	uv run --locked python -m tender_system.api.api_schema --output .codex/phase13/openapi.json
	cd frontend && npx --no-install openapi-typescript ../.codex/phase13/openapi.json -o src/schema.d.ts
	cd frontend && npx --no-install prettier --write src/schema.d.ts

api-contract-check:
	uv run --locked python -m tender_system.api.api_schema --output .codex/phase13/openapi.json
	cd frontend && npx --no-install openapi-typescript ../.codex/phase13/openapi.json -o ../.codex/phase13/schema.d.ts
	cd frontend && npx --no-install prettier --write ../.codex/phase13/schema.d.ts
	cmp frontend/src/schema.d.ts .codex/phase13/schema.d.ts

test-api:
	uv run --locked pytest tests/integration/test_product_composition.py tests/integration/test_web_api.py

test-product-e2e: frontend-build
	uv run --locked pytest tests/e2e/test_web_workspace.py

# Run the complete gate sequentially, including packaging and real database drift.
.NOTPARALLEL: integrity

build:
	uv build --no-sources --out-dir .codex/integrity/dist

schema-check:
	uv run --locked python -m tender_system.testing.schema_check

integrity: check build migration-check test-browser schema-check

matcher-benchmark:
	uv run --locked python -m tender_system.matching.matcher_evaluation --output .codex/phase9-5/offline-benchmark.json

real-matcher-benchmark:
	uv run --locked python -m tender_system.matching.matcher_evaluation --benchmark benchmarks/matcher/matcher_real_v2.json --replay benchmarks/matcher/matcher_real_replay_v2.json --output .codex/phase9-5/real-offline-benchmark.json

live-matcher:
	uv run --locked python -m tender_system.matching.matcher_evaluation --live --workers 1 --output .codex/phase9-5/groq-live-benchmark.json

live-real-matcher:
	uv run --locked python -m tender_system.matching.matcher_evaluation --benchmark benchmarks/matcher/matcher_real_v2.json --live --workers 1 --output .codex/phase9-5/real-live-benchmark.json

.PHONY: serve serve-monitoring
serve: frontend-build
	uv run --locked python -m tender_system.api.server

# Explicit deployment mode. Run under a process supervisor for restart recovery.
serve-monitoring: frontend-build
	QUARRY_MANUAL_ONLY=false QUARRY_OPERATOR_WORKER_ENABLED=true uv run --locked python -m tender_system.api.server

.PHONY: manual
manual:
	uv run --locked quarry-manual

.PHONY: test-manual-acceptance
# Manual application services, owned network fixtures, isolated PostgreSQL only.
test-manual-acceptance:
	uv run --locked pytest tests/integration/test_bounded_workflow.py tests/unit/test_transport_contracts.py tests/unit/test_module_boundaries.py

.PHONY: test-application-acceptance
# Real paginated loopback HTTP/JS, Chromium, CLI services and isolated PostgreSQL.
test-application-acceptance:
	uv run --locked pytest tests/e2e/test_manual_acceptance.py
