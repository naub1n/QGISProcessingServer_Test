#!/bin/sh
set -e

# Un process OGC par algorithme QGIS, décrit à partir de qgis_process.
# La config générée remplace PYGEOAPI_CONFIG pour la suite (openapi + uvicorn).
export PYGEOAPI_GENERATED_CONFIG="${PYGEOAPI_GENERATED_CONFIG:-/tmp/pygeoapi.generated.yml}"

# Fichier témoin créé par l'application (restart.py) pour demander un
# redémarrage : uvicorn est alors relancé, config régénérée, sans arrêter le
# conteneur.
export RESTART_FLAG="${RESTART_FLAG:-/tmp/restart_requested}"

child=""
stopping=0

# docker stop envoie SIGTERM à ce script (PID 1) : on le transmet à uvicorn
# et on quitte au lieu de redémarrer.
on_signal() {
    stopping=1
    if [ -n "$child" ]; then
        kill -TERM "$child" 2>/dev/null || true
    fi
}
trap on_signal TERM INT

while true; do
    # Signal reçu pendant la génération de config : ne pas relancer l'appli.
    if [ "$stopping" -eq 1 ]; then
        exit 0
    fi

    rm -f "$RESTART_FLAG"

    echo "Génération de la configuration pygeoapi..."
    if ! python /qgis_processing_api/generate_config.py; then
        # Lors d'un redémarrage, mieux vaut republier la config précédente
        # que laisser le service arrêté (worker momentanément indisponible).
        if [ -f "$PYGEOAPI_GENERATED_CONFIG" ]; then
            echo "Génération échouée, réutilisation de la configuration précédente"
        else
            exit 1
        fi
    fi
    export PYGEOAPI_CONFIG="$PYGEOAPI_GENERATED_CONFIG"

    echo "Génération du fichier OpenAPI..."
    pygeoapi openapi generate "$PYGEOAPI_CONFIG" --output-file "$PYGEOAPI_OPENAPI"

    echo "Démarrage de l'application..."
    uvicorn app:app --host 0.0.0.0 --port 8000 &
    child=$!

    if [ "$stopping" -eq 1 ]; then
        kill -TERM "$child" 2>/dev/null || true
    fi

    code=0
    wait "$child" || code=$?

    # Un signal reçu interrompt le premier wait : uvicorn n'est pas encore
    # arrêté, on attend sa vraie fin.
    if [ "$stopping" -eq 1 ]; then
        code=0
        wait "$child" || code=$?
        exit "$code"
    fi

    child=""

    if [ -f "$RESTART_FLAG" ]; then
        echo "Redémarrage demandé par l'application"
        continue
    fi

    exit "$code"
done
