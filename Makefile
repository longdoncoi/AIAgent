.PHONY: install infra-up infra-down serve-vllm serve-llama-chat serve-llama-embed serve-mcp serve-agents dev lint audit test test-prepare eval eval-smoke eval-release eval-deep eval-ragas prr-evidence ingest docker-build load-test db-migrate db-upgrade clean memory-status pre-flight

PYTHON := uv run --no-sync python
PYTEST := uv run --no-sync pytest

install:
	uv pip install -e ".[dev]"

infra-up:
	docker compose -f deploy/docker/docker-compose.dev.yml up -d postgres qdrant redis neo4j litellm

infra-down:
	docker compose -f deploy/docker/docker-compose.dev.yml down

serve-vllm:
	bash deploy/serving/vllm.sh

serve-llama-chat:
	bash deploy/serving/llamacpp-chat.sh

serve-llama-embed:
	bash deploy/serving/llamacpp-embed.sh

serve-mcp:
	$(PYTHON) -m src.agentic.tools.servers.code_exec_server --port 9001 & \
	$(PYTHON) -m src.agentic.tools.servers.repo_server --port 9002 & \
	$(PYTHON) -m src.agentic.tools.servers.web_search_server --port 9003

serve-agents:
	$(PYTHON) -m src.agentic.agents.chat.server --port 9100 & \
	$(PYTHON) -m src.agentic.agents.rag.server --port 9101 & \
	$(PYTHON) -m src.agentic.agents.coding.server --port 9102

dev:
	uvicorn src.agentic.api.main:app --host 0.0.0.0 --port 8000 --reload

lint:
	uv run --no-sync ruff check .
	uv run --no-sync mypy src/ evals/ scripts/

audit:
	pip-audit

test-prepare:
	$(PYTHON) scripts/ingest.py --data-dir data/raw --index-dir data/index

test: test-prepare
	$(PYTEST) tests/ -n auto -v --cov=src --cov-branch --cov-report=term-missing --cov-report=json:coverage.json --cov-fail-under=90
	$(PYTHON) scripts/check_coverage_thresholds.py coverage.json

# CI smoke gate only. Never claim production readiness from this target.
eval: eval-smoke

eval-smoke:
	$(PYTHON) evals/run_eval.py --mode smoke

# STRICT release gate. Fails on proxy answers / missing LLM judge / downgraded thresholds.
# This is the only target allowed to produce a "Ready for Production" verdict.
eval-release:
	$(PYTHON) evals/release_gate.py

eval-deep:
	$(PYTHON) evals/deepeval_runner.py --mode smoke

eval-ragas:
	$(PYTHON) evals/ragas_runner.py --mode smoke

prr-evidence:
	$(PYTHON) scripts/generate_prr_evidence.py

ingest:
	$(PYTHON) scripts/ingest.py --data-dir data/raw --index-dir data/index

db-migrate:
	$(PYTHON) scripts/migrate_db.py

db-upgrade:
	uv run --no-sync alembic upgrade head

docker-build:
	docker build -f deploy/docker/Dockerfile.api -t agentic-platform/api:latest .
	docker build -f deploy/docker/Dockerfile.agent -t agentic-platform/agent:latest .
	docker build -f deploy/docker/Dockerfile.mcp -t agentic-platform/mcp:latest .

load-test:
	uv run --no-sync locust -f tests/load/locustfile.py --headless -u 100 -r 10 -t 5m --host http://localhost:8000

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +

memory-status:
	@cat .memory/activeContext.md

pre-flight: lint test audit eval
	@echo "All pre-flight quality gates passed successfully!"
	@echo "NOTE: pre-flight includes SMOKE eval only. Run 'make eval-release' for the strict release gate."

