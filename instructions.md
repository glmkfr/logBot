# Brief de développement — Bot Discord pour groupe World of Warcraft

Tu es un agent développeur chargé de faire évoluer un bot Discord existant. Lis l'intégralité de ce brief avant de coder. Quand une information ne peut pas être vérifiée (voir la section « À vérifier impérativement »), **n'invente pas** : code de façon défensive, documente l'hypothèse prise, et signale-la.

## 1. Contexte

- Petit serveur Discord privé d'un groupe d'amis qui joue à World of Warcraft (clés Mythique+ et raid).
- Organisation retenue : **un seul canal de type Forum, un fil par run**. La route et la VoD sont ajoutées **manuellement** en réponse dans le fil. (Ce choix est définitif : ne propose pas d'architecture multi-canaux ni de déplacement de posts entre canaux — Discord ne sait pas déplacer un fil.)
- Un bot **Python (discord.py 2.x)** existe déjà. Commande `/logs <lien Warcraft Logs>` qui :
    1. extrait le code du rapport depuis l'URL ;
    2. interroge l'**API GraphQL v2 de Warcraft Logs** (auth OAuth2 *client credentials*, client_id/secret) ;
    3. en tire donjon, niveau de clé, temps, timé/non timé ;
    4. crée un fil dans le forum : titre `Donjon +Niveau — Date`, tags appliqués, lien des logs dans le message d'ouverture.
- Le bot gère **déjà plusieurs runs dans un même rapport** : conserve et respecte ce comportement.

## 2. Objectif

Reprendre le bot existant et implémenter l'ensemble des améliorations et nouvelles fonctionnalités ci-dessous, de façon propre, testée et documentée.

## 3. Contraintes techniques (impératives)

- Python 3.x, discord.py 2.x, slash commands synchronisées par serveur.
- **Toute la configuration et tous les secrets dans un fichier `.env`**, chargé via `python-dotenv`. **Aucune valeur sensible en dur dans le code.** Fournir un `.env.example` documenté (toutes les clés, sans valeurs réelles) et ajouter `.env` au `.gitignore`.
    - Variables attendues (au minimum) : `DISCORD_TOKEN`, `GUILD_ID`, `FORUM_CHANNEL_ID`, `WCL_CLIENT_ID`, `WCL_CLIENT_SECRET`, `DEBUG`.
- Stockage local léger : **SQLite** (aucun service externe).
- Code modulaire et commenté : séparer la couche API Warcraft Logs, la logique métier (extraction, sélection des pulls, dédoublonnage) et la couche Discord.
- Fonctionnement prévu en service persistant (compatible systemd / pm2 / conteneur), avec redémarrage automatique au crash.

## 4. Améliorations de fiabilité

- Mettre en cache le jeton Warcraft Logs et le rafraîchir à l'expiration (ne pas ré-authentifier à chaque commande).
- Anti-doublon : ne pas recréer un fil pour une run déjà publiée. Clé d'unicité = `code de rapport + identifiant de fight`, stockée en SQLite.
- Retries avec back-off exponentiel sur les limites de débit et erreurs transitoires.
- Gérer proprement un rapport sans clé M+ (raid, donjon normal, rapport vide) : message d'erreur clair, jamais de crash.
- Désambiguïser les titres si plusieurs clés du même donjon le même jour (ajouter l'heure ou un index).

## 5. Nouvelles fonctionnalités

- Créer automatiquement les tags manquants (donjon, statut Timé/Non timé) s'ils n'existent pas encore dans le forum.
- Post enrichi (embed) : composition, morts, affixes, marge par rapport au chrono — **uniquement si l'API expose ces données** ; sinon, dégrader proprement.
- Lien direct vers le bon fight / la bonne clé dans Warcraft Logs (pas seulement vers le rapport).
- Paramètres facultatifs `route:` et `vod:` sur `/logs`, pour publier en une seule fois quand tout est déjà disponible.
- Bouton(s) sous le message de confirmation pour ajouter la route / la VoD a posteriori.

## 6. Fonctionnalité WoWAnalyzer (liens profonds, PAS d'extraction d'analyse)

Objectif : donner à chacun un accès rapide à son analyse, sans extraire ni recopier l'analyse.

- À partir d'un rapport (M+ **ou raid**), générer des **liens profonds vers l'interface WoWAnalyzer** (page rapport + fight, sélection du joueur laissée à l'utilisateur sur la page).
- **Pour le raid (voie à privilégier)** : poster dans le fil **un lien WoWAnalyzer par boss**, basé sur le **pull représentatif = le kill** (ou, à défaut de kill, le meilleur essai). Pas de mapping perso↔Discord, pas de MP, pas d'opt-in nécessaires pour cette voie. C'est l'approche retenue.
- Option secondaire et facultative (à ne traiter que si le reste est terminé) : MP par joueur regroupant ses liens de la session. Elle nécessite un mapping personnage→utilisateur Discord, un **opt-in** explicite, et un **repli** si les MP du membre sont fermés.

## 7. Exploitation & sécurité

- Restreindre les commandes sensibles à certains rôles Discord.
- Valider que le lien fourni pointe bien vers `warcraftlogs.com`.
- Journaliser les erreurs (fichier et/ou canal Discord dédié). **Ne jamais écrire de secret dans les logs, ni en mode DEBUG.**

## 8. Données & suivi

- Stocker les runs en SQLite (donjon, niveau, timé, date, code de rapport, fight).
- Commande `/stats` et/ou récap hebdomadaire : nombre de clés, pourcentage timées, niveau moyen, répartition par donjon, etc.

## 9. À vérifier impérativement — ne rien supposer

Ces points n'ont pas pu être confirmés en amont. Tu dois les vérifier toi-même contre les sources officielles **avant** de coder la partie concernée, et coder de façon défensive (avec dégradation propre si un champ manque) :

1. **Schéma GraphQL exact de Warcraft Logs**, pour la M+ **et** le raid (noms et sémantique réels des champs : niveau de clé, temps, bonus/chests, zone/donjon, `encounterID`, statut kill/wipe, etc.). À confirmer via l'explorateur GraphQL de WCL sur de vrais rapports. Rends la fonction d'extraction robuste et facilement ajustable.
2. **Format exact des URL profondes WoWAnalyzer** (rapport / fight / joueur). À confirmer avant de générer des liens.
3. **API de suggestions WoWAnalyzer : son existence n'est PAS confirmée. N'en dépends pas.** Utilise uniquement des liens profonds vers l'interface web. Ne construis aucune fonctionnalité reposant sur une récupération programmatique de l'analyse tant que tu n'as pas prouvé qu'une telle API publique existe.
4. **Limites de débit et conditions d'utilisation de l'API Warcraft Logs** : à respecter (cache du jeton, back-off). Vérifie aussi la **licence de WoWAnalyzer** si tu envisages tout usage allant au-delà du simple lien.
5. **Couverture d'analyse inégale selon les spécialisations** côté WoWAnalyzer : ne promets rien sur ce point ; le lien peut être peu utile pour certaines spés.

## 10. Livrables attendus

- Code source organisé et commenté.
- `.env.example` documenté + `.env` dans `.gitignore`.
- `README` : installation, configuration des variables d'environnement, enregistrement du client API Warcraft Logs, invitation du bot et permissions requises, déploiement en service persistant.
- Gestion d'erreurs et journalisation sans secrets.
- Quelques tests sur le parsing d'URL et l'extraction des données (au moins).
- Une note finale listant les **hypothèses retenues** et les **points restés à vérifier**.
