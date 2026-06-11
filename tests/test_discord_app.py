"""Tests de la couche Discord (slash-commands et helpers).

On n'a pas besoin d'un vrai client Discord : on capture les callbacks enregistrés
par `register_commands` via un faux arbre de commandes, puis on les exécute avec
de fausses `interaction`/`Member` contre une vraie base SQLite en mémoire. On
vérifie le contenu des réponses (texte ou embed) plutôt que des effets réseau.
"""

import asyncio
import types

import pytest

from bot import discord_app
from bot.config import Config
from bot.db import Database


# --------------------------------------------------------------------------- #
# Faux objets Discord
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self):
        self.messages = []
        self._done = False

    def is_done(self):
        return self._done

    async def defer(self, *, ephemeral=False):
        self._done = True

    async def send_message(self, content=None, *, embed=None, ephemeral=False):
        self._done = True
        self.messages.append({"content": content, "embed": embed})


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, *, embed=None, **kwargs):
        self.messages.append({"content": content, "embed": embed})


class FakeInteraction:
    def __init__(self, user, guild=None):
        self.user = user
        self.guild = guild
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    def sent(self):
        """Dernier message envoyé (réponse directe ou followup)."""
        msgs = self.response.messages + self.followup.messages
        return msgs[-1] if msgs else None


class FakeMember:
    def __init__(self, id, name="Membre", roles=(), manage_guild=False):
        self.id = id
        self.display_name = name
        self.name = name
        self.nick = None
        self.global_name = None
        self.mention = f"<@{id}>"
        self.roles = [types.SimpleNamespace(id=r) for r in roles]
        self.guild_permissions = types.SimpleNamespace(manage_guild=manage_guild)


class FakeCommand:
    """Imite app_commands.Command : callable + .error()."""

    def __init__(self, fn):
        self.callback = fn

    def error(self, fn):  # le décorateur @cmd.error est ignoré en test
        return fn

    async def __call__(self, *args, **kwargs):
        return await self.callback(*args, **kwargs)


class FakeTree:
    def __init__(self):
        self.commands = {}

    def command(self, *, name, description, guild=None):
        def deco(fn):
            cmd = FakeCommand(fn)
            self.commands[name] = cmd
            return cmd
        return deco


class FakeBot:
    def __init__(self, db, config):
        self.db = db
        self.config = config
        self.tree = FakeTree()

    def get_guild(self, _gid):
        return None

    def get_channel(self, _cid):
        return None

    async def report_error(self, *_a):
        pass


# --------------------------------------------------------------------------- #
# Outils
# --------------------------------------------------------------------------- #

def _config(**over) -> Config:
    base = dict(
        discord_token="x", guild_id=1, forum_channel_id=1,
        wcl_client_id="x", wcl_client_secret="x",
    )
    base.update(over)
    return Config(**base)


def _setup(tmp_path, **config_over):
    db = Database(str(tmp_path / "t.db"))
    bot = FakeBot(db, _config(**config_over))
    discord_app.register_commands(bot)
    return bot, db


def _record_timed(db, *, code, fight, dungeon, level, time_ms, players):
    db.record_run(
        report_code=code, fight_id=fight, kind="mplus", dungeon=dungeon,
        level=level, timed=True, keystone_time=time_ms, encounter_id=None,
        date="01/01", thread_id=fight,
    )
    db.record_run_players(code, fight, players)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Commandes : classements
# --------------------------------------------------------------------------- #

def test_leaderboard_empty(tmp_path):
    bot, _db = _setup(tmp_path)
    itx = FakeInteraction(FakeMember(111))
    run(bot.tree.commands["leaderboard"].callback(itx))
    assert "Aucune clé timée" in itx.sent()["content"]


def test_leaderboard_with_data(tmp_path):
    bot, db = _setup(tmp_path)
    _record_timed(db, code="A", fight=1, dungeon="Skyreach", level=20,
                  time_ms=800_000, players=[("Alice", "Mage")])
    itx = FakeInteraction(FakeMember(111))
    run(bot.tree.commands["leaderboard"].callback(itx))
    embed = itx.sent()["embed"]
    assert embed is not None and "Records" in embed.title


def test_classement_joueurs(tmp_path):
    bot, db = _setup(tmp_path)
    db.link_character("alice", 111)
    _record_timed(db, code="A", fight=1, dungeon="Skyreach", level=20,
                  time_ms=800_000, players=[("Alice", "Mage")])
    itx = FakeInteraction(FakeMember(111))
    run(bot.tree.commands["classement-joueurs"].callback(itx))
    embed = itx.sent()["embed"]
    assert embed is not None and "Classement" in embed.title
    assert "<@111>" in embed.description


def test_profil_self(tmp_path):
    bot, db = _setup(tmp_path)
    db.link_character("alice", 111)
    _record_timed(db, code="A", fight=1, dungeon="Skyreach", level=20,
                  time_ms=800_000, players=[("Alice", "Mage")])
    itx = FakeInteraction(FakeMember(111, name="Alice"))
    run(bot.tree.commands["profil"].callback(itx))
    embed = itx.sent()["embed"]
    assert embed is not None and "Profil" in embed.title


def test_profil_no_data(tmp_path):
    bot, _db = _setup(tmp_path)
    itx = FakeInteraction(FakeMember(111))
    run(bot.tree.commands["profil"].callback(itx))
    assert "Aucune clé" in itx.sent()["content"]


def test_versus(tmp_path):
    bot, db = _setup(tmp_path)
    db.link_character("alice", 111)
    db.link_character("bob", 222)
    _record_timed(db, code="A", fight=1, dungeon="Skyreach", level=20,
                  time_ms=800_000, players=[("Alice", "Mage"), ("Bob", "Priest")])
    itx = FakeInteraction(FakeMember(111))
    a, b = FakeMember(111, "Alice"), FakeMember(222, "Bob")
    run(bot.tree.commands["versus"].callback(itx, a, b))
    embed = itx.sent()["embed"]
    assert embed is not None and "Alice" in embed.title and "Bob" in embed.title


def test_versus_same_player_rejected(tmp_path):
    bot, _db = _setup(tmp_path)
    itx = FakeInteraction(FakeMember(111))
    a = FakeMember(111, "Alice")
    run(bot.tree.commands["versus"].callback(itx, a, a))
    assert "différents" in itx.sent()["content"]


# --------------------------------------------------------------------------- #
# Commandes : liaison
# --------------------------------------------------------------------------- #

def test_lier_success(tmp_path):
    bot, db = _setup(tmp_path)
    itx = FakeInteraction(FakeMember(111))
    run(bot.tree.commands["lier"].callback(itx, "Bob-Hyjal"))
    assert "associé" in itx.sent()["content"]
    assert db.get_character_link("bob") == 111


def test_lier_already_claimed(tmp_path):
    bot, db = _setup(tmp_path)
    db.link_character("bob", 222)
    itx = FakeInteraction(FakeMember(111))
    run(bot.tree.commands["lier"].callback(itx, "Bob"))
    assert "déjà associé" in itx.sent()["content"]
    assert db.get_character_link("bob") == 222  # inchangé


def test_lier_invalid(tmp_path):
    bot, _db = _setup(tmp_path)
    itx = FakeInteraction(FakeMember(111))
    run(bot.tree.commands["lier"].callback(itx, ""))
    assert "invalide" in itx.sent()["content"]


def test_mes_persos(tmp_path):
    bot, db = _setup(tmp_path)
    db.link_character("alice", 111)
    db.link_character("bob", 111)
    itx = FakeInteraction(FakeMember(111))
    run(bot.tree.commands["mes-persos"].callback(itx))
    content = itx.sent()["content"]
    assert "alice" in content and "bob" in content


def test_lier_admin_reassign(tmp_path):
    bot, db = _setup(tmp_path)
    db.link_character("bob", 222)
    itx = FakeInteraction(FakeMember(1, roles=[99], manage_guild=True))
    run(bot.tree.commands["lier-admin"].callback(itx, FakeMember(333), "Bob"))
    assert "réassigné" in itx.sent()["content"]
    assert db.get_character_link("bob") == 333


# --------------------------------------------------------------------------- #
# Commandes : saisons
# --------------------------------------------------------------------------- #

def test_saisons_empty(tmp_path):
    bot, _db = _setup(tmp_path)
    itx = FakeInteraction(FakeMember(111))
    run(bot.tree.commands["saisons"].callback(itx))
    assert "Aucune saison" in itx.sent()["content"]


def test_nouvelle_saison_valid_and_invalid(tmp_path):
    bot, db = _setup(tmp_path)
    admin = FakeMember(1, manage_guild=True)
    # Date invalide.
    itx = FakeInteraction(admin)
    run(bot.tree.commands["nouvelle-saison"].callback(itx, "S1", "pas-une-date"))
    assert "invalide" in itx.sent()["content"].lower()
    assert db.list_seasons() == []
    # Date valide.
    itx = FakeInteraction(admin)
    run(bot.tree.commands["nouvelle-saison"].callback(itx, "S1", "2026-03-01"))
    assert "créée" in itx.sent()["content"]
    assert db.get_season_by_name("S1").start_date == "2026-03-01"


# --------------------------------------------------------------------------- #
# Commandes : raid
# --------------------------------------------------------------------------- #

def test_progression_raid_empty(tmp_path):
    bot, _db = _setup(tmp_path)
    itx = FakeInteraction(FakeMember(111))
    run(bot.tree.commands["progression-raid"].callback(itx))
    assert "Aucune donnée de raid" in itx.sent()["content"]


def test_progression_raid_with_data(tmp_path):
    bot, db = _setup(tmp_path)
    db.update_raid_progress("Nerub-ar", [
        (1, "Ulgrax", True, 10, None),
        (2, "Bloodbound", False, 4, 12.0),
    ])
    itx = FakeInteraction(FakeMember(111))
    run(bot.tree.commands["progression-raid"].callback(itx))
    embed = itx.sent()["embed"]
    assert embed is not None and "Progression" in embed.title
    assert "1/2" in embed.description


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def test_resolve_player_ids_dedup_and_order():
    resolver = {"alice": 111, "bob": 222}
    players = [("Alice", "Mage"), ("Bob", "Priest"), ("Alice", "Mage"), ("Carol", None)]
    assert discord_app.resolve_player_ids(players, resolver) == [111, 222]


def test_build_member_resolver_links_only(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.link_character("alice", 111)
    bot = FakeBot(db, _config())  # enable_member_matching=False
    resolver = run(discord_app.build_member_resolver(bot, None))
    assert resolver == {"alice": 111}


def test_resolve_season_window(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    bot = FakeBot(db, _config())
    # Sans saison définie -> tout l'historique.
    since, until, label = run(discord_app.resolve_season_window(bot, None))
    assert (since, until) == (None, None) and label == "tout l'historique"
    # Choix explicite « all-time ».
    db.add_season("S1", "2020-01-01")
    s, u, lbl = run(discord_app.resolve_season_window(bot, discord_app.SEASON_ALL))
    assert (s, u) == (None, None) and lbl == "tout l'historique"
    # Saison en cours par défaut.
    s, u, lbl = run(discord_app.resolve_season_window(bot, None))
    assert s == "2020-01-01" and lbl == "S1"


def test_season_autocomplete(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.add_season("TWW S1", "2024-01-01")
    db.add_season("TWW S2", "2024-06-01")
    bot = FakeBot(db, _config())
    auto = discord_app.season_autocomplete(bot)
    itx = FakeInteraction(FakeMember(111))
    choices = run(auto(itx, ""))
    values = [c.value for c in choices]
    assert discord_app.SEASON_ALL in values
    assert "TWW S2" in values and "TWW S1" in values


def test_linkable_character_autocomplete_excludes_linked(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.record_run_players("A", 1, [("Alice", "Mage"), ("Bob", "Priest")])
    db.link_character("alice", 111)  # déjà lié -> exclu des suggestions
    bot = FakeBot(db, _config())
    auto = discord_app.linkable_character_autocomplete(bot)
    itx = FakeInteraction(FakeMember(111))
    names = [c.value for c in run(auto(itx, ""))]
    assert names == ["Bob"]


def test_admin_check_predicate(tmp_path, monkeypatch):
    from discord import app_commands

    # Le check fait isinstance(user, discord.Member) : on fait passer FakeMember.
    monkeypatch.setattr(discord_app.discord, "Member", FakeMember)

    def predicate_of(config):
        @discord_app.make_admin_check(config)
        async def f(_i):
            return None
        return f.__discord_app_commands_checks__[0]

    # Rôle responsable autorisé.
    pred = predicate_of(_config(admin_role_ids=[99]))
    assert run(pred(FakeInteraction(FakeMember(1, roles=[99]))))
    with pytest.raises(app_commands.CheckFailure):
        run(pred(FakeInteraction(FakeMember(1, roles=[1]))))
    # Sans rôle configuré : repli sur la permission « Gérer le serveur ».
    pred = predicate_of(_config())
    assert run(pred(FakeInteraction(FakeMember(1, manage_guild=True))))
    with pytest.raises(app_commands.CheckFailure):
        run(pred(FakeInteraction(FakeMember(1, manage_guild=False))))
