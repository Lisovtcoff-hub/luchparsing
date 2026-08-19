# LuchParsing

[![CI](https://github.com/lisovcoff/luchparsing/actions/workflows/ci.yml/badge.svg)](https://github.com/lisovcoff/luchparsing/actions/workflows/ci.yml)

Freight rate aggregation service built with FastAPI. The application runs carrier adapters concurrently, normalizes responses from different providers, stores calculation history, and exports comparison reports to XLSX.

## Highlights

- unified adapter contract for HTTP and Selenium-based carriers;
- concurrent job orchestration with per-adapter limits, retries, timeouts, and progress tracking;
- route and cargo preset management;
- calculation history with comparison against the previous successful run;
- web dashboard protected with HTTP Basic Authentication;
- REST API, health checks, and Docker-based local deployment.

## Stack

`Python 3.11` · `FastAPI` · `SQLite` · `Selenium` · `httpx` · `openpyxl` · `Docker Compose` · `pytest` · `GitHub Actions`

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

Each carrier implements the same adapter contract and returns a normalized `CalcResult`. The orchestration layer handles concurrency, temporary failures, timeouts, and job progress.

## Run locally

```bash
git clone https://github.com/lisovcoff/luchparsing.git
cd luchparsing
cp .env.example .env
docker compose up --build -d
```

PowerShell:

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

The dashboard is available at `http://localhost:8000`, and the health check is available at `http://localhost:8000/health`.

## Development and tests

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
pytest -q
uvicorn app:app --reload --port 8000
```

On Windows, activate the environment with `.venv\Scripts\Activate.ps1`.

## Repository layout

```text
app.py                    FastAPI routes and web application
core/                     contracts, orchestration, Selenium infrastructure
parsers/                  carrier integrations
database/db.py            SQLite access and migrations
button/                   job startup and XLSX export
web/                      dashboard frontend
tests/                    automated tests
Dockerfile                application image
docker-compose.yml        local deployment
```

## Notes

- Configuration is documented in `.env.example`.
- This public repository excludes carrier credentials and operational datasets.
- Selenium-based adapters depend on third-party markup and require periodic maintenance.
