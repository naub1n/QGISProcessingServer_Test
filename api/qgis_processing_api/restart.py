"""
Redémarrage planifié de l'application pour prendre en compte les modèles
et scripts QGIS ajoutés, modifiés ou supprimés.

La liste des process OGC est générée au démarrage (entrypoint.sh ->
generate_config.py) : un nouveau modèle n'apparaît qu'après un redémarrage.
Plutôt que de reconstruire la config pygeoapi à chaud, on redémarre à heure
fixe, et seulement si quelque chose a changé.

Chaque jour à l'heure configurée (QGIS_RESTART_CRON) :
  1. l'état des fichiers modèles/scripts annoncé par le worker (`sources` de
     GET /algorithms) est comparé à celui retenu par generate_config.py dans
     son cache ; sans différence, rien ne se passe ;
  2. sinon, les nouvelles exécutions sont refusées (503) et on attend la fin
     des jobs en cours, au plus QGIS_RESTART_DRAIN_TIMEOUT secondes ;
  3. les jobs encore actifs sont passés en "failed" (motif : redémarrage) ;
  4. l'application se termine (SIGTERM) en laissant un fichier témoin que la
     boucle d'entrypoint.sh reconnaît : elle régénère alors la config et
     relance uvicorn dans le même conteneur.

Variables d'environnement :
  QGIS_RESTART_CRON           expression cron à 5 champs (ex. "0 3 * * *").
                              Absente ou vide : aucun redémarrage planifié.
  QGIS_RESTART_TIMEZONE       fuseau de l'heure ci-dessus (ex. Europe/Paris),
                              défaut : fuseau local du conteneur (TZ)
  QGIS_RESTART_DRAIN_TIMEOUT  attente maximale des jobs en cours, en secondes
                              (défaut 1800 = 30 min)
  RESTART_FLAG                fichier témoin lu par entrypoint.sh
                              (défaut /tmp/restart_requested)
  QGIS_PROCESS_CACHE, QGIS_PUBLISHED_ALGORITHMS, QGIS_WORKER_URL : comme
                              generate_config.py / app.py

Limite : annuler un job ne l'arrête pas côté worker, qui termine son
qgis_process ; son résultat est simplement perdu.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone

import httpx
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from starlette.responses import JSONResponse

from auth import current_is_admin_var

logger = logging.getLogger(__name__)

WORKER_URL = os.environ.get("QGIS_WORKER_URL", "http://qgis-worker:8000")
CACHE_PATH = os.environ.get("QGIS_PROCESS_CACHE", "/data/qgis_processes_cache.json")
RESTART_FLAG = os.environ.get("RESTART_FLAG", "/tmp/restart_requested")
PATTERNS = [
    p.strip() for p in
    os.environ.get("QGIS_PUBLISHED_ALGORITHMS", "native:*,model:*,script:*").split(",")
    if p.strip()
]

# Mêmes providers que generate_config.SOURCE_PROVIDERS (non importé : ce module
# lit sa config et configure le logging à l'import).
SOURCE_PROVIDERS = ("model", "script")

ACTIVE_STATUSES = ("accepted", "running")
RESTART_REASON = "Interrupted by application restart"
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

POLL_INTERVAL = 10
# Si l'arrêt d'uvicorn traîne (threads de jobs non terminés), on force la fin.
FORCE_EXIT_AFTER = 60

EXECUTION_PATH = "/oapi/processes/"

# Vrai entre la décision de redémarrer et le redémarrage effectif.
_draining = threading.Event()


def is_draining() -> bool:
    return _draining.is_set()


class DrainMiddleware:
    """Refuse (503) les nouvelles exécutions pendant l'attente du redémarrage."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"].startswith(EXECUTION_PATH)
            and scope["path"].endswith("/execution")
            and is_draining()
        ):
            response = JSONResponse(
                {"error": "Application is about to restart, retry shortly"},
                status_code=503,
                headers={"Retry-After": "60"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class RestartManager:
    """Vérifie chaque jour si un redémarrage est nécessaire."""

    def __init__(
        self,
        cron: str | None,
        tz: str | None = None,
        drain_timeout: int = 1800,
    ) -> None:
        self.cron = cron
        self.tz = tz
        self.drain_timeout = drain_timeout
        self._scheduler = BackgroundScheduler()
        self._job_manager = None

    @classmethod
    def from_env(cls) -> "RestartManager":
        return cls(
            cron=os.environ.get("QGIS_RESTART_CRON", "").strip() or None,
            tz=os.environ.get("QGIS_RESTART_TIMEZONE", "").strip() or None,
            drain_timeout=int(
                os.environ.get("QGIS_RESTART_DRAIN_TIMEOUT", "1800")
            ),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self.cron:
            logger.info("Scheduled restart disabled (QGIS_RESTART_CRON not set)")
            return

        try:
            trigger = CronTrigger.from_crontab(self.cron, timezone=self.tz)
        except ValueError:
            logger.exception(
                "Invalid QGIS_RESTART_CRON %r: scheduled restart disabled",
                self.cron,
            )
            return

        self._scheduler.add_job(
            self.check_and_restart,
            trigger=trigger,
            id="restart-check",
            max_instances=1,
            coalesce=True,
            # Une vérification retardée (démarrage tardif) reste valable.
            misfire_grace_time=3600,
        )
        self._scheduler.start()

        logger.warning(
            "Scheduled restart check: '%s' (timezone: %s), next at %s",
            self.cron,
            self.tz or "local",
            self._scheduler.get_job("restart-check").next_run_time,
        )

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Check
    # ------------------------------------------------------------------

    def check_and_restart(self) -> None:
        try:
            changed = self.sources_changed()
        except Exception:
            logger.exception("Restart check failed, no restart")
            return

        if not changed:
            logger.info("Restart check: no model/script change, no restart")
            return

        logger.warning("Models/scripts changed: preparing application restart")

        _draining.set()
        try:
            self._wait_for_jobs()
            self._cancel_active_jobs()
            self._request_restart()
        except Exception:
            logger.exception("Restart aborted")
            _draining.clear()

    def sources_changed(self) -> bool:
        """
        Vrai si l'état des fichiers modèles/scripts du worker diffère de celui
        vu par la dernière génération de config réussie (cache).
        """

        cached = self._cached_sources()
        if cached is None:
            # Rien à comparer : redémarrer ne ferait que recommencer chaque
            # jour si le cache ne peut pas être écrit.
            logger.warning(
                "Cache %s unreadable, cannot detect model changes", CACHE_PATH
            )
            return False

        response = httpx.get(f"{WORKER_URL}/algorithms", timeout=60)
        response.raise_for_status()
        sources = response.json().get("sources", {})

        for provider in SOURCE_PROVIDERS:
            if not self._is_published(provider) or provider not in sources:
                continue
            if cached.get(provider) != sources[provider]:
                logger.warning(
                    "Provider '%s' changed: %s -> %s",
                    provider,
                    cached.get(provider),
                    sources[provider],
                )
                return True

        return False

    @staticmethod
    def _cached_sources() -> dict | None:
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)["sources"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _is_published(provider: str) -> bool:
        """Un motif publié (`model:*`, `native:buffer`, `*`) vise ce provider."""

        return any(
            fnmatch.fnmatch(provider, pattern.split(":", 1)[0])
            for pattern in PATTERNS
        )

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def _manager(self):
        """Process manager pygeoapi configuré (même stockage que l'API)."""

        if self._job_manager is None:
            from pygeoapi.plugin import load_plugin

            with open(os.environ["PYGEOAPI_CONFIG"], encoding="utf-8") as f:
                config = yaml.safe_load(f)

            # Comme pygeoapi.api le fait pour server.manager.
            self._job_manager = load_plugin(
                "process_manager", config["server"]["manager"]
            )

        return self._job_manager

    def _active_jobs(self) -> list[dict]:
        # UserTinyDBManager ne liste que les jobs de l'utilisateur courant,
        # sauf pour un admin.
        token = current_is_admin_var.set(True)
        try:
            jobs = self._manager().get_jobs()["jobs"]
        finally:
            current_is_admin_var.reset(token)

        return [job for job in jobs if job.get("status") in ACTIVE_STATUSES]

    def _wait_for_jobs(self) -> None:
        deadline = time.monotonic() + self.drain_timeout

        while time.monotonic() < deadline:
            try:
                active = self._active_jobs()
            except Exception:
                logger.exception("Cannot list running jobs")
            else:
                if not active:
                    return
                logger.info("Waiting for %d running job(s)", len(active))

            time.sleep(POLL_INTERVAL)

        logger.warning(
            "Drain timeout (%ds) reached, running jobs will be cancelled",
            self.drain_timeout,
        )

    def _cancel_active_jobs(self) -> None:
        try:
            active = self._active_jobs()
        except Exception:
            logger.exception("Cannot list running jobs to cancel")
            return

        now = datetime.now(timezone.utc).strftime(DATETIME_FORMAT)
        token = current_is_admin_var.set(True)
        try:
            for job in active:
                self._manager().update_job(
                    job["identifier"],
                    {
                        "status": "failed",
                        "message": RESTART_REASON,
                        "progress": 100,
                        "finished": now,
                        "updated": now,
                    },
                )
                logger.warning("Job %s cancelled: restart", job["identifier"])
        finally:
            current_is_admin_var.reset(token)

    # ------------------------------------------------------------------
    # Restart
    # ------------------------------------------------------------------

    @staticmethod
    def _request_restart() -> None:
        # entrypoint.sh relance uvicorn s'il trouve ce fichier à la sortie.
        with open(RESTART_FLAG, "w", encoding="utf-8"):
            pass

        logger.warning("Restarting application (SIGTERM)")

        def force_exit():
            time.sleep(FORCE_EXIT_AFTER)
            logger.error("Graceful shutdown too long, forcing exit")
            os._exit(0)

        threading.Thread(target=force_exit, daemon=True).start()
        os.kill(os.getpid(), signal.SIGTERM)
