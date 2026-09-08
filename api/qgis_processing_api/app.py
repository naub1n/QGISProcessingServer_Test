from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from pygeoapi.starlette_app import APP as pygeoapi_app

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


async def list_schedules(request):
    schedules = scheduler.list_schedules()

    return JSONResponse(
        [
            {
                "id": schedule.id,
                "process_id": schedule.process_id,
                "trigger": schedule.trigger,
                "trigger_args": schedule.trigger_args,
                "inputs": schedule.inputs,
                "enabled": schedule.enabled,
            }
            for schedule in schedules
        ]
    )


# ----------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------

routes = [
    Route("/", homepage),
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
)

# ----------------------------------------------------------------------
# pygeoapi
# ----------------------------------------------------------------------

app.mount("/oapi", pygeoapi_app)
