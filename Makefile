.PHONY: help up down restart logs ps build test test-live install dev-backend dev-frontend clean

COMPOSE ?= docker compose

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

up: ## Build and start frontend (:3000) and backend (:8000)
	$(COMPOSE) up -d --build
	@echo "Frontend: http://localhost:3000"
	@echo "Backend:  http://localhost:8000  (health: http://localhost:8000/health)"

down: ## Stop the stack (data volume is kept)
	$(COMPOSE) down

restart: ## Restart containers (verifies SQLite persistence)
	$(COMPOSE) restart

logs: ## Follow logs
	$(COMPOSE) logs -f

ps: ## Show container status
	$(COMPOSE) ps

build: ## Build images
	$(COMPOSE) build

test: ## Run backend tests (mocked LLM, no credits used)
	cd backend && python -m pytest

test-live: ## Run real-provider integration test (needs LIVE_LLM_API_KEYS)
	cd backend && python -m pytest -m integration -v

install: ## Install backend + frontend dependencies for local (non-Docker) dev
	cd backend && python -m pip install -r requirements-dev.txt
	cd frontend && npm ci

dev-backend: ## Run backend locally with reload
	cd backend && DATABASE_URL=sqlite:///./data/app.db uvicorn app.main:app --reload --port 8000

dev-frontend: ## Run frontend dev server
	cd frontend && npm run dev

clean: ## Stop the stack AND delete the SQLite volume
	$(COMPOSE) down -v
