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

`app.py` also proxies a route to the worker, via `httpx` to `WORKER_URL` (502 if the worker is unreachable; FastAPI's `{"detail": ...}` error wrapper is unwrapped):

- `POST /oapi/worker/models` with `{"name", "model3"}` → worker `POST /models`, which validates the name (`MODEL_NAME_RE`) and that the XML root is `<Option>`, then atomically writes `<name>.model3` into `MODELS_DIR` (the `./worker/processing/models` volume), overwriting any existing model. Usable afterwards as `model:<name>`.

This route is declared **before** the `/oapi` mount in `routes` so it takes precedence over pygeoapi; moving it after the mount would silently route them to pygeoapi (404). Algorithm listing/lookup is no longer proxied: use `/oapi/processes[/{id}]` (published algorithms only). The worker's own `GET /algorithms` and `GET /algorithms/{id}[?details=true]` remain, used by `generate_config.py`; `GET /algorithms` must stay declared before `/algorithms/{algorithm_id:path}`.

### One OGC process per QGIS algorithm (generated config)

`api/entrypoint.sh` runs `api/qgis_processing_api/generate_config.py` before `pygeoapi openapi generate`, then points `PYGEOAPI_CONFIG` at the generated file (`/tmp/pygeoapi.generated.yml`; `pygeoapi.yml` stays the base template, still holding the legacy generic `qgis` resource). The generator waits for the worker, lists algorithms (`GET /algorithms`), keeps those matching `QGIS_PUBLISHED_ALGORITHMS` (fnmatch patterns), fetches `GET /algorithms/{id}?details=true` (= `qgis_process help --json`) and adds one `type: process` resource per algorithm, keyed by id (`native:buffer`). Each resource's `processor` embeds the algorithm id, `worker_url` and the full OGC `metadata`, so `QGISAlgorithmProcessor` (`pygeoapi/plugins/qgis_algorithm.py`) needs no network at load time.

- `build_process_metadata()` in that plugin is the only place mapping QGIS parameter types → JSON Schema; unknown types fall back to `{}` rather than blocking the algorithm. Do not put `contentMediaType` in output schemas: pygeoapi's OpenAPI generation uses the first output's one as the whole response media type.
- `qgis_process run` fills omitted parameters with defaults but rejects omitted mandatory ones (e.g. destination `OUTPUT`), hence `minOccurs: 1` only when neither optional nor defaulted.
- Help descriptions are cached in `/data/qgis_processes_cache.json` (not volume-mounted) as `{"algorithms": {id: help}, "sources": {provider: state}}`; only ids missing from the cache are fetched. For `model`/`script`, the worker's `GET /algorithms` returns `sources` (newest mtime + file count of the `.model3`/`.py` files); a provider whose state differs from the cached one is refetched entirely. **Known limit of this POC**: invalidation is per provider, not per algorithm, so one changed model/script refetches all models/scripts (slow with many of them). Per-algorithm invalidation needs an algorithm→file mapping: model ids don't necessarily match file names, but `QgsProcessingModelAlgorithm.sourceFilePath()` gives a model's file (and so its mtime); no known equivalent for scripts yet. Better lever (not available today, would need an upstream/patched `qgis_process`): a `qgis_process --json help all` returning every algorithm's description in one QGIS startup, which would make this whole per-algorithm caching largely unnecessary. The state is only saved if all that provider's fetches succeeded. New models only show up after an `pygeoapi` container restart.
- `/oapi/processes` pagination comes from `server.limits` in `pygeoapi.yml` (`default_items`/`max_items`, set to 1500 so every published algorithm is listed). pygeoapi 0.24's `list_processes` is just `list(processes)[:limit]`: no `offset`, no `next` link, and missing `limits` falls back to 10/10 (so removing the block caps the list at 10 and `?limit=` can't go above). The same limits apply to `/oapi/jobs`.
- Process ids come from QGIS algorithm ids; model ids use the model's internal name, so names with spaces/accents give awkward URLs.
- Execution passes `data` (the OGC `inputs`) straight to the worker and returns only qgis_process's `results`.

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

Job parameters: pygeoapi doesn't store what the client sent when starting a job. `pygeoapi/plugins/job_parameters.py` provides `JobParametersMixin` (first base of all three managers): its `execute_process()` captures `inputs`/`outputs`/`response`/`subscriber` in a `ContextVar` and `add_job()` saves them under the job's `parameters` key (PostgreSQL has a `parameters JSON` column, added by `ALTER TABLE` on existing tables). Importing the module also **monkeypatches `pygeoapi.api.processes.get_jobs`**: in the JSON `/oapi/jobs[/{id}]` output, `parameters` is replaced by top-level `inputs`, `outputs`, `response`, `subscriber` (null when absent; the HTML rendering is untouched). Jobs created before this change simply return nulls. Written against pygeoapi 0.24.0's `BaseManager.execute_process` signature — recheck on upgrade.

### PYTHONPATH

`docker-compose.yml` sets `PYTHONPATH=/qgis_processing_api:/qgis_processing_api/pygeoapi/plugins`. Both entries matter:
- `/qgis_processing_api/pygeoapi/plugins` lets pygeoapi's `load_plugin()` resolve bare dotted names configured in `pygeoapi.yml` (e.g. `qgis_process.QGISRemoteProcessor`, `user_tinydb_manager.UserTinyDBManager`).
- `/qgis_processing_api` lets those same plugins (and `app.py`/`scheduler/`) `import auth`. This directory is only on `sys.path` implicitly when `uvicorn app:app` runs; the separate `pygeoapi openapi generate` CLI call in `api/entrypoint.sh` (which also instantiates the configured manager, and so also imports `auth`) does not get this for free — hence the explicit `PYTHONPATH` entry.

### Config files

- `api/qgis_processing_api/pygeoapi/config/pygeoapi.yml` — pygeoapi config (`PYGEOAPI_CONFIG` env var); `api/entrypoint.sh` generates `openapi.yml` from it at container startup (`PYGEOAPI_OPENAPI` env var) before starting uvicorn.
- `worker/QGIS3.ini` — QGIS profile settings, volume-mounted read-only into the worker container.
