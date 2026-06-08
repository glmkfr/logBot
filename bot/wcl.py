"""Couche API Warcraft Logs (GraphQL v2).

Responsabilités :
- Authentification OAuth2 *client credentials* avec **cache du jeton** et
  rafraîchissement automatique avant expiration.
- Requêtes GraphQL pour les rapports (M+ et raid).
- **Retries avec back-off exponentiel** sur les limites de débit (HTTP 429)
  et les erreurs transitoires (5xx, coupures réseau).

Aucun secret n'est journalisé. Les requêtes sont volontairement défensives :
si un champ est absent côté API, la couche métier (logic.py) dégrade proprement.

⚠️ Schéma GraphQL : les noms de champs (keystoneLevel, keystoneBonus,
keystoneTime, keystoneAffixes, kill, encounterID, gameZone, friendlyPlayers,
masterData.actors) ont été vérifiés sur la doc officielle v2
(https://www.warcraftlogs.com/v2-api-docs/warcraft/reportfight.doc.html).
Voir ASSUMPTIONS.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("bot_logs.wcl")

WCL_TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
WCL_API_URL = "https://www.warcraftlogs.com/api/v2/client"

# Requête principale : tout ce dont on a besoin pour M+ et raid en un appel.
# `masterData.actors` sert à reconstituer la composition (subType = classe).
REPORT_QUERY = """
query($code: String!) {
  reportData {
    report(code: $code) {
      code
      startTime
      endTime
      title
      zone { id name }
      masterData {
        actors(type: "Player") {
          id
          name
          subType
        }
      }
      fights {
        id
        name
        encounterID
        difficulty
        kill
        fightPercentage
        startTime
        endTime
        keystoneLevel
        keystoneTime
        keystoneBonus
        keystoneAffixes
        averageItemLevel
        gameZone { id name }
        friendlyPlayers
      }
    }
  }
}
"""

# Requête séparée pour le nombre de morts d'un combat (best-effort).
# `table(dataType: Deaths)` renvoie un JSON dont on compte les entrées.
DEATHS_QUERY = """
query($code: String!, $fightID: Int!) {
  reportData {
    report(code: $code) {
      table(dataType: Deaths, fightIDs: [$fightID])
    }
  }
}
"""


class WCLError(RuntimeError):
    """Erreur renvoyée par l'API Warcraft Logs (après épuisement des retries)."""


@dataclass
class _CachedToken:
    value: str
    expires_at: float  # timestamp epoch (secondes)


class WarcraftLogsClient:
    """Client asynchrone Warcraft Logs avec cache de jeton et retries.

    À utiliser comme gestionnaire de contexte asynchrone :

        async with WarcraftLogsClient(cid, secret) as wcl:
            report = await wcl.fetch_report(code)
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        max_retries: int = 4,
        base_backoff: float = 1.0,
        token_safety_margin: float = 60.0,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._token_safety_margin = token_safety_margin

        self._session: aiohttp.ClientSession | None = None
        self._token: _CachedToken | None = None
        self._token_lock = asyncio.Lock()

    async def __aenter__(self) -> "WarcraftLogsClient":
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- Jeton OAuth ----------------------------------------------------------

    async def _get_token(self) -> str:
        """Retourne un jeton valide, en réutilisant le cache si possible."""
        async with self._token_lock:
            now = time.time()
            if self._token and self._token.expires_at - self._token_safety_margin > now:
                return self._token.value

            log.debug("Demande d'un nouveau jeton OAuth Warcraft Logs.")
            data = await self._request_token()
            access = data["access_token"]
            # expires_in en secondes (≈ 1 an pour client_credentials WCL).
            expires_in = float(data.get("expires_in", 3600))
            self._token = _CachedToken(value=access, expires_at=now + expires_in)
            return access

    async def _request_token(self) -> dict:
        assert self._session is not None
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with self._session.post(
                    WCL_TOKEN_URL,
                    data={"grant_type": "client_credentials"},
                    auth=aiohttp.BasicAuth(self._client_id, self._client_secret),
                ) as resp:
                    if resp.status in (429, 500, 502, 503, 504):
                        await self._sleep_for_retry(resp, attempt)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientError as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    break
                await self._sleep_backoff(attempt)
        raise WCLError(
            "Échec d'authentification auprès de Warcraft Logs "
            f"(vérifiez WCL_CLIENT_ID / WCL_CLIENT_SECRET). Cause: {last_exc}"
        )

    # -- Requêtes GraphQL -----------------------------------------------------

    async def _graphql(self, query: str, variables: dict) -> dict:
        """Exécute une requête GraphQL avec retries/back-off et renvoie `data`."""
        assert self._session is not None
        last_exc: Exception | None = None

        for attempt in range(self._max_retries + 1):
            token = await self._get_token()
            headers = {"Authorization": f"Bearer {token}"}
            try:
                async with self._session.post(
                    WCL_API_URL,
                    json={"query": query, "variables": variables},
                    headers=headers,
                ) as resp:
                    if resp.status == 401:
                        # Jeton invalide/expiré : on l'invalide et on retente.
                        log.warning("401 reçu : invalidation du jeton et nouvel essai.")
                        self._token = None
                        if attempt < self._max_retries:
                            await self._sleep_backoff(attempt)
                            continue
                    if resp.status in (429, 500, 502, 503, 504):
                        await self._sleep_for_retry(resp, attempt)
                        continue
                    resp.raise_for_status()
                    payload = await resp.json()
            except aiohttp.ClientError as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    break
                await self._sleep_backoff(attempt)
                continue

            if payload.get("errors"):
                # Erreur GraphQL applicative : on n'expose pas la requête brute.
                messages = "; ".join(
                    e.get("message", str(e)) for e in payload["errors"]
                )
                raise WCLError(f"Erreur GraphQL Warcraft Logs : {messages}")

            return payload.get("data") or {}

        raise WCLError(
            f"Warcraft Logs indisponible après {self._max_retries + 1} tentatives. "
            f"Cause: {last_exc}"
        )

    async def fetch_report(self, code: str) -> dict | None:
        """Récupère un rapport complet. Retourne None si le rapport est introuvable."""
        data = await self._graphql(REPORT_QUERY, {"code": code})
        report = (data.get("reportData") or {}).get("report")
        if report is None:
            log.info("Rapport introuvable ou privé : code=%s", code)
        return report

    async def fetch_death_count(self, code: str, fight_id: int) -> int | None:
        """Compte les morts d'un combat (best-effort). None si indisponible."""
        try:
            data = await self._graphql(
                DEATHS_QUERY, {"code": code, "fightID": fight_id}
            )
            table = (
                (data.get("reportData") or {})
                .get("report", {})
                .get("table")
            )
            if not table:
                return None
            entries = table.get("data", {}).get("entries", []) if isinstance(
                table, dict
            ) else []
            return len(entries)
        except Exception as exc:  # noqa: BLE001 — best-effort, on dégrade.
            log.debug("Comptage des morts indisponible (fight=%s): %s", fight_id, exc)
            return None

    # -- Helpers retry --------------------------------------------------------

    async def _sleep_for_retry(self, resp: aiohttp.ClientResponse, attempt: int) -> None:
        """Respecte l'en-tête Retry-After si présent, sinon back-off exponentiel."""
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
                log.warning("Limite de débit WCL : pause de %.1fs (Retry-After).", delay)
                await asyncio.sleep(delay)
                return
            except ValueError:
                pass
        await self._sleep_backoff(attempt)

    async def _sleep_backoff(self, attempt: int) -> None:
        # Back-off exponentiel borné (1s, 2s, 4s, 8s, … max 30s).
        delay = min(self._base_backoff * (2 ** attempt), 30.0)
        log.warning("Nouvelle tentative dans %.1fs (essai %d).", delay, attempt + 1)
        await asyncio.sleep(delay)
