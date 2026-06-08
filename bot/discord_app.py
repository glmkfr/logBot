"""Couche Discord : client, slash-commands, tags, embeds enrichis et boutons.

Ne contient aucune logique d'extraction ni d'appel réseau bas niveau : elle
orchestre les couches `wcl`, `logic`, `links` et `db`.
"""

from __future__ import annotations

import asyncio
import datetime
import logging

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

    async def close(self) -> None:
        if self.weekly_recap_loop.is_running():
            self.weekly_recap_loop.cancel()
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
        await channel.send(embed=embed)

    @weekly_recap_loop.before_loop
    async def _before_recap(self) -> None:
        await self.wait_until_ready()

    async def on_ready(self) -> None:
        log.info("Connecté en tant que %s (guild=%s)", self.user, self.config.guild_id)

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


def build_stats_embed(data, title: str) -> discord.Embed:
    """Embed de statistiques M+ (réutilisé par /stats et le récap hebdo)."""
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    embed.add_field(name="Clés", value=str(data.total), inline=True)
    embed.add_field(
        name="Timées", value=f"{data.timed} ({data.timed_pct:.0f} %)", inline=True
    )
    embed.add_field(name="Niveau moyen", value=f"+{data.avg_level:.1f}", inline=True)
    if data.by_dungeon:
        breakdown = "\n".join(
            f"{logic.abbreviate(d)} : {n}" for d, n in data.by_dungeon.items()
        )
        embed.add_field(name="Par donjon", value=breakdown, inline=False)
    return embed


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


# --------------------------------------------------------------------------- #
# Enregistrement des commandes
# --------------------------------------------------------------------------- #

def register_commands(bot: BotLogsClient) -> None:
    config = bot.config
    guild = discord.Object(id=config.guild_id)
    role_check = make_role_check(config)

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

        # 1) Validation du lien (doit pointer vers warcraftlogs.com).
        if not links.is_warcraftlogs_url(lien):
            await interaction.followup.send(
                "Lien invalide : seul warcraftlogs.com est accepté."
            )
            return
        code = links.extract_report_code(lien)
        if not code:
            await interaction.followup.send(
                "Lien Warcraft Logs invalide (pas de code de rapport)."
            )
            return

        # 2) Récupération du rapport.
        try:
            report = await bot.wcl.fetch_report(code)
        except WCLError as exc:
            await interaction.followup.send(f"Erreur côté Warcraft Logs : {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            await bot.report_error("Récupération du rapport", exc)
            await interaction.followup.send(
                "Erreur inattendue lors de la récupération du rapport."
            )
            return

        if not report:
            await interaction.followup.send(
                "Rapport introuvable (ou privé). Vérifie le lien."
            )
            return

        forum = bot.get_channel(config.forum_channel_id)
        if not isinstance(forum, discord.ForumChannel):
            await interaction.followup.send("Le canal configuré n'est pas un forum.")
            return

        # 3) Traitement M+ et raid.
        runs = logic.extract_keystone_runs(report)
        raid_encounters = logic.extract_raid_encounters(report)

        if not runs and not raid_encounters:
            await interaction.followup.send(
                "Aucune clé M+ ni boss de raid trouvé dans ce rapport. "
                "(Lance avec DEBUG=1 pour inspecter les données.)"
            )
            return

        messages: list[str] = []

        # Seuil effectif : paramètre de la commande sinon valeur de config.
        threshold = niveau_min if niveau_min is not None else config.min_key_level

        try:
            await _handle_mplus(
                bot, interaction, forum, report, runs, lien, route, vod,
                messages, threshold,
            )
            await _handle_raid(
                bot, forum, report, raid_encounters, lien, messages
            )
        except Exception as exc:  # noqa: BLE001
            await bot.report_error("Création des fils", exc)
            await interaction.followup.send(
                "Une erreur est survenue pendant la création des fils. "
                "Certains fils ont pu être créés."
            )
            return

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
    @app_commands.describe(
        periode="Fenêtre : 'semaine', 'mois' ou 'tout' (défaut : tout)"
    )
    async def stats(interaction: discord.Interaction, periode: str = "tout"):
        await interaction.response.defer()

        import datetime

        since = None
        periode = (periode or "tout").lower()
        if periode in {"semaine", "week", "7j"}:
            since = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
        elif periode in {"mois", "month", "30j"}:
            since = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat()

        data = await asyncio.to_thread(bot.db.stats, since)
        if data.total == 0:
            await interaction.followup.send("Aucune clé enregistrée pour cette période.")
            return

        label = {"semaine": "7 derniers jours", "mois": "30 derniers jours"}.get(
            periode, "depuis toujours"
        )
        await interaction.followup.send(
            embed=build_stats_embed(data, f"Statistiques Mythique+ — {label}")
        )


# --------------------------------------------------------------------------- #
# Sous-traitements
# --------------------------------------------------------------------------- #

async def _handle_mplus(
    bot: BotLogsClient,
    interaction: discord.Interaction,
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
        comp = logic.composition_summary(report, _find_fight(report, run.fight_id))
        deaths = await bot.wcl.fetch_death_count(run.report_code, run.fight_id)

        embed = build_mplus_embed(
            run, report_url=report_url, composition=comp, deaths=deaths,
            route=route, vod=vod,
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
