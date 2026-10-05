"""
Process manager pygeoapi (MongoDB) associant chaque job à l'utilisateur
qui l'a créé.

Même principe que user_tinydb_manager.UserTinyDBManager, à une différence
importante près : MongoDBManager.delete_job() et .get_job_result() ne
passent PAS par self.get_job() (contrairement à TinyDBManager et
PostgreSQLManager, où delete_job()/get_job_result() s'appuient sur
get_job() et héritent donc automatiquement du contrôle d'accès par
polymorphisme). Ces deux méthodes sont donc surchargées explicitement
ici pour que la restriction par propriétaire s'applique bien partout.

MongoDBManager.get_job() a également une particularité : il renvoie None
(au lieu de lever JobNotFoundError) quand le job n'existe pas. C'est
corrigé au passage dans la surcharge ci-dessous, condition nécessaire
pour que le contrôle d'accès ait un sens.
"""

from __future__ import annotations

import logging
from typing import Any, Tuple

from pygeoapi.process.base import JobNotFoundError
from pygeoapi.process.manager.mongodb_ import MongoDBManager

from auth import can_access_job, current_is_admin_var, current_user_var

LOGGER = logging.getLogger(__name__)


class UserMongoDBManager(MongoDBManager):
    """MongoDBManager filtrant les jobs par utilisateur propriétaire."""

    def add_job(self, job_metadata: dict):
        job_metadata = dict(job_metadata)
        job_metadata["owner"] = current_user_var.get()

        return super().add_job(job_metadata)

    def get_jobs(self, status=None, limit=None, offset=None) -> dict:
        try:
            self._connect()
            database = self.db.job_manager_pygeoapi
            collection = database.jobs

            query = {}
            if not current_is_admin_var.get():
                query["owner"] = current_user_var.get()

            if status is not None:
                # Comportement hérité de MongoDBManager : le 2e argument de
                # find() est une projection, pas un filtre - ce n'est donc
                # pas un vrai filtrage par statut. Conservé à l'identique,
                # un owner-filter est tout de même appliqué via `query`.
                jobs = list(collection.find(query, {"status": status}))
            else:
                jobs = list(collection.find(query))

            LOGGER.info("JOBMANAGER - MongoDB jobs queried")
            return {
                "jobs": jobs,
                "numberMatched": len(jobs),
            }
        except Exception:
            LOGGER.exception("JOBMANAGER - get_jobs error")
            return False

    def get_job(self, job_id: str) -> dict:
        # MongoDBManager.get_job() renvoie None (sans lever
        # JobNotFoundError) quand le job n'existe pas.
        job = super().get_job(job_id)

        if job is None or not can_access_job(job.get("owner")):
            # 404 plutôt que 403 : ne pas révéler l'existence du job
            # à un utilisateur qui n'en est pas propriétaire.
            raise JobNotFoundError()

        return job

    def delete_job(self, job_id: str) -> bool:
        # Lève JobNotFoundError si le job n'existe pas ou n'appartient
        # pas à l'utilisateur courant. MongoDBManager.delete_job() ne
        # fait pas ce contrôle lui-même (il ne passe pas par get_job()).
        self.get_job(job_id)

        return super().delete_job(job_id)

    def get_job_result(self, job_id: str) -> Tuple[str, Any]:
        # Même remarque que delete_job() : MongoDBManager.get_job_result()
        # ne passe pas par get_job(), donc pas de contrôle d'accès sans
        # cette surcharge.
        self.get_job(job_id)

        return super().get_job_result(job_id)

    def __repr__(self):
        return f"<UserMongoDBManager> {self.name}"
