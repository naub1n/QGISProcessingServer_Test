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

## Gestion des utilisateurs

L'API ne gère pas elle-même l'authentification : elle fait confiance aux
headers injectés par **Traefik** (middleware OIDC / forward-auth) placé
devant elle :

- `X-Forwarded-User` : identifiant de l'utilisateur authentifié ;
- `X-User-Groups` : groupes de l'utilisateur (séparés par des virgules).

L'API ne doit **jamais** être joignable autrement que via Traefik, sans
quoi ces headers pourraient être forgés par n'importe quel client. Un
exemple de configuration des labels Traefik (règle de routage, middleware
`oidc-base@file`, etc.) est fourni en commentaire dans
[docker-compose.yml](docker-compose.yml).

Ce que ça change concrètement :

- `GET /me` renvoie l'utilisateur courant (`user`, `groups`, `is_admin`).
- `/schedules` : chaque planification créée via `POST /schedules` est
  associée à son créateur (`owner`). Seul le propriétaire (ou un admin)
  peut la consulter, la (dés)activer, la supprimer ou la lancer
  manuellement ; `GET /schedules` ne liste que les planifications de
  l'utilisateur courant (toutes pour un admin).
- `/oapi/jobs` (pygeoapi) : même principe via un plugin de process manager
  (`UserTinyDBManager`, cf. [pygeoapi.yml](api/qgis_processing_api/pygeoapi/config/pygeoapi.yml))
  qui associe chaque job créé à son créateur et filtre `GET /oapi/jobs` en
  conséquence. Accéder au job d'un autre utilisateur renvoie une 404 (pour
  ne pas révéler son existence).
- Toute route de l'API (sauf `/`) exige un `X-Forwarded-User` et répond
  **401** en son absence.

### Désactiver la gestion des utilisateurs

Si l'API n'est pas (encore) placée derrière un Traefik avec OIDC, la
fonctionnalité peut être désactivée entièrement via la variable
d'environnement `AUTH_ENABLED` (voir [docker-compose.yml](docker-compose.yml)) :

```yaml
environment:
  AUTH_ENABLED: "false"
```

Dans ce mode : aucun header n'est requis, aucune restriction d'accès
n'est appliquée (équivalent à un accès administrateur), et le champ
`owner` reste vide sur les planifications et les jobs pygeoapi.

### Administrateurs

Deux variables d'environnement optionnelles permettent de désigner des
administrateurs, qui voient et gèrent les planifications/jobs de tous les
utilisateurs :

```yaml
environment:
  ADMIN_USERS: "alice,bob"
  ADMIN_GROUPS: "admins"
```

### Tester sans Traefik

En local, le header peut être simulé directement :

```bash
curl --location 'http://localhost:8080/me' \
--header 'X-Forwarded-User: alice'
```

### Stockage des jobs pygeoapi (TinyDB / PostgreSQL / MongoDB)

Le filtrage des jobs par propriétaire (`/oapi/jobs`) est implémenté pour
les trois process managers pygeoapi, choisis via `server.manager.name`
dans [pygeoapi.yml](api/qgis_processing_api/pygeoapi/config/pygeoapi.yml) :

| Manager                                    | Stockage par défaut | Dépendance supplémentaire |
|---------------------------------------------|----------------------|----------------------------|
| `user_tinydb_manager.UserTinyDBManager`     | fichier TinyDB (actif par défaut) | - |
| `user_postgresql_manager.UserPostgreSQLManager` | PostgreSQL | `psycopg2-binary` |
| `user_mongodb_manager.UserMongoDBManager`   | MongoDB | `pymongo` |

Les configurations PostgreSQL et MongoDB sont fournies en commentaire
dans `pygeoapi.yml` ; décommenter la dépendance correspondante dans
[requirements.txt](api/requirements.txt) avant de les utiliser.

Pour PostgreSQL, la table `jobs` créée par pygeoapi n'a pas nativement de
colonne `owner` : `UserPostgreSQLManager` l'ajoute automatiquement au
démarrage (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS owner`), que la
table soit nouvelle ou déjà existante.

⚠️ `MongoDBManager.delete_job()` et `.get_job_result()` ne passent pas par
`get_job()` dans pygeoapi (contrairement à TinyDB/PostgreSQL) : le plugin
`UserMongoDBManager` les surcharge explicitement pour que le contrôle
d'accès par propriétaire s'y applique aussi.

