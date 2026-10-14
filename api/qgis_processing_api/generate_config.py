"""
Génère la config pygeoapi avec un process OGC par algorithme QGIS.

Lancé par entrypoint.sh avant `pygeoapi openapi generate` :
  1. attend que le worker réponde, liste ses algorithmes (`GET /algorithms`) ;
  2. garde ceux qui correspondent à QGIS_PUBLISHED_ALGORITHMS ;
  3. récupère la description de chacun (`GET /algorithms/{id}?details=true`,
     soit `qgis_process help --json`) et la convertit en description de process
     OGC (qgis_algorithm.build_process_metadata) ;
  4. écrit la config de base + ces ressources dans PYGEOAPI_GENERATED_CONFIG.

Variables d'environnement :
  PYGEOAPI_CONFIG             config de base (obligatoire)
  PYGEOAPI_GENERATED_CONFIG   config générée (défaut /tmp/pygeoapi.generated.yml)
  QGIS_PUBLISHED_ALGORITHMS   motifs séparés par des virgules (fnmatch),
                              défaut "native:*,model:*,script:*"
  QGIS_PROCESS_CACHE          cache des descriptions (défaut
                              /data/qgis_processes_cache.json). Pour les
                              modèles et scripts, il retient aussi la date de
                              dernière modification de leurs fichiers (donnée
                              par le worker) : tant qu'elle ne change pas, ils
                              ne sont pas relus.
  QGIS_PROCESS_REFRESH        "true" pour ignorer le cache
  WORKER_URL                  défaut http://qgis-worker:8000
"""

import fnmatch
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests
import yaml

from qgis_algorithm import build_process_metadata

logging.basicConfig(level=logging.INFO, format="%(levelname)s generate_config: %(message)s")
LOGGER = logging.getLogger("generate_config")

WORKER_URL = os.environ.get("WORKER_URL", "http://qgis-worker:8000")
PATTERNS = [
    p.strip() for p in
    os.environ.get("QGIS_PUBLISHED_ALGORITHMS", "native:*,model:*,script:*").split(",")
    if p.strip()
]
CACHE_PATH = os.environ.get("QGIS_PROCESS_CACHE", "/data/qgis_processes_cache.json")
REFRESH = os.environ.get("QGIS_PROCESS_REFRESH", "false").strip().lower() in ("1", "true", "yes", "on")
OUTPUT_PATH = os.environ.get("PYGEOAPI_GENERATED_CONFIG", "/tmp/pygeoapi.generated.yml")

# Même plafond que le worker (4 qgis_process simultanés).
CONCURRENCY = 4
# Fournisseurs dont les algorithmes changent sans mise à jour de QGIS : leur
# cache est invalidé quand l'état de leurs fichiers (`sources`) change.
# Limite connue (POC) : l'invalidation se fait par provider, pas par
# algorithme : un seul modèle/script modifié fait relire tous les autres.
SOURCE_PROVIDERS = ("model", "script")


def wait_for_worker(timeout=180):
    """Liste (et état des sources) du worker, en attendant qu'il démarre."""

    deadline = time.monotonic() + timeout
    while True:
        try:
            response = requests.get(f"{WORKER_URL}/algorithms", timeout=60)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            if time.monotonic() > deadline:
                raise RuntimeError(f"Worker injoignable ({WORKER_URL}): {e}") from e
            LOGGER.info("Worker pas prêt (%s), nouvel essai...", e)
            time.sleep(3)


def load_cache():
    """Cache {"algorithms": {id: help}, "sources": {provider: état}}."""

    empty = {"algorithms": {}, "sources": {}}
    if REFRESH:
        return empty
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)
        return {"algorithms": cache["algorithms"], "sources": cache["sources"]}
    except (OSError, ValueError, KeyError, TypeError):
        return empty


def save_cache(cache):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError as e:
        LOGGER.warning("Cache non enregistré (%s): %s", CACHE_PATH, e)


def fetch_help(algorithm_id):
    response = requests.get(
        f"{WORKER_URL}/algorithms/{quote(algorithm_id, safe=':')}",
        params={"details": "true"},
        timeout=120
    )
    response.raise_for_status()
    return response.json()["help"]


def main():
    base_path = os.environ["PYGEOAPI_CONFIG"]
    with open(base_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    worker = wait_for_worker()
    sources = worker.get("sources", {})
    listed = {a["id"]: a for a in worker["algorithms"]}
    algorithms = [
        a for a in listed.values()
        if any(fnmatch.fnmatch(a["id"], p) for p in PATTERNS)
    ]
    LOGGER.info("%d algorithme(s) à publier (motifs: %s)", len(algorithms), PATTERNS)

    cache = load_cache()
    helps = {}
    to_fetch = []
    for a in algorithms:
        provider = a["provider"]
        # Modèles/scripts : le cache ne vaut que si leurs fichiers n'ont pas
        # changé depuis la dernière lecture.
        stale = (provider in SOURCE_PROVIDERS
                 and cache["sources"].get(provider) != sources.get(provider))
        if a["id"] in cache["algorithms"] and not stale:
            helps[a["id"]] = cache["algorithms"][a["id"]]
        else:
            to_fetch.append(a["id"])

    def fetch(algorithm_id):
        try:
            return algorithm_id, fetch_help(algorithm_id)
        except Exception as e:
            LOGGER.warning("%s ignoré: %s", algorithm_id, e)
            return algorithm_id, None

    LOGGER.info("%d description(s) lue(s) du cache, %d à demander au worker",
                len(helps), len(to_fetch))
    failed = set()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for algorithm_id, help_json in pool.map(fetch, to_fetch):
            if help_json is not None:
                helps[algorithm_id] = help_json
            else:
                failed.add(listed[algorithm_id]["provider"])

    # On ne retient l'état des fichiers d'un provider que si toutes ses
    # descriptions ont pu être lues, sinon la prochaine fois on réessaie.
    new_sources = dict(cache["sources"])
    for provider in SOURCE_PROVIDERS:
        if provider in sources and provider not in failed:
            new_sources[provider] = sources[provider]

    # Les algorithmes disparus du worker (modèle supprimé) sont retirés.
    kept = {k: v for k, v in {**cache["algorithms"], **helps}.items()
            if k in listed}
    save_cache({"algorithms": kept, "sources": new_sources})

    resources = config.setdefault("resources", {})
    for algorithm_id, help_json in sorted(helps.items()):
        resources[algorithm_id] = {
            "type": "process",
            "processor": {
                "name": "qgis_algorithm.QGISAlgorithmProcessor",
                "algorithm": algorithm_id,
                "worker_url": WORKER_URL,
                "metadata": build_process_metadata(algorithm_id, help_json),
            },
        }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)

    LOGGER.info("%d process écrits dans %s", len(helps), OUTPUT_PATH)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        LOGGER.error("%s", e)
        sys.exit(1)
