"""
Process manager pygeoapi (PostgreSQL) associant chaque job à l'utilisateur
qui l'a créé.

Même principe que user_tinydb_manager.UserTinyDBManager : l'utilisateur
courant est lu depuis les ContextVar posées par auth.ForwardedUserMiddleware
(le process manager pygeoapi n'a jamais accès à la requête HTTP).

Contrairement à TinyDB, la table "jobs" de PostgreSQLManager a un schéma
SQL fixe (pygeoapi.process.manager.postgresql.get_table_model) qui ne
prévoit pas de colonne "owner". Ce plugin définit donc son propre modèle
de table (mêmes colonnes + "owner"), et ajoute la colonne via une
migration ALTER TABLE ... ADD COLUMN IF NOT EXISTS si la table "jobs"
existait déjà sans elle (déploiement PostgreSQL préexistant).
"""

from __future__ import annotations

import functools
import logging

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    LargeBinary,
    String,
    Table,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, Session

from pygeoapi.process.base import JobNotFoundError
from pygeoapi.process.manager.postgresql import PostgreSQLManager
from pygeoapi.util import JobStatus

from auth import can_access_job, current_is_admin_var, current_user_var

LOGGER = logging.getLogger(__name__)


@functools.cache
def get_user_table_model(
    db_search_path: tuple[str], engine: Engine, table_output: bool
) -> Table:
    """Même modèle que PostgreSQLManager, avec une colonne "owner" en plus."""

    Base = declarative_base()
    schema = db_search_path[0]

    jobs = Table(
        "jobs",
        Base.metadata,
        Column("identifier", String, primary_key=True, nullable=False),
        Column(
            "type",
            String,
            nullable=False,
            server_default=text("'process'::character varying"),
        ),
        Column("process_id", String, nullable=False),
        Column("created", DateTime),
        Column("started", DateTime),
        Column("finished", DateTime),
        Column("updated", DateTime),
        Column("status", String, nullable=False),
        Column("location", String),
        Column("mimetype", String),
        Column("message", String),
        Column("progress", Integer, nullable=False),
        Column("owner", String),
        schema=schema,
    )

    if table_output:
        jobs.append_column(Column("output", LargeBinary))

    Base.metadata.create_all(engine, tables=[jobs], checkfirst=True)

    # create_all(checkfirst=True) ne migre pas une table déjà existante :
    # si "jobs" a été créée par PostgreSQLManager avant ce plugin, la
    # colonne "owner" est ajoutée explicitement ici (no-op sinon).
    with engine.begin() as conn:
        conn.execute(
            text(
                f'ALTER TABLE "{schema}".jobs '
                f"ADD COLUMN IF NOT EXISTS owner VARCHAR"
            )
        )

    return jobs


class UserPostgreSQLManager(PostgreSQLManager):
    """PostgreSQLManager filtrant les jobs par utilisateur propriétaire."""

    def __init__(self, manager_def: dict):
        super().__init__(manager_def)

        self.table_model = get_user_table_model(
            self.db_search_path, self._engine, self.table_output
        )
        self.c = self.table_model.c

    def add_job(self, job_metadata: dict) -> str:
        job_metadata = dict(job_metadata)
        job_metadata["owner"] = current_user_var.get()

        return super().add_job(job_metadata)

    def get_jobs(self, status: JobStatus = None, limit=None, offset=None
                 ) -> dict:
        LOGGER.debug("Querying for jobs")
        with Session(self._engine) as session:
            results = session.query(self.table_model)

            if status is not None:
                results = results.filter(self.c.status == status.value)

            if not current_is_admin_var.get():
                results = results.filter(
                    self.c.owner == current_user_var.get()
                )

            jobs = [r._asdict() for r in results.all()]
            return {"jobs": jobs, "numberMatched": len(jobs)}

    def get_job(self, job_id: str) -> dict:
        # Peut lever JobNotFoundError si le job n'existe pas.
        job = super().get_job(job_id)

        if not can_access_job(job.get("owner")):
            # 404 plutôt que 403 : ne pas révéler l'existence du job
            # à un utilisateur qui n'en est pas propriétaire.
            raise JobNotFoundError()

        return job

    def __repr__(self):
        return f"<UserPostgreSQLManager> {self.name}"
