# LuchParsing

[![CI](https://github.com/Lisovtcoff-hub/luchparsing/actions/workflows/ci.yml/badge.svg)](https://github.com/Lisovtcoff-hub/luchparsing/actions/workflows/ci.yml)

A FastAPI service that collects and compares freight delivery prices from multiple Russian transport companies. The application combines direct API integrations and Selenium-based adapters behind one interface, runs calculations concurrently, stores calculation history, and exports results to Excel.

**Live demo:** https://pilot.lisovcoff.ru/

> This public repository is a portfolio version. Production credentials and customer data are not included.

## What the service does

- calculates delivery prices for multiple routes and cargo presets;
- runs carrier adapters concurrently with per-adapter limits and timeouts;
- supports both HTTP API and browser-automation integrations;
- stores jobs, results, errors, routes, and presets in SQLite;
- compares each run with the previous successful calculation;
- exports calculation results and price changes to XLSX;
- exposes a web dashboard protected with HTTP Basic Authentication;
- provides health checks and Docker-based deployment.

## Architecture

```text
Web dashboard / REST API
          |
       FastAPI
          |
  job orchestration layer
     /             \
HTTP API adapters   Selenium adapters
          \         /
        normalized results
               |
             SQLite
               |
          XLSX export
```

Each carrier implements a common adapter contract and returns a normalized `CalcResult`. The orchestrator handles concurrency, retries, timeouts, temporary failures, progress tracking, and circuit-breaker-like behavior.

## Technology stack

- Python 3.11+
- FastAPI and Uvicorn
- asyncio, httpx, requests
- Selenium and Chromium
- SQLite
- openpyxl
- Docker and Docker Compose
- pytest and GitHub Actions

## Quick start with Docker

1. Clone the repository:

```bash
git clone https://github.com/Lisovtcoff-hub/luchparsing.git
cd luchparsing
```

2. Create a local environment file:

```bash
cp .env.example .env
```

PowerShell:

```powershell
Copy-Item .env.example .env
```

3. Replace placeholder credentials in `.env` with your own API keys and set a strong panel password.

4. Start the application:

```bash
docker compose up --build -d
```

5. Check the health endpoint:

```bash
curl http://localhost:8000/health
```

The dashboard is available at `http://localhost:8000`.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

On Windows, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
```

## Configuration

The application reads configuration from environment variables. See `.env.example` for the complete template. Important variables include:

- `PANEL_USER`, `PANEL_PASSWORD` — dashboard credentials;
- `DELLIN_APPKEY` — Dellin API key;
- `ENERGIYA_DEV_TOKEN` — Energiya API token;
- `TKKIT_TOKEN` — TK KIT API token;
- `VOZOVOZ_TOKEN` — Vozovoz API token;
- `ORCH_WORKERS`, `ORCH_GLOBAL_LIMIT` — concurrency limits;
- adapter-specific retry, timeout, cache, and browser settings.

Never commit `.env`, API keys, exported databases, logs, or customer data.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

GitHub Actions checks Python syntax and runs tests on Python 3.11 and 3.12 for every push and pull request.

## Project structure

```text
luchparsing/
├── app.py                    # FastAPI routes and web application
├── core/
│   ├── contracts.py          # Shared adapter contracts
│   ├── orchestrator.py       # Concurrent calculation orchestration
│   ├── selenium_base.py      # Shared Selenium behavior
│   └── driver_pool.py        # Browser driver lifecycle
├── parsers/                  # Carrier integrations
├── database/db.py            # SQLite access and migrations
├── button/
│   ├── start_calc.py         # Job startup
│   └── export_xlsx.py        # XLSX export
├── web/                      # Dashboard frontend
├── tests/                    # Automated tests
├── Dockerfile
└── docker-compose.yml
```

## Operational notes

Carrier websites and undocumented endpoints can change without notice. Selenium adapters therefore require monitoring and periodic selector updates. API-backed adapters require valid credentials from the corresponding carrier. The repository does not include a production database or real credentials.

## Author

Sergey Inozemtsev — Python Backend Developer

GitHub: https://github.com/Lisovtcoff-hub
