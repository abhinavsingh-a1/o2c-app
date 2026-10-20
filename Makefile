.PHONY: up down ps logs buckets lint clean sync test ui cluster-up cluster-down poc erp-migrate erp-api erp-seed-history dlq-list dlq-replay lag topics-reset intake classifier extractor send contracts-ingest agent gen-api

up:
	docker compose up -d --build

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs -f --tail 50

buckets:
	uv run --with boto3 --with python-dotenv python local/create_buckets.py

lint:
	pre-commit run --all-files

clean:
	docker compose down -v

test:
	uv run pytest

ui:
	pnpm --filter ui dev

cluster-up:
	kind create cluster --config local/kind-config.yaml

cluster-down:
	kind delete cluster --name o2c

sync:
	uv sync --all-packages --all-groups

poc:
	uv run --group poc jupyter lab

erp-migrate:
	uv run alembic -c services/erp-mock/alembic.ini upgrade head

erp-seed:
	uv run python -m erp_mock.seed

erp-api:
	uv run uvicorn erp_mock.api:app --port 8100 --reload

erp-seed-history:
	uv run python -m erp_mock.seed_history

topics:
	uv run python -m o2c_common.topics

dlq-list:
	uv run python -m o2c_common.dlq --dry-run

dlq-replay:
	uv run python -m o2c_common.dlq

lag:
	docker compose exec kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --all-groups

topics-reset:
	uv run python -m o2c_common.topics --reset

intake:
	uv run flask --app "intake.app:create_app" run --port 8200

classifier:
	uv run python -m classifier.app

extractor:
	uv run uvicorn extractor.app:create_app --factory --port 8300

send:
	uv run --with python-dotenv python local/send_po.py $(PDF)

contracts-ingest:
	uv run python -m agent.contracts

agent:
	uv run uvicorn agent.app:create_app --factory --port 8400

gen-api:
	pnpm --dir ui gen:api
