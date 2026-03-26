#!/bin/bash
set -e

echo "🚀 Deploy trading-grid — $(date)"

# Pull la dernière image
docker compose pull

# Redémarrer avec la nouvelle image
docker compose up -d --remove-orphans

# Cleanup images inutilisées
docker image prune -f

echo "✅ Deploy terminé"
