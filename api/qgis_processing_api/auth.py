"""
Gestion des utilisateurs à partir des headers injectés par Traefik
(middleware OIDC / forward-auth) :

- X-Forwarded-User   : identifiant de l'utilisateur authentifié
- X-User-Groups : groupes de l'utilisateur (séparés par des virgules)

L'API n'est jamais exposée autrement que derrière Traefik : ces headers
sont donc considérés comme fiables et ne sont pas revalidés ici.

La fonctionnalité peut être désactivée entièrement via AUTH_ENABLED=false
(aucun header requis, aucune restriction, owner laissé vide sur les jobs
et les planifications).
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


USER_HEADER = "X-Forwarded-User"
GROUPS_HEADER = "X-User-Groups"

# Chemins accessibles sans utilisateur authentifié.
PUBLIC_PATHS = {"/"}

# Permet de désactiver entièrement la gestion des utilisateurs (pas de
# Traefik/OIDC devant l'API, ou authentification pas encore en place).
AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "true").strip().lower() not in (
    "0", "false", "no", "off",
)

ADMIN_USERS = {
    user.strip()
    for user in os.environ.get("ADMIN_USERS", "").split(",")
    if user.strip()
}

ADMIN_GROUPS = {
    group.strip()
    for group in os.environ.get("ADMIN_GROUPS", "").split(",")
    if group.strip()
}

# Copie de l'utilisateur courant accessible en dehors du cycle
# requête/réponse Starlette (ex: process manager de pygeoapi, qui ne
# reçoit jamais la requête HTTP). Valable uniquement dans la tâche asyncio
# qui traite la requête courante.
current_user_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_user", default=None
)
current_is_admin_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "current_is_admin", default=False
)


class ContextPreservingExecutor(concurrent.futures.ThreadPoolExecutor):
    """
    ThreadPoolExecutor qui propage les ContextVar du thread appelant.

    pygeoapi exécute chacun de ses endpoints (donc le process manager)
    via `loop.run_in_executor(None, ...)`, c'est-à-dire l'executor par
    défaut de la boucle asyncio. Contrairement à `asyncio.to_thread()`,
    `run_in_executor()` ne copie PAS le contexte contextvars courant dans
    le thread : sans cet executor dédié posé comme executor par défaut,
    current_user_var/current_is_admin_var retrouveraient leur valeur par
    défaut dans ce thread, et UserTinyDBManager ne saurait plus jamais
    qui est l'utilisateur courant.
    """

    def submit(self, fn, /, *args, **kwargs):
        ctx = contextvars.copy_context()
        return super().submit(ctx.run, fn, *args, **kwargs)


def get_user(request: Request) -> str | None:
    """Retourne l'utilisateur authentifié associé à la requête, ou None."""

    return getattr(request.state, "user", None)


def get_groups(request: Request) -> list[str]:
    """Retourne les groupes de l'utilisateur associé à la requête."""

    return getattr(request.state, "groups", [])


def is_admin(request: Request) -> bool:
    """Indique si l'utilisateur de la requête est administrateur."""

    return bool(getattr(request.state, "is_admin", False))


def can_access_job(owner: str | None) -> bool:
    """
    Indique si l'utilisateur courant (ContextVar, donc utilisable depuis
    un process manager pygeoapi qui n'a jamais accès à la requête HTTP)
    peut accéder à un job dont le propriétaire est `owner`.

    Utilisé par les process managers UserTinyDBManager,
    UserPostgreSQLManager et UserMongoDBManager.
    """

    return current_is_admin_var.get() or owner == current_user_var.get()


class ForwardedUserMiddleware(BaseHTTPMiddleware):
    """
    Lit les headers posés par Traefik et les expose sur `request.state`.

    Rejette avec un 401 toute requête sans utilisateur authentifié,
    sauf sur les chemins publics (PUBLIC_PATHS).

    Si AUTH_ENABLED=false, ne lit aucun header, n'impose aucune
    restriction (équivalent à un accès admin) et laisse `owner` vide
    partout (schedules, jobs pygeoapi).
    """

    async def dispatch(self, request: Request, call_next):
        if not AUTH_ENABLED:
            user, groups, is_admin_user = None, [], True
        else:
            user = request.headers.get(USER_HEADER)

            groups = [
                group.strip()
                for group in request.headers.get(GROUPS_HEADER, "").split(",")
                if group.strip()
            ]

            if not user and request.url.path not in PUBLIC_PATHS:
                return JSONResponse(
                    {
                        "error":
                            f"Missing authenticated user ({USER_HEADER} header)"
                    },
                    status_code=401,
                )

            is_admin_user = (
                user in ADMIN_USERS or bool(ADMIN_GROUPS & set(groups))
            )

        request.state.user = user
        request.state.groups = groups
        request.state.is_admin = is_admin_user

        user_token = current_user_var.set(user)
        admin_token = current_is_admin_var.set(is_admin_user)

        try:
            return await call_next(request)
        finally:
            current_user_var.reset(user_token)
            current_is_admin_var.reset(admin_token)
