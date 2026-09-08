# QGIS Processing Server - Test

## Utilisation

### Planifier un job

```
curl --location --request POST 'http://localhost:8080/schedules' \
--header 'Content-Type: application/json' \
--data '{
    "id": "test",
    "process_id": "qgis",
    "trigger": "cron",
    "trigger_args": {
      "minute": "*/2"
    },
    "inputs": {
        "algorithm":"native:httprequest",
        "parameters":
        { 
            "AUTH_CONFIG" : "", 
            "DATA" : "", 
            "FAIL_ON_ERROR" : true, 
            "METHOD" : 0, 
            "URL" : "https://qgis.github.io/qgis-uni-navigation/logo.svg" }
    },
    "enabled": true
  }'
```

### Lister les planification

`curl --location 'http://localhost:8080/schedules'
`
### Arrêter une planification

`curl --location --request POST 'http://localhost:8080/schedules/test/disable'
`
### Accéder à l'interface pygeoapi

http://localhost:8080/oapi/

