# bot_logs — Bot Discord Warcraft Logs (Mythique+ & Raid)

Bot Discord pour un petit serveur privé WoW. La commande `/logs <lien Warcraft Logs>`
crée automatiquement un fil par run dans un canal **Forum** : titre, tags,
embed enrichi, liens profonds Warcraft Logs et WoWAnalyzer. Anti-doublon,
statistiques, et boutons pour ajouter route/VoD a posteriori.

## Sommaire
- [Fonctionnalités](#fonctionnalités)
- [Architecture](#architecture)
- [Prérequis](#prérequis)
- [Installation](#installation)
- [Configuration (`.env`)](#configuration-env)
- [Enregistrer le client API Warcraft Logs](#enregistrer-le-client-api-warcraft-logs)
- [Inviter le bot & permissions](#inviter-le-bot--permissions)
- [Lancer le bot](#lancer-le-bot)
- [Commandes](#commandes)
- [Déploiement en service persistant](#déploiement-en-service-persistant)
- [Tests](#tests)
- [Hypothèses & points à vérifier](#hypothèses--points-à-vérifier)

## Fonctionnalités

- **`/logs`** : extrait le code du rapport, interroge l'API GraphQL v2 de
  Warcraft Logs, crée un fil de forum par run (gère plusieurs runs par rapport).
- **Fiabilité** : cache du jeton OAuth (rafraîchi avant expiration), retries
  avec back-off exponentiel (429/5xx), anti-doublon SQLite
  (`code de rapport + fight`), gestion propre des rapports sans clé.
- **Tags auto** : crée les tags manquants (donjon + statut Timé/Non timé).
- **Embed enrichi** (best-effort, dégradation propre) : composition, morts,
  affixes, temps, coffres, **iLvl moyen**.
- **Liens profonds** : vers le combat précis sur Warcraft Logs et vers
  WoWAnalyzer (rapport + combat ; sélection du joueur laissée à l'utilisateur).
- **Raid** : un lien WoWAnalyzer par boss, basé sur le pull représentatif
  (le kill, sinon le meilleur essai). Pas de mapping perso↔Discord.
- **Paramètres `route:` / `vod:`** sur `/logs`, et **boutons** qui **éditent
  l'embed** du fil pour y afficher la route / la VoD (persistées en base).
- **`/stats`** : nombre de clés, % timées, niveau moyen, **meilleure clé timée**,
  répartition par donjon et **tendance hebdomadaire** (mini-graphe 6 semaines).
- **`/leaderboard`** : meilleure clé Mythique+ **timée par donjon** (niveau record,
  meilleur temps à ce niveau, nombre de clés timées).
- **Auto-détection** : coller un lien Warcraft Logs dans un canal configuré
  (`AUTO_DETECT_CHANNEL_IDS`) crée les fils automatiquement, sans taper `/logs`.
- **Récap hebdomadaire automatique** : poste les stats de la semaine dans un
  canal configurable (jour/heure réglables).
- **Supervision & sauvegarde** : *heartbeat* + healthcheck Docker, et
  **sauvegarde SQLite quotidienne** avec rotation.
- **Sécurité** : restriction par rôles, validation du domaine `warcraftlogs.com`,
  logs sans secrets.

## Architecture

```
bot/
  config.py         Chargement config & secrets (.env), validation
  logging_setup.py  Journalisation + filtre anti-secret
  wcl.py            API Warcraft Logs (OAuth, GraphQL, cache jeton, retries)
  logic.py          Métier : extraction runs M+/raid, dédoublonnage, formatage
  links.py          Liens profonds WCL & WoWAnalyzer + validation d'URL
  db.py             SQLite (anti-doublon, stats)
  discord_app.py    Couche Discord (commandes, tags, embeds, boutons)
  __main__.py       Point d'entrée (python -m bot)
bot_logs.py         Lanceur de compatibilité (python bot_logs.py)
tests/              Tests parsing URL, extraction, DB
```

## Prérequis

- Python 3.10+
- Un bot Discord (token) et un serveur où vous avez les droits d'admin
- Un client API v2 Warcraft Logs (client_id / client_secret)

## Installation

> Sur macOS (Homebrew), les commandes s'appellent `python3` / `pip3` (il n'y a
> pas de `python`/`pip`). **Une fois le venv activé**, `python` et `pip` y
> deviennent disponibles.

```bash
python3 -m venv .venv
source .venv/bin/activate        # active le venv : `python`/`pip` deviennent dispo
pip install -r requirements.txt  # (ou pip3 si vous n'activez pas le venv)
cp .env.example .env             # puis éditez .env
```

Sans activer le venv, utilisez les binaires du venv directement :

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m bot
```

## Configuration (`.env`)

Toute la configuration passe par `.env` (jamais de secret en dur). Variables :

| Variable | Obligatoire | Description |
|---|---|---|
| `DISCORD_TOKEN` | oui | Jeton du bot Discord |
| `GUILD_ID` | oui | ID du serveur (clic droit > Copier l'ID, mode dev activé) |
| `FORUM_CHANNEL_ID` | oui | ID du canal **Forum** cible |
| `WCL_CLIENT_ID` | oui | Client ID API v2 Warcraft Logs |
| `WCL_CLIENT_SECRET` | oui | Client secret API v2 Warcraft Logs |
| `DEBUG` | non | `1` pour des logs verbeux (jamais de secret) |
| `MIN_KEY_LEVEL` | non | Niveau M+ minimum pour créer un fil (défaut 2) |
| `ALLOWED_ROLE_IDS` | non | IDs de rôles autorisés à `/logs`, séparés par des virgules. Vide = tout le monde |
| `DATABASE_PATH` | non | Chemin SQLite (défaut `data/bot_logs.db`) |
| `LOG_CHANNEL_ID` | non | Canal Discord où relayer les erreurs |
| `LOG_FILE` | non | Fichier de log (défaut `logs/bot.log`) |
| `WOWANALYZER_RAID_LINKS` | non | `1` = poster les liens WoWAnalyzer pour les raids |
| `RECAP_CHANNEL_ID` | non | Canal du récap hebdomadaire. Vide = récap désactivé |
| `RECAP_WEEKDAY` | non | Jour du récap : 0 = lundi … 6 = dimanche (défaut 0) |
| `RECAP_HOUR` | non | Heure locale du récap, 0–23 (défaut 10) |
| `AUTO_DETECT_CHANNEL_IDS` | non | IDs de canaux où un lien WCL collé crée les fils sans `/logs` (virgules). Vide = désactivé. **Requiert l'intent privilégié *Message Content*.** |
| `HEARTBEAT_FILE` | non | Fichier de battement de cœur lu par le healthcheck (défaut `data/heartbeat`) |
| `BACKUP_DIR` | non | Dossier des sauvegardes SQLite quotidiennes (défaut `data/backups`). Vide = désactivé |
| `BACKUP_KEEP` | non | Nombre de sauvegardes conservées par rotation (défaut 7) |

> ⚠️ **Sécurité** : si vous reprenez ce projet, les anciens secrets qui étaient
> codés en dur dans `bot_logs.py` doivent être **révoqués/régénérés**
> (token Discord et secret WCL), car ils ont pu fuiter.

## Enregistrer le client API Warcraft Logs

1. Connectez-vous sur https://www.warcraftlogs.com/api/clients/
2. Créez un client (« v2 API »). La *redirect URL* peut être quelconque
   (ex. `http://localhost`) : on utilise le flux *client credentials*.
3. Copiez le **Client ID** et le **Client Secret** dans `.env`.

## Inviter le bot & permissions

Dans le Developer Portal Discord (https://discord.com/developers/applications) :
- Onglet **Bot** : créez le bot, copiez le token.
- **Privileged Intents** : aucun intent privilégié n'est requis par défaut.
  ⚠️ **Exception** : si vous activez l'auto-détection (`AUTO_DETECT_CHANNEL_IDS`),
  cochez **Message Content Intent** (onglet *Bot* > *Privileged Gateway Intents*),
  sinon le bot ne pourra pas lire les liens collés dans le chat.
- **OAuth2 > URL Generator** : scopes `bot` + `applications.commands`.
- Permissions du bot dans le canal Forum :
  - *View Channel*, *Send Messages*, *Create Posts* (threads),
    *Send Messages in Threads*, *Manage Threads*,
    *Manage Channels* ou *Manage Posts* (pour **créer les tags**),
    *Embed Links*.

## Lancer le bot

Avec le venv activé (`source .venv/bin/activate`) :

```bash
python -m bot
# ou (compatibilité)
python bot_logs.py
```

Sans activer le venv (macOS/Homebrew) :

```bash
.venv/bin/python -m bot
```

Les slash-commands sont synchronisées sur le `GUILD_ID` au démarrage
(disponibles immédiatement, sans attendre la propagation globale).

## Commandes

- `/logs lien:<url> [niveau_min:<n>] [route:<url>] [vod:<url>]` — crée le(s)
  fil(s) du rapport. `niveau_min` ne publie que les clés `>= n` (défaut :
  `MIN_KEY_LEVEL` du `.env`). Ex. `niveau_min:15` ⇒ uniquement les +15 et plus.
- `/stats [periode: semaine|mois|tout]` — statistiques M+ (avec meilleure clé
  timée et, sur la vue globale, la tendance des 6 dernières semaines).
- `/leaderboard` — meilleure clé timée par donjon (niveau record + meilleur temps).

**Auto-détection** : si `AUTO_DETECT_CHANNEL_IDS` est renseigné, coller un lien
`warcraftlogs.com/reports/...` dans l'un de ces canaux déclenche le même
traitement que `/logs` (mêmes restrictions de rôle, anti-doublon inclus).

Sous chaque fil créé : boutons **« Ajouter la route »** / **« Ajouter la VoD »**
(modale → message posté dans le fil). Les boutons sont persistants (survivent
au redémarrage).

## Déploiement en service persistant

### systemd

`/etc/systemd/system/bot_logs.service` :

```ini
[Unit]
Description=Bot Discord Warcraft Logs
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/bot_logs
ExecStart=/opt/bot_logs/.venv/bin/python -m bot
EnvironmentFile=/opt/bot_logs/.env
Restart=always
RestartSec=5
User=botlogs

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bot_logs
journalctl -u bot_logs -f
```

### pm2

```bash
pm2 start "python -m bot" --name bot_logs --cwd /opt/bot_logs
pm2 save && pm2 startup
```

### Docker

Un `Dockerfile` et un `docker-compose.yml` sont fournis (utilisateur non-root,
SQLite et logs persistés via volumes).

> **Permissions des volumes** : les dossiers montés (`./data`, `./logs`) doivent
> appartenir à l'uid du conteneur (`1000`), sinon le bot ne peut pas écrire
> (`PermissionError`). Sur l'hôte : `sudo chown -R 1000:1000 ./data ./logs`.

**Healthcheck** : le conteneur expose un healthcheck (`python -m bot.healthcheck`)
qui vérifie un fichier de *heartbeat* touché chaque minute tant que la passerelle
Discord est saine. `docker ps` affiche alors `healthy` / `unhealthy`. Pour
**redémarrer automatiquement** sur `unhealthy`, ajoutez un conteneur autoheal :

```yaml
  autoheal:
    image: willfarrell/autoheal
    restart: always
    environment: { AUTOHEAL_CONTAINER_LABEL: all }
    volumes: ["/var/run/docker.sock:/var/run/docker.sock"]
```

**Sauvegardes** : une copie compacte (`VACUUM INTO`) de la base est écrite chaque
jour dans `BACKUP_DIR` (défaut `data/backups`, persisté via le volume `./data`),
avec rotation sur les `BACKUP_KEEP` dernières (défaut 7).

```bash
# Avec docker compose (recommandé) : lit .env, monte ./data et ./logs
docker compose up -d --build
docker compose logs -f

# Ou en image seule
docker build -t bot_logs .
docker run -d --restart=always --env-file .env \
  -v "$PWD/data:/app/data" -v "$PWD/logs:/app/logs" bot_logs
```

### Déploiement continu (self-hosted runner)

Le déploiement sur la VM est automatisé : à chaque **CI réussie sur `main`**, le
workflow `Deploy` (`.github/workflows/deploy.yml`) exécute `scripts/deploy.sh`,
qui **pull puis reconstruit le conteneur uniquement s'il y a un nouveau commit**.

**Mise en place (une fois) sur la VM :**

1. **Installer le runner self-hosted** : dans GitHub → *Settings → Actions →
   Runners → New self-hosted runner*, suivre les commandes affichées sur la VM.
   - Ajouter le **label** `bot_logs` (`./config.sh --labels bot_logs …`).
   - L'installer **en service** pour qu'il survive aux reboots :
     `sudo ./svc.sh install && sudo ./svc.sh start`.
2. **Pré-requis du compte du runner** : appartenir au groupe `docker` (pour
   `docker compose` sans sudo) et avoir accès en lecture au dépôt git de prod.
3. **Indiquer le chemin de prod** : GitHub → *Settings → Secrets and variables →
   Actions → Variables* → créer `DEPLOY_PATH` = chemin du dépôt sur la VM
   (ex. `/opt/bot_logs`). À défaut, `~/bot_logs` est utilisé.

Ensuite, chaque merge sur `main` déclenche le déploiement. Déclenchement manuel
possible via *Actions → Deploy → Run workflow*. Le script reste utilisable à la
main sur la VM : `./scripts/deploy.sh`.

> Le déploiement est en *fast-forward only* : si l'arbre de prod a divergé
> (commits/édits locaux), le script s'arrête au lieu d'écraser — corrige l'état
> de la VM puis relance.

## Tests

```bash
pip install -r requirements-dev.txt   # ou: pip install pytest
pytest -q
```

Une **intégration continue** GitHub Actions (`.github/workflows/ci.yml`) lance
les tests (Python 3.10 et 3.12) et vérifie le build Docker à chaque push/PR.

Les tests couvrent le parsing d'URL, la validation de domaine, l'extraction M+/raid, la
désambiguïsation des titres, le formatage et l'anti-doublon SQLite.

## Hypothèses & points à vérifier

Voir [`ASSUMPTIONS.md`](ASSUMPTIONS.md).
