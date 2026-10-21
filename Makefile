.PHONY: up down test lint lock

# Build all services
up:
	docker compose up

down:
	docker compose down

# Needs Postgres running (e.g. `docker compose up -d postgres`)
test:
	cd service && python manage.py test vending

lint:
	ruff check service operator ussd-gateway

# Recompile every lockfile from its requirements.in
lock:
	for d in service operator ussd-gateway; do \
		(cd $$d && pip-compile -q --generate-hashes --strip-extras --no-emit-index-url \
			--output-file requirements.txt requirements.in) || exit 1; \
	done
