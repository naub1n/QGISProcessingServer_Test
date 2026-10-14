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

### Vérifier le contenu du worker

Les algorithmes disponibles se consultent via l'API OGC Processes (voir
« Un process OGC par algorithme » plus bas) : `GET /oapi/processes` les liste et
`GET /oapi/processes/{id}` (404 si inconnu) donne leur description détaillée. Seuls les
algorithmes publiés (`QGIS_PUBLISHED_ALGORITHMS`) y figurent.

```bash
curl 'http://localhost:8080/oapi/processes' -H 'X-Forwarded-User: alice'
curl 'http://localhost:8080/oapi/processes/native:buffer' -H 'X-Forwarded-User: alice'
curl 'http://localhost:8080/oapi/processes/model:ma_procedure' -H 'X-Forwarded-User: alice'
```

### Publier un modèle

`POST /oapi/worker/models` dépose un modèle `.model3` dans le dossier des
modèles du worker (écrase un modèle de même nom). Il est ensuite utilisable
via l'identifiant `model:<name>` ; il apparaît dans `/oapi/processes` après un
redémarrage du conteneur `pygeoapi`. Réponses : 201 (publié), 400 (nom ou XML
invalide), 502 (worker injoignable).

```bash
curl -X POST 'http://localhost:8080/oapi/worker/models' \
  -H 'X-Forwarded-User: alice' -H 'Content-Type: application/json' \
  -d "$(jq -n --rawfile m ma_procedure.model3 '{name:"ma_procedure", model3:$m}')"
```

### Un process OGC par algorithme (prototype)

Au démarrage, l'API interroge le worker et publie chaque algorithme comme un
process OGC : `/oapi/processes/native:buffer`, `/oapi/processes/model:ma_procedure`...
Les `inputs` et `outputs` (types, valeurs par défaut, énumérations, bornes,
obligatoire/optionnel) sont déduits de `qgis_process help --json` : rien n'est
écrit en dur par algorithme, un nouveau script ou modèle est décrit
automatiquement.

```bash
curl 'http://localhost:8080/oapi/processes/native:buffer'
curl -X POST 'http://localhost:8080/oapi/processes/native:buffer/execution' \
  -H 'X-Forwarded-User: alice' -H 'Content-Type: application/json' \
  -d '{"inputs": {"INPUT": "/data/communes.gpkg", "DISTANCE": 100, "OUTPUT": "/data/buffer.gpkg"}}'
```

Les paramètres envoyés au démarrage d'un job sont conservés : `GET /oapi/jobs/{id}`
renvoie les clés de la requête d'exécution OGC (`inputs`, `outputs`, `response`,
`subscriber`, `null` si absentes ; `response` vaut `raw` par défaut). Les jobs
créés avant cette fonctionnalité renvoient des `null`.

```bash
curl 'http://localhost:8080/oapi/jobs/<jobID>' -H 'X-Forwarded-User: alice'
```

- `QGIS_PUBLISHED_ALGORITHMS` (docker-compose) : motifs `fnmatch` séparés par des
  virgules (défaut `native:*,model:*,script:*`). Chaque algorithme coûte un
  `qgis_process help` (quelques secondes) à la première génération.
- Les descriptions sont mises en cache dans `/data/qgis_processes_cache.json`
  (`QGIS_PROCESS_REFRESH=true` pour l'ignorer) : seuls les algorithmes absents du
  cache sont demandés au worker. Pour les modèles et scripts, le cache retient aussi
  la date de dernière modification de leurs fichiers (donnée par le worker dans
  `GET /algorithms`, clé `sources`) ; si elle change, ils sont relus.
- **Limite identifiée de ce POC** : cette détection se fait par provider (`model`,
  `script`), pas par algorithme. Dès qu'un seul modèle ou script est ajouté,
  modifié ou supprimé, *tous* les modèles (ou tous les scripts) sont relus avec
  `qgis_process help`, ce qui peut devenir long avec de nombreux modèles et scripts
  en entreprise. Piste : pour les modèles, `QgsProcessingModelAlgorithm.sourceFilePath()`
  donne le fichier de chaque algorithme, donc sa date de modification ; il n'existe
  pas d'équivalent connu pour les scripts, à étudier.
- **Piste d'amélioration** : faire évoluer `qgis_process` lui-même pour qu'il renvoie
  la description de tous les algorithmes en un seul appel (par exemple
  `qgis_process --json help all`, au même format que `help <algorithme>`). Un seul
  démarrage de QGIS (~4 s) remplacerait les ~4 s par algorithme actuels, pour les
  algorithmes natifs comme pour les modèles et scripts, sans reconstruire la
  description côté API. Cela suppose une contribution à QGIS (ou un `qgis_process`
  patché dans l'image du worker) : cette commande n'existe pas aujourd'hui.
- `GET /oapi/processes` est paginé par `server.limits` dans `pygeoapi.yml`
  (`default_items` / `max_items`, ici 1500). pygeoapi 0.24 n'a ni `offset` ni lien
  `next` : au-delà de `max_items` des process sont inaccessibles dans la liste, et
  supprimer le bloc `limits` ramènerait la liste à 10 process. Ces limites valent
  aussi pour `/oapi/jobs`.
- Les identifiants des modèles viennent de leur nom interne : évitez espaces et
  accents (`model:Importer un référentiel`), qui donnent des URL fragiles.
- Les algorithmes ajoutés après le démarrage (nouveau modèle publié) n'apparaissent
  qu'après un redémarrage du conteneur `pygeoapi`.
- Les sorties fichiers (`OUTPUT`) sont des chemins **dans le worker** : le résultat
  ne contient pas encore le fichier lui-même.
- L'ancien process générique `qgis` (`{"algorithm", "parameters"}`) reste disponible.

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
colonnes `owner` et `parameters` : `UserPostgreSQLManager` les ajoute
automatiquement au démarrage (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`), que
la table soit nouvelle ou déjà existante.

⚠️ `MongoDBManager.delete_job()` et `.get_job_result()` ne passent pas par
`get_job()` dans pygeoapi (contrairement à TinyDB/PostgreSQL) : le plugin
`UserMongoDBManager` les surcharge explicitement pour que le contrôle
d'accès par propriétaire s'y applique aussi.

