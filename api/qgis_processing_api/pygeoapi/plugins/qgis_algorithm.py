"""
Un process OGC par algorithme QGIS.

La description du process (inputs / outputs, JSON Schema) est déduite de la
sortie de `qgis_process help <algo> --json`, via build_process_metadata().
Aucun paramétrage d'algorithme n'est écrit en dur : un nouveau script ou
modèle est décrit automatiquement.

Ce module est utilisé :
  - par generate_config.py (au démarrage), qui interroge le worker et embarque
    la description dans la config pygeoapi générée ;
  - par pygeoapi, qui instancie QGISAlgorithmProcessor pour chaque algorithme.
"""

import requests

from pygeoapi.process.base import BaseProcessor

WORKER_URL = "http://qgis-worker:8000"

# Bornes "infinies" renvoyées par QGIS (±DBL_MAX) : on ne les publie pas.
_INFINITE = 1e300

# Types QGIS (type.id) dont la valeur est une simple chaîne.
_STRING_TYPES = {
    "string", "expression", "crs", "authcfg", "file", "folder", "color",
    "extent", "point", "geometry", "layout", "layoutitem", "maptheme",
    "coordinateoperation", "providerconnection", "databaseschema",
    "databasetable", "source", "vector", "raster", "layer", "mesh",
    "pointcloud", "annotation", "vectortile", "sink", "field",
}
_NUMBER_TYPES = {"number", "distance", "area", "volume", "duration", "scale"}
_ARRAY_TYPES = {"multilayer", "matrix", "aggregates", "fieldmapping"}


def _finite(value):
    return isinstance(value, (int, float)) and abs(value) < _INFINITE


def _enum_schema(parameter, raw):
    """Schéma d'un enum, à partir de `available_options` ({"0": "Round"})."""

    options = parameter.get("available_options") or {}

    if raw.get("uses_static_strings"):
        values = [(label, label) for label in raw.get("options", [])]
    else:
        values = [(int(key), label) for key, label in options.items()]
        values.sort(key=lambda item: item[0])

    if values:
        schema = {
            "oneOf": [{"const": value, "title": label}
                      for value, label in values]
        }
    else:
        schema = {"type": "string" if raw.get("uses_static_strings")
                  else "integer"}

    if raw.get("allow_multiple"):
        return {"type": "array", "items": schema}

    return schema


def _schema_for(parameter):
    """JSON Schema d'un paramètre, déduit de sa définition qgis_process."""

    raw = parameter.get("raw_definition") or {}
    type_info = parameter.get("type") or {}
    type_id = type_info.get("id", raw.get("parameter_type", ""))

    if parameter.get("is_destination"):
        schema = {"type": "string"}
    elif type_id == "boolean":
        schema = {"type": "boolean"}
    elif type_id == "enum":
        schema = _enum_schema(parameter, raw)
    elif type_id in _NUMBER_TYPES:
        # data_type 0 = entier, 1 = réel (Qgis.ProcessingNumberParameterType)
        integer = type_id == "number" and raw.get("data_type") == 0
        schema = {"type": "integer" if integer else "number"}
        if _finite(raw.get("min")):
            schema["minimum"] = raw["min"]
        if _finite(raw.get("max")):
            schema["maximum"] = raw["max"]
    elif type_id == "band":
        schema = {"type": "integer", "minimum": 1}
    elif type_id == "range":
        schema = {"type": "array", "items": {"type": "number"},
                  "minItems": 2, "maxItems": 2}
    elif type_id in ("datetime", "date", "time"):
        schema = {"type": "string",
                  "format": {"datetime": "date-time"}.get(type_id, type_id)}
    elif type_id in _ARRAY_TYPES or (
            type_id == "field" and raw.get("allow_multiple")):
        schema = {"type": "array"}
        if type_id in ("multilayer", "field"):
            schema["items"] = {"type": "string"}
    elif type_id in _STRING_TYPES:
        schema = {"type": "string"}
    else:
        # Type inconnu (ajouté par une future version de QGIS) : on
        # n'impose rien plutôt que de bloquer l'algorithme.
        schema = {}

    default = parameter.get("default_value")
    if default is not None:
        schema["default"] = default

    schema["x-qgis"] = {"type": type_id}
    if parameter.get("is_advanced"):
        schema["x-qgis"]["advanced"] = True

    return schema


def _output_schema(output):
    """JSON Schema d'une sortie (`outputVector`, `outputNumber`, ...)."""

    output_type = output.get("type", "")

    if output_type == "outputNumber":
        schema = {"type": "number"}
    elif output_type == "outputBoolean":
        schema = {"type": "boolean"}
    elif output_type == "outputMultilayer":
        schema = {"type": "array", "items": {"type": "string"}}
    else:
        # Fichiers, couches, dossiers, html... : chemin dans le worker.
        # Pas de contentMediaType ici : pygeoapi l'utiliserait comme type de
        # réponse de toute l'exécution.
        schema = {"type": "string"}

    schema["x-qgis"] = {"type": output_type}

    return schema


def build_process_metadata(algorithm_id, help_json):
    """
    Construit la description OGC d'un process à partir du JSON de
    `qgis_process help` ({"algorithm_details", "parameters", "outputs"}).
    """

    details = help_json.get("algorithm_details") or {}
    title = details.get("name") or algorithm_id
    description = details.get("short_description") or title

    keywords = ["qgis", algorithm_id.split(":", 1)[0]]
    if details.get("group"):
        keywords.append(details["group"])
    keywords.extend(details.get("tags") or [])

    inputs = {}
    for name, parameter in (help_json.get("parameters") or {}).items():
        type_info = parameter.get("type") or {}
        accepted = type_info.get("acceptable_values") or []

        text = parameter.get("help") or parameter.get("description") or name
        if accepted and not parameter.get("is_destination"):
            text = f"{text} (accepted: {'; '.join(accepted)})"

        # Obligatoire si ni optionnel ni muni d'une valeur par défaut
        # (qgis_process applique les défauts aux paramètres omis).
        required = (not parameter.get("optional")
                    and parameter.get("default_value") is None)

        inputs[name] = {
            "title": parameter.get("description") or name,
            "description": text,
            "schema": _schema_for(parameter),
            "minOccurs": 1 if required else 0,
            "maxOccurs": 1,
        }

    outputs = {}
    for name, output in (help_json.get("outputs") or {}).items():
        outputs[name] = {
            "title": output.get("description") or name,
            "schema": _output_schema(output),
        }

    return {
        "version": "1.0.0",
        "id": algorithm_id,
        "title": title,
        "description": description,
        "keywords": keywords,
        "jobControlOptions": ["sync-execute", "async-execute"],
        "inputs": inputs,
        "outputs": outputs,
    }


def _error_message(response):
    """Message d'erreur lisible à partir d'une réponse d'erreur du worker."""

    try:
        error = response.json()
        detail = error.get("detail", error)

        if isinstance(detail, dict):
            for key in ("message", "stderr", "stdout"):
                message = (detail.get(key) or "").strip()
                if message:
                    return message
            return str(detail)

        return str(detail)

    except Exception:
        return response.text.strip()


class QGISAlgorithmProcessor(BaseProcessor):
    """
    Exécute l'algorithme QGIS `processor_def["algorithm"]` sur le worker.
    La description du process est fournie par `processor_def["metadata"]`
    (générée par generate_config.py).
    """

    def __init__(self, processor_def):
        super().__init__(processor_def, processor_def["metadata"])

        self.algorithm = processor_def["algorithm"]
        self.worker_url = processor_def.get("worker_url", WORKER_URL)

    def execute(self, data, outputs=None):

        try:
            response = requests.post(
                f"{self.worker_url}/execute",
                json={"algorithm": self.algorithm, "inputs": data},
                timeout=None
            )
            response.raise_for_status()

        except requests.HTTPError:
            raise RuntimeError(_error_message(response)) from None

        # On ne renvoie que les sorties de l'algorithme : un process OGC
        # décrit ses `outputs`, pas l'enveloppe de qgis_process.
        results = response.json().get("results", {})

        return "application/json", results
