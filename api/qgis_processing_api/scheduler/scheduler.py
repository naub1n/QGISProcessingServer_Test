"""
Scheduler pour l'application pygeoapi-scheduler.

Responsabilités :
- enregistrer des tâches planifiées ;
- supprimer / activer / désactiver des tâches ;
- déclencher manuellement une tâche ;
- exécuter une fonction lorsque l'heure programmée arrive.

La fonction execute_process() est volontairement séparée :
elle sera remplacée ensuite par l'appel direct au ProcessManager
de pygeoapi.
"""

from __future__ import annotations

import json
import logging
import os
import httpx
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger


logger = logging.getLogger(__name__)

# Fichier où sont conservées les planifications (survivent aux redémarrages
# de l'application). Vide : planifications en mémoire uniquement.
SCHEDULES_PATH = os.environ.get("SCHEDULES_PATH", "/data/schedules.json")


@dataclass
class Schedule:
    """Définition d'une tâche planifiée."""

    id: str
    process_id: str

    # Type de déclencheur : "cron" ou "interval"
    trigger: str

    # Paramètres du déclencheur
    trigger_args: dict[str, Any] = field(default_factory=dict)

    # Inputs transmis au processus pygeoapi
    inputs: dict[str, Any] = field(default_factory=dict)

    # Tâche active ou non
    enabled: bool = True

    # Utilisateur (X-Forwarded-User) ayant créé la tâche
    owner: str | None = None


class Scheduler:
    """
    Gestionnaire des tâches planifiées.

    Cette classe encapsule APScheduler afin que le reste de
    l'application ne dépende pas directement de son API.
    """

    def __init__(self, storage_path: str | None = SCHEDULES_PATH) -> None:
        self._scheduler = BackgroundScheduler()
        self._schedules: dict[str, Schedule] = {}
        self._storage_path = storage_path or None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Démarre le scheduler (après rechargement des tâches enregistrées)."""

        if not self._scheduler.running:
            self._load()
            logger.info("Starting scheduler")
            self._scheduler.start()

    def shutdown(self) -> None:
        """Arrête proprement le scheduler."""

        if self._scheduler.running:
            logger.info("Stopping scheduler")
            self._scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Schedule management
    # ------------------------------------------------------------------

    def add_schedule(self, schedule: Schedule) -> None:
        """
        Ajoute une tâche planifiée.

        Exemple :

            Schedule(
                id="import_ign",
                process_id="import-ign",
                trigger="cron",
                trigger_args={
                    "hour": 2,
                    "minute": 0,
                },
                inputs={
                    "source": "https://example.com/data"
                }
            )
        """

        if schedule.id in self._schedules:
            raise ValueError(
                f"Schedule '{schedule.id}' already exists"
            )

        self._schedules[schedule.id] = schedule

        if schedule.enabled:
            self._register_job(schedule)

        self._save()

        logger.info(
            "Schedule '%s' added for process '%s'",
            schedule.id,
            schedule.process_id,
        )

    def remove_schedule(self, schedule_id: str) -> None:
        """Supprime une tâche planifiée."""

        if schedule_id not in self._schedules:
            raise KeyError(
                f"Schedule '{schedule_id}' does not exist"
            )

        try:
            self._scheduler.remove_job(schedule_id)
        except JobLookupError:
            # Tâche désactivée : aucun job APScheduler à retirer.
            pass

        del self._schedules[schedule_id]
        self._save()

        logger.info("Schedule '%s' removed", schedule_id)

    def enable_schedule(self, schedule_id: str) -> None:
        """Active une tâche."""

        schedule = self._get_schedule(schedule_id)

        if schedule.enabled:
            return

        schedule.enabled = True
        self._register_job(schedule)
        self._save()

        logger.info("Schedule '%s' enabled", schedule_id)

    def disable_schedule(self, schedule_id: str) -> None:
        """Désactive une tâche sans la supprimer."""

        schedule = self._get_schedule(schedule_id)

        if not schedule.enabled:
            return

        schedule.enabled = False

        try:
            self._scheduler.remove_job(schedule_id)
        except Exception:
            # Le job peut ne pas exister si le scheduler vient
            # d'être démarré ou si la tâche n'a jamais été activée.
            pass

        self._save()

        logger.info("Schedule '%s' disabled", schedule_id)

    def get_schedule(self, schedule_id: str) -> Schedule:
        """Retourne une tâche."""

        return self._get_schedule(schedule_id)

    def get_next_run_time(self, schedule_id: str) -> datetime | None:
        """Prochaine exécution prévue, None si désactivée/inconnue."""

        job = self._scheduler.get_job(schedule_id)

        # Un job ajouté avant le démarrage du scheduler n'a pas encore
        # de next_run_time (état "pending").
        return getattr(job, "next_run_time", None) if job else None

    def list_schedules(self) -> list[Schedule]:
        """Retourne toutes les tâches."""

        return list(self._schedules.values())

    # ------------------------------------------------------------------
    # Manual execution
    # ------------------------------------------------------------------

    def run_now(self, schedule_id: str) -> None:
        """
        Lance immédiatement une tâche.

        Cette méthode ne modifie pas la programmation.
        """

        schedule = self._get_schedule(schedule_id)

        logger.info(
            "Manual execution requested for schedule '%s'",
            schedule_id,
        )

        self._execute_schedule(schedule)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """
        Écrit toutes les tâches dans le fichier de stockage (écriture
        atomique). Une erreur est journalisée sans faire échouer l'appel :
        la tâche reste active, mais ne survivrait pas à un redémarrage.
        """

        if not self._storage_path:
            return

        tmp_path = f"{self._storage_path}.tmp"

        try:
            os.makedirs(os.path.dirname(self._storage_path) or ".", exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(
                    [asdict(s) for s in self._schedules.values()],
                    f,
                    indent=2,
                )
            os.replace(tmp_path, self._storage_path)
        except (OSError, TypeError, ValueError):
            logger.exception(
                "Schedules not saved to %s", self._storage_path
            )

    def _load(self) -> None:
        """
        Recharge les tâches enregistrées et réarme celles qui sont actives.

        Les exécutions qui tombaient pendant l'arrêt ne sont pas rattrapées.
        Une tâche illisible ou invalide est ignorée (et journalisée).
        """

        if not self._storage_path:
            return

        try:
            with open(self._storage_path, encoding="utf-8") as f:
                entries = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.exception(
                "Schedules file %s unreadable, starting empty",
                self._storage_path,
            )
            return

        for entry in entries:
            try:
                schedule = Schedule(**entry)

                if schedule.id in self._schedules:
                    continue

                # Valide le trigger avant d'adopter la tâche.
                self._create_trigger(schedule)

                self._schedules[schedule.id] = schedule

                if schedule.enabled:
                    self._register_job(schedule)
            except Exception:
                logger.exception("Schedule ignored (invalid entry): %r", entry)

        logger.info(
            "%d schedule(s) restored from %s",
            len(self._schedules),
            self._storage_path,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _register_job(self, schedule: Schedule) -> None:
        """Enregistre une tâche auprès d'APScheduler."""

        trigger = self._create_trigger(schedule)

        self._scheduler.add_job(
            self._execute_schedule,
            trigger=trigger,
            args=[schedule],
            id=schedule.id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    def _create_trigger(self, schedule: Schedule):
        """Construit le trigger APScheduler."""

        if schedule.trigger == "cron":
            return CronTrigger(**schedule.trigger_args)

        if schedule.trigger == "interval":
            return IntervalTrigger(**schedule.trigger_args)

        raise ValueError(
            f"Unsupported trigger type: {schedule.trigger}"
        )

    def _execute_schedule(self, schedule: Schedule) -> None:
        """
        Exécute réellement le processus.

        Pour l'instant, on appelle une fonction locale.
        Cette méthode sera ensuite connectée au ProcessManager
        de pygeoapi.
        """

        logger.info(
            "Executing schedule '%s' -> process '%s'",
            schedule.id,
            schedule.process_id,
        )

        try:
            job_id = self.execute_process(
                process_id=schedule.process_id,
                inputs=schedule.inputs,
                owner=schedule.owner,
                schedule_id=schedule.id,
            )

            logger.info(
                "Schedule '%s' started pygeoapi job '%s'",
                schedule.id,
                job_id,
            )

        except Exception:
            logger.exception(
                "Error while executing schedule '%s'",
                schedule.id,
            )

    def execute_process(
        self,
        process_id: str,
        inputs: dict[str, Any],
        owner: str | None = None,
        schedule_id: str | None = None,
    ) -> str:
        url = f"http://127.0.0.1:8000/oapi/processes/{process_id}/execution"

        payload = {
            "inputs": inputs
        }

        # Exécution asynchrone : pygeoapi répond tout de suite 201 avec
        # l'URL du job dans `Location`. En synchrone il ne renvoie que les
        # sorties brutes de l'algorithme (parfois `{}`), sans identifiant de
        # job, et l'appel est borné par le timeout ci-dessous.
        headers = {"Prefer": "respond-async"}
        if owner:
            # Le manager pygeoapi (UserTinyDBManager) associe le job à
            # l'utilisateur porté par ce header, comme pour un appel
            # entrant via Traefik.
            headers["X-Forwarded-User"] = owner

        if schedule_id:
            # Lie le job pygeoapi à sa planification (cf. auth.py : header
            # accepté uniquement depuis la boucle locale).
            headers["X-Schedule-Id"] = schedule_id

        logger.info(
            "Calling pygeoapi process endpoint: %s",
            url,
        )

        response = httpx.post(
            url,
            json=payload,
            headers=headers,
            timeout=30.0,
        )

        response.raise_for_status()

        # Identifiant du job : dernier segment de l'URL `Location`
        # (.../jobs/<id>), à défaut `jobID` dans le corps.
        job_id = response.headers.get("Location", "").rstrip("/").rsplit("/", 1)[-1]

        if not job_id:
            try:
                result = response.json()
            except ValueError:
                result = {}

            if isinstance(result, dict):
                job_id = result.get("jobID") or result.get("job_id")

        if not job_id:
            raise RuntimeError(
                f"pygeoapi did not return a job id "
                f"(HTTP {response.status_code}, headers {dict(response.headers)})"
            )

        return job_id

    # ------------------------------------------------------------------

    def _get_schedule(self, schedule_id: str) -> Schedule:
        """Récupère une tâche ou lève une erreur."""

        try:
            return self._schedules[schedule_id]
        except KeyError:
            raise KeyError(
                f"Schedule '{schedule_id}' does not exist"
            ) from None


# ----------------------------------------------------------------------
# Exemple d'utilisation
# ----------------------------------------------------------------------

if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    scheduler = Scheduler(storage_path=None)

    scheduler.add_schedule(
        Schedule(
            id="test-every-minute",
            process_id="my-process",
            trigger="cron",
            trigger_args={
                "minute": "*",
            },
            inputs={
                "foo": "bar",
            },
        )
    )

    scheduler.start()

    logger.info("Scheduler started. Press Ctrl+C to stop.")

    try:
        while True:
            import time

            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Stopping...")

    finally:
        scheduler.shutdown()

