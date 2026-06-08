# Hypothèses retenues & points restés à vérifier

Conformément au §9 du brief, voici les hypothèses prises et les points à
confirmer sur de vrais rapports. Le code dégrade proprement si une donnée
manque (champ absent → on l'omet, jamais de crash).

## 1. Schéma GraphQL Warcraft Logs (vérifié, à reconfirmer sur vos rapports)

Champs utilisés sur `ReportFight`, vérifiés via la doc officielle v2
(<https://www.warcraftlogs.com/v2-api-docs/warcraft/reportfight.doc.html>) :

- `keystoneLevel` (Int) — niveau de clé M+.
- `keystoneBonus` (Int, 1–3) — paliers gagnés. **Hypothèse : `> 0` ⇒ clé timée.**
- `keystoneTime` (Int) — temps de complétion **en millisecondes**.
- `keystoneAffixes` ([Int]) — IDs d'affixes (mapping nom partiel dans `logic.py`).
- `kill` (Boolean) — kill de boss (raid).
- `id`, `name`, `encounterID`, `gameZone { name }`, `friendlyPlayers`,
  `fightPercentage`, `startTime`/`endTime`.
- Composition : `report.masterData.actors(type:"Player") { id, name, subType }`,
  où **`subType` = la classe**. On compte les classes des `friendlyPlayers`.

**À reconfirmer** : exécutez la requête `REPORT_QUERY` (dans `bot/wcl.py`) sur
un vrai rapport via l'explorateur GraphQL WCL, et lancez le bot avec `DEBUG=1`.
L'extraction (`bot/logic.py`) est isolée et facile à ajuster si un champ diffère.

## 2. Marge par rapport au chrono — NON disponible

L'API **n'expose pas le temps-limite (par time)** du donjon. On affiche donc le
**temps de complétion** (`keystoneTime`) et le **nombre de coffres**
(`keystoneBonus`), mais **pas** de « marge ± X:XX ». Hardcoder des par-times
serait risqué — la liste de donjons (`ABBREVIATIONS`) suggère un serveur
privé/custom avec des temps-limites inconnus.

## 3. Morts — best-effort

Comptées via `report.table(dataType: Deaths, fightIDs:[id])` (un appel par run).
Si l'appel échoue ou que la structure JSON diffère, le champ « Morts » est
simplement omis (voir `WarcraftLogsClient.fetch_death_count`).

## 4. URL profondes WoWAnalyzer (à confirmer)

Formats utilisés :
- Rapport + combat : `https://wowanalyzer.com/report/<code>/<fightId>`
- Rapport seul : `https://wowanalyzer.com/report/<code>`

La **sélection du joueur est laissée à l'utilisateur** sur la page (cf. brief
§6). **Aucune** API de suggestions WoWAnalyzer n'est utilisée ni supposée
exister (brief §9.3) : on ne fait que **construire des liens** vers l'interface
web — aucune extraction d'analyse. À confirmer en ouvrant un lien généré.

## 5. Liens profonds Warcraft Logs

`https://www.warcraftlogs.com/reports/<code>#fight=<id>` (ancre `#fight=`).

## 6. Raid — pull représentatif

Pour chaque `encounterID` : on prend **le kill** si présent, sinon **l'essai au
`fightPercentage` le plus bas** (boss le plus entamé). Un seul fil par
(rapport, zone), avec un lien WoWAnalyzer + WCL par boss.

## 7. Couverture d'analyse WoWAnalyzer inégale

Aucune promesse : selon la spécialisation, le lien peut être peu utile. C'est
documenté pour les utilisateurs mais n'affecte pas le comportement du bot.

## 8. Limites de débit & conditions d'utilisation

- Jeton OAuth mis en cache et réutilisé (pas de ré-auth à chaque commande).
- Back-off exponentiel + respect de l'en-tête `Retry-After` sur 429.
- Vérifiez les conditions d'utilisation WCL et la **licence WoWAnalyzer** si
  vous envisagez un usage au-delà du simple lien (ici : liens uniquement).

## 9. Affixes

Mapping ID→nom **partiel** (`AFFIX_NAMES` dans `logic.py`). IDs inconnus rendus
« Affixe #<id> ». Complétez la table au besoin.

## 10. Sécurité — secrets historiques

Les secrets précédemment codés en dur (token Discord, client_id/secret WCL)
**doivent être régénérés** : considérez-les comme compromis.
