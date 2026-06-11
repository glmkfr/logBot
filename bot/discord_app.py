"""Couche Discord : client, slash-commands, tags, embeds enrichis et boutons.

Ne contient aucune logique d'extraction ni d'appel réseau bas niveau : elle
orchestre les couches `wcl`, `logic`, `links` et `db`.
"""

from __future__ import annotations

import asyncio
import datetime
import glob
import logging
import os

import discord
from discord import app_commands
from discord.ext import tasks

from . import links, logic
from .config import Config
from .db import Database
from .wcl import WarcraftLogsClient, WCLError

log = logging.getLogger("bot_logs.discord")


# --------------------------------------------------------------------------- #
# Boutons / modale pour ajouter route & VoD a posteriori
# --------------------------------------------------------------------------- #

class RouteVodModal(discord.ui.Modal):
    """Modale demandant un lien (route ou VoD) à publier dans le fil."""

    def __init__(self, kind: str):
        self.kind = kind
        self.label_fr = "Route" if kind == "route" else "VoD"
        super().__init__(title=f"Ajouter la {self.label_fr}")
        self.link = discord.ui.TextInput(
            label=f"Lien {self.label_fr}",
            placeholder="https://...",
            required=True,
            max_length=500,
        )
        self.add_item(self.link)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        value = self.link.value.strip()
        try:
            edited = await _upsert_run_link(channel, self.label_fr, value)
            # Persiste en base (best-effort) pour conserver l'info au redémarrage.
            client = interaction.client
            if isinstance(client, BotLogsClient) and isinstance(channel, discord.Thread):
                await asyncio.to_thread(
                    client.db.set_thread_link, channel.id, self.kind, value
                )
            note = (
                "mise à jour dans le message d'ouverture"
                if edited
                else "ajoutée"
            )
            await interaction.response.send_message(
                f"{self.label_fr} {note} ✅", ephemeral=True
            )
        except discord.DiscordException:
            log.exception("Échec d'ajout de la %s", self.label_fr)
            await interaction.response.send_message(
                f"Impossible d'ajouter la {self.label_fr} (permissions ?).",
                ephemeral=True,
            )


async def _upsert_run_link(channel, label: str, value: str) -> bool:
    """Ajoute/maj un lien (Route/VoD) dans l'embed du message d'ouverture.

    Retourne True si l'embed a été édité, False si on a dû se rabattre sur un
    simple message (fil sans embed, ex. fil de raid).
    """
    if not isinstance(channel, discord.Thread):
        return False
    # Pour un post de forum, le message d'ouverture a le même id que le fil.
    try:
        starter = channel.get_partial_message(channel.id)
        message = await starter.fetch()
    except discord.DiscordException:
        message = None

    if message and message.embeds:
        embed = message.embeds[0]
        link_md = f"[{label}]({value})"
        # Cherche un champ existant du même nom pour le remplacer.
        for i, fld in enumerate(embed.fields):
            if fld.name == label:
                embed.set_field_at(i, name=label, value=link_md, inline=True)
                break
        else:
            embed.add_field(name=label, value=link_md, inline=True)
        await message.edit(embed=embed)
        return True

    # Repli : pas d'embed (fil de raid) → on poste un message.
    await channel.send(f"**{label} :** {value}")
    return False


class RunView(discord.ui.View):
    """Vue persistante : boutons « Ajouter la route » / « Ajouter la VoD ».

    custom_id fixes => la vue survit aux redémarrages (cf. add_view en setup).
    Le fil ciblé est simplement `interaction.channel` (le message porteur des
    boutons vit dans le fil du run).
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Ajouter la route",
        emoji="🗺️",
        style=discord.ButtonStyle.secondary,
        custom_id="runlogs:add_route",
    )
    async def add_route(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.send_modal(RouteVodModal("route"))

    @discord.ui.button(
        label="Ajouter la VoD",
        emoji="🎬",
        style=discord.ButtonStyle.secondary,
        custom_id="runlogs:add_vod",
    )
    async def add_vod(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.send_modal(RouteVodModal("vod"))


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class BotLogsClient(discord.Client):
    """Client Discord du bot, avec son arbre de commandes."""

    def __init__(self, config: Config):
        intents = discord.Intents.default()
        # L'auto-détection lit le contenu des messages : intent privilégié requis
        # (à activer dans le Developer Portal). Activé seulement si la feature
        # est configurée, pour ne rien imposer aux installations qui ne s'en
        # servent pas.
        if config.auto_detect_channel_ids:
            intents.message_content = True
        # Auto-match des joueurs du /leaderboard avec les pseudos Discord : lit
        # la liste des membres du serveur, donc requiert l'intent privilégié
        # « Server Members » (à cocher dans le Developer Portal). Gardé derrière
        # un réglage pour ne pas faire planter la connexion des installations qui
        # n'ont pas activé cet intent côté portail.
        if config.enable_member_matching:
            intents.members = True
        super().__init__(intents=intents)
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.db = Database(config.database_path)
        self.wcl = WarcraftLogsClient(config.wcl_client_id, config.wcl_client_secret)
        self._guild = discord.Object(id=config.guild_id)
        # Garde-fou anti-doublon du récap (clé ISO année-semaine déjà postée).
        self._last_recap_key: tuple[int, int] | None = None

    async def setup_hook(self) -> None:
        # Ouvre la session HTTP partagée (le cache de jeton vit avec le client).
        await self.wcl.__aenter__()
        # Enregistre la vue persistante pour que les boutons survivent au reboot.
        self.add_view(RunView())
        register_commands(self)
        await self.tree.sync(guild=self._guild)
        # Démarre le récap hebdomadaire si un canal est configuré.
        if self.config.recap_channel_id and not self.weekly_recap_loop.is_running():
            self.weekly_recap_loop.start()
        # Battement de cœur pour le healthcheck Docker.
        if not self.heartbeat_loop.is_running():
            self.heartbeat_loop.start()
        # Sauvegarde quotidienne de la base (si un dossier est configuré).
        if self.config.backup_dir and not self.backup_loop.is_running():
            self.backup_loop.start()

    async def close(self) -> None:
        for loop in (self.weekly_recap_loop, self.heartbeat_loop, self.backup_loop):
            if loop.is_running():
                loop.cancel()
        await self.wcl.__aexit__(None, None, None)
        self.db.close()
        await super().close()

    # -- Récap hebdomadaire ---------------------------------------------------

    @tasks.loop(hours=1)
    async def weekly_recap_loop(self) -> None:
        """Poste un récap M+ une fois par semaine (jour/heure configurés).

        La boucle tourne toutes les heures et n'agit qu'au créneau voulu ; un
        garde-fou (clé année-semaine) évite tout doublon si elle re-déclenche.
        """
        now = datetime.datetime.now()
        if now.weekday() != self.config.recap_weekday or now.hour != self.config.recap_hour:
            return
        iso = now.isocalendar()
        key = (iso.year, iso.week)
        if self._last_recap_key == key:
            return
        self._last_recap_key = key

        channel = self.get_channel(self.config.recap_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.warning("RECAP_CHANNEL_ID introuvable ou n'est pas un canal texte.")
            return

        since = (now - datetime.timedelta(days=7)).isoformat()
        data = await asyncio.to_thread(self.db.stats, since)
        if data.total == 0:
            await channel.send(
                "📊 **Récap hebdomadaire** — aucune clé Mythique+ cette semaine."
            )
            return
        embed = build_stats_embed(data, "📊 Récap hebdomadaire Mythique+")

        # Joueur de la semaine : meilleur au classement sur les runs de la semaine.
        rows = await asyncio.to_thread(self.db.player_run_rows, since)
        resolver = await build_member_resolver(self, self.get_guild(self.config.guild_id))
        rankings = logic.player_rankings(rows, resolver)
        if rankings:
            top = rankings[0]
            embed.add_field(
                name="🏅 Joueur de la semaine",
                value=(
                    f"<@{top.user_id}> — {top.timed_count} clé"
                    f"{'s' if top.timed_count > 1 else ''} timée"
                    f"{'s' if top.timed_count > 1 else ''}, "
                    f"meilleure **+{top.best_level}** "
                    f"{logic.abbreviate(top.best_dungeon)}"
                ),
                inline=False,
            )

        await channel.send(embed=embed)

    @weekly_recap_loop.before_loop
    async def _before_recap(self) -> None:
        await self.wait_until_ready()

    async def on_ready(self) -> None:
        log.info("Connecté en tant que %s (guild=%s)", self.user, self.config.guild_id)

    # -- Supervision (heartbeat) & sauvegarde --------------------------------

    @tasks.loop(minutes=1)
    async def heartbeat_loop(self) -> None:
        """Touche un fichier tant que la passerelle Discord est saine.

        Le healthcheck Docker (`python -m bot.healthcheck`) vérifie la fraîcheur
        de ce fichier : s'il n'est plus mis à jour (boucle d'événements bloquée,
        passerelle déconnectée durablement), le conteneur est marqué *unhealthy*.
        """
        if not self.is_ready():
            return
        path = self.config.heartbeat_file
        try:
            if os.path.dirname(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(datetime.datetime.now().isoformat(timespec="seconds"))
        except OSError as exc:
            log.warning("Impossible d'écrire le heartbeat (%s) : %s", path, exc)

    @heartbeat_loop.before_loop
    async def _before_heartbeat(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(hours=24)
    async def backup_loop(self) -> None:
        """Sauvegarde quotidienne de la base SQLite, avec rotation."""
        await asyncio.to_thread(self._run_backup)

    @backup_loop.before_loop
    async def _before_backup(self) -> None:
        await self.wait_until_ready()

    def _run_backup(self) -> None:
        """Écrit une sauvegarde horodatée et ne conserve que les N plus récentes."""
        bdir = self.config.backup_dir
        if not bdir:
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(bdir, f"bot_logs-{stamp}.db")
        try:
            self.db.backup(dest)
        except Exception:  # noqa: BLE001 — une sauvegarde ratée ne doit pas crasher
            log.exception("Échec de la sauvegarde SQLite vers %s", dest)
            return
        # Rotation : on garde les `backup_keep` fichiers les plus récents.
        existing = sorted(glob.glob(os.path.join(bdir, "bot_logs-*.db")))
        for old in existing[: -self.config.backup_keep]:
            try:
                os.remove(old)
            except OSError:
                pass
        log.info("Sauvegarde SQLite écrite : %s", dest)

    # -- Auto-détection des liens Warcraft Logs collés dans le chat -----------

    async def on_message(self, message: discord.Message) -> None:
        """Crée les fils d'un rapport dès qu'un lien WCL est collé dans un canal suivi."""
        if not self.config.auto_detect_channel_ids:
            return
        if message.author.bot or message.author.id == (self.user.id if self.user else 0):
            return
        if message.channel.id not in self.config.auto_detect_channel_ids:
            return
        # Même restriction de rôle que /logs (si configurée).
        if self.config.allowed_role_ids:
            member = message.author
            allowed = isinstance(member, discord.Member) and (
                {r.id for r in member.roles} & set(self.config.allowed_role_ids)
            )
            if not allowed:
                return

        url = links.find_warcraftlogs_url(message.content)
        if not url:
            return

        log.info("Auto-détection d'un lien WCL dans #%s", getattr(message.channel, "name", "?"))
        try:
            async with message.channel.typing():
                results = await process_logs(self, url)
        except Exception as exc:  # noqa: BLE001
            await self.report_error("Auto-détection", exc)
            return
        if results:
            await message.reply("\n".join(results), mention_author=False)

    # -- Journalisation des erreurs vers un canal Discord (sans secret) -------

    async def report_error(self, context: str, exc: BaseException) -> None:
        log.exception("Erreur (%s)", context)
        channel_id = self.config.log_channel_id
        if not channel_id:
            return
        channel = self.get_channel(channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                await channel.send(f"⚠️ **{context}** : `{type(exc).__name__}: {exc}`")
            except discord.DiscordException:
                log.warning("Impossible d'écrire dans le canal de logs.")


# --------------------------------------------------------------------------- #
# Helpers Discord (tags, embeds)
# --------------------------------------------------------------------------- #

async def ensure_tags(
    forum: discord.ForumChannel, names: list[str]
) -> dict[str, discord.ForumTag]:
    """Crée les tags manquants et retourne un index {nom_minuscule: ForumTag}.

    Discord limite un forum à 20 tags : si la limite est atteinte, on n'échoue
    pas — on réutilise simplement les tags existants (dégradation propre).
    """
    existing = {t.name.lower(): t for t in forum.available_tags}
    for name in names:
        key = name.lower()
        if not name or key in existing:
            continue
        if len(forum.available_tags) >= 20:
            log.warning("Limite de 20 tags atteinte : tag « %s » non créé.", name)
            continue
        try:
            new_tag = await forum.create_tag(name=name)
            existing[key] = new_tag
        except discord.Forbidden:
            # Cause la plus fréquente : le bot n'a pas la permission de gérer le
            # forum (créer des tags = permission « Gérer les salons »/Manage
            # Channels). On le signale clairement, une seule fois suffit.
            log.warning(
                "Tag « %s » non créé : permission manquante. Donnez au bot la "
                "permission « Gérer les salons » (Manage Channels) sur le forum.",
                name,
            )
        except discord.DiscordException as exc:
            log.warning("Impossible de créer le tag « %s » : %s", name, exc)
    # Recharge l'index complet après création.
    return {t.name.lower(): t for t in forum.available_tags}


def build_mplus_embed(
    run: logic.KeystoneRun,
    *,
    report_url: str,
    composition: str | None,
    deaths: int | None,
    route: str | None = None,
    vod: str | None = None,
    member_mentions: str | None = None,
) -> discord.Embed:
    """Construit l'embed enrichi d'un run M+ (dégrade les champs manquants)."""
    color = discord.Color.green() if run.timed else discord.Color.orange()
    embed = discord.Embed(
        title=f"{run.dungeon_abbr} +{run.level} — {run.status_label}",
        color=color,
    )
    embed.add_field(name="Donjon", value=run.dungeon or "?", inline=True)
    embed.add_field(name="Niveau", value=f"+{run.level}", inline=True)
    if run.timed and run.bonus:
        embed.add_field(name="Coffres", value="⭐" * run.bonus, inline=True)

    duration = logic.format_duration(run.keystone_time_ms)
    if duration:
        embed.add_field(name="Temps", value=duration, inline=True)

    if run.item_level:
        embed.add_field(name="iLvl moyen", value=f"{run.item_level:.0f}", inline=True)

    affixes = logic.affixes_summary(run)
    if affixes:
        embed.add_field(name="Affixes", value=affixes, inline=False)

    if composition:
        embed.add_field(name="Composition", value=composition, inline=False)

    if member_mentions:
        embed.add_field(name="Joueurs (Discord)", value=member_mentions, inline=False)

    if deaths is not None:
        embed.add_field(name="Morts", value=str(deaths), inline=True)

    # Route / VoD fournies directement (les boutons éditeront ces mêmes champs).
    if route:
        embed.add_field(name="Route", value=f"[Route]({route})", inline=True)
    if vod:
        embed.add_field(name="VoD", value=f"[VoD]({vod})", inline=True)

    # Liens profonds : combat WCL + WoWAnalyzer (sélection du joueur sur place).
    wcl_link = links.wcl_fight_url(run.report_code, run.fight_id)
    wa_link = links.wowanalyzer_url(run.report_code, run.fight_id)
    embed.add_field(
        name="Liens",
        value=(
            f"[Warcraft Logs (ce combat)]({wcl_link})\n"
            f"[WoWAnalyzer]({wa_link})\n"
            f"[Rapport complet]({report_url})"
        ),
        inline=False,
    )
    embed.set_footer(text="Route et VoD à ajouter via les boutons ci-dessous.")
    return embed


def _sparkbars(counts: list[int]) -> str:
    """Mini histogramme en blocs Unicode (▁▂▃▄▅▆▇█) normalisé sur le max."""
    blocks = "▁▂▃▄▅▆▇█"
    peak = max(counts) if counts else 0
    if peak == 0:
        return blocks[0] * len(counts)
    return "".join(blocks[min(len(blocks) - 1, round(c / peak * (len(blocks) - 1)))] for c in counts)


def build_stats_embed(data, title: str, trend: list[tuple[str, int]] | None = None) -> discord.Embed:
    """Embed de statistiques M+ (réutilisé par /stats et le récap hebdo)."""
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    embed.add_field(name="Clés", value=str(data.total), inline=True)
    embed.add_field(
        name="Timées", value=f"{data.timed} ({data.timed_pct:.0f} %)", inline=True
    )
    embed.add_field(name="Niveau moyen", value=f"+{data.avg_level:.1f}", inline=True)
    if data.best_level:
        best = f"+{data.best_level}"
        if data.best_dungeon:
            best += f" — {logic.abbreviate(data.best_dungeon)}"
        embed.add_field(name="Meilleure clé timée", value=best, inline=True)
    if data.by_dungeon:
        breakdown = "\n".join(
            f"{logic.abbreviate(d)} : {n}" for d, n in data.by_dungeon.items()
        )
        embed.add_field(name="Par donjon", value=breakdown, inline=False)
    if trend:
        counts = [n for _, n in trend]
        bars = _sparkbars(counts)
        labels = " ".join(lbl for lbl, _ in trend)
        embed.add_field(
            name=f"Tendance ({len(trend)} dern. semaines)",
            value=f"`{bars}`  ({counts[0]} → {counts[-1]} /sem.)\n{labels}",
            inline=False,
        )
    return embed


def build_leaderboard_embed(
    entries: list, title: str, member_ids: list[list[int]] | None = None
) -> discord.Embed:
    """Embed du classement des meilleures clés timées par donjon.

    `member_ids` (facultatif) aligne, pour chaque entrée, les IDs Discord des
    joueurs de la clé record présents sur le serveur. Quand il est fourni, leurs
    pseudos sont mentionnés sous la ligne du donjon (aspect compétitif).
    """
    embed = discord.Embed(title=title, color=discord.Color.gold())
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    lines = []
    for i, e in enumerate(entries):
        rank = medals.get(i, f"`{i + 1}.`")
        time_str = logic.format_duration(e.best_time_ms)
        suffix = f" en {time_str}" if time_str else ""
        lines.append(
            f"{rank} **{logic.abbreviate(e.dungeon)}** — +{e.best_level}{suffix} "
            f"_({e.timed_count} timée{'s' if e.timed_count > 1 else ''})_"
        )
        ids = member_ids[i] if member_ids and i < len(member_ids) else []
        if ids:
            mentions = " ".join(f"<@{uid}>" for uid in ids)
            lines.append(f"  └ {mentions}")
    embed.description = "\n".join(lines)
    return embed


def build_player_ranking_embed(rankings: list, title: str) -> discord.Embed:
    """Embed du classement des joueurs (meilleure clé timée, nb de clés)."""
    embed = discord.Embed(title=title, color=discord.Color.gold())
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    lines = []
    for i, r in enumerate(rankings):
        rank = medals.get(i, f"`{i + 1}.`")
        time_str = logic.format_duration(r.best_time_ms)
        suffix = f" en {time_str}" if time_str else ""
        lines.append(
            f"{rank} <@{r.user_id}> — **+{r.best_level}** "
            f"{logic.abbreviate(r.best_dungeon)}{suffix} · "
            f"{r.timed_count} timée{'s' if r.timed_count > 1 else ''} · "
            f"moy. +{r.avg_level:.0f}"
        )
    embed.description = "\n".join(lines)
    embed.set_footer(text="Lie tes persos avec /lier pour apparaître ici.")
    return embed


def build_player_profile_embed(profile, title: str) -> discord.Embed:
    """Embed du profil d'un joueur (clés, meilleures par donjon, partenaires)."""
    embed = discord.Embed(title=title, color=discord.Color.teal())
    embed.add_field(name="Clés", value=str(profile.total), inline=True)
    embed.add_field(
        name="Timées",
        value=f"{profile.timed} ({profile.timed_pct:.0f} %)",
        inline=True,
    )
    embed.add_field(name="Niveau moyen", value=f"+{profile.avg_level:.0f}", inline=True)

    if profile.best_by_dungeon:
        top = profile.best_by_dungeon[:8]
        lines = []
        for dungeon, level, time_ms in top:
            time_str = logic.format_duration(time_ms)
            suffix = f" en {time_str}" if time_str else ""
            lines.append(f"**{logic.abbreviate(dungeon)}** +{level}{suffix}")
        embed.add_field(
            name="Meilleures clés timées par donjon",
            value="\n".join(lines),
            inline=False,
        )

    if profile.partners:
        partners = " · ".join(
            f"<@{uid}> ({n})" for uid, n in profile.partners
        )
        embed.add_field(name="Partenaires fréquents", value=partners, inline=False)

    return embed


def build_help_embed() -> discord.Embed:
    """Embed d'aide : présentation du bot et de ses commandes."""
    embed = discord.Embed(
        title="🤖 Aide — Bot Warcraft Logs",
        description=(
            "Je transforme un lien **Warcraft Logs** en fils de forum par run "
            "(M+ et raid), avec embed enrichi, tags, liens profonds, et je tiens "
            "des statistiques et des classements."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="📥 Publier des logs",
        value=(
            "• `/logs lien:<url> [niveau_min] [route] [vod]` — crée le(s) fil(s) "
            "d'un rapport.\n"
            "• Coller un lien WCL dans un canal suivi crée les fils "
            "automatiquement (selon configuration)."
        ),
        inline=False,
    )
    embed.add_field(
        name="📊 Statistiques & classements",
        value=(
            "• `/stats [periode]` — clés, % timées, niveau moyen, tendance.\n"
            "• `/leaderboard [saison]` — meilleure clé timée **par donjon**.\n"
            "• `/classement-joueurs [saison]` — meilleurs **joueurs** par clés timées.\n"
            "• `/profil [membre] [saison]` — stats d'un joueur (clés, partenaires…).\n"
            "• `/saisons` — saisons enregistrées (par défaut : la saison en cours)."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔗 Apparaître dans les classements",
        value=(
            "• `/lier <personnage>` — associe un perso WoW à ton compte Discord.\n"
            "• `/delier <personnage>` · `/mes-persos` — gère tes associations."
        ),
        inline=False,
    )
    embed.add_field(
        name="🧵 Sur chaque fil de run",
        value="Boutons **« Ajouter la route »** / **« Ajouter la VoD »**.",
        inline=False,
    )
    return embed


async def build_member_resolver(
    bot: BotLogsClient, guild: discord.Guild | None
) -> dict[str, int]:
    """Construit le dictionnaire {nom de perso normalisé -> ID Discord}.

    Deux sources fusionnées : l'auto-match (pseudos/surnoms du serveur, seulement
    si `ENABLE_MEMBER_MATCHING` et l'intent « members » sont disponibles) puis,
    par-dessus (donc prioritaires), les liaisons manuelles `/lier`.
    """
    resolver: dict[str, int] = {}
    if bot.config.enable_member_matching and guild is not None:
        for member in guild.members:
            for label in (member.nick, getattr(member, "global_name", None), member.name):
                if label:
                    resolver.setdefault(logic.normalize_character(label), member.id)
    links = await asyncio.to_thread(bot.db.all_character_links)
    resolver.update(links)  # les liens manuels priment sur l'auto-match
    return resolver


def resolve_player_ids(
    players: list[tuple[str, str | None]], resolver: dict[str, int]
) -> list[int]:
    """Résout un roster en IDs Discord uniques (ordre conservé, dédupliqué)."""
    ids: list[int] = []
    seen: set[int] = set()
    for name, _cls in players:
        uid = resolver.get(logic.normalize_character(name))
        if uid is not None and uid not in seen:
            seen.add(uid)
            ids.append(uid)
    return ids


async def resolve_leaderboard_players(
    bot: BotLogsClient, guild: discord.Guild | None, entries: list
) -> list[list[int]]:
    """Pour chaque entrée du classement, résout les joueurs de la clé record en
    IDs de membres Discord présents sur le serveur (cf. build_member_resolver)."""
    resolver = await build_member_resolver(bot, guild)
    result: list[list[int]] = []
    for e in entries:
        ids: list[int] = []
        if e.report_code is not None and e.fight_id is not None:
            players = await asyncio.to_thread(
                bot.db.get_run_players, e.report_code, e.fight_id
            )
            ids = resolve_player_ids(players, resolver)
        result.append(ids)
    return result


# --------------------------------------------------------------------------- #
# Vérification de rôle (commandes sensibles)
# --------------------------------------------------------------------------- #

def make_role_check(config: Config):
    """Retourne un check app_commands restreignant aux rôles autorisés."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if not config.allowed_role_ids:
            return True  # pas de restriction configurée
        member = interaction.user
        if isinstance(member, discord.Member):
            member_role_ids = {r.id for r in member.roles}
            if member_role_ids & set(config.allowed_role_ids):
                return True
        raise app_commands.CheckFailure(
            "Tu n'as pas le rôle requis pour utiliser cette commande."
        )

    return app_commands.check(predicate)


def make_admin_check(config: Config):
    """Check pour les commandes d'administration (saisons, /lier-admin…).

    Autorise les membres ayant un des `ADMIN_ROLE_IDS`. Si aucun n'est configuré,
    on se replie sur la permission Discord « Gérer le serveur » (manage_guild).
    """

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if isinstance(member, discord.Member):
            if config.admin_role_ids:
                if {r.id for r in member.roles} & set(config.admin_role_ids):
                    return True
            elif member.guild_permissions.manage_guild:
                return True
        raise app_commands.CheckFailure(
            "Cette commande est réservée aux responsables."
        )

    return app_commands.check(predicate)


# Valeur spéciale du paramètre `saison` pour ignorer le filtre de saison.
SEASON_ALL = "__all__"


async def resolve_season_window(
    bot: BotLogsClient, saison: str | None
) -> tuple[str | None, str | None, str]:
    """Traduit le choix `saison` en fenêtre (since_iso, until_iso, libellé).

    - None  -> saison en cours (ou tout l'historique si aucune saison définie) ;
    - SEASON_ALL -> pas de filtre (« tout l'historique ») ;
    - nom de saison -> bornes de cette saison.
    Un nom inconnu est traité comme « saison en cours » (dégradation propre).
    """
    if saison == SEASON_ALL:
        return None, None, "tout l'historique"

    seasons = await asyncio.to_thread(bot.db.list_seasons)
    if not seasons:
        return None, None, "tout l'historique"

    season = None
    if saison:
        season = await asyncio.to_thread(bot.db.get_season_by_name, saison)
    if season is None:
        today = datetime.date.today().isoformat()
        season = logic.current_season(seasons, today)
    if season is None:
        return None, None, "tout l'historique"

    since, until = logic.season_bounds(seasons, season)
    return since, until, season.name


def season_autocomplete(bot: BotLogsClient):
    """Autocomplétion du paramètre `saison` : « tout » + les saisons connues."""

    async def autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        choices = [app_commands.Choice(name="Tout l'historique", value=SEASON_ALL)]
        seasons = await asyncio.to_thread(bot.db.list_seasons)
        for s in reversed(seasons):  # plus récentes d'abord
            if current.lower() in s.name.lower():
                choices.append(app_commands.Choice(name=s.name, value=s.name))
        return choices[:25]

    return autocomplete


# --------------------------------------------------------------------------- #
# Enregistrement des commandes
# --------------------------------------------------------------------------- #

def register_commands(bot: BotLogsClient) -> None:
    config = bot.config
    guild = discord.Object(id=config.guild_id)
    role_check = make_role_check(config)
    admin_check = make_admin_check(config)

    @bot.tree.command(
        name="logs",
        description="Crée un fil de run à partir d'un lien Warcraft Logs",
        guild=guild,
    )
    @app_commands.describe(
        lien="Le lien du rapport Warcraft Logs",
        niveau_min=f"(facultatif) Niveau de clé minimum à publier "
        f"(défaut : {config.min_key_level})",
        route="(facultatif) Lien de la route Mythic+ à publier directement",
        vod="(facultatif) Lien de la VoD à publier directement",
    )
    @role_check
    async def logs(
        interaction: discord.Interaction,
        lien: str,
        niveau_min: app_commands.Range[int, 2, 50] | None = None,
        route: str | None = None,
        vod: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        # Seuil effectif : paramètre de la commande sinon valeur de config.
        threshold = niveau_min if niveau_min is not None else config.min_key_level
        messages = await process_logs(
            bot, lien, route=route, vod=vod, threshold=threshold
        )
        if not messages:
            await interaction.followup.send(
                "Rien de nouveau à publier (tout est déjà présent dans le forum)."
            )
        else:
            await interaction.followup.send("\n".join(messages))

    @logs.error
    async def logs_error(interaction: discord.Interaction, error: Exception):
        if isinstance(error, app_commands.CheckFailure):
            msg = "⛔ Tu n'as pas le rôle requis pour utiliser /logs."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            await bot.report_error("Commande /logs", error)

    @bot.tree.command(
        name="stats",
        description="Statistiques des clés Mythique+ publiées",
        guild=guild,
    )
    @app_commands.describe(periode="Fenêtre temporelle (défaut : tout)")
    @app_commands.choices(
        periode=[
            app_commands.Choice(name="7 derniers jours", value="semaine"),
            app_commands.Choice(name="30 derniers jours", value="mois"),
            app_commands.Choice(name="Depuis toujours", value="tout"),
        ]
    )
    async def stats(
        interaction: discord.Interaction,
        periode: app_commands.Choice[str] | None = None,
    ):
        await interaction.response.defer()

        choice = periode.value if periode else "tout"
        since = None
        if choice == "semaine":
            since = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
        elif choice == "mois":
            since = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat()

        data = await asyncio.to_thread(bot.db.stats, since)
        if data.total == 0:
            await interaction.followup.send("Aucune clé enregistrée pour cette période.")
            return

        # Tendance hebdo : seulement sur la vue globale (sinon redondant avec la fenêtre).
        trend = None
        if choice == "tout":
            trend = await asyncio.to_thread(bot.db.weekly_counts, 6)

        label = {"semaine": "7 derniers jours", "mois": "30 derniers jours"}.get(
            choice, "depuis toujours"
        )
        await interaction.followup.send(
            embed=build_stats_embed(data, f"Statistiques Mythique+ — {label}", trend)
        )

    @bot.tree.command(
        name="leaderboard",
        description="Meilleure clé Mythique+ timée par donjon",
        guild=guild,
    )
    @app_commands.describe(
        saison="Saison à afficher (défaut : saison en cours)"
    )
    @app_commands.autocomplete(saison=season_autocomplete(bot))
    async def leaderboard(
        interaction: discord.Interaction, saison: str | None = None
    ):
        await interaction.response.defer()
        since, until, label = await resolve_season_window(bot, saison)
        entries = await asyncio.to_thread(bot.db.leaderboard, since, until)
        if not entries:
            await interaction.followup.send(
                f"Aucune clé timée enregistrée ({label})."
            )
            return
        guild = interaction.guild or bot.get_guild(config.guild_id)
        member_ids = await resolve_leaderboard_players(bot, guild, entries)
        await interaction.followup.send(
            embed=build_leaderboard_embed(
                entries, f"🏆 Records Mythique+ par donjon — {label}", member_ids
            )
        )

    @bot.tree.command(
        name="lier",
        description="Associe un personnage WoW à ton compte Discord (leaderboard)",
        guild=guild,
    )
    @app_commands.describe(
        personnage="Nom du personnage WoW (le royaume est ignoré)"
    )
    async def lier(interaction: discord.Interaction, personnage: str):
        key = logic.normalize_character(personnage)
        if not key:
            await interaction.response.send_message(
                "Nom de personnage invalide.", ephemeral=True
            )
            return
        owner = await asyncio.to_thread(bot.db.get_character_link, key)
        if owner is not None and owner != interaction.user.id:
            await interaction.response.send_message(
                f"⚠️ Ce personnage est déjà associé à <@{owner}>. "
                f"Demande-lui de faire `/delier` s'il s'agit d'une erreur.",
                ephemeral=True,
            )
            return
        await asyncio.to_thread(bot.db.link_character, key, interaction.user.id)
        await interaction.response.send_message(
            f"✅ **{personnage}** est désormais associé à ton compte. "
            f"Tu apparaîtras sur le `/leaderboard` pour ses clés records.",
            ephemeral=True,
        )

    @bot.tree.command(
        name="delier",
        description="Supprime l'association d'un de tes personnages WoW",
        guild=guild,
    )
    @app_commands.describe(personnage="Nom du personnage WoW à dissocier")
    async def delier(interaction: discord.Interaction, personnage: str):
        key = logic.normalize_character(personnage)
        removed = await asyncio.to_thread(
            bot.db.unlink_character, key, interaction.user.id
        )
        if removed:
            await interaction.response.send_message(
                f"✅ **{personnage}** n'est plus associé à ton compte.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Aucune association à ton nom pour ce personnage.", ephemeral=True
            )

    @bot.tree.command(
        name="mes-persos",
        description="Liste les personnages WoW associés à ton compte",
        guild=guild,
    )
    async def mes_persos(interaction: discord.Interaction):
        keys = await asyncio.to_thread(
            bot.db.get_links_for_user, interaction.user.id
        )
        if not keys:
            await interaction.response.send_message(
                "Tu n'as associé aucun personnage. Utilise `/lier <personnage>`.",
                ephemeral=True,
            )
            return
        listing = "\n".join(f"• {k}" for k in keys)
        await interaction.response.send_message(
            f"Tes personnages associés :\n{listing}", ephemeral=True
        )

    @bot.tree.command(
        name="classement-joueurs",
        description="Classement des joueurs par meilleure clé Mythique+ timée",
        guild=guild,
    )
    @app_commands.describe(saison="Saison à afficher (défaut : saison en cours)")
    @app_commands.autocomplete(saison=season_autocomplete(bot))
    async def classement_joueurs(
        interaction: discord.Interaction, saison: str | None = None
    ):
        await interaction.response.defer()
        since, until, label = await resolve_season_window(bot, saison)
        rows = await asyncio.to_thread(bot.db.player_run_rows, since, until)
        guild_obj = interaction.guild or bot.get_guild(config.guild_id)
        resolver = await build_member_resolver(bot, guild_obj)
        rankings = logic.player_rankings(rows, resolver)
        if not rankings:
            await interaction.followup.send(
                f"Aucun joueur à classer ({label}). Liez vos persos avec "
                "`/lier <personnage>` pour apparaître ici."
            )
            return
        await interaction.followup.send(
            embed=build_player_ranking_embed(
                rankings[:15], f"🏅 Classement des joueurs Mythique+ — {label}"
            )
        )

    @bot.tree.command(
        name="profil",
        description="Statistiques Mythique+ d'un joueur",
        guild=guild,
    )
    @app_commands.describe(
        membre="Le membre à inspecter (par défaut : toi)",
        saison="Saison à afficher (défaut : saison en cours)",
    )
    @app_commands.autocomplete(saison=season_autocomplete(bot))
    async def profil(
        interaction: discord.Interaction,
        membre: discord.Member | None = None,
        saison: str | None = None,
    ):
        await interaction.response.defer()
        target = membre or interaction.user
        since, until, label = await resolve_season_window(bot, saison)
        rows = await asyncio.to_thread(bot.db.player_run_rows, since, until)
        guild_obj = interaction.guild or bot.get_guild(config.guild_id)
        resolver = await build_member_resolver(bot, guild_obj)
        profile = logic.player_profile(rows, resolver, target.id)
        if profile.total == 0:
            await interaction.followup.send(
                f"Aucune clé enregistrée pour {target.mention} ({label}). "
                f"A-t-il lié ses persos avec `/lier` ?"
            )
            return
        await interaction.followup.send(
            embed=build_player_profile_embed(
                profile,
                f"📇 Profil Mythique+ — {target.display_name} ({label})",
            )
        )

    @bot.tree.command(
        name="aide",
        description="Comment fonctionne le bot et la liste des commandes",
        guild=guild,
    )
    async def aide(interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=build_help_embed(), ephemeral=True
        )

    @bot.tree.command(
        name="saisons",
        description="Liste les saisons Mythique+ enregistrées",
        guild=guild,
    )
    async def saisons(interaction: discord.Interaction):
        seasons = await asyncio.to_thread(bot.db.list_seasons)
        if not seasons:
            await interaction.response.send_message(
                "Aucune saison définie. Un responsable peut en créer une avec "
                "`/nouvelle-saison`.",
                ephemeral=True,
            )
            return
        today = datetime.date.today().isoformat()
        current = logic.current_season(seasons, today)
        lines = []
        for s in reversed(seasons):  # plus récentes en premier
            marker = " ⬅️ en cours" if current and s.id == current.id else ""
            lines.append(f"• **{s.name}** — depuis le {s.start_date}{marker}")
        await interaction.response.send_message(
            "**Saisons Mythique+ :**\n" + "\n".join(lines), ephemeral=True
        )

    @bot.tree.command(
        name="nouvelle-saison",
        description="(Responsables) Crée une saison Mythique+",
        guild=guild,
    )
    @app_commands.describe(
        nom="Nom de la saison (ex. « TWW Saison 2 »)",
        debut="Date de début au format AAAA-MM-JJ",
    )
    @admin_check
    async def nouvelle_saison(
        interaction: discord.Interaction, nom: str, debut: str
    ):
        try:
            parsed = datetime.date.fromisoformat(debut.strip())
        except ValueError:
            await interaction.response.send_message(
                "Date invalide. Format attendu : **AAAA-MM-JJ** (ex. 2026-03-04).",
                ephemeral=True,
            )
            return
        season = await asyncio.to_thread(
            bot.db.add_season, nom.strip(), parsed.isoformat()
        )
        if season is None:
            await interaction.response.send_message(
                f"Une saison débute déjà le {parsed.isoformat()}.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ Saison **{season.name}** créée (début le {season.start_date}). "
            f"Les classements la prennent désormais comme saison en cours.",
            ephemeral=True,
        )

    @bot.tree.command(
        name="supprimer-saison",
        description="(Responsables) Supprime une saison Mythique+",
        guild=guild,
    )
    @app_commands.describe(saison="La saison à supprimer")
    @app_commands.autocomplete(saison=season_autocomplete(bot))
    @admin_check
    async def supprimer_saison(interaction: discord.Interaction, saison: str):
        season = await asyncio.to_thread(bot.db.get_season_by_name, saison)
        if season is None:
            await interaction.response.send_message(
                "Saison introuvable.", ephemeral=True
            )
            return
        await asyncio.to_thread(bot.db.delete_season, season.id)
        await interaction.response.send_message(
            f"🗑️ Saison **{season.name}** supprimée (les clés restent en base).",
            ephemeral=True,
        )

    @nouvelle_saison.error
    @supprimer_saison.error
    async def _season_admin_error(
        interaction: discord.Interaction, error: Exception
    ):
        if isinstance(error, app_commands.CheckFailure):
            msg = "⛔ Commande réservée aux responsables."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            await bot.report_error("Commande saison", error)


# --------------------------------------------------------------------------- #
# Traitement d'un rapport (partagé par /logs et l'auto-détection)
# --------------------------------------------------------------------------- #

async def process_logs(
    bot: BotLogsClient,
    lien: str,
    *,
    route: str | None = None,
    vod: str | None = None,
    threshold: int | None = None,
) -> list[str]:
    """Valide un lien, récupère le rapport et crée les fils M+/raid.

    Retourne la liste des messages de compte rendu (succès, doublons, erreurs)
    à afficher par l'appelant. Ne lève pas pour les erreurs attendues : elles
    sont transformées en message. Partagé par la commande /logs et le handler
    d'auto-détection pour garantir un comportement identique.
    """
    if threshold is None:
        threshold = bot.config.min_key_level

    # 1) Validation du lien (doit pointer vers warcraftlogs.com).
    if not links.is_warcraftlogs_url(lien):
        return ["Lien invalide : seul warcraftlogs.com est accepté."]
    code = links.extract_report_code(lien)
    if not code:
        return ["Lien Warcraft Logs invalide (pas de code de rapport)."]

    # 2) Récupération du rapport.
    try:
        report = await bot.wcl.fetch_report(code)
    except WCLError as exc:
        return [f"Erreur côté Warcraft Logs : {exc}"]
    except Exception as exc:  # noqa: BLE001
        await bot.report_error("Récupération du rapport", exc)
        return ["Erreur inattendue lors de la récupération du rapport."]

    if not report:
        return ["Rapport introuvable (ou privé). Vérifie le lien."]

    forum = bot.get_channel(bot.config.forum_channel_id)
    if not isinstance(forum, discord.ForumChannel):
        return ["Le canal configuré n'est pas un forum."]

    # 3) Traitement M+ et raid.
    runs = logic.extract_keystone_runs(report)
    raid_encounters = logic.extract_raid_encounters(report)
    if not runs and not raid_encounters:
        return [
            "Aucune clé M+ ni boss de raid trouvé dans ce rapport. "
            "(Lance avec DEBUG=1 pour inspecter les données.)"
        ]

    messages: list[str] = []
    try:
        await _handle_mplus(
            bot, forum, report, runs, lien, route, vod, messages, threshold
        )
        await _handle_raid(bot, forum, report, raid_encounters, lien, messages)
    except Exception as exc:  # noqa: BLE001
        await bot.report_error("Création des fils", exc)
        messages.append(
            "Une erreur est survenue pendant la création des fils. "
            "Certains fils ont pu être créés."
        )
    return messages


# --------------------------------------------------------------------------- #
# Sous-traitements
# --------------------------------------------------------------------------- #

async def _handle_mplus(
    bot: BotLogsClient,
    forum: discord.ForumChannel,
    report: dict,
    runs: list[logic.KeystoneRun],
    report_url: str,
    route: str | None,
    vod: str | None,
    messages: list[str],
    threshold: int,
) -> None:
    eligible = [r for r in runs if r.level >= threshold]
    if runs and not eligible:
        messages.append(
            f"ℹ️ Aucune clé +{threshold} ou plus : pas de fil M+ créé."
        )
        return

    titles = logic.build_titles(eligible)
    # Résout les joueurs en membres Discord (liens manuels + auto-match) pour les
    # afficher dans chaque embed. Construit une seule fois pour tout le rapport.
    resolver = await build_member_resolver(bot, getattr(forum, "guild", None))

    for run in eligible:
        # Anti-doublon : (report_code, fight_id) déjà publié ?
        already = await asyncio.to_thread(
            bot.db.run_exists, run.report_code, run.fight_id
        )
        if already:
            messages.append(f"↩️ {run.dungeon_abbr} +{run.level} déjà publié — ignoré.")
            continue

        title = titles.get(run.fight_id, f"{run.dungeon_abbr} +{run.level}")

        # Données enrichies (best-effort : on dégrade si indisponible).
        fight = _find_fight(report, run.fight_id)
        comp = logic.composition_summary(report, fight)
        players = logic.composition_names(report, fight)
        deaths = await bot.wcl.fetch_death_count(run.report_code, run.fight_id)

        member_ids = resolve_player_ids(players, resolver)
        member_mentions = (
            " ".join(f"<@{uid}>" for uid in member_ids) if member_ids else None
        )

        embed = build_mplus_embed(
            run, report_url=report_url, composition=comp, deaths=deaths,
            route=route, vod=vod, member_mentions=member_mentions,
        )

        # Tags : donjon (complet + abrégé) + statut.
        tag_names = [
            run.dungeon,
            run.dungeon_abbr,
            "Timé" if run.timed else "Non timé",
        ]
        index = await ensure_tags(forum, tag_names)
        applied, tag_error = _resolve_applied_tags(forum, index, tag_names)
        if tag_error:
            if tag_error not in messages:
                messages.append(tag_error)
            break  # condition au niveau du forum : inutile de continuer

        # Message d'ouverture (route/VoD figurent dans l'embed, pas ici).
        opening = f"**Logs :** {report_url}"

        created = await forum.create_thread(
            name=title,
            content=opening,
            embed=embed,
            view=RunView(),
            applied_tags=applied,
        )
        thread = created.thread

        await asyncio.to_thread(
            bot.db.record_run,
            report_code=run.report_code,
            fight_id=run.fight_id,
            kind="mplus",
            dungeon=run.dungeon,
            level=run.level,
            timed=run.timed,
            keystone_time=run.keystone_time_ms,
            encounter_id=None,
            date=run.date,
            thread_id=thread.id,
        )
        # Persiste le roster pour le /leaderboard compétitif (best-effort).
        await asyncio.to_thread(
            bot.db.record_run_players, run.report_code, run.fight_id, players
        )
        messages.append(f"✅ Fil créé : {thread.mention}")


async def _handle_raid(
    bot: BotLogsClient,
    forum: discord.ForumChannel,
    report: dict,
    encounters: list[logic.RaidEncounter],
    report_url: str,
    messages: list[str],
) -> None:
    if not encounters or not bot.config.wowanalyzer_raid_links:
        return

    code = report.get("code") or ""
    zone = logic.raid_zone_name(report)
    date = logic.report_date(report)

    existing_thread_id = await asyncio.to_thread(bot.db.get_raid_thread, code, zone)
    if existing_thread_id:
        messages.append("↩️ Fil de raid déjà publié pour ce rapport — ignoré.")
        return

    # Un lien WoWAnalyzer par boss, basé sur le pull représentatif (kill/best try).
    lines = [f"**Logs :** {report_url}", "", "**Liens WoWAnalyzer par boss :**"]
    for enc in encounters:
        status = "✅ Kill" if enc.killed else f"💀 Wipe (best {enc.best_percentage:.0f} %)" \
            if enc.best_percentage is not None else "💀 Wipe"
        wa = links.wowanalyzer_url(code, enc.fight_id)
        wcl = links.wcl_fight_url(code, enc.fight_id)
        lines.append(f"- **{enc.name}** — {status} · [WoWAnalyzer]({wa}) · [WCL]({wcl})")

    content = "\n".join(lines)[:4000]

    tag_names = [zone, "Raid"]
    index = await ensure_tags(forum, tag_names)
    applied, tag_error = _resolve_applied_tags(forum, index, tag_names)
    if tag_error:
        if tag_error not in messages:
            messages.append(tag_error)
        return

    title = f"Raid {logic.abbreviate(zone)} — {date}"[:100]
    created = await forum.create_thread(
        name=title, content=content, view=RunView(), applied_tags=applied
    )
    thread = created.thread

    await asyncio.to_thread(bot.db.record_raid_thread, code, zone, thread.id)
    messages.append(f"✅ Fil de raid créé : {thread.mention}")


# --------------------------------------------------------------------------- #
# Utilitaires internes
# --------------------------------------------------------------------------- #

def _find_fight(report: dict, fight_id: int) -> dict:
    for f in report.get("fights") or []:
        if f.get("id") == fight_id:
            return f
    return {}


def _select_tags(
    index: dict[str, discord.ForumTag], names: list[str]
) -> list[discord.ForumTag]:
    """Sélectionne jusqu'à 5 tags (limite Discord) à partir des noms voulus."""
    seen: set[int] = set()
    selected: list[discord.ForumTag] = []
    for name in names:
        if not name:
            continue
        tag = index.get(name.lower())
        if tag and tag.id not in seen:
            selected.append(tag)
            seen.add(tag.id)
    return selected[:5]


def _resolve_applied_tags(
    forum: discord.ForumChannel,
    index: dict[str, discord.ForumTag],
    names: list[str],
) -> tuple[list[discord.ForumTag], str | None]:
    """Tags à appliquer + message d'erreur éventuel.

    Gère les forums configurés « étiquette obligatoire » (Discord renvoie sinon
    l'erreur 40067) : si aucun des tags voulus n'est disponible, on applique un
    tag de repli ; s'il n'existe aucun tag du tout, on signale l'impossibilité.
    """
    applied = _select_tags(index, names)
    requires_tag = bool(getattr(forum.flags, "require_tag", False))
    if applied or not requires_tag:
        return applied, None
    if forum.available_tags:
        # Repli : on applique le premier tag existant pour pouvoir poster.
        return [forum.available_tags[0]], None
    return [], (
        "⛔ Ce forum exige une étiquette, mais le bot n'a pas pu en créer "
        "(permission « Gérer les salons » manquante) et aucune n'existe encore. "
        "Donne la permission au bot, ou crée au moins une étiquette dans le forum."
    )
