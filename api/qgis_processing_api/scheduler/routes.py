from dataclasses import asdict

from starlette.requests import Request
from starlette.responses import JSONResponse

from auth import get_user, is_admin


def schedule_to_dict(schedule):
    """Convertit un Schedule en dictionnaire JSON."""
    return asdict(schedule)


def _forbidden(schedule_id: str):
    return JSONResponse(
        {
            "error": "Not allowed to access this schedule",
            "id": schedule_id,
        },
        status_code=403,
    )


async def list_schedules(request: Request):
    """GET /schedules"""

    scheduler = request.app.state.scheduler

    schedules = scheduler.list_schedules()

    if not is_admin(request):
        user = get_user(request)
        schedules = [
            schedule
            for schedule in schedules
            if schedule.owner == user
        ]

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

    if not is_admin(request) and schedule.owner != get_user(request):
        return _forbidden(schedule_id)

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
            owner=get_user(request),
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
        schedule = scheduler.get_schedule(schedule_id)
    except KeyError:
        return JSONResponse(
            {
                "error": "Schedule not found",
                "id": schedule_id,
            },
            status_code=404,
        )

    if not is_admin(request) and schedule.owner != get_user(request):
        return _forbidden(schedule_id)

    scheduler.remove_schedule(schedule_id)

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
        schedule = scheduler.get_schedule(schedule_id)
    except KeyError:
        return JSONResponse(
            {
                "error": "Schedule not found",
                "id": schedule_id,
            },
            status_code=404,
        )

    if not is_admin(request) and schedule.owner != get_user(request):
        return _forbidden(schedule_id)

    scheduler.enable_schedule(schedule_id)

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
        schedule = scheduler.get_schedule(schedule_id)
    except KeyError:
        return JSONResponse(
            {
                "error": "Schedule not found",
                "id": schedule_id,
            },
            status_code=404,
        )

    if not is_admin(request) and schedule.owner != get_user(request):
        return _forbidden(schedule_id)

    scheduler.disable_schedule(schedule_id)

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
        schedule = scheduler.get_schedule(schedule_id)
    except KeyError:
        return JSONResponse(
            {
                "error": "Schedule not found",
                "id": schedule_id,
            },
            status_code=404,
        )

    if not is_admin(request) and schedule.owner != get_user(request):
        return _forbidden(schedule_id)

    scheduler.run_now(schedule_id)

    return JSONResponse(
        {
            "id": schedule_id,
            "started": True,
        }
    )
