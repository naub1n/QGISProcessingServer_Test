#!/bin/sh
set -e

echo "Génération du fichier OpenAPI..."
pygeoapi openapi generate "$PYGEOAPI_CONFIG" --output-file "$PYGEOAPI_OPENAPI"

echo "Démarrage de l'application..."
exec uvicorn app:app --host 0.0.0.0 --port 8000