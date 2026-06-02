from __future__ import annotations

import asyncio
import csv
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import typing
from typing import Any, Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config


# ============================================================
# CONFIGURAÇÕES COM FALLBACK
# ============================================================

PONTO_DB_PATH = getattr(config, "PONTO_DB_PATH", "data/ponto.db")

PONTO_CATEGORY_NAME = getattr(config, "PONTO_CATEGORY_NAME", "📌 - PONTO")
PONTO_PANEL_CHANNEL_NAME = getattr(config, "PONTO_PANEL_CHANNEL_NAME", "📌-ponto")
PONTO_RECORDS_CHANNEL_NAME = getattr(config, "PONTO_RECORDS_CHANNEL_NAME", "📊-registros-ponto")

PONTO_ADMIN_ROLE_IDS = list(getattr(config, "PONTO_ADMIN_ROLE_IDS", [])) or list(
    getattr(config, "CARGOS_PERMITIDOS_IDS", [])
)

PONTO_TIMEZONE = getattr(config, "PONTO_TIMEZONE", "America/Sao_Paulo")

PONTO_ALERT_AFTER_HOURS = float(getattr(config, "PONTO_ALERT_AFTER_HOURS", 3))
PONTO_REVIEW_AFTER_HOURS = float(getattr(config, "PONTO_REVIEW_AFTER_HOURS", 6))
PONTO_ALERT_CHECK_MINUTES = int(getattr(config, "PONTO_ALERT_CHECK_MINUTES", 10))

PONTO_EXPORT_CSV_PATH = getattr(config, "PONTO_EXPORT_CSV_PATH", "data/ponto_export.csv")


# ============================================================
# HELPERS
# ============================================================

_db_lock = asyncio.Lock()


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(PONTO_TIMEZONE)
    except Exception:
        return ZoneInfo("America/Sao_Paulo")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _local(dt: datetime | str | None) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = _parse_iso(dt)
    if dt is None:
        return None
    return dt.astimezone(_tz())


def _fmt_date(dt: datetime | str | None) -> str:
    local = _local(dt)
    return local.strftime("%d/%m/%Y") if local else "-"


def _fmt_time(dt: datetime | str | None) -> str:
    local = _local(dt)
    return local.strftime("%H:%M") if local else "-"


def _fmt_datetime(dt: datetime | str | None) -> str:
    local = _local(dt)
    return local.strftime("%d/%m/%Y %H:%M") if local else "-"


def _fmt_duration(seconds: int | float | None) -> str:
    if not seconds or seconds <= 0:
        return "0min"

    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, _ = divmod(rem, 60)

    if hours and minutes:
        return f"{hours}h{minutes:02d}min"
    if hours:
        return f"{hours}h"
    return f"{minutes}min"


def _has_admin_permission(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True

    user_role_ids = {role.id for role in member.roles}
    return any(role_id in user_role_ids for role_id in PONTO_ADMIN_ROLE_IDS)


def _get_db_connection() -> sqlite3.Connection:
    db_path = Path(PONTO_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ponto_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                total_pause_seconds INTEGER NOT NULL DEFAULT 0,
                total_seconds INTEGER,
                status TEXT NOT NULL DEFAULT 'OPEN',
                closed_by INTEGER,
                closed_reason TEXT,
                pending_reason TEXT,
                last_alert_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ponto_pauses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                pause_started_at TEXT NOT NULL,
                pause_ended_at TEXT,
                duration_seconds INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES ponto_sessions(id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ponto_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                guild_id INTEGER NOT NULL,
                user_id INTEGER,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                actor_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ponto_sessions_guild_user_status
            ON ponto_sessions (guild_id, user_id, status)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ponto_sessions_guild_status
            ON ponto_sessions (guild_id, status)
            """
        )

        conn.commit()


def _row_to_dict(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
    return dict(row) if row else None


def _audit(
    conn: sqlite3.Connection,
    *,
    session_id: int | None,
    guild_id: int,
    user_id: int | None,
    event_type: str,
    message: str,
    actor_id: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO ponto_audit_logs (
            session_id, guild_id, user_id, event_type, message, actor_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, guild_id, user_id, event_type, message, actor_id, _iso(_now_utc())),
    )


def _get_active_session(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    user_id: int,
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT *
        FROM ponto_sessions
        WHERE guild_id = ?
          AND user_id = ?
          AND ended_at IS NULL
          AND status IN ('OPEN', 'PAUSED', 'PENDING_REVIEW')
        ORDER BY id DESC
        LIMIT 1
        """,
        (guild_id, user_id),
    ).fetchone()
    return _row_to_dict(row)


def _get_session_by_id(conn: sqlite3.Connection, session_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM ponto_sessions WHERE id = ?", (session_id,)).fetchone()
    return _row_to_dict(row)


def _get_open_pause(
    conn: sqlite3.Connection,
    *,
    session_id: int,
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT *
        FROM ponto_pauses
        WHERE session_id = ?
          AND pause_ended_at IS NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    return _row_to_dict(row)


def _calculate_total_seconds(session: dict[str, Any], ended_at: datetime) -> int:
    started_at = _parse_iso(session["started_at"])
    if started_at is None:
        return 0

    bruto = int((ended_at - started_at).total_seconds())
    pausas = int(session.get("total_pause_seconds") or 0)
    total = max(0, bruto - pausas)
    return total


def _build_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📌 Sistema de Ponto",
        description=(
            "Use os botões abaixo para registrar sua atividade.\n\n"
            "🟢 **Entrada** — inicia seu ponto\n"
            "🔴 **Saída** — finaliza seu ponto\n"
            "⏸️ **AFK** — pausa o tempo\n"
            "▶️ **Retomar** — volta a contar\n\n"
            "O canal de registros receberá apenas o resumo final quando o ponto for encerrado."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Sistema de ponto • Discord")
    return embed


def _build_record_embed(
    *,
    member_mention: str,
    session: dict[str, Any],
    closed_by_mention: str | None = None,
    closed_reason: str | None = None,
) -> discord.Embed:
    started_at = _parse_iso(session["started_at"])
    ended_at = _parse_iso(session.get("ended_at"))
    pause_seconds = int(session.get("total_pause_seconds") or 0)
    total_seconds = int(session.get("total_seconds") or 0)
    status = session.get("status", "CLOSED")
    pending_reason = session.get("pending_reason")

    if status == "CLOSED_BY_ADMIN":
        title = "🛠️ Registro de Ponto Fechado por Responsável"
        color = discord.Color.orange()
    elif status == "PENDING_REVIEW":
        title = "⚠️ Registro de Ponto Pendente de Revisão"
        color = discord.Color.gold()
    else:
        title = "📌 Registro de Ponto"
        color = discord.Color.green()

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="👤 Usuário", value=member_mention, inline=False)
    embed.add_field(name="📅 Data", value=_fmt_date(started_at), inline=True)
    embed.add_field(name="🟢 Entrada", value=_fmt_time(started_at), inline=True)
    embed.add_field(name="🔴 Saída", value=_fmt_time(ended_at), inline=True)
    embed.add_field(name="⏸️ Pausas", value=_fmt_duration(pause_seconds), inline=True)
    embed.add_field(name="⏱️ Tempo total", value=_fmt_duration(total_seconds), inline=True)

    if closed_by_mention:
        embed.add_field(name="🛠️ Fechado por", value=closed_by_mention, inline=False)

    if closed_reason:
        embed.add_field(name="📝 Motivo", value=closed_reason, inline=False)

    if pending_reason and status == "PENDING_REVIEW":
        embed.add_field(name="⚠️ Revisão necessária", value=pending_reason, inline=False)

    embed.set_footer(text=f"Sessão #{session['id']}")
    return embed


async def _ensure_ponto_channels(
    guild: discord.Guild,
) -> tuple[discord.CategoryChannel, discord.TextChannel, discord.TextChannel]:
    category = discord.utils.get(guild.categories, name=PONTO_CATEGORY_NAME)
    if not category:
        category = await guild.create_category(PONTO_CATEGORY_NAME)

    panel_ch = discord.utils.get(category.text_channels, name=PONTO_PANEL_CHANNEL_NAME)
    if not panel_ch:
        panel_ch = await guild.create_text_channel(PONTO_PANEL_CHANNEL_NAME, category=category)

    records_ch = discord.utils.get(category.text_channels, name=PONTO_RECORDS_CHANNEL_NAME)
    if not records_ch:
        records_ch = await guild.create_text_channel(PONTO_RECORDS_CHANNEL_NAME, category=category)

    return category, panel_ch, records_ch


# ============================================================
# VIEW COM BOTÕES
# ============================================================

class PontoPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    def _cog(self, interaction: discord.Interaction) -> "PontoSystem":
        bot = typing.cast(commands.Bot, interaction.client)
        cog = bot.get_cog("PontoSystem")
        if cog is None:
            raise RuntimeError("Cog PontoSystem não carregado.")
        return cog  # type: ignore[return-value]

    @discord.ui.button(
        label="Entrada",
        emoji="🟢",
        style=discord.ButtonStyle.success,
        custom_id="ponto:entrada",
    )
    async def entrada(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._cog(interaction).handle_start(interaction)

    @discord.ui.button(
        label="Saída",
        emoji="🔴",
        style=discord.ButtonStyle.danger,
        custom_id="ponto:saida",
    )
    async def saida(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._cog(interaction).handle_stop(interaction)

    @discord.ui.button(
        label="AFK",
        emoji="⏸️",
        style=discord.ButtonStyle.secondary,
        custom_id="ponto:pause",
    )
    async def pause(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._cog(interaction).handle_pause(interaction)

    @discord.ui.button(
        label="Retomar",
        emoji="▶️",
        style=discord.ButtonStyle.primary,
        custom_id="ponto:resume",
    )
    async def resume(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._cog(interaction).handle_resume(interaction)


# ============================================================
# COG PRINCIPAL
# ============================================================

class PontoSystem(commands.Cog):
    """Sistema de ponto via Discord com painel, pausa/AFK, relatórios e revisão."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        init_db()
        bot.add_view(PontoPanelView())
        self.alertas_ponto.start()

    def cog_unload(self) -> None:
        self.alertas_ponto.cancel()

    # --------------------------------------------------------
    # AÇÕES DO USUÁRIO
    # --------------------------------------------------------

    async def handle_start(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ Este botão só funciona dentro de um servidor.",
                ephemeral=True,
            )
            return

        now = _now_utc()

        async with _db_lock:
            with _get_db_connection() as conn:
                existing = _get_active_session(
                    conn,
                    guild_id=interaction.guild.id,
                    user_id=interaction.user.id,
                )

                if existing:
                    await interaction.response.send_message(
                        f"⚠️ Você já possui um ponto aberto desde **{_fmt_datetime(existing['started_at'])}**.",
                        ephemeral=True,
                    )
                    return

                cursor = conn.execute(
                    """
                    INSERT INTO ponto_sessions (
                        guild_id, user_id, username, started_at, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 'OPEN', ?, ?)
                    """,
                    (
                        interaction.guild.id,
                        interaction.user.id,
                        str(interaction.user),
                        _iso(now),
                        _iso(now),
                        _iso(now),
                    ),
                )
                session_id = int(cursor.lastrowid) if cursor.lastrowid is not None else 0

                _audit(
                    conn,
                    session_id=session_id,
                    guild_id=interaction.guild.id,
                    user_id=interaction.user.id,
                    event_type="STARTED",
                    message="Usuário iniciou o ponto.",
                    actor_id=interaction.user.id,
                )

                conn.commit()

        await interaction.response.send_message(
            f"✅ Entrada registrada às **{_fmt_time(now)}**.",
            ephemeral=True,
        )

    async def handle_stop(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ Este botão só funciona dentro de um servidor.",
                ephemeral=True,
            )
            return

        result = await self._close_session(
            guild=interaction.guild,
            user_id=interaction.user.id,
            actor_id=interaction.user.id,
            closed_by_admin=False,
            reason=None,
        )

        if result["ok"] is False:
            await interaction.response.send_message(result["message"], ephemeral=True)
            return

        await interaction.response.send_message(result["message"], ephemeral=True)

    async def handle_pause(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ Este botão só funciona dentro de um servidor.",
                ephemeral=True,
            )
            return

        now = _now_utc()

        async with _db_lock:
            with _get_db_connection() as conn:
                session = _get_active_session(
                    conn,
                    guild_id=interaction.guild.id,
                    user_id=interaction.user.id,
                )

                if not session:
                    await interaction.response.send_message(
                        "⚠️ Você não possui ponto aberto para pausar.",
                        ephemeral=True,
                    )
                    return

                if session["status"] == "PAUSED":
                    await interaction.response.send_message(
                        "⚠️ Seu ponto já está pausado/AFK.",
                        ephemeral=True,
                    )
                    return

                conn.execute(
                    """
                    INSERT INTO ponto_pauses (
                        session_id, pause_started_at, created_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (session["id"], _iso(now), _iso(now)),
                )

                conn.execute(
                    """
                    UPDATE ponto_sessions
                    SET status = 'PAUSED', updated_at = ?
                    WHERE id = ?
                    """,
                    (_iso(now), session["id"]),
                )

                _audit(
                    conn,
                    session_id=session["id"],
                    guild_id=interaction.guild.id,
                    user_id=interaction.user.id,
                    event_type="PAUSED",
                    message="Usuário pausou o ponto/ficou AFK.",
                    actor_id=interaction.user.id,
                )

                conn.commit()

        await interaction.response.send_message(
            f"⏸️ Pausa/AFK registrada às **{_fmt_time(now)}**.",
            ephemeral=True,
        )

    async def handle_resume(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ Este botão só funciona dentro de um servidor.",
                ephemeral=True,
            )
            return

        now = _now_utc()

        async with _db_lock:
            with _get_db_connection() as conn:
                session = _get_active_session(
                    conn,
                    guild_id=interaction.guild.id,
                    user_id=interaction.user.id,
                )

                if not session:
                    await interaction.response.send_message(
                        "⚠️ Você não possui ponto aberto.",
                        ephemeral=True,
                    )
                    return

                if session["status"] != "PAUSED":
                    await interaction.response.send_message(
                        "⚠️ Seu ponto não está pausado.",
                        ephemeral=True,
                    )
                    return

                pause = _get_open_pause(conn, session_id=session["id"])
                if not pause:
                    await interaction.response.send_message(
                        "⚠️ Não encontrei pausa aberta para retomar.",
                        ephemeral=True,
                    )
                    return

                pause_started = _parse_iso(pause["pause_started_at"])
                duration = int((now - pause_started).total_seconds()) if pause_started else 0
                new_pause_total = int(session.get("total_pause_seconds") or 0) + max(0, duration)

                previous_status = "PENDING_REVIEW" if session.get("pending_reason") else "OPEN"

                conn.execute(
                    """
                    UPDATE ponto_pauses
                    SET pause_ended_at = ?, duration_seconds = ?
                    WHERE id = ?
                    """,
                    (_iso(now), max(0, duration), pause["id"]),
                )

                conn.execute(
                    """
                    UPDATE ponto_sessions
                    SET status = ?, total_pause_seconds = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (previous_status, new_pause_total, _iso(now), session["id"]),
                )

                _audit(
                    conn,
                    session_id=session["id"],
                    guild_id=interaction.guild.id,
                    user_id=interaction.user.id,
                    event_type="RESUMED",
                    message=f"Usuário retomou o ponto. Pausa: {_fmt_duration(duration)}.",
                    actor_id=interaction.user.id,
                )

                conn.commit()

        await interaction.response.send_message(
            f"▶️ Ponto retomado às **{_fmt_time(now)}**. Pausa: **{_fmt_duration(duration)}**.",
            ephemeral=True,
        )

    # --------------------------------------------------------
    # FECHAMENTO CENTRALIZADO
    # --------------------------------------------------------

    async def _close_session(
        self,
        *,
        guild: discord.Guild,
        user_id: int,
        actor_id: int,
        closed_by_admin: bool,
        reason: str | None,
    ) -> dict[str, Any]:
        now = _now_utc()

        async with _db_lock:
            with _get_db_connection() as conn:
                session = _get_active_session(conn, guild_id=guild.id, user_id=user_id)

                if not session:
                    return {
                        "ok": False,
                        "message": "⚠️ Não existe ponto aberto para este usuário.",
                    }

                # Se estiver pausado, fecha automaticamente a pausa antes de fechar o ponto.
                if session["status"] == "PAUSED":
                    pause = _get_open_pause(conn, session_id=session["id"])
                    if pause:
                        pause_started = _parse_iso(pause["pause_started_at"])
                        duration = int((now - pause_started).total_seconds()) if pause_started else 0
                        new_pause_total = int(session.get("total_pause_seconds") or 0) + max(0, duration)

                        conn.execute(
                            """
                            UPDATE ponto_pauses
                            SET pause_ended_at = ?, duration_seconds = ?
                            WHERE id = ?
                            """,
                            (_iso(now), max(0, duration), pause["id"]),
                        )

                        conn.execute(
                            """
                            UPDATE ponto_sessions
                            SET total_pause_seconds = ?
                            WHERE id = ?
                            """,
                            (new_pause_total, session["id"]),
                        )

                        session["total_pause_seconds"] = new_pause_total

                if closed_by_admin:
                    final_status = "CLOSED_BY_ADMIN"
                elif session["status"] == "PENDING_REVIEW":
                    final_status = "PENDING_REVIEW"
                else:
                    final_status = "CLOSED"

                total_seconds = _calculate_total_seconds(session, now)

                conn.execute(
                    """
                    UPDATE ponto_sessions
                    SET ended_at = ?,
                        total_seconds = ?,
                        status = ?,
                        closed_by = ?,
                        closed_reason = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        _iso(now),
                        total_seconds,
                        final_status,
                        actor_id if closed_by_admin else None,
                        reason,
                        _iso(now),
                        session["id"],
                    ),
                )

                _audit(
                    conn,
                    session_id=session["id"],
                    guild_id=guild.id,
                    user_id=user_id,
                    event_type="CLOSED_BY_ADMIN" if closed_by_admin else "CLOSED",
                    message=reason or "Ponto encerrado pelo usuário.",
                    actor_id=actor_id,
                )

                conn.commit()

                updated = _get_session_by_id(conn, session["id"])

        if not updated:
            return {
                "ok": False,
                "message": "❌ Ocorreu um erro ao recuperar a sessão fechada.",
            }

        await self._send_final_record(guild, updated)

        return {
            "ok": True,
            "message": (
                f"✅ Saída registrada às **{_fmt_time(now)}**.\n"
                f"⏱️ Tempo total: **{_fmt_duration(updated.get('total_seconds'))}**."
            ),
            "session": updated,
        }

    async def _send_final_record(self, guild: discord.Guild, session: dict[str, Any]) -> None:
        try:
            _, _, records_ch = await _ensure_ponto_channels(guild)
        except Exception:
            return

        member = guild.get_member(int(session["user_id"]))
        member_mention = member.mention if member else f"<@{session['user_id']}>"

        closed_by_mention = None
        if session.get("closed_by"):
            closed_by_member = guild.get_member(int(session["closed_by"]))
            closed_by_mention = closed_by_member.mention if closed_by_member else f"<@{session['closed_by']}>"

        embed = _build_record_embed(
            member_mention=member_mention,
            session=session,
            closed_by_mention=closed_by_mention,
            closed_reason=session.get("closed_reason"),
        )

        try:
            await records_ch.send(embed=embed)
        except discord.Forbidden:
            pass

    # --------------------------------------------------------
    # COMANDOS ADMINISTRATIVOS
    # --------------------------------------------------------

    @app_commands.command(name="painelponto", description="Cria o painel de ponto com botões.")
    async def painelponto(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ Este comando só funciona em servidores.",
                ephemeral=True,
            )
            return

        if not _has_admin_permission(interaction.user):
            await interaction.response.send_message(
                "❌ Você não tem permissão para criar o painel de ponto.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        _, panel_ch, records_ch = await _ensure_ponto_channels(interaction.guild)

        await panel_ch.send(embed=_build_panel_embed(), view=PontoPanelView())

        await interaction.followup.send(
            f"✅ Painel criado em {panel_ch.mention}.\n"
            f"📊 Registros finais serão enviados em {records_ch.mention}.",
            ephemeral=True,
        )

    @app_commands.command(name="ponto_abertos", description="[Responsável] Lista pontos abertos/pausados/pendentes.")
    async def ponto_abertos(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Apenas em servidores.", ephemeral=True)
            return

        if not _has_admin_permission(interaction.user):
            await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
            return

        async with _db_lock:
            with _get_db_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM ponto_sessions
                    WHERE guild_id = ?
                      AND ended_at IS NULL
                      AND status IN ('OPEN', 'PAUSED', 'PENDING_REVIEW')
                    ORDER BY started_at ASC
                    """,
                    (interaction.guild.id,),
                ).fetchall()

        if not rows:
            await interaction.response.send_message(
                "📭 Não há pontos abertos no momento.",
                ephemeral=True,
            )
            return

        now = _now_utc()
        lines: list[str] = []

        for row in rows[:30]:
            session = dict(row)
            started = _parse_iso(session["started_at"]) or now
            elapsed = max(0, int((now - started).total_seconds()))
            pause = int(session.get("total_pause_seconds") or 0)
            valid = max(0, elapsed - pause)

            member = interaction.guild.get_member(int(session["user_id"]))
            mention = member.mention if member else f"<@{session['user_id']}>"

            status_emoji = {
                "OPEN": "🟢",
                "PAUSED": "⏸️",
                "PENDING_REVIEW": "⚠️",
            }.get(session["status"], "⚪")

            lines.append(
                f"{status_emoji} {mention} • entrada **{_fmt_time(session['started_at'])}** "
                f"• ativo **{_fmt_duration(valid)}** • sessão `#{session['id']}`"
            )

        embed = discord.Embed(
            title="🟢 Pontos abertos",
            description="\n".join(lines),
            color=discord.Color.green(),
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ponto_fechar", description="[Responsável] Fecha o ponto aberto de um usuário com motivo.")
    @app_commands.describe(
        membro="Usuário que terá o ponto fechado",
        motivo="Motivo do fechamento manual",
    )
    async def ponto_fechar(
        self,
        interaction: discord.Interaction,
        membro: discord.Member,
        motivo: str,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Apenas em servidores.", ephemeral=True)
            return

        if not _has_admin_permission(interaction.user):
            await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
            return

        motivo = motivo.strip()
        if len(motivo) < 5:
            await interaction.response.send_message(
                "⚠️ Informe um motivo mais descritivo para o fechamento.",
                ephemeral=True,
            )
            return

        result = await self._close_session(
            guild=interaction.guild,
            user_id=membro.id,
            actor_id=interaction.user.id,
            closed_by_admin=True,
            reason=motivo,
        )

        await interaction.response.send_message(result["message"], ephemeral=True)

    @app_commands.command(name="ponto_pendentes", description="[Responsável] Lista pontos pendentes de revisão.")
    async def ponto_pendentes(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Apenas em servidores.", ephemeral=True)
            return

        if not _has_admin_permission(interaction.user):
            await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
            return

        async with _db_lock:
            with _get_db_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM ponto_sessions
                    WHERE guild_id = ?
                      AND status = 'PENDING_REVIEW'
                    ORDER BY started_at DESC
                    LIMIT 30
                    """,
                    (interaction.guild.id,),
                ).fetchall()

        if not rows:
            await interaction.response.send_message(
                "✅ Nenhum ponto pendente de revisão.",
                ephemeral=True,
            )
            return

        lines = []
        for row in rows:
            session = dict(row)
            member = interaction.guild.get_member(int(session["user_id"]))
            mention = member.mention if member else f"<@{session['user_id']}>"

            if session.get("ended_at"):
                duration = _fmt_duration(session.get("total_seconds"))
                estado = f"fechado • {duration}"
            else:
                started = _parse_iso(session["started_at"]) or _now_utc()
                duration = _fmt_duration((_now_utc() - started).total_seconds())
                estado = f"aberto há {duration}"

            lines.append(
                f"⚠️ {mention} • sessão `#{session['id']}` • {estado}\n"
                f"Motivo: {session.get('pending_reason') or 'Revisão necessária'}"
            )

        embed = discord.Embed(
            title="⚠️ Pontos pendentes de revisão",
            description="\n\n".join(lines),
            color=discord.Color.gold(),
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ponto_relatorio_dia", description="[Responsável] Relatório de ponto do dia atual.")
    async def ponto_relatorio_dia(self, interaction: discord.Interaction) -> None:
        await self._send_report(interaction, period="dia")

    @app_commands.command(name="ponto_relatorio_semana", description="[Responsável] Relatório de ponto da semana atual.")
    async def ponto_relatorio_semana(self, interaction: discord.Interaction) -> None:
        await self._send_report(interaction, period="semana")

    async def _send_report(self, interaction: discord.Interaction, *, period: str) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Apenas em servidores.", ephemeral=True)
            return

        if not _has_admin_permission(interaction.user):
            await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
            return

        now_local = _now_utc().astimezone(_tz())

        if period == "dia":
            start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            title = f"📊 Relatório diário - {start_local.strftime('%d/%m/%Y')}"
        else:
            start_local = (now_local - timedelta(days=now_local.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            title = f"📊 Relatório semanal - desde {start_local.strftime('%d/%m/%Y')}"

        end_local = now_local

        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)

        async with _db_lock:
            with _get_db_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT user_id,
                           username,
                           SUM(COALESCE(total_seconds, 0)) AS total_seconds,
                           COUNT(*) AS total_sessions,
                           SUM(CASE WHEN status = 'PENDING_REVIEW' THEN 1 ELSE 0 END) AS pending_count,
                           SUM(CASE WHEN status = 'CLOSED_BY_ADMIN' THEN 1 ELSE 0 END) AS admin_closed_count
                    FROM ponto_sessions
                    WHERE guild_id = ?
                      AND ended_at IS NOT NULL
                      AND ended_at >= ?
                      AND ended_at <= ?
                    GROUP BY user_id, username
                    ORDER BY total_seconds DESC
                    """,
                    (interaction.guild.id, _iso(start_utc), _iso(end_utc)),
                ).fetchall()

                summary = conn.execute(
                    """
                    SELECT COUNT(*) AS total_closed,
                           SUM(COALESCE(total_seconds, 0)) AS general_total,
                           SUM(CASE WHEN status = 'PENDING_REVIEW' THEN 1 ELSE 0 END) AS pending_total,
                           SUM(CASE WHEN status = 'CLOSED_BY_ADMIN' THEN 1 ELSE 0 END) AS admin_closed_total
                    FROM ponto_sessions
                    WHERE guild_id = ?
                      AND ended_at IS NOT NULL
                      AND ended_at >= ?
                      AND ended_at <= ?
                    """,
                    (interaction.guild.id, _iso(start_utc), _iso(end_utc)),
                ).fetchone()

        if not rows:
            await interaction.response.send_message(
                f"📭 Nenhum ponto fechado encontrado para este período.",
                ephemeral=True,
            )
            return

        lines = []
        for row in rows[:30]:
            user_id = int(row["user_id"])
            member = interaction.guild.get_member(user_id)
            mention = member.mention if member else f"<@{user_id}>"

            extra = []
            if int(row["pending_count"] or 0):
                extra.append(f"⚠️ {int(row['pending_count'])} pendente(s)")
            if int(row["admin_closed_count"] or 0):
                extra.append(f"🛠️ {int(row['admin_closed_count'])} fechado(s) por responsável")

            suffix = f" • {' • '.join(extra)}" if extra else ""

            lines.append(
                f"{mention} — **{_fmt_duration(row['total_seconds'])}** "
                f"em **{int(row['total_sessions'])}** registro(s){suffix}"
            )

        general_total = int(summary["general_total"] or 0) if summary else 0
        total_closed = int(summary["total_closed"] or 0) if summary else 0
        pending_total = int(summary["pending_total"] or 0) if summary else 0
        admin_closed_total = int(summary["admin_closed_total"] or 0) if summary else 0

        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.add_field(name="⏱️ Total geral", value=_fmt_duration(general_total), inline=True)
        embed.add_field(name="✅ Pontos fechados", value=str(total_closed), inline=True)
        embed.add_field(name="⚠️ Pendentes", value=str(pending_total), inline=True)
        embed.add_field(name="🛠️ Fechados por responsável", value=str(admin_closed_total), inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ponto_exportar", description="[Responsável] Exporta registros de ponto em CSV.")
    async def ponto_exportar(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Apenas em servidores.", ephemeral=True)
            return

        if not _has_admin_permission(interaction.user):
            await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
            return

        path = Path(PONTO_EXPORT_CSV_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)

        async with _db_lock:
            with _get_db_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM ponto_sessions
                    WHERE guild_id = ?
                    ORDER BY started_at DESC
                    LIMIT 1000
                    """,
                    (interaction.guild.id,),
                ).fetchall()

        if not rows:
            await interaction.response.send_message(
                "📭 Nenhum registro encontrado para exportar.",
                ephemeral=True,
            )
            return

        header = [
            "id",
            "guild_id",
            "user_id",
            "username",
            "data",
            "entrada",
            "saida",
            "pausas",
            "tempo_total",
            "status",
            "closed_by",
            "closed_reason",
            "pending_reason",
        ]

        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=header)
            writer.writeheader()

            for row in rows:
                session = dict(row)
                writer.writerow(
                    {
                        "id": session["id"],
                        "guild_id": session["guild_id"],
                        "user_id": session["user_id"],
                        "username": session["username"],
                        "data": _fmt_date(session["started_at"]),
                        "entrada": _fmt_time(session["started_at"]),
                        "saida": _fmt_time(session["ended_at"]),
                        "pausas": _fmt_duration(session["total_pause_seconds"]),
                        "tempo_total": _fmt_duration(session["total_seconds"]),
                        "status": session["status"],
                        "closed_by": session.get("closed_by") or "",
                        "closed_reason": session.get("closed_reason") or "",
                        "pending_reason": session.get("pending_reason") or "",
                    }
                )

        await interaction.response.send_message(
            "📤 Exportação gerada:",
            file=discord.File(str(path)),
            ephemeral=True,
        )

    # --------------------------------------------------------
    # ALERTAS AUTOMÁTICOS
    # --------------------------------------------------------

    @tasks.loop(minutes=PONTO_ALERT_CHECK_MINUTES)
    async def alertas_ponto(self) -> None:
        await self.bot.wait_until_ready()

        now = _now_utc()
        alert_after = timedelta(hours=PONTO_ALERT_AFTER_HOURS)
        review_after = timedelta(hours=PONTO_REVIEW_AFTER_HOURS)

        for guild in self.bot.guilds:
            async with _db_lock:
                with _get_db_connection() as conn:
                    rows = conn.execute(
                        """
                        SELECT *
                        FROM ponto_sessions
                        WHERE guild_id = ?
                          AND ended_at IS NULL
                          AND status IN ('OPEN', 'PAUSED', 'PENDING_REVIEW')
                        """,
                        (guild.id,),
                    ).fetchall()

                    sessions = [dict(row) for row in rows]

                    for session in sessions:
                        started_at = _parse_iso(session["started_at"])
                        if not started_at:
                            continue

                        elapsed = now - started_at
                        user_id = int(session["user_id"])

                        # Primeiro alerta: ponto aberto por muito tempo.
                        if elapsed >= alert_after and not session.get("last_alert_at"):
                            conn.execute(
                                """
                                UPDATE ponto_sessions
                                SET last_alert_at = ?, updated_at = ?
                                WHERE id = ?
                                """,
                                (_iso(now), _iso(now), session["id"]),
                            )

                            _audit(
                                conn,
                                session_id=session["id"],
                                guild_id=guild.id,
                                user_id=user_id,
                                event_type="AUTO_ALERT_SENT",
                                message=f"Alerta automático enviado após {_fmt_duration(elapsed.total_seconds())}.",
                                actor_id=None,
                            )

                        # Segundo estágio: pendente de revisão.
                        if elapsed >= review_after and session["status"] != "PENDING_REVIEW":
                            reason = (
                                f"Ponto aberto por mais de {PONTO_REVIEW_AFTER_HOURS:g}h "
                                "sem fechamento."
                            )

                            conn.execute(
                                """
                                UPDATE ponto_sessions
                                SET status = 'PENDING_REVIEW',
                                    pending_reason = ?,
                                    updated_at = ?
                                WHERE id = ?
                                """,
                                (reason, _iso(now), session["id"]),
                            )

                            _audit(
                                conn,
                                session_id=session["id"],
                                guild_id=guild.id,
                                user_id=user_id,
                                event_type="MARKED_PENDING_REVIEW",
                                message=reason,
                                actor_id=None,
                            )

                    conn.commit()

            # Envia DMs fora da transação.
            for session in sessions:
                started_at = _parse_iso(session["started_at"])
                if not started_at:
                    continue

                elapsed = now - started_at
                member = guild.get_member(int(session["user_id"]))

                if not member:
                    continue

                # Envia alerta somente uma vez, logo após cruzar o limite.
                if elapsed >= alert_after and not session.get("last_alert_at"):
                    try:
                        await member.send(
                            "⚠️ Seu ponto está aberto há bastante tempo.\n\n"
                            f"Entrada: **{_fmt_datetime(session['started_at'])}**\n"
                            f"Tempo aberto: **{_fmt_duration(elapsed.total_seconds())}**\n\n"
                            "Se ainda estiver jogando, pode continuar normalmente. "
                            f"Se não estiver, volte ao canal **#{PONTO_PANEL_CHANNEL_NAME}** e clique em **Saída**."
                        )
                    except Exception:
                        pass

                if elapsed >= review_after and session["status"] != "PENDING_REVIEW":
                    try:
                        await member.send(
                            "⚠️ Seu ponto foi marcado como **pendente de revisão** "
                            "porque ficou aberto por muito tempo.\n\n"
                            "Um responsável poderá validar ou fechar seu ponto manualmente."
                        )
                    except Exception:
                        pass

    @alertas_ponto.before_loop
    async def before_alertas_ponto(self) -> None:
        await self.bot.wait_until_ready()

# --------------------------------------------------------
    # PONTO AUTOMÁTICO VIA CALL DE VOZ
    # --------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return

        # Busca os canais permitidos no config.py
        allowed_channels = getattr(config, "PONTO_VOICE_CHANNELS", [])
        if not allowed_channels:
            return

        before_name = before.channel.name if before.channel else ""
        after_name = after.channel.name if after.channel else ""

        # Lógica: O jogador entrou em um canal de serviço vindo de outro lugar
        joined_service = (after.channel is not None and after_name in allowed_channels and before_name not in allowed_channels)
        
        # Lógica: O jogador estava em serviço e foi para um canal comum ou desconectou
        left_service = (before.channel is not None and before_name in allowed_channels and after_name not in allowed_channels)

        if joined_service:
            await self._auto_start_ponto(member)
        elif left_service:
            await self._auto_stop_ponto(member)

    async def _auto_start_ponto(self, member: discord.Member) -> None:
        now = _now_utc()
        async with _db_lock:
            with _get_db_connection() as conn:
                # Verifica se o jogador já não abriu o ponto manualmente no painel
                existing = _get_active_session(conn, guild_id=member.guild.id, user_id=member.id)
                if existing:
                    return

                cursor = conn.execute(
                    """
                    INSERT INTO ponto_sessions (
                        guild_id, user_id, username, started_at, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 'OPEN', ?, ?)
                    """,
                    (member.guild.id, member.id, str(member), _iso(now), _iso(now), _iso(now))
                )
                session_id = cursor.lastrowid

                _audit(
                    conn,
                    session_id=session_id,
                    guild_id=member.guild.id,
                    user_id=member.id,
                    event_type="AUTO_STARTED",
                    message="Ponto iniciado automaticamente via Call de Voz.",
                    actor_id=member.id
                )
                conn.commit()

        # Envia DM para o jogador avisando que começou
        try:
            await member.send("🟢 **Ponto Automático Iniciado!** Detectei que você entrou na call de serviço.\n*(Se entrou por engano, não se preocupe, basta sair da call que ele fecha sozinho)*")
        except discord.Forbidden:
            pass # Ignora se a DM do usuário for fechada

    async def _auto_stop_ponto(self, member: discord.Member) -> None:
        # Reutiliza sua função de fechamento que já manda log e calcula tudo!
        result = await self._close_session(
            guild=member.guild,
            user_id=member.id,
            actor_id=member.id,
            closed_by_admin=False,
            reason="Saída automática (Saiu da Call de Serviço)"
        )

        if result["ok"]:
            try:
                await member.send(f"🔴 **Ponto Automático Encerrado!** Detectei que você saiu da call.\n{result['message']}")
            except discord.Forbidden:
                pass
# ============================================================
# SETUP
# ============================================================

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PontoSystem(bot))
