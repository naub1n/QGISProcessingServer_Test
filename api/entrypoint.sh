#!/bin/sh
set -e

# Un process OGC par algorithme QGIS, décrit à partir de qgis_process.
# La config générée remplace PYGEOAPI_CONFIG pour la suite (openapi + uvicorn).
echo "Génération de la configuration pygeoapi..."
export PYGEOAPI_GENERATED_CONFIG="${PYGEOAPI_GENERATED_CONFIG:-/tmp/pygeoapi.generated.yml}"
python /qgis_processing_api/generate_config.py
export PYGEOAPI_CONFIG="$PYGEOAPI_GENERATED_CONFIG"

echo "Génération du fichier OpenAPI..."
pygeoapi openapi generate "$PYGEOAPI_CONFIG" --output-file "$PYGEOAPI_OPENAPI"

echo "Démarrage de l'application..."
exec uvicorn app:app --host 0.0.0.0 --port 8000
