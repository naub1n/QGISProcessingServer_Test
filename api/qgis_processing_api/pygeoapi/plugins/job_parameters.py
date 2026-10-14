"""
Conservation des paramètres d'exécution d'un job.

pygeoapi n'enregistre pas ce que le client a envoyé pour démarrer un job
(corps de la requête d'exécution OGC API - Processes) : BaseManager.execute_process
ne les transmet qu'au processeur. Ce module :

- JobParametersMixin : à mélanger à un process manager ; capture les
  paramètres reçus par execute_process() et les ajoute, sous la clé
  "parameters", au job enregistré par add_job() (même thread, d'où le
  ContextVar). Cette clé est celle que pygeoapi relit déjà à la
  sérialisation d'un job.
- patch de pygeoapi.api.processes.get_jobs : la description d'un job
  (/jobs et /jobs/{id}) expose alors `inputs`, `outputs`, `response` et
  `subscriber` (comme dans la requête d'exécution OGC) au lieu de
  `parameters`. Le patch est appliqué à l'import ; pygeoapi résolvant
  processes_api.get_jobs à chaque requête, il suffit que ce module soit
  importé au chargement du manager.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar

import pygeoapi.api.processes as processes_api

from auth import current_schedule_var

LOGGER = logging.getLogger(__name__)

# Clés OGC de la requête d'exécution, reprises dans la description du job.
EXECUTION_KEYS = ("inputs", "outputs", "response", "subscriber")

_parameters_var: ContextVar[dict | None] = ContextVar(
    "job_parameters", default=None
)


def _subscriber_to_dict(subscriber) -> dict | None:
    """Subscriber pygeoapi -> forme OGC (successUri, ...), None si absent."""

    if not subscriber:
        return None

    uris = {
        "successUri": subscriber.success_uri,
        "inProgressUri": subscriber.in_progress_uri,
        "failedUri": subscriber.failed_uri,
    }

    return {key: uri for key, uri in uris.items() if uri}


class JobParametersMixin:
    """Enregistre les paramètres d'exécution dans chaque job ajouté."""

    def execute_process(self, process_id, data_dict, execution_mode=None,
                        requested_outputs=None, subscriber=None,
                        requested_response="raw"):
        token = _parameters_var.set({
            "inputs": data_dict or {},
            "outputs": requested_outputs,
            "response": getattr(requested_response, "value",
                                requested_response),
            "subscriber": _subscriber_to_dict(subscriber),
        })

        try:
            return super().execute_process(
                process_id, data_dict, execution_mode, requested_outputs,
                subscriber, requested_response)
        finally:
            _parameters_var.reset(token)

    def add_job(self, job_metadata: dict):
        parameters = _parameters_var.get()

        if parameters is not None:
            # schedule_id voyage dans `parameters` : pygeoapi ne relit que
            # cette clé (les clés inconnues du job sont ignorées).
            parameters = {
                **parameters,
                "schedule_id": current_schedule_var.get(),
            }
            job_metadata = {**job_metadata, "parameters": parameters}

        return super().add_job(job_metadata)


def _describe_job(job: dict) -> None:
    """Remplace `parameters` par les clés OGC (null si job sans paramètres)."""

    parameters = job.pop("parameters", None) or {}

    for key in EXECUTION_KEYS:
        job[key] = parameters.get(key)

    # Planification à l'origine du job (null pour un job lancé à la main).
    job["scheduleID"] = parameters.get("schedule_id")


def _get_jobs_with_parameters(original):

    def get_jobs(api, request, job_id=None):
        headers, status, content = original(api, request, job_id)

        # Seul le rendu JSON est enrichi (le HTML garde la forme native).
        if not isinstance(content, str):
            return headers, status, content

        try:
            body = json.loads(content)
        except ValueError:
            return headers, status, content

        if not isinstance(body, dict):
            return headers, status, content

        jobs = body["jobs"] if "jobs" in body else [body]

        for job in jobs:
            if isinstance(job, dict) and "jobID" in job:
                _describe_job(job)

        return headers, status, json.dumps(
            body, indent=4 if api.pretty_print else None, default=str)

    get_jobs.__wrapped__ = original

    return get_jobs


if not hasattr(processes_api.get_jobs, "__wrapped__"):
    processes_api.get_jobs = _get_jobs_with_parameters(processes_api.get_jobs)
