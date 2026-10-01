.PHONY: seed run test bench lint
seed:
	python -m fleetpulse.simulator.seed && python -m fleetpulse.ml.train && python -m fleetpulse.storage.seed_pg && python -m fleetpulse.storage.apply_sql db/optimize.sql
run:
	BUS=redis PARTITIONS=8 WORKER_REPLICAS=2 WORKER_INDEX=0 python -m fleetpulse.stream.worker & \
	BUS=redis PARTITIONS=8 WORKER_REPLICAS=2 WORKER_INDEX=1 python -m fleetpulse.stream.worker & \
	BUS=redis PARTITIONS=8 python -m fleetpulse.simulator.run --rate 8000 & \
	uvicorn fleetpulse.api.app:app --port 8000 --workers 2 --http httptools --loop uvloop
test:
	pytest --cov --cov-report=html:docs/test-reports/coverage && cd tests/bdd && behave features
lint:
	ruff check fleetpulse tests bench && bandit -q -r fleetpulse -ll && pip-audit -r requirements.txt
bench:
	python -m bench.algorithms_bench && python -m bench.pipeline_bench && python -m bench.chaos_bench && \
	python -m fleetpulse.storage.query_bench && python -m bench.api_load && python -m bench.dast_probe
