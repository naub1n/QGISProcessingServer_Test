import asyncio
from contextlib import asynccontextmanager

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


# ----------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------

routes = [
    Route("/", homepage),
    Route("/me", me),
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
