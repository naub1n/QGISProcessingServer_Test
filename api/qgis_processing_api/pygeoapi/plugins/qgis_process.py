import requests
import time

from pygeoapi.process.base import BaseProcessor


PROCESS_METADATA = {
    "version": "1.0.1",
    "id": "qgis",
    "title": "QGIS Processing",
    "description": "Execute QGIS Processing algorithms through a remote worker",
    "keywords": [
        "qgis",
        "processing",
        "gis"
    ],
    "jobControlOptions": [
        "async-execute"
    ],
    "inputs": {
        "algorithm": {
            "title": "QGIS algorithm",
            "description": "QGIS processing algorithm id, e.g. native:buffer",
            "schema": {
                "type": "string"
            }
        },
        "parameters": {
            "title": "Algorithm parameters",
            "schema": {
                "type": "object"
            }
        }
    },
    "outputs": {
        "result": {
            "title": "Execution result",
            "schema": {
                "type": "object"
            }
        }
    }
}

class QGISRemoteProcessor(BaseProcessor):

    def __init__(self, processor_def):
        super().__init__(
            processor_def,
            PROCESS_METADATA
        )

        self.worker_url = "http://qgis-worker:8000"

    def execute(self, data, outputs=None):

        algorithm = data["algorithm"]
        parameters = data.get("parameters", {})

        try:
            response = requests.post(
                f"{self.worker_url}/execute",
                json={
                    "algorithm": algorithm,
                    "inputs": parameters
                },
                timeout=None
            )

            response.raise_for_status()

        except requests.HTTPError as e:

            try:
                error = response.json()
                detail = error.get("detail", error)

                if isinstance(detail, dict):

                    # Prendre stderr en priorité : c'est là que
                    # qgis_process écrit normalement son erreur.
                    message = detail.get("stderr", "").strip()

                    if not message:
                        message = detail.get("stdout", "").strip()

                    if not message:
                        message = detail.get("message", "").strip()

                    if not message:
                        message = str(detail)

                else:
                    message = str(detail)

            except Exception:
                message = response.text.strip()

            raise RuntimeError(message) from None

        result = response.json()

        return (
            "application/json",
            {
                "result": result
            }
        )

