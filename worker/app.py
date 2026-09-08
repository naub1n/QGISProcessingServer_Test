from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess
import json


app = FastAPI(
    title="QGIS Processing Worker",
    version="1.0"
)


# Maximum 4 qgis_process simultanés
qgis_executor = ThreadPoolExecutor(max_workers=4)


class JobRequest(BaseModel):
    algorithm: str
    inputs: dict


class QGISExecutionError(Exception):

    def __init__(
        self,
        algorithm: str,
        returncode: int,
        message: str
    ):
        self.algorithm = algorithm
        self.returncode = returncode
        self.message = message

        super().__init__(message)
        
def clean_error_message(stderr: str, stdout: str) -> str:
    """
    Retourne le message d'erreur QGIS sans doublons.
    """

    message = stderr.strip()

    if not message:
        message = stdout.strip()

    if not message:
        return "qgis_process execution failed"

    # Déduplication des lignes identiques
    lines = message.splitlines()

    unique_lines = []
    seen = set()

    for line in lines:
        line = line.strip()

        if not line:
            continue

        if line not in seen:
            seen.add(line)
            unique_lines.append(line)

    return "\n".join(unique_lines)


def run_qgis_process(algorithm: str, inputs: dict):

    cmd = [
        "qgis_process",
        "run",
        algorithm,
        "-"
    ]

    payload = {
        "inputs": inputs
    }

    try:
        process = subprocess.run(
            cmd,
            input=json.dumps(payload),
            text=True,
            capture_output=True
        )

    except Exception as e:
        raise QGISExecutionError(
            algorithm=algorithm,
            returncode=-1,
            message=str(e)
        ) from e

    try:
        result = json.loads(process.stdout)
    except Exception:
        result = {
            "raw": process.stdout
        }

    if process.returncode != 0:

        # On privilégie stderr, qui contient l'erreur de qgis_process.
        # On ne renvoie pas stdout + stderr afin d'éviter les doublons.
        message = clean_error_message(
            process.stderr,
            process.stdout
        )

        raise QGISExecutionError(
            algorithm=algorithm,
            returncode=process.returncode,
            message=message
        )

    return result


@app.post("/execute")
def execute(request: JobRequest):

    try:

        future = qgis_executor.submit(
            run_qgis_process,
            request.algorithm,
            request.inputs
        )

        # Attend la fin du job.
        # Les 3 autres slots peuvent continuer à exécuter QGIS.
        result = future.result()

        return result

    except QGISExecutionError as e:

        # On transmet une erreur structurée à pygeoapi.
        # Le message est directement celui de qgis_process.
        raise HTTPException(
            status_code=500,
            detail={
                "message": e.message,
                "algorithm": e.algorithm,
                "returncode": e.returncode
            }
        ) from e

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail={
                "message": str(e)
            }
        ) from e

