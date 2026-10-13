# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

There is no test suite, linter, or build tooling configured in this repo (no `pytest.ini`/`pyproject.toml`, no lint config). Development is done by running the Docker Compose stack:

```bash
docker-compose up --build
```

This starts two services:
- `pygeoapi` (container `pygeoapi_with_scheduler`) — published on host port `8080` → container port `8000`.
- `qgis-worker` — published on host port `8000` → container port `8000`.

After a code change, a rebuild (`--build`) is required for it to take effect — there is no hot reload. To iterate only on the `pygeoapi` container:

```bash
docker-compose up --build pygeoapi
```

To exercise an endpoint manually (see [README.md](README.md) for more examples):

```bash
curl --location 'http://localhost:8080/me' --header 'X-Forwarded-User: alice'
```

## Architecture

This is a two-container system that exposes a [pygeoapi](https://pygeoapi.io/) OGC API Processes instance backed by a QGIS processing engine, plus a custom job scheduler and a user-management layer bolted on top of pygeoapi.

### Request flow

1. `api/qgis_processing_api/app.py` is the actual ASGI entrypoint (`uvicorn app:app`). It is a Starlette app that mounts the real pygeoapi Starlette app (`pygeoapi.starlette_app.APP`) at `/oapi`, and adds its own routes (`/`, `/me`, `/schedules*`, `/oapi/worker/*`).
2. A QGIS algorithm execution request hits pygeoapi's `/oapi/processes/qgis/execution`. pygeoapi resolves the `qgis` process to `api/qgis_processing_api/pygeoapi/plugins/qgis_process.py` (`QGISRemoteProcessor`), which does a synchronous HTTP POST to `http://qgis-worker:8000/execute`.
3. `worker/app.py` (FastAPI, running inside the `qgis-worker` container which has a real QGIS install) runs the algorithm via the `qgis_process` CLI in a subprocess, capped at 4 concurrent executions (`ThreadPoolExecutor(max_workers=4)`), and returns the result synchronously.
4. pygeoapi itself handles the async/job-tracking semantics (job IDs, status polling via `/oapi/jobs`) on top of that synchronous call, using a process manager (see "pygeoapi process managers" below).

### Worker introspection and model publishing

`app.py` also proxies three routes to the worker, via `httpx` to `WORKER_URL` (502 if the worker is unreachable; FastAPI's `{"detail": ...}` error wrapper is unwrapped):

- `GET /oapi/worker/algorithms` → worker `GET /algorithms` (lists everything from `qgis_process list --json`).
- `GET /oapi/worker/algorithms/{id}` → worker `GET /algorithms/{id}` (200/404; models use `model:<name>`).
- `POST /oapi/worker/models` with `{"name", "model3"}` → worker `POST /models`, which validates the name (`MODEL_NAME_RE`) and that the XML root is `<Option>`, then atomically writes `<name>.model3` into `MODELS_DIR` (the `./worker/processing/models` volume), overwriting any existing model. Usable afterwards as `model:<name>`.

These routes are declared **before** the `/oapi` mount in `routes` so they take precedence over pygeoapi; moving them after the mount would silently route them to pygeoapi (404). In the worker, `GET /algorithms` must stay declared before `/algorithms/{algorithm_id:path}`.

### Scheduler

`api/qgis_processing_api/scheduler/` is a separate, self-contained module (APScheduler `BackgroundScheduler`) exposing `/schedules*` REST routes (`app.py`/`scheduler/routes.py`). It does **not** call pygeoapi's Python API directly — when a schedule fires, `Scheduler.execute_process()` (`scheduler/scheduler.py`) makes an HTTP call to the API's own `/oapi/processes/{id}/execution` endpoint (`http://127.0.0.1:8000/...`), going through the full middleware stack like any other client. Schedules are **in-memory only** (no persistence) — they are lost on container restart.

### User management (Traefik OIDC forward-auth)

The API trusts `X-Forwarded-User` / `X-User-Groups` headers injected by a Traefik forward-auth/OIDC middleware sitting in front of it (example labels in `docker-compose.yml`, commented out). It must never be reachable except through Traefik, or these headers could be forged.

- `api/qgis_processing_api/auth.py` — `ForwardedUserMiddleware` (Starlette middleware wrapping the whole app, including the mounted pygeoapi sub-app) reads the headers, enforces 401 when missing (unless `AUTH_ENABLED=false`), and exposes identity two ways:
  - `request.state.user` / `.groups` / `.is_admin`, for routes defined in this repo (`scheduler/routes.py`, `/me`).
  - `current_user_var` / `current_is_admin_var` (`contextvars.ContextVar`), because pygeoapi's process manager code never receives the HTTP request object.
- **Load-bearing detail**: pygeoapi dispatches every endpoint via `loop.run_in_executor(None, ...)` (see `pygeoapi/starlette_app.py`), which does **not** propagate `contextvars` into the worker thread by default (unlike `asyncio.to_thread`). `auth.ContextPreservingExecutor` is installed as the event loop's default executor in `app.py`'s `lifespan` specifically to fix this. If this executor is ever removed, every pygeoapi-managed job silently loses its owner and the ownership filtering becomes a no-op — this is easy to break without an obvious error, see commit history / README for how it was diagnosed.
- Schedules (`scheduler/scheduler.py`) carry an `owner` field; the scheduler re-injects it as `X-Forwarded-User` when triggering pygeoapi internally, so scheduled jobs are still attributed correctly.
- `ADMIN_USERS` / `ADMIN_GROUPS` env vars (comma-separated) bypass ownership restrictions.

### pygeoapi process managers

`api/qgis_processing_api/pygeoapi/plugins/user_{tinydb,postgresql,mongodb}_manager.py` subclass pygeoapi's built-in `TinyDBManager` / `PostgreSQLManager` / `MongoDBManager` to tag each job with its creator (`owner`) and filter/guard access to `/oapi/jobs` accordingly (a non-owned job 404s rather than 403s, to avoid leaking its existence). Only `UserTinyDBManager` is wired up in `pygeoapi.yml` (`server.manager.name`); the PostgreSQL/MongoDB configs are present but commented out, since they need extra dependencies (`psycopg2-binary` / `pymongo`, commented out in `api/requirements.txt`). pygeoapi's own job storage is unrelated to the scheduler's (separate, in-memory) storage described above.

Note: `MongoDBManager.delete_job()`/`.get_job_result()` don't call `self.get_job()` internally (unlike the TinyDB/PostgreSQL managers), so `UserMongoDBManager` has to override them explicitly to apply the ownership check — the other two managers get it "for free" through polymorphism.

### PYTHONPATH

`docker-compose.yml` sets `PYTHONPATH=/qgis_processing_api:/qgis_processing_api/pygeoapi/plugins`. Both entries matter:
- `/qgis_processing_api/pygeoapi/plugins` lets pygeoapi's `load_plugin()` resolve bare dotted names configured in `pygeoapi.yml` (e.g. `qgis_process.QGISRemoteProcessor`, `user_tinydb_manager.UserTinyDBManager`).
- `/qgis_processing_api` lets those same plugins (and `app.py`/`scheduler/`) `import auth`. This directory is only on `sys.path` implicitly when `uvicorn app:app` runs; the separate `pygeoapi openapi generate` CLI call in `api/entrypoint.sh` (which also instantiates the configured manager, and so also imports `auth`) does not get this for free — hence the explicit `PYTHONPATH` entry.

### Config files

- `api/qgis_processing_api/pygeoapi/config/pygeoapi.yml` — pygeoapi config (`PYGEOAPI_CONFIG` env var); `api/entrypoint.sh` generates `openapi.yml` from it at container startup (`PYGEOAPI_OPENAPI` env var) before starting uvicorn.
- `worker/QGIS3.ini` — QGIS profile settings, volume-mounted read-only into the worker container.
