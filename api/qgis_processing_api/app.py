import asyncio
import os
from contextlib import asynccontextmanager

import httpx

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from pygeoapi.starlette_app import APP as pygeoapi_app

from auth import (
    ContextPreservingExecutor,
    ForwardedUserMiddleware,
    get_groups,
    get_user,
    is_admin,
)
from scheduler.scheduler import Scheduler
from scheduler.routes import (
    list_schedules,
    get_schedule,
    create_schedule,
    delete_schedule,
    enable_schedule,
    disable_schedule,
    run_schedule,
)


# ----------------------------------------------------------------------
# Scheduler
# ----------------------------------------------------------------------

scheduler = Scheduler()


# ----------------------------------------------------------------------
# Starlette lifecycle
# ----------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: Starlette):
    """
    Lifecycle de l'application Starlette.

    Le scheduler est démarré lorsque Starlette démarre
    et arrêté proprement lorsque Starlette s'arrête.
    """

    # pygeoapi exécute ses endpoints (et donc UserTinyDBManager) via
    # loop.run_in_executor(None, ...), qui par défaut ne propage pas les
    # ContextVar dans le thread. Sans cet executor, le manager ne saurait
    # jamais quel utilisateur a fait la requête.
    asyncio.get_running_loop().set_default_executor(
        ContextPreservingExecutor()
    )

    app.state.scheduler = scheduler

    scheduler.start()

    yield

    scheduler.shutdown()


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------

async def homepage(request):
    return JSONResponse(
        {
            "application": "pygeoapi-scheduler",
            "status": "running",
            "services": {
                "pygeoapi": "/oapi/",
                "schedules": "/schedules",
            },
        }
    )


async def me(request: Request):
    return JSONResponse(
        {
            "user": get_user(request),
            "groups": get_groups(request),
            "is_admin": is_admin(request),
        }
    )


WORKER_URL = os.environ.get("QGIS_WORKER_URL", "http://qgis-worker:8000")


async def publish_worker_model(request: Request):
    """
    Publie un modèle QGIS sur le worker.
    Corps attendu : {"name": "<nom>", "model3": "<contenu XML du .model3>"}
    """

    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(
            {"message": "Request body must be valid JSON"},
            status_code=400,
        )

    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("name"), str)
        or not isinstance(payload.get("model3"), str)
    ):
        return JSONResponse(
            {"message": "Body must be {\"name\": str, \"model3\": str}"},
            status_code=400,
        )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{WORKER_URL}/models",
                json={"name": payload["name"], "model3": payload["model3"]},
            )
    except httpx.HTTPError as e:
        return JSONResponse(
            {"message": f"Worker unreachable: {e}"},
            status_code=502,
        )

    try:
        body = response.json()
    except ValueError:
        body = {"message": response.text}

    if isinstance(body, dict) and "detail" in body:
        body = body["detail"]

    return JSONResponse(body, status_code=response.status_code)


# ----------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------

routes = [
    Route("/", homepage),
    Route("/me", me),
    # Publication de modèles sur le worker QGIS (déclaré avant le mount
    # /oapi pour prendre le pas sur pygeoapi)
    Route(
        "/oapi/worker/models",
        publish_worker_model,
        methods=["POST"],
    ),
    # Scheduler
    Route(
        "/schedules",
        list_schedules,
        methods=["GET"],
    ),
    Route(
        "/schedules",
        create_schedule,
        methods=["POST"],
    ),
    Route(
        "/schedules/{schedule_id}",
        get_schedule,
        methods=["GET"],
    ),
    Route(
        "/schedules/{schedule_id}",
        delete_schedule,
        methods=["DELETE"],
    ),
    Route(
        "/schedules/{schedule_id}/enable",
        enable_schedule,
        methods=["POST"],
    ),
    Route(
        "/schedules/{schedule_id}/disable",
        disable_schedule,
        methods=["POST"],
    ),
    Route(
        "/schedules/{schedule_id}/run",
        run_schedule,
        methods=["POST"],
    ),
]


app = Starlette(
    debug=True,
    routes=routes,
    lifespan=lifespan,
    middleware=[Middleware(ForwardedUserMiddleware)],
)

# ----------------------------------------------------------------------
# pygeoapi
# ----------------------------------------------------------------------

app.mount("/oapi", pygeoapi_app)
