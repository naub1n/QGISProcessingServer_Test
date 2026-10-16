"""
Gestion des utilisateurs à partir des headers injectés par Traefik
(middleware OIDC / forward-auth) :

- X-Forwarded-User   : identifiant de l'utilisateur authentifié
- X-User-Groups : groupes de l'utilisateur (séparés par des virgules)

L'API n'est jamais exposée autrement que derrière Traefik : ces headers
sont donc considérés comme fiables et ne sont pas revalidés ici.

Deuxième méthode : un access token Azure AD / Entra ID (JWT) envoyé en
`Authorization: Bearer <token>` (cas du plugin QGIS, qui contourne Traefik).
Il est validé ici (signature via JWKS, exp, iss, aud). Le Bearer est
prioritaire s'il est présent ; sinon on retombe sur les headers Traefik.
Variables : OIDC_TENANT_ID et OIDC_AUDIENCE (obligatoires pour activer le
Bearer), OIDC_ISSUER, OIDC_JWKS_URL, OIDC_USER_CLAIM, OIDC_GROUPS_CLAIM.

La fonctionnalité peut être désactivée entièrement via AUTH_ENABLED=false
(aucun header requis, aucune restriction, owner laissé vide sur les jobs
et les planifications).
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import os

import jwt
from jwt import PyJWKClient
from starlette.concurrency import run_in_threadpool
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

# Liste blanche d'accès (en plus des admins). Si au moins une des deux est
# définie, seuls les utilisateurs listés ou membres d'un groupe listé passent
# (403 sinon). Vides = tout utilisateur authentifié est accepté.
ALLOWED_USERS = {
    u.strip() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip()
}
ALLOWED_GROUPS = {
    g.strip() for g in os.environ.get("ALLOWED_GROUPS", "").split(",") if g.strip()
}

def _csv(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


# --- Validation des tokens Bearer (Azure AD / Entra ID) ---------------------
# Actif seulement si OIDC_TENANT_ID et OIDC_AUDIENCE sont définis.
OIDC_TENANT_ID = os.environ.get("OIDC_TENANT_ID", "").strip()
# Audiences acceptées (claim `aud`), ex: "api://<client-id>,<client-id>".
OIDC_AUDIENCES = _csv("OIDC_AUDIENCE")
# Issuers acceptés : v2.0 et v1.0 par défaut (selon `accessTokenAcceptedVersion`
# de l'enregistrement d'application).
OIDC_ISSUERS = _csv("OIDC_ISSUER") or (
    [
        f"https://login.microsoftonline.com/{OIDC_TENANT_ID}/v2.0",
        f"https://sts.windows.net/{OIDC_TENANT_ID}/",
    ]
    if OIDC_TENANT_ID
    else []
)
OIDC_JWKS_URL = os.environ.get("OIDC_JWKS_URL") or (
    f"https://login.microsoftonline.com/{OIDC_TENANT_ID}/discovery/v2.0/keys"
    if OIDC_TENANT_ID
    else ""
)
# Claims utilisateur essayés dans l'ordre (v2 : preferred_username ; v1 : upn).
OIDC_USER_CLAIMS = _csv("OIDC_USER_CLAIM") or [
    "preferred_username", "upn", "unique_name", "email", "oid", "sub",
]
# Claims de groupes : `groups` (GUID des groupes, à activer dans l'app
# registration) et `roles` (app roles).
OIDC_GROUPS_CLAIMS = _csv("OIDC_GROUPS_CLAIM") or ["groups", "roles"]

BEARER_ENABLED = bool(OIDC_TENANT_ID and OIDC_AUDIENCES and OIDC_JWKS_URL)
_jwks_client = (
    PyJWKClient(OIDC_JWKS_URL, cache_keys=True) if BEARER_ENABLED else None
)


class InvalidToken(Exception):
    pass


def identity_from_bearer(token: str) -> tuple[str, list[str]]:
    """Valide un JWT Azure AD et retourne (utilisateur, groupes)."""

    try:
        key = _jwks_client.get_signing_key_from_jwt(token).key
        # L'issuer est vérifié à la main : v1 et v2 sont acceptés.
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=OIDC_AUDIENCES,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidToken(str(exc)) from exc

    if claims.get("iss") not in OIDC_ISSUERS:
        raise InvalidToken("Invalid issuer")
    if claims.get("tid") not in (None, OIDC_TENANT_ID):
        raise InvalidToken("Invalid tenant")

    user = next(
        (str(claims[c]) for c in OIDC_USER_CLAIMS if claims.get(c)), None
    )
    if not user:
        raise InvalidToken("No user claim in token")

    groups: list[str] = []
    for claim in OIDC_GROUPS_CLAIMS:
        value = claims.get(claim)
        if isinstance(value, str):
            value = [value]
        groups.extend(str(v) for v in value or [])

    return user, groups


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

# Planification à l'origine de la requête courante (jobs lancés par le
# scheduler), pour que le process manager puisse lier le job à celle-ci.
current_schedule_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_schedule", default=None
)

# Header posé par le scheduler sur ses appels internes ; n'est pris en compte
# que pour les requêtes venant de la boucle locale (jamais via Traefik).
SCHEDULE_HEADER = "X-Schedule-Id"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


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
            user = None
            groups: list[str] = []

            authorization = request.headers.get("Authorization", "")
            if BEARER_ENABLED and authorization.lower().startswith("bearer "):
                try:
                    # Appel réseau bloquant possible (JWKS, mis en cache).
                    user, groups = await run_in_threadpool(
                        identity_from_bearer, authorization[7:].strip()
                    )
                except InvalidToken as exc:
                    return JSONResponse(
                        {"error": f"Invalid bearer token: {exc}"},
                        status_code=401,
                    )
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
                        "error": "Missing authenticated user "
                        f"({USER_HEADER} header or Bearer token)"
                    },
                    status_code=401,
                )

            is_admin_user = (
                user in ADMIN_USERS or bool(ADMIN_GROUPS & set(groups))
            )

        if (
            AUTH_ENABLED
            and (ALLOWED_USERS or ALLOWED_GROUPS)
            and request.url.path not in PUBLIC_PATHS
            and not is_admin_user
            and user not in ALLOWED_USERS
            and not (ALLOWED_GROUPS & set(groups))
        ):
            return JSONResponse(
                {"error": "User is not allowed to access this API"},
                status_code=403,
            )

        request.state.user = user
        request.state.groups = groups
        request.state.is_admin = is_admin_user

        schedule_id = None
        if request.client and request.client.host in LOCAL_HOSTS:
            schedule_id = request.headers.get(SCHEDULE_HEADER)

        user_token = current_user_var.set(user)
        admin_token = current_is_admin_var.set(is_admin_user)
        schedule_token = current_schedule_var.set(schedule_id)

        try:
            return await call_next(request)
        finally:
            current_user_var.reset(user_token)
            current_is_admin_var.reset(admin_token)
            current_schedule_var.reset(schedule_token)
