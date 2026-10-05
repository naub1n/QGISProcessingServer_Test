"""
Process manager pygeoapi associant chaque job à l'utilisateur qui l'a créé.

L'utilisateur courant (posé par auth.ForwardedUserMiddleware dans des
ContextVar, seul moyen de l'atteindre ici puisque pygeoapi n'expose pas
la requête HTTP au manager) est enregistré sur chaque job ajouté, et
utilisé pour restreindre l'accès aux jobs des autres utilisateurs :

- get_jobs() ne liste que les jobs de l'utilisateur courant ;
- get_job() (et donc get_job_result()/delete_job(), qui s'appuient dessus)
  lève JobNotFoundError pour un job appartenant à un autre utilisateur,
  afin de ne pas révéler son existence.

Les administrateurs (cf. auth.ADMIN_USERS / auth.ADMIN_GROUPS) voient et
gèrent tous les jobs.
"""

from __future__ import annotations

import logging

from pygeoapi.process.base import JobNotFoundError
from pygeoapi.process.manager.tinydb_ import TinyDBManager
from pygeoapi.util import JobStatus

from auth import can_access_job, current_is_admin_var, current_user_var

LOGGER = logging.getLogger(__name__)


class UserTinyDBManager(TinyDBManager):
    """TinyDBManager filtrant les jobs par utilisateur propriétaire."""

    def add_job(self, job_metadata: dict) -> str:
        job_metadata = dict(job_metadata)
        job_metadata["owner"] = current_user_var.get()

        return super().add_job(job_metadata)

    def get_jobs(self, status: JobStatus = None, limit=None, offset=None
                 ) -> dict:
        with self._db() as db:
            jobs_list = db.all()

        if not current_is_admin_var.get():
            user = current_user_var.get()
            jobs_list = [
                job
                for job in jobs_list
                if job.get("owner") == user
            ]

        number_matched = len(jobs_list)

        if offset:
            jobs_list = jobs_list[offset:]

        if limit:
            jobs_list = jobs_list[:limit]

        return {
            "jobs": jobs_list,
            "numberMatched": number_matched,
        }

    def get_job(self, job_id: str) -> dict:
        # Peut lever JobNotFoundError si le job n'existe pas.
        job = super().get_job(job_id)

        if not can_access_job(job.get("owner")):
            # 404 plutôt que 403 : ne pas révéler l'existence du job
            # à un utilisateur qui n'en est pas propriétaire.
            raise JobNotFoundError()

        return job

    def __repr__(self):
        return f"<UserTinyDBManager> {self.name}"
