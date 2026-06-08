# Image du bot Discord Warcraft Logs.
FROM python:3.12-slim

# Évite les .pyc et force la sortie non bufferisée (logs en temps réel).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dépendances d'abord (meilleur cache des couches Docker).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code applicatif.
COPY bot/ ./bot/
COPY bot_logs.py .

# Utilisateur non-root + dossiers de données/logs accessibles.
RUN useradd --create-home --uid 1000 botlogs \
    && mkdir -p /app/data /app/logs \
    && chown -R botlogs:botlogs /app
USER botlogs

# La config arrive via l'environnement (.env / --env-file / EnvironmentFile).
CMD ["python", "-m", "bot"]
