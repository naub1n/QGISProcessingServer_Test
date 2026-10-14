from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess
import json
import os
import sys
import time
import uuid
import logging
import re
from xml.etree import ElementTree


# WORKER_DEBUG=true : trace les inputs/outputs de tous les jobs.
DEBUG = os.environ.get("WORKER_DEBUG", "false").strip().lower() in (
    "1", "true", "yes", "on"
)

# Taille maximale (caractères) des inputs/outputs/stderr écrits dans les logs.
LOG_MAX_CHARS = int(os.environ.get("WORKER_LOG_MAX_CHARS", "5000"))

# Dossier des modèles du profil QGIS (volume monté dans docker-compose).
MODELS_DIR = os.environ.get(
    "QGIS_MODELS_DIR",
    "/root/.local/share/QGIS/QGIS3/profiles/default/processing/models"
)

# Dossier des scripts du profil QGIS (voisin du dossier des modèles).
SCRIPTS_DIR = os.environ.get(
    "QGIS_SCRIPTS_DIR",
    os.path.join(os.path.dirname(MODELS_DIR.rstrip("/\\")), "scripts")
)

MODEL_NAME_RE =re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")

# Logger dédié avec son propre handler : indépendant de la configuration
# de logging d'uvicorn (qui ne configure pas le logger racine).
logger = logging.getLogger("qgis-worker")
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
logger.propagate = False

if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(_handler)

logger.info("Worker démarré (WORKER_DEBUG=%s)", DEBUG)


def to_log(value) -> str:
    """Sérialise une valeur pour les logs, tronquée à LOG_MAX_CHARS."""

    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = repr(value)

    if len(value) > LOG_MAX_CHARS:
        return f"{value[:LOG_MAX_CHARS]}... [tronqué, {len(value)} caractères]"

    return value


app = FastAPI(
    title="QGIS Processing Worker",
    version="1.0"
)


# Maximum 4 qgis_process simultanés
qgis_executor = ThreadPoolExecutor(max_workers=4)


class JobRequest(BaseModel):
    algorithm: str
    inputs: dict


class ModelRequest(BaseModel):
    name: str
    model3: str


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


def run_qgis_process(algorithm: str, inputs: dict, job_id: str = "-"):

    cmd = [
        "qgis_process",
        "run",
        algorithm,
        "-"
    ]

    payload = {
        "inputs": inputs
    }

    logger.info("[job %s] START algorithm=%s", job_id, algorithm)
    logger.debug("[job %s] inputs=%s", job_id, to_log(inputs))

    started = time.monotonic()

    try:
        process = subprocess.run(
            cmd,
            input=json.dumps(payload),
            text=True,
            capture_output=True
        )

    except Exception as e:
        logger.error(
            "[job %s] ERROR algorithm=%s duration=%.2fs "
            "(échec du lancement: %s)\n  inputs=%s",
            job_id, algorithm, time.monotonic() - started, e, to_log(inputs)
        )
        raise QGISExecutionError(
            algorithm=algorithm,
            returncode=-1,
            message=str(e)
        ) from e

    duration = time.monotonic() - started

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

        logger.error(
            "[job %s] ERROR algorithm=%s returncode=%s duration=%.2fs\n"
            "  message=%s\n  inputs=%s\n  stdout=%s\n  stderr=%s",
            job_id, algorithm, process.returncode, duration,
            to_log(message), to_log(inputs),
            to_log(process.stdout), to_log(process.stderr)
        )

        raise QGISExecutionError(
            algorithm=algorithm,
            returncode=process.returncode,
            message=message
        )

    logger.info(
        "[job %s] SUCCESS algorithm=%s duration=%.2fs",
        job_id, algorithm, duration
    )
    logger.debug("[job %s] outputs=%s", job_id, to_log(result))

    return result


def source_files_state(directory: str, extension: str) -> dict:
    """
    Date de dernière modification (la plus récente) et nombre des fichiers
    `extension` d'un dossier. Permet de savoir si les modèles ou scripts ont
    changé sans relire chaque algorithme (un fichier supprimé change `count`).
    """

    modified = 0.0
    count = 0

    for root, _dirs, files in os.walk(directory):
        for name in files:
            if not name.endswith(extension):
                continue
            try:
                modified = max(modified,
                               os.path.getmtime(os.path.join(root, name)))
            except OSError:
                continue
            count += 1

    return {"modified": modified, "count": count}


@app.get("/algorithms")
def list_algorithms():
    """
    Liste tous les algorithmes (et modèles) connus du worker, via
    `qgis_process list --json`.

    `sources` donne l'état des fichiers des providers `model` et `script`
    (dernière modification, nombre de fichiers).
    """

    try:
        process = qgis_executor.submit(
            subprocess.run,
            ["qgis_process", "list", "--json"],
            text=True,
            capture_output=True
        ).result()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"message": str(e)}
        ) from e

    if process.returncode != 0:
        logger.error("qgis_process list a échoué\n  stderr=%s",
                     to_log(process.stderr))
        raise HTTPException(
            status_code=500,
            detail={
                "message": "qgis_process list failed",
                "stderr": process.stderr
            }
        )

    try:
        providers = json.loads(process.stdout).get("providers", {})
    except ValueError as e:
        raise HTTPException(
            status_code=500,
            detail={"message": f"Invalid qgis_process output: {e}"}
        ) from e

    algorithms = []
    for provider_id, provider in providers.items():
        for algorithm_id, algorithm in provider.get("algorithms", {}).items():
            algorithms.append({
                "id": algorithm.get("id", algorithm_id),
                "name": algorithm.get("name"),
                "provider": provider_id,
                "group": algorithm.get("group"),
                "short_description": algorithm.get("short_description"),
            })

    return {
        "algorithms": algorithms,
        "count": len(algorithms),
        "sources": {
            "model": source_files_state(MODELS_DIR, ".model3"),
            "script": source_files_state(SCRIPTS_DIR, ".py"),
        }
    }


@app.get("/algorithms/{algorithm_id:path}")
def check_algorithm(algorithm_id: str, details: bool = False):
    """
    Vérifie que le worker connaît l'algorithme QGIS (ex. native:buffer,
    model:mon_modele). 200 si présent, 404 sinon.

    Avec `?details=true`, ajoute la clé `help` : la sortie JSON de
    `qgis_process help` (algorithm_details, parameters, outputs), utilisée
    pour décrire l'algorithme sous forme de process OGC.
    """

    try:
        process = qgis_executor.submit(
            subprocess.run,
            ["qgis_process", "help", algorithm_id, "--json"],
            text=True,
            capture_output=True
        ).result()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"message": str(e)}
        ) from e

    if process.returncode != 0:
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"Algorithm '{algorithm_id}' not found",
                "algorithm": algorithm_id
            }
        )

    response = {
        "algorithm": algorithm_id,
        "exists": True
    }

    if details:
        try:
            help_json = json.loads(process.stdout)
        except ValueError as e:
            raise HTTPException(
                status_code=500,
                detail={"message": f"Invalid qgis_process output: {e}"}
            ) from e

        # On écarte les versions de QGIS/GDAL/... et le détail du provider.
        response["help"] = {
            key: help_json.get(key)
            for key in ("algorithm_details", "parameters", "outputs")
        }

    return response


@app.post("/models", status_code=201)
def publish_model(request: ModelRequest):
    """
    Publie un modèle QGIS (.model3) dans le dossier des modèles du profil.
    Il devient disponible sous l'identifiant `model:<name>`.
    """

    if not MODEL_NAME_RE.match(request.name):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Invalid model name: use letters, digits, "
                           "'_', '-' or '.' only (max 100 characters)"
            }
        )

    try:
        root = ElementTree.fromstring(request.model3)
    except ElementTree.ParseError as e:
        raise HTTPException(
            status_code=400,
            detail={"message": f"Invalid model3 XML: {e}"}
        ) from e

    if root.tag != "Option":
        raise HTTPException(
            status_code=400,
            detail={"message": "Invalid model3 content: root element "
                               "must be <Option>"}
        )

    os.makedirs(MODELS_DIR, exist_ok=True)
    path = os.path.join(MODELS_DIR, f"{request.name}.model3")
    existed = os.path.exists(path)

    # Écriture atomique pour ne jamais exposer un modèle tronqué
    tmp_path = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(request.model3)
        os.replace(tmp_path, path)
    except OSError as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise HTTPException(
            status_code=500,
            detail={"message": f"Unable to write model: {e}"}
        ) from e

    logger.info("Modèle %s %s", request.name,
                "remplacé" if existed else "publié")

    return {
        "name": request.name,
        "algorithm": f"model:{request.name}",
        "replaced": existed
    }


@app.post("/execute")
def execute(request: JobRequest):

    job_id = uuid.uuid4().hex[:8]

    try:

        future = qgis_executor.submit(
            run_qgis_process,
            request.algorithm,
            request.inputs,
            job_id
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

        logger.exception(
            "[job %s] UNEXPECTED ERROR algorithm=%s\n  inputs=%s",
            job_id, request.algorithm, to_log(request.inputs)
        )

        raise HTTPException(
            status_code=500,
            detail={
                "message": str(e)
            }
        ) from e

