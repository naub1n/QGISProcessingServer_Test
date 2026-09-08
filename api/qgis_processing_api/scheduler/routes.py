from dataclasses import asdict

from starlette.requests import Request
from starlette.responses import JSONResponse


def schedule_to_dict(schedule):
    """Convertit un Schedule en dictionnaire JSON."""
    return asdict(schedule)


async def list_schedules(request: Request):
    """GET /schedules"""

    scheduler = request.app.state.scheduler

    schedules = scheduler.list_schedules()

    return JSONResponse([
        schedule_to_dict(schedule)
        for schedule in schedules
    ])


async def get_schedule(request: Request):
    """GET /schedules/{schedule_id}"""

    scheduler = request.app.state.scheduler

    schedule_id = request.path_params["schedule_id"]

    try:
        schedule = scheduler.get_schedule(schedule_id)
    except KeyError:
        return JSONResponse(
            {
                "error": "Schedule not found",
                "id": schedule_id,
            },
            status_code=404,
        )

    return JSONResponse(schedule_to_dict(schedule))


async def create_schedule(request: Request):
    """POST /schedules"""

    scheduler = request.app.state.scheduler

    data = await request.json()

    required_fields = [
        "id",
        "process_id",
        "trigger",
    ]

    missing = [
        field
        for field in required_fields
        if field not in data
    ]

    if missing:
        return JSONResponse(
            {
                "error": "Missing required fields",
                "fields": missing,
            },
            status_code=400,
        )

    from .scheduler import Schedule

    try:
        schedule = Schedule(
            id=data["id"],
            process_id=data["process_id"],
            trigger=data["trigger"],
            trigger_args=data.get("trigger_args", {}),
            inputs=data.get("inputs", {}),
            enabled=data.get("enabled", True),
        )

        scheduler.add_schedule(schedule)

    except ValueError as exc:
        return JSONResponse(
            {"error": str(exc)},
            status_code=409,
        )

    return JSONResponse(
        schedule_to_dict(schedule),
        status_code=201,
    )


async def delete_schedule(request: Request):
    """DELETE /schedules/{schedule_id}"""

    scheduler = request.app.state.scheduler

    schedule_id = request.path_params["schedule_id"]

    try:
        scheduler.remove_schedule(schedule_id)
    except KeyError:
        return JSONResponse(
            {
                "error": "Schedule not found",
                "id": schedule_id,
            },
            status_code=404,
        )

    return JSONResponse(
        {
            "id": schedule_id,
            "deleted": True,
        }
    )


async def enable_schedule(request: Request):
    """POST /schedules/{schedule_id}/enable"""

    scheduler = request.app.state.scheduler

    schedule_id = request.path_params["schedule_id"]

    try:
        scheduler.enable_schedule(schedule_id)
    except KeyError:
        return JSONResponse(
            {
                "error": "Schedule not found",
                "id": schedule_id,
            },
            status_code=404,
        )

    return JSONResponse(
        {
            "id": schedule_id,
            "enabled": True,
        }
    )


async def disable_schedule(request: Request):
    """POST /schedules/{schedule_id}/disable"""

    scheduler = request.app.state.scheduler

    schedule_id = request.path_params["schedule_id"]

    try:
        scheduler.disable_schedule(schedule_id)
    except KeyError:
        return JSONResponse(
            {
                "error": "Schedule not found",
                "id": schedule_id,
            },
            status_code=404,
        )

    return JSONResponse(
        {
            "id": schedule_id,
            "enabled": False,
        }
    )


async def run_schedule(request: Request):
    """POST /schedules/{schedule_id}/run"""

    scheduler = request.app.state.scheduler

    schedule_id = request.path_params["schedule_id"]

    try:
        scheduler.run_now(schedule_id)
    except KeyError:
        return JSONResponse(
            {
                "error": "Schedule not found",
                "id": schedule_id,
            },
            status_code=404,
        )

    return JSONResponse(
        {
            "id": schedule_id,
            "started": True,
        }
    )
