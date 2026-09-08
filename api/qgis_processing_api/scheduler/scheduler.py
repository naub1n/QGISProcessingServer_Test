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

import logging
import httpx
from dataclasses import dataclass, field
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger


logger = logging.getLogger(__name__)


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


class Scheduler:
    """
    Gestionnaire des tâches planifiées.

    Cette classe encapsule APScheduler afin que le reste de
    l'application ne dépende pas directement de son API.
    """

    def __init__(self) -> None:
        self._scheduler = BackgroundScheduler()
        self._schedules: dict[str, Schedule] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Démarre le scheduler."""

        if not self._scheduler.running:
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

        self._scheduler.remove_job(schedule_id)
        del self._schedules[schedule_id]

        logger.info("Schedule '%s' removed", schedule_id)

    def enable_schedule(self, schedule_id: str) -> None:
        """Active une tâche."""

        schedule = self._get_schedule(schedule_id)

        if schedule.enabled:
            return

        schedule.enabled = True
        self._register_job(schedule)

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

        logger.info("Schedule '%s' disabled", schedule_id)

    def get_schedule(self, schedule_id: str) -> Schedule:
        """Retourne une tâche."""

        return self._get_schedule(schedule_id)

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
    ) -> str:
        url = f"http://127.0.0.1:8000/oapi/processes/{process_id}/execution"

        payload = {
            "inputs": inputs
        }

        logger.info(
            "Calling pygeoapi process endpoint: %s",
            url,
        )

        response = httpx.post(
            url,
            json=payload,
            timeout=30.0,
        )

        response.raise_for_status()

        result = response.json()

        logger.info(
            "pygeoapi response: %s",
            result,
        )

        # Pour un async-execute, pygeoapi devrait retourner un job.
        job_id = result.get("jobID") or result.get("job_id")

        if not job_id:
            raise RuntimeError(
                f"pygeoapi did not return a job id: {result}"
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

    scheduler = Scheduler()

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

