.PHONY: install infra-up infra-down serve-vllm serve-llama-chat serve-llama-embed serve-mcp serve-agents dev lint audit test eval eval-smoke eval-release eval-deep eval-ragas prr-evidence ingest docker-build load-test db-migrate db-upgrade clean memory-status pre-flight

install:
	uv pip install -e ".[dev]"

infra-up:
	docker compose -f deploy/docker/docker-compose.yml up -d postgres qdrant redis neo4j litellm

infra-down:
	docker compose -f deploy/docker/docker-compose.yml down

serve-vllm:
	bash deploy/serving/vllm.sh

serve-llama-chat:
	bash deploy/serving/llamacpp-chat.sh

serve-llama-embed:
	bash deploy/serving/llamacpp-embed.sh

serve-mcp:
	python -m src.agentic.tools.servers.code_exec_server --port 9001 & \
	python -m src.agentic.tools.servers.repo_server --port 9002 & \
	python -m src.agentic.tools.servers.web_search_server --port 9003

serve-agents:
	python -m src.agentic.agents.chat.server --port 9100 & \
	python -m src.agentic.agents.rag.server --port 9101 & \
	python -m src.agentic.agents.coding.server --port 9102

dev:
	uvicorn src.agentic.api.main:app --host 0.0.0.0 --port 8000 --reload

lint:
	ruff check .
	mypy src/

audit:
	pip-audit

test:
	pytest tests/ -n auto -v --cov=src --cov-fail-under=80

# CI smoke gate only. Never claim production readiness from this target.
eval: eval-smoke

eval-smoke:
	python evals/run_eval.py --mode smoke

# STRICT release gate. Fails on proxy answers / missing LLM judge / downgraded thresholds.
# This is the only target allowed to produce a "Ready for Production" verdict.
eval-release:
	python evals/release_gate.py

eval-deep:
	python evals/deepeval_runner.py --mode smoke

eval-ragas:
	python evals/ragas_runner.py --mode smoke

prr-evidence:
	python scripts/generate_prr_evidence.py

ingest:
	python scripts/ingest.py --data-dir data/raw --index-dir data/index

db-migrate:
	python scripts/migrate_db.py

db-upgrade:
	alembic upgrade head

docker-build:
	docker build -f deploy/docker/Dockerfile.api -t agentic-platform/api:latest .
	docker build -f deploy/docker/Dockerfile.agent -t agentic-platform/agent:latest .
	docker build -f deploy/docker/Dockerfile.mcp -t agentic-platform/mcp:latest .

load-test:
	locust -f tests/load/locustfile.py --host http://localhost:8000

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +

memory-status:
	@cat .memory/activeContext.md

pre-flight: lint test audit eval
	@echo "All pre-flight quality gates passed successfully!"
	@echo "NOTE: pre-flight includes SMOKE eval only. Run 'make eval-release' for the strict release gate."

