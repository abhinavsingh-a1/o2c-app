.PHONY: up down ps logs buckets lint clean sync test ui cluster-up cluster-down poc erp-migrate erp-api erp-seed-history dlq-list dlq-replay lag topics-reset intake classifier extractor send contracts-ingest agent gen-api  ui-test ml-data ml-image ml-pipeline-local ml-pipeline-compile eval-quick eval grafana prom-alerts prom-test kfp-install kfp-load-image kfp-ui kfp-run kfp-uninstall

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

ui-test:
	pnpm --dir ui test

# E10 datasets: POs for extraction (train, hard holdout) and documents for the classifier.
ml-data:
	uv run --group poc python poc/generate_pos.py --seed 2026 --count 100 --out ml/data/train
	uv run --group poc python poc/generate_pos.py --seed 4242 --count 20 --hard --out ml/data/holdout
	uv run --group poc python poc/generate_pos.py --seed 3101 --count 60 --out ml/data/classifier/train
	uv run --group poc python poc/generate_other_docs.py --seed 3102 --count 60 --out ml/data/classifier/train
	uv run --group poc python poc/generate_pos.py --seed 3201 --count 60 --hard --out ml/data/classifier/train_hard
	uv run --group poc python poc/generate_other_docs.py --seed 3202 --count 60 --hard --out ml/data/classifier/train_hard
	uv run --group poc python poc/generate_pos.py --seed 5101 --count 10 --hard --out ml/data/classifier/holdout
	uv run --group poc python poc/generate_other_docs.py --seed 5102 --count 10 --hard --out ml/data/classifier/holdout

# E10: Kubeflow pipeline for the quantity anomaly model (ml/pipelines/README.md)
ml-image:
	docker build -f docker/ml.Dockerfile -t o2c-ml:dev .

ml-pipeline-compile:
	uv run --group ml python ml/pipelines/anomaly_pipeline.py

ml-pipeline-local:
	uv run --group ml python ml/pipelines/run_local.py

# Kubeflow Pipelines (standalone, pinned) on the kind cluster "o2c" (ml/pipelines/README.md)
KFP_VERSION ?= 2.17.2
KFP_MANIFESTS = github.com/kubeflow/pipelines/manifests/kustomize

kfp-install:
	kubectl apply -k "$(KFP_MANIFESTS)/cluster-scoped-resources?ref=$(KFP_VERSION)"
	kubectl wait --for condition=established --timeout=120s crd/applications.app.k8s.io
	kubectl apply -k "$(KFP_MANIFESTS)/env/platform-agnostic?ref=$(KFP_VERSION)"
	# kind's kindnet enforces NetworkPolicies and drops allowed traffic to the artifact store
	# with this policy (S3 timeouts in pipeline steps); local single-node cluster only.
	kubectl delete networkpolicy seaweedfs -n kubeflow --ignore-not-found
	kubectl wait --for=condition=Ready pods --all -n kubeflow --timeout=1800s
	kind load docker-image o2c-ml:dev --name o2c

kfp-load-image:
	kind load docker-image o2c-ml:dev --name o2c

kfp-ui:
	kubectl port-forward -n kubeflow svc/ml-pipeline-ui 18088:80

kfp-run:
	uv run --group ml python ml/pipelines/run_kfp.py

kfp-uninstall:
	kubectl delete -k "$(KFP_MANIFESTS)/env/platform-agnostic?ref=$(KFP_VERSION)"
	kubectl delete -k "$(KFP_MANIFESTS)/cluster-scoped-resources?ref=$(KFP_VERSION)"

# E11: evaluation gate against eval/thresholds.yaml (needs make up); exits 1 on any FAIL
eval-quick:
	uv run --group ml python ml/eval_gate.py --quick

eval:
	uv run --group ml python ml/eval_gate.py --full

grafana:
	@echo "Grafana: http://localhost:3001 (user admin, password GRAFANA_ADMIN_PASSWORD in .env)"
	@echo "Dashboard O2C Operations: http://localhost:3001/d/o2c-operations"
	@echo "Prometheus: http://localhost:9090 (alerts: http://localhost:9090/alerts)"

prom-alerts:
	uv run python observability/prom_alerts.py

prom-test:
	MSYS_NO_PATHCONV=1 docker run --rm -v "$(CURDIR)/observability/prometheus:/etc/prometheus" --entrypoint promtool prom/prometheus:v3.5.0 check config /etc/prometheus/prometheus.yml
	MSYS_NO_PATHCONV=1 docker run --rm -v "$(CURDIR)/observability/prometheus:/etc/prometheus" --entrypoint promtool prom/prometheus:v3.5.0 test rules /etc/prometheus/alerts_test.yml
