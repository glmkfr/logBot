#!/usr/bin/env bash
# Déploiement du bot sur la VM : récupère origin/<branche> et ne reconstruit le
# conteneur QUE si un nouveau commit est présent (évite les redémarrages inutiles).
#
# Idempotent et utilisable à la main : `./scripts/deploy.sh`.
# Variables d'environnement facultatives :
#   DEPLOY_BRANCH  branche à déployer (défaut: main)
#
# Le script se positionne dans la racine du dépôt (parent de scripts/), donc il
# opère sur le dépôt de production où il se trouve, pas sur un autre checkout.
set -euo pipefail

cd "$(dirname "$0")/.."

BRANCH="${DEPLOY_BRANCH:-main}"

echo "[deploy] Récupération de origin/$BRANCH…"
git fetch --quiet origin "$BRANCH"

local_rev="$(git rev-parse HEAD)"
remote_rev="$(git rev-parse "origin/$BRANCH")"

if [ "$local_rev" = "$remote_rev" ]; then
  echo "[deploy] Déjà à jour ($local_rev) — aucune reconstruction."
  exit 0
fi

echo "[deploy] Mise à jour : ${local_rev:0:8} -> ${remote_rev:0:8}"
# --ff-only : refuse d'écraser d'éventuels commits locaux (la prod ne doit jamais
# diverger). En cas d'échec, on s'arrête au lieu de clobberer l'arbre de travail.
git merge --ff-only "origin/$BRANCH"

echo "[deploy] Reconstruction + redémarrage du conteneur…"
docker compose up -d --build

# Nettoyage best-effort des images orphelines laissées par le rebuild.
docker image prune -f >/dev/null 2>&1 || true

echo "[deploy] Terminé. État des services :"
docker compose ps
