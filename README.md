# LuchParsing

[![CI](https://github.com/lisovcoff/luchparsing/actions/workflows/ci.yml/badge.svg)](https://github.com/lisovcoff/luchparsing/actions/workflows/ci.yml)

A FastAPI service that collects and compares freight delivery prices from multiple Russian transport companies. The application combines direct API integrations and Selenium-based adapters behind one interface, runs calculations concurrently, stores calculation history, and exports results to Excel.

**Live demo:** https://pilot.lisovcoff.ru/

> This public repository is a portfolio version. Production credentials and customer data are not included.

## What the project does

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

## Quick start

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

Set a strong panel password and add only carrier credentials you are authorized to use. The dashboard is available at `http://localhost:8000`; health check: `http://localhost:8000/health`.

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

GitHub Actions checks Python syntax and runs tests on Python 3.11 and 3.12.

## Project structure

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

## Security and operational notes

- Configuration is loaded from environment variables documented in `.env.example`.
- Real API credentials, databases, logs, and customer data must not be committed.
- Carrier websites and undocumented endpoints can change without notice.
- Selenium adapters require periodic selector maintenance and browser compatibility checks.
- API-backed adapters require valid credentials from the corresponding carriers.

## Project status

This is a portfolio-safe version of a commercial logistics automation project. Company-specific operational data and credentials have been removed while preserving the core architecture and adapter model.

## Author

Sergey Inozemtsev — Python backend developer

GitHub: https://github.com/lisovcoff
