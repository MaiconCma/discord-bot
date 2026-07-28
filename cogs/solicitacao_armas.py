from __future__ import annotations

"""
Sistema simplificado de solicitações de armas/itens para GTA RP.

Fluxo:
1. Um pedido é criado pelo painel ou por /registrarpedido.
2. O pedido é publicado em 📋-pedidos-armas com o botão "Marcar como entregue".
3. Um cargo administrativo confirma a entrega pelo botão ou por /entregapedido.
4. A mensagem original é editada para "ENTREGUE", perde o botão e o registro
   completo é publicado em 🧾-logs-pedidos.
5. /desfazerentrega reabre o pedido, restaura o botão e apaga o registro do log.

Status disponíveis: solicitado e entregue.
"""

import asyncio
import json
import logging
import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands

import config


logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTES E CONFIGURAÇÕES
# ============================================================

STATUS_SOLICITADO = "solicitado"
STATUS_ENTREGUE = "entregue"

VALID_STATUSES = (STATUS_SOLICITADO, STATUS_ENTREGUE)
STATUS_ATIVOS = (STATUS_SOLICITADO,)

STATUS_LABELS = {
    STATUS_SOLICITADO: "Solicitado",
    STATUS_ENTREGUE: "Entregue",
}

STATUS_EMOJIS = {
    STATUS_SOLICITADO: "🟡",
    STATUS_ENTREGUE: "✅",
}

STATUS_COLORS = {
    STATUS_SOLICITADO: discord.Color.gold(),
    STATUS_ENTREGUE: discord.Color.green(),
}

LIST_STATUS_CHOICES = [
    app_commands.Choice(name="Todos", value="todos"),
    app_commands.Choice(name="Solicitados", value=STATUS_SOLICITADO),
    app_commands.Choice(name="Entregues", value=STATUS_ENTREGUE),
]

DEFAULT_PAGE_SIZE = 5
MAX_PAGE_SIZE = 8


def cfg(name: str, default: Any) -> Any:
    return getattr(config, name, default)


def page_size() -> int:
    try:
        value = int(cfg("SOLICITACAO_ARMAS_ITENS_POR_PAGINA", DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        value = DEFAULT_PAGE_SIZE
    return max(1, min(value, MAX_PAGE_SIZE))


def get_timezone() -> ZoneInfo:
    timezone_name = str(cfg("SOLICITACAO_ARMAS_TIMEZONE", "America/Bahia"))
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning("Fuso %s não encontrado. Usando UTC.", timezone_name)
        return ZoneInfo("UTC")


def now_local() -> datetime:
    return datetime.now(get_timezone())


def today_local() -> date:
    return now_local().date()


def _parse_role_ids(raw_ids: Any) -> set[int]:
    result: set[int] = set()

    if raw_ids is None:
        values: Iterable[Any] = []
    elif isinstance(raw_ids, (str, int)):
        values = [raw_ids]
    else:
        try:
            values = list(raw_ids)
        except TypeError:
            values = [raw_ids]

    for raw_id in values:
        try:
            result.add(int(raw_id))
        except (TypeError, ValueError):
            logger.warning("ID de cargo inválido ignorado: %r", raw_id)

    return result


def allowed_role_ids() -> set[int]:
    return _parse_role_ids(
        cfg(
            "SOLICITACAO_ARMAS_CARGOS_PERMITIDOS_IDS",
            cfg("CARGOS_PERMITIDOS_IDS", []),
        )
    )


def admin_role_ids() -> set[int]:
    return _parse_role_ids(
        cfg(
            "SOLICITACAO_ARMAS_ADMIN_ROLE_IDS",
            cfg(
                "SOLICITACAO_ARMAS_CARGOS_PERMITIDOS_IDS",
                cfg("CARGOS_PERMITIDOS_IDS", []),
            ),
        )
    )


def _member_has_any_role(member: discord.Member, role_ids: set[int]) -> bool:
    if member.guild_permissions.administrator:
        return True
    member_roles = {role.id for role in member.roles}
    return bool(member_roles.intersection(role_ids))


def has_order_permission(interaction: discord.Interaction) -> bool:
    return bool(
        interaction.guild
        and isinstance(interaction.user, discord.Member)
        and _member_has_any_role(interaction.user, allowed_role_ids())
    )


def has_delivery_permission(interaction: discord.Interaction) -> bool:
    return bool(
        interaction.guild
        and isinstance(interaction.user, discord.Member)
        and _member_has_any_role(interaction.user, admin_role_ids())
    )


# ============================================================
# RESPOSTAS E VALIDAÇÕES
# ============================================================

async def send_ephemeral(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    file: discord.File | None = None,
) -> None:
    kwargs: dict[str, Any] = {
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }

    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if file is not None:
        kwargs["file"] = file

    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


async def defer_ephemeral(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)


def normalize_text(
    value: str | None,
    *,
    field_name: str,
    min_length: int = 0,
    max_length: int,
    optional: bool = False,
) -> str | None:
    if value is None:
        if optional:
            return None
        raise ValueError(f"Informe {field_name.lower()}.")

    cleaned = " ".join(str(value).strip().split())
    if not cleaned:
        if optional:
            return None
        raise ValueError(f"Informe {field_name.lower()}.")

    if len(cleaned) < min_length:
        raise ValueError(
            f"{field_name} precisa ter pelo menos {min_length} caracteres."
        )
    if len(cleaned) > max_length:
        raise ValueError(
            f"{field_name} pode ter no máximo {max_length} caracteres."
        )

    return cleaned


def safe_display(
    value: str | None,
    limit: int,
    fallback: str = "Não informado",
) -> str:
    text = value or fallback
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(str(text)))
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def parse_quantity(raw_value: str | int) -> int:
    try:
        quantity = int(str(raw_value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("A quantidade precisa ser um número inteiro.") from exc

    if quantity < 1:
        raise ValueError("A quantidade precisa ser maior que zero.")
    if quantity > 100_000:
        raise ValueError("A quantidade informada é muito alta.")

    return quantity


def parse_brl_to_cents(raw_value: str | int | float | Decimal) -> int:
    value = str(raw_value).strip()
    value = (
        value.replace("R$", "")
        .replace("$", "")
        .replace(" ", "")
        .replace("\u00a0", "")
    )

    if not value:
        raise ValueError("Informe o valor unitário.")
    if value.startswith("-"):
        raise ValueError("O valor não pode ser negativo.")
    if value.startswith("+"):
        value = value[1:]

    if not re.fullmatch(r"\d+(?:[.,]\d+)*", value):
        raise ValueError("Valor inválido. Exemplos: 25000 ou 25.000,00.")

    if "," in value and "." in value:
        decimal_separator = "," if value.rfind(",") > value.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        integer_part, decimal_part = value.rsplit(decimal_separator, 1)

        if len(decimal_part) not in (1, 2):
            raise ValueError("A parte decimal deve ter no máximo 2 dígitos.")

        integer_part = integer_part.replace(thousands_separator, "")
        normalized = f"{integer_part}.{decimal_part}"
    elif "," in value:
        parts = value.split(",")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            normalized = f"{parts[0]}.{parts[1]}"
        elif all(part.isdigit() for part in parts) and all(
            len(part) == 3 for part in parts[1:]
        ):
            normalized = "".join(parts)
        else:
            raise ValueError("Valor monetário inválido.")
    elif "." in value:
        parts = value.split(".")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            normalized = f"{parts[0]}.{parts[1]}"
        elif all(part.isdigit() for part in parts) and all(
            len(part) == 3 for part in parts[1:]
        ):
            normalized = "".join(parts)
        else:
            raise ValueError("Valor monetário inválido.")
    else:
        normalized = value

    try:
        decimal_value = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("Valor monetário inválido.") from exc

    cents = int(
        (decimal_value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    if cents > 9_999_999_999_999:
        raise ValueError("O valor informado é muito alto.")

    return cents


def format_currency(cents: int) -> str:
    currency = str(cfg("SOLICITACAO_ARMAS_CURRENCY", "R$"))
    amount = Decimal(int(cents)) / Decimal(100)
    formatted = f"{amount:,.2f}"
    formatted = formatted.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{currency} {formatted}"


def parse_deadline(raw_value: str) -> date:
    value = raw_value.strip()
    for date_format in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    raise ValueError("Prazo inválido. Use DD/MM/AAAA.")


def validate_deadline(deadline: date) -> None:
    if deadline < today_local():
        raise ValueError("O prazo não pode ser anterior à data de hoje.")

    try:
        max_days = int(cfg("SOLICITACAO_ARMAS_PRAZO_MAXIMO_DIAS", 3650))
    except (TypeError, ValueError):
        max_days = 3650

    max_days = max(1, max_days)
    if (deadline - today_local()).days > max_days:
        raise ValueError(
            f"O prazo não pode ultrapassar {max_days} dias a partir de hoje."
        )


def format_date(raw_value: str | date | None) -> str:
    if raw_value is None:
        return "Não informado"

    if isinstance(raw_value, date):
        parsed = raw_value
    else:
        try:
            parsed = date.fromisoformat(str(raw_value))
        except ValueError:
            return str(raw_value)

    return parsed.strftime("%d/%m/%Y")


def format_datetime(raw_value: str | None) -> str:
    if not raw_value:
        return "Não informado"

    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return raw_value

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(get_timezone())

    return parsed.strftime("%d/%m/%Y às %H:%M")


def status_text(status: str) -> str:
    return f"{STATUS_EMOJIS.get(status, '⚪')} {STATUS_LABELS.get(status, status)}"


def order_is_overdue(order: dict[str, Any]) -> bool:
    if str(order.get("status")) != STATUS_SOLICITADO:
        return False

    try:
        deadline = date.fromisoformat(str(order["prazo_maximo"]))
    except (KeyError, TypeError, ValueError):
        return False

    return deadline < today_local()


# ============================================================
# BANCO DE DADOS
# ============================================================


def database_path() -> Path:
    path = Path(str(cfg("SOLICITACAO_ARMAS_DB_PATH", "data/solicitacao_armas.db")))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(str(database_path()), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def _ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    if column_name not in _table_columns(connection, table_name):
        connection.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
        )


def init_database() -> None:
    with get_connection() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS solicitacoes_armas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                faccao TEXT NOT NULL,
                contato TEXT,
                arma TEXT NOT NULL,
                quantidade INTEGER NOT NULL CHECK (quantidade > 0),
                valor_unitario_centavos INTEGER NOT NULL
                    CHECK (valor_unitario_centavos >= 0),
                valor_total_centavos INTEGER NOT NULL
                    CHECK (valor_total_centavos >= 0),
                data_pedido TEXT NOT NULL,
                prazo_maximo TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'solicitado',
                observacoes TEXT,
                criado_por_id INTEGER NOT NULL,
                criado_por_nome TEXT NOT NULL,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL,
                entregue_em TEXT,
                mensagem_canal_id INTEGER,
                mensagem_id INTEGER,
                log_canal_id INTEGER,
                log_mensagem_id INTEGER,
                entregue_por_id INTEGER,
                entregue_por_nome TEXT
            )
            """
        )

        for column_name, definition in (
            ("mensagem_canal_id", "INTEGER"),
            ("mensagem_id", "INTEGER"),
            ("log_canal_id", "INTEGER"),
            ("log_mensagem_id", "INTEGER"),
            ("entregue_por_id", "INTEGER"),
            ("entregue_por_nome", "TEXT"),
        ):
            _ensure_column(
                connection,
                "solicitacoes_armas",
                column_name,
                definition,
            )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS solicitacoes_armas_historico (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                pedido_id INTEGER NOT NULL,
                acao TEXT NOT NULL,
                status_anterior TEXT,
                status_novo TEXT,
                ator_id INTEGER,
                ator_nome TEXT,
                detalhes_json TEXT,
                criado_em TEXT NOT NULL
            )
            """
        )

        # Migração dos status antigos para o novo fluxo simplificado.
        connection.execute(
            """
            UPDATE solicitacoes_armas
            SET status = ?, entregue_em = NULL,
                entregue_por_id = NULL, entregue_por_nome = NULL
            WHERE status NOT IN (?, ?)
            """,
            (STATUS_SOLICITADO, STATUS_SOLICITADO, STATUS_ENTREGUE),
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_solicitacoes_guild_status
            ON solicitacoes_armas (guild_id, status)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_solicitacoes_guild_prazo
            ON solicitacoes_armas (guild_id, prazo_maximo)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_solicitacoes_historico_pedido
            ON solicitacoes_armas_historico (guild_id, pedido_id, criado_em)
            """
        )
        connection.commit()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _record_history(
    connection: sqlite3.Connection,
    *,
    guild_id: int,
    order_id: int,
    action: str,
    actor_id: int | None,
    actor_name: str | None,
    old_status: str | None = None,
    new_status: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO solicitacoes_armas_historico (
            guild_id, pedido_id, acao, status_anterior, status_novo,
            ator_id, ator_nome, detalhes_json, criado_em
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            order_id,
            action,
            old_status,
            new_status,
            actor_id,
            actor_name,
            json.dumps(details or {}, ensure_ascii=False, default=str),
            now_local().isoformat(),
        ),
    )


def create_order(
    *,
    guild_id: int,
    faccao: str,
    arma: str,
    quantidade: int,
    valor_unitario_centavos: int,
    prazo_maximo: date,
    criado_por_id: int,
    criado_por_nome: str,
) -> int:
    current_time = now_local()
    total_cents = quantidade * valor_unitario_centavos

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            INSERT INTO solicitacoes_armas (
                guild_id, faccao, contato, arma, quantidade,
                valor_unitario_centavos, valor_total_centavos,
                data_pedido, prazo_maximo, status, observacoes,
                criado_por_id, criado_por_nome, criado_em, atualizado_em
            )
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                guild_id,
                faccao,
                arma,
                quantidade,
                valor_unitario_centavos,
                total_cents,
                current_time.date().isoformat(),
                prazo_maximo.isoformat(),
                STATUS_SOLICITADO,
                criado_por_id,
                criado_por_nome,
                current_time.isoformat(),
                current_time.isoformat(),
            ),
        )

        last_row_id = cursor.lastrowid
        if last_row_id is None:
            row = connection.execute("SELECT last_insert_rowid() AS id").fetchone()
            if row is None or row["id"] is None:
                connection.rollback()
                raise RuntimeError("Não foi possível recuperar o ID do pedido.")
            order_id = int(row["id"])
        else:
            order_id = int(last_row_id)

        _record_history(
            connection,
            guild_id=guild_id,
            order_id=order_id,
            action="SOLICITADO",
            actor_id=criado_por_id,
            actor_name=criado_por_nome,
            new_status=STATUS_SOLICITADO,
            details={
                "faccao": faccao,
                "arma": arma,
                "quantidade": quantidade,
                "valor_unitario_centavos": valor_unitario_centavos,
                "prazo_maximo": prazo_maximo.isoformat(),
            },
        )
        connection.commit()
        return order_id


def get_order(guild_id: int, order_id: int) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT * FROM solicitacoes_armas
            WHERE guild_id = ? AND id = ?
            """,
            (guild_id, order_id),
        ).fetchone()
    return _row_to_dict(row)


def list_orders(
    guild_id: int,
    statuses: Sequence[str] | None = None,
    *,
    overdue_only: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> list[dict[str, Any]]:
    params: list[Any] = [guild_id]
    conditions = ["guild_id = ?"]

    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        conditions.append(f"status IN ({placeholders})")
        params.extend(statuses)

    if overdue_only:
        conditions.append("status = ?")
        conditions.append("prazo_maximo < ?")
        params.extend([STATUS_SOLICITADO, today_local().isoformat()])

    params.extend([limit, offset])

    query = f"""
        SELECT * FROM solicitacoes_armas
        WHERE {' AND '.join(conditions)}
        ORDER BY
            CASE status
                WHEN '{STATUS_SOLICITADO}' THEN 1
                WHEN '{STATUS_ENTREGUE}' THEN 2
                ELSE 3
            END,
            prazo_maximo ASC,
            id DESC
        LIMIT ? OFFSET ?
    """

    with get_connection() as connection:
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def count_orders(
    guild_id: int,
    statuses: Sequence[str] | None = None,
    *,
    overdue_only: bool = False,
) -> int:
    params: list[Any] = [guild_id]
    conditions = ["guild_id = ?"]

    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        conditions.append(f"status IN ({placeholders})")
        params.extend(statuses)

    if overdue_only:
        conditions.append("status = ?")
        conditions.append("prazo_maximo < ?")
        params.extend([STATUS_SOLICITADO, today_local().isoformat()])

    with get_connection() as connection:
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM solicitacoes_armas
            WHERE {' AND '.join(conditions)}
            """,
            params,
        ).fetchone()

    return int(row["total"]) if row else 0


def mark_order_delivered(
    guild_id: int,
    order_id: int,
    *,
    actor_id: int,
    actor_name: str,
) -> tuple[dict[str, Any] | None, bool]:
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT * FROM solicitacoes_armas
            WHERE guild_id = ? AND id = ?
            """,
            (guild_id, order_id),
        ).fetchone()
        current = _row_to_dict(row)

        if not current:
            connection.rollback()
            return None, False

        if str(current["status"]) == STATUS_ENTREGUE:
            connection.rollback()
            return current, False

        current_time = now_local().isoformat()
        connection.execute(
            """
            UPDATE solicitacoes_armas
            SET status = ?, atualizado_em = ?, entregue_em = ?,
                entregue_por_id = ?, entregue_por_nome = ?
            WHERE guild_id = ? AND id = ?
            """,
            (
                STATUS_ENTREGUE,
                current_time,
                current_time,
                actor_id,
                actor_name,
                guild_id,
                order_id,
            ),
        )

        _record_history(
            connection,
            guild_id=guild_id,
            order_id=order_id,
            action="ENTREGA_CONFIRMADA",
            actor_id=actor_id,
            actor_name=actor_name,
            old_status=str(current["status"]),
            new_status=STATUS_ENTREGUE,
        )
        connection.commit()

    return get_order(guild_id, order_id), True


def reopen_delivered_order(
    guild_id: int,
    order_id: int,
    *,
    actor_id: int,
    actor_name: str,
) -> tuple[dict[str, Any] | None, bool]:
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT * FROM solicitacoes_armas
            WHERE guild_id = ? AND id = ?
            """,
            (guild_id, order_id),
        ).fetchone()
        current = _row_to_dict(row)

        if not current:
            connection.rollback()
            return None, False

        if str(current["status"]) != STATUS_ENTREGUE:
            connection.rollback()
            return current, False

        connection.execute(
            """
            UPDATE solicitacoes_armas
            SET status = ?, atualizado_em = ?, entregue_em = NULL,
                entregue_por_id = NULL, entregue_por_nome = NULL
            WHERE guild_id = ? AND id = ?
            """,
            (STATUS_SOLICITADO, now_local().isoformat(), guild_id, order_id),
        )

        _record_history(
            connection,
            guild_id=guild_id,
            order_id=order_id,
            action="ENTREGA_DESFEITA",
            actor_id=actor_id,
            actor_name=actor_name,
            old_status=STATUS_ENTREGUE,
            new_status=STATUS_SOLICITADO,
        )
        connection.commit()

    return get_order(guild_id, order_id), True


def update_order_data(
    guild_id: int,
    order_id: int,
    *,
    actor_id: int,
    actor_name: str,
    faccao: str | None = None,
    arma: str | None = None,
    quantidade: int | None = None,
    valor_unitario_centavos: int | None = None,
    prazo_maximo: date | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT * FROM solicitacoes_armas
            WHERE guild_id = ? AND id = ?
            """,
            (guild_id, order_id),
        ).fetchone()
        current = _row_to_dict(row)

        if not current:
            connection.rollback()
            return None, {}

        new_values: dict[str, Any] = {
            "faccao": faccao if faccao is not None else current["faccao"],
            "arma": arma if arma is not None else current["arma"],
            "quantidade": quantidade if quantidade is not None else current["quantidade"],
            "valor_unitario_centavos": (
                valor_unitario_centavos
                if valor_unitario_centavos is not None
                else current["valor_unitario_centavos"]
            ),
            "prazo_maximo": (
                prazo_maximo.isoformat()
                if prazo_maximo is not None
                else current["prazo_maximo"]
            ),
        }
        new_values["valor_total_centavos"] = (
            int(new_values["quantidade"])
            * int(new_values["valor_unitario_centavos"])
        )

        tracked = (
            "faccao",
            "arma",
            "quantidade",
            "valor_unitario_centavos",
            "valor_total_centavos",
            "prazo_maximo",
        )
        changes = {
            field: {"antes": current.get(field), "depois": new_values.get(field)}
            for field in tracked
            if current.get(field) != new_values.get(field)
        }

        if not changes:
            connection.rollback()
            return current, {}

        connection.execute(
            """
            UPDATE solicitacoes_armas
            SET faccao = ?, arma = ?, quantidade = ?,
                valor_unitario_centavos = ?, valor_total_centavos = ?,
                prazo_maximo = ?, atualizado_em = ?
            WHERE guild_id = ? AND id = ?
            """,
            (
                new_values["faccao"],
                new_values["arma"],
                new_values["quantidade"],
                new_values["valor_unitario_centavos"],
                new_values["valor_total_centavos"],
                new_values["prazo_maximo"],
                now_local().isoformat(),
                guild_id,
                order_id,
            ),
        )

        _record_history(
            connection,
            guild_id=guild_id,
            order_id=order_id,
            action="DADOS_ALTERADOS",
            actor_id=actor_id,
            actor_name=actor_name,
            old_status=str(current["status"]),
            new_status=str(current["status"]),
            details={"alteracoes": changes},
        )
        connection.commit()

    return get_order(guild_id, order_id), changes


def delete_order(
    guild_id: int,
    order_id: int,
    *,
    actor_id: int,
    actor_name: str,
    reason: str,
) -> dict[str, Any] | None:
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT * FROM solicitacoes_armas
            WHERE guild_id = ? AND id = ?
            """,
            (guild_id, order_id),
        ).fetchone()
        existing = _row_to_dict(row)

        if not existing:
            connection.rollback()
            return None

        _record_history(
            connection,
            guild_id=guild_id,
            order_id=order_id,
            action="REMOVIDO",
            actor_id=actor_id,
            actor_name=actor_name,
            old_status=str(existing["status"]),
            details={"motivo": reason, "pedido": existing},
        )
        connection.execute(
            "DELETE FROM solicitacoes_armas WHERE guild_id = ? AND id = ?",
            (guild_id, order_id),
        )
        connection.commit()
        return existing


def set_order_message_reference(
    guild_id: int,
    order_id: int,
    channel_id: int | None,
    message_id: int | None,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE solicitacoes_armas
            SET mensagem_canal_id = ?, mensagem_id = ?
            WHERE guild_id = ? AND id = ?
            """,
            (channel_id, message_id, guild_id, order_id),
        )
        connection.commit()


def set_order_log_reference(
    guild_id: int,
    order_id: int,
    channel_id: int | None,
    message_id: int | None,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE solicitacoes_armas
            SET log_canal_id = ?, log_mensagem_id = ?
            WHERE guild_id = ? AND id = ?
            """,
            (channel_id, message_id, guild_id, order_id),
        )
        connection.commit()


def get_summary(guild_id: int) -> dict[str, Any]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT status, COUNT(*) AS pedidos,
                   COALESCE(SUM(quantidade), 0) AS itens,
                   COALESCE(SUM(valor_total_centavos), 0) AS valor
            FROM solicitacoes_armas
            WHERE guild_id = ?
            GROUP BY status
            """,
            (guild_id,),
        ).fetchall()
        overdue = connection.execute(
            """
            SELECT COUNT(*) AS total
            FROM solicitacoes_armas
            WHERE guild_id = ? AND status = ? AND prazo_maximo < ?
            """,
            (guild_id, STATUS_SOLICITADO, today_local().isoformat()),
        ).fetchone()

    by_status = {
        str(row["status"]): {
            "pedidos": int(row["pedidos"]),
            "itens": int(row["itens"]),
            "valor": int(row["valor"]),
        }
        for row in rows
    }
    return {
        "por_status": by_status,
        "atrasados": int(overdue["total"]) if overdue else 0,
    }


def get_order_history(
    guild_id: int,
    order_id: int,
    *,
    limit: int = 15,
) -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM solicitacoes_armas_historico
            WHERE guild_id = ? AND pedido_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (guild_id, order_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# ============================================================
# EMBEDS
# ============================================================


def order_embed(
    order: dict[str, Any],
    *,
    title_prefix: str = "📦 Pedido",
    compact: bool = False,
) -> discord.Embed:
    status = str(order.get("status") or STATUS_SOLICITADO)
    overdue = order_is_overdue(order)

    if status == STATUS_ENTREGUE:
        description = "## ✅ ENTREGUE"
    elif overdue:
        description = "## ⚠️ PRAZO ATRASADO\n🟡 Solicitado"
    else:
        description = "🟡 **Solicitado**"

    embed = discord.Embed(
        title=f"{title_prefix} #{order['id']}",
        description=description,
        color=(
            discord.Color.red()
            if overdue and status != STATUS_ENTREGUE
            else STATUS_COLORS.get(status, discord.Color.blurple())
        ),
        timestamp=now_local(),
    )

    # Cartão reduzido usado na mensagem oficial do canal de pedidos.
    if compact:
        embed.add_field(
            name="Facção",
            value=safe_display(order.get("faccao"), 100),
            inline=True,
        )
        embed.add_field(
            name="Pedido",
            value=(
                f"{safe_display(order.get('arma'), 80)} "
                f"× **{int(order['quantidade'])}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Total",
            value=format_currency(int(order["valor_total_centavos"])),
            inline=True,
        )
        embed.add_field(
            name="Prazo",
            value=format_date(order.get("prazo_maximo")),
            inline=True,
        )
        embed.add_field(
            name="Solicitado por",
            value=safe_display(order.get("criado_por_nome"), 100),
            inline=True,
        )

        if status == STATUS_ENTREGUE:
            embed.add_field(
                name="Confirmado por",
                value=safe_display(order.get("entregue_por_nome"), 100),
                inline=True,
            )
            embed.add_field(
                name="Entregue em",
                value=format_datetime(order.get("entregue_em")),
                inline=True,
            )

        embed.set_footer(
            text=(
                "Use /verpedido para detalhes"
                if status != STATUS_ENTREGUE
                else "Entrega concluída"
            )
        )
        return embed

    # Ficha completa: usada somente em /verpedido e no registro final de entrega.
    embed.add_field(
        name="Facção solicitante / recebedora",
        value=safe_display(order.get("faccao"), 100),
        inline=False,
    )
    embed.add_field(
        name="Item/arma",
        value=safe_display(order.get("arma"), 100),
        inline=False,
    )
    embed.add_field(name="Quantidade", value=str(order["quantidade"]), inline=True)
    embed.add_field(
        name="Valor unitário",
        value=format_currency(int(order["valor_unitario_centavos"])),
        inline=True,
    )
    embed.add_field(
        name="Valor total",
        value=format_currency(int(order["valor_total_centavos"])),
        inline=True,
    )
    embed.add_field(
        name="Data da solicitação",
        value=format_date(order.get("data_pedido")),
        inline=True,
    )
    embed.add_field(
        name="Prazo",
        value=format_date(order.get("prazo_maximo")),
        inline=True,
    )
    embed.add_field(
        name="Solicitado por",
        value=safe_display(order.get("criado_por_nome"), 100),
        inline=True,
    )

    if status == STATUS_ENTREGUE:
        embed.add_field(
            name="Entrega confirmada por",
            value=safe_display(order.get("entregue_por_nome"), 100),
            inline=True,
        )
        embed.add_field(
            name="Entregue em",
            value=format_datetime(order.get("entregue_em")),
            inline=True,
        )

    embed.set_footer(text="Sistema de pedidos GTA RP")
    return embed

def orders_list_embed(
    orders: Iterable[dict[str, Any]],
    *,
    title: str,
    total_count: int,
    page: int,
    total_pages: int,
) -> discord.Embed:
    order_list = list(orders)
    embed = discord.Embed(
        title=title,
        description=(
            f"Página **{page + 1}/{max(total_pages, 1)}** • "
            f"**{total_count}** pedido(s)."
            if order_list
            else "Nenhum pedido encontrado."
        ),
        color=discord.Color.blurple(),
        timestamp=now_local(),
    )

    for order in order_list:
        overdue = " • ⚠️ ATRASADO" if order_is_overdue(order) else ""
        status = str(order.get("status"))
        embed.add_field(
            name=(
                f"{STATUS_EMOJIS.get(status, '⚪')} #{order['id']} — "
                f"{safe_display(order.get('faccao'), 45)}{overdue}"
            ),
            value=(
                f"**Item:** {safe_display(order.get('arma'), 75)}\n"
                f"**Quantidade:** {order['quantidade']}\n"
                f"**Total:** {format_currency(int(order['valor_total_centavos']))}\n"
                f"**Prazo:** {format_date(order.get('prazo_maximo'))}\n"
                f"**Status:** {status_text(status)}"
            ),
            inline=False,
        )

    embed.set_footer(text="Use /verpedido para consultar todos os detalhes.")
    return embed


def summary_embed(summary: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title="📊 Resumo dos pedidos",
        color=discord.Color.dark_teal(),
        timestamp=now_local(),
    )

    for status in VALID_STATUSES:
        data = summary["por_status"].get(
            status,
            {"pedidos": 0, "itens": 0, "valor": 0},
        )
        embed.add_field(
            name=status_text(status),
            value=(
                f"Pedidos: **{data['pedidos']}**\n"
                f"Itens: **{data['itens']}**\n"
                f"Valor: **{format_currency(int(data['valor']))}**"
            ),
            inline=True,
        )

    embed.add_field(
        name="⚠️ Prazos atrasados",
        value=f"**{summary['atrasados']}** pedido(s)",
        inline=False,
    )
    return embed


def history_embed(order_id: int, rows: Sequence[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title=f"🧾 Histórico do pedido #{order_id}",
        color=discord.Color.dark_grey(),
        timestamp=now_local(),
    )

    if not rows:
        embed.description = "Nenhum histórico encontrado."
        return embed

    lines: list[str] = []
    for row in rows:
        action = str(row.get("acao") or "ALTERAÇÃO").replace("_", " ").title()
        actor = safe_display(row.get("ator_nome"), 60, "Sistema")
        created_at = format_datetime(row.get("criado_em"))
        old_status = row.get("status_anterior")
        new_status = row.get("status_novo")

        status_change = ""
        if old_status != new_status and (old_status or new_status):
            status_change = (
                f" • {STATUS_LABELS.get(str(old_status), str(old_status))} → "
                f"{STATUS_LABELS.get(str(new_status), str(new_status))}"
            )

        lines.append(
            f"**{action}**{status_change}\nPor: {actor} • {created_at}"
        )

    embed.description = "\n\n".join(lines[:15])
    return embed


# ============================================================
# CANAIS
# ============================================================


def _logs_overwrites(guild: discord.Guild) -> dict[Any, discord.PermissionOverwrite]:
    overwrites: dict[Any, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }

    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
        )

    for role_id in allowed_role_ids().union(admin_role_ids()):
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

    return overwrites


async def ensure_order_channels(
    guild: discord.Guild,
) -> tuple[discord.TextChannel | None, discord.TextChannel | None]:
    category_name = str(
        cfg("SOLICITACAO_ARMAS_CATEGORIA_NOME", "📦 - PEDIDOS RP")
    )
    orders_name = str(
        cfg("SOLICITACAO_ARMAS_CHANNEL_NAME", "📋-pedidos-armas")
    )
    logs_name = str(
        cfg("SOLICITACAO_ARMAS_LOG_CHANNEL_NAME", "🧾-logs-pedidos")
    )

    try:
        category = discord.utils.get(guild.categories, name=category_name)
        if category is None:
            category = await guild.create_category(
                category_name,
                reason="Sistema de solicitações de armas",
            )

        orders_channel = discord.utils.get(category.text_channels, name=orders_name)
        if orders_channel is None:
            orders_channel = await guild.create_text_channel(
                orders_name,
                category=category,
                topic="Pedidos solicitados e acompanhamento de entrega.",
            )

        logs_channel = discord.utils.get(category.text_channels, name=logs_name)
        if logs_channel is None:
            kwargs: dict[str, Any] = {
                "name": logs_name,
                "category": category,
                "topic": "Registro definitivo dos pedidos entregues.",
            }
            if bool(cfg("SOLICITACAO_ARMAS_LOGS_PRIVADOS", True)):
                kwargs["overwrites"] = _logs_overwrites(guild)
            logs_channel = await guild.create_text_channel(**kwargs)

        return orders_channel, logs_channel
    except discord.Forbidden:
        logger.warning("Sem permissão para preparar os canais na guild %s.", guild.id)
    except discord.HTTPException:
        logger.exception("Falha HTTP ao preparar canais na guild %s.", guild.id)

    return None, None


# ============================================================
# MODAL E VIEWS
# ============================================================


class NewOrderModal(discord.ui.Modal, title="Solicitar pedido"):
    faction = discord.ui.TextInput(
        label="Facção solicitante / recebedora",
        placeholder="Ex.: Ballas",
        min_length=2,
        max_length=100,
    )
    weapon = discord.ui.TextInput(
        label="Arma/item",
        placeholder="Ex.: Pistola .50",
        min_length=2,
        max_length=100,
    )
    quantity = discord.ui.TextInput(
        label="Quantidade",
        placeholder="Ex.: 10",
        min_length=1,
        max_length=6,
    )
    unit_value = discord.ui.TextInput(
        label="Valor unitário",
        placeholder="Ex.: 25.000 ou 25.000,00",
        min_length=1,
        max_length=30,
    )
    deadline = discord.ui.TextInput(
        label="Prazo",
        placeholder="DD/MM/AAAA",
        min_length=10,
        max_length=10,
    )

    def __init__(self, cog: "SolicitacaoArmas") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.register_order(
            interaction=interaction,
            faccao=self.faction.value,
            arma=self.weapon.value,
            quantidade=self.quantity.value,
            valor_unitario=self.unit_value.value,
            prazo_maximo=self.deadline.value,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        logger.error(
            "Erro no modal de solicitação",
            exc_info=(type(error), error, error.__traceback__),
        )
        await send_ephemeral(interaction, "❌ Erro inesperado ao criar o pedido.")


class OrderListView(discord.ui.View):
    def __init__(
        self,
        cog: "SolicitacaoArmas",
        *,
        guild_id: int,
        statuses: Sequence[str] | None,
        overdue_only: bool,
        title: str,
        total_count: int,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.statuses = tuple(statuses) if statuses else None
        self.overdue_only = overdue_only
        self.title = title
        self.total_count = total_count
        self.current_page = 0
        self._sync_buttons()

    @property
    def total_pages(self) -> int:
        return max(1, (self.total_count + page_size() - 1) // page_size())

    def _sync_buttons(self) -> None:
        self.previous_page.disabled = self.current_page <= 0
        self.next_page.disabled = self.current_page >= self.total_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await send_ephemeral(interaction, "❌ Esta lista pertence a outro servidor.")
            return False
        if not has_order_permission(interaction):
            await send_ephemeral(interaction, "❌ Você não pode consultar os pedidos.")
            return False
        return True

    async def _render(self, interaction: discord.Interaction) -> None:
        orders = await asyncio.to_thread(
            list_orders,
            self.guild_id,
            self.statuses,
            overdue_only=self.overdue_only,
            limit=page_size(),
            offset=self.current_page * page_size(),
        )
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=orders_list_embed(
                orders,
                title=self.title,
                total_count=self.total_count,
                page=self.current_page,
                total_pages=self.total_pages,
            ),
            view=self,
        )

    @discord.ui.button(label="Anterior", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.current_page = max(0, self.current_page - 1)
        await self._render(interaction)

    @discord.ui.button(label="Próxima", emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next_page(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.current_page = min(self.total_pages - 1, self.current_page + 1)
        await self._render(interaction)


class ConfirmDeliveryView(discord.ui.View):
    def __init__(self, cog: "SolicitacaoArmas", order_id: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.order_id = order_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if has_delivery_permission(interaction):
            return True
        await send_ephemeral(
            interaction,
            "❌ Apenas os cargos administrativos configurados podem confirmar entregas.",
        )
        return False

    @discord.ui.button(
        label="Confirmar entrega",
        emoji="✅",
        style=discord.ButtonStyle.success,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="⏳ Confirmando a entrega...",
            embed=None,
            view=None,
        )
        await self.cog.complete_order(interaction, self.order_id)

    @discord.ui.button(
        label="Cancelar",
        emoji="✖️",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="ℹ️ Confirmação de entrega cancelada.",
            embed=None,
            view=None,
        )


class DeliveryButtonView(discord.ui.View):
    """Botão persistente presente em todas as mensagens de pedidos solicitados."""

    def __init__(self, cog: "SolicitacaoArmas") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if has_delivery_permission(interaction):
            return True
        await send_ephemeral(
            interaction,
            "❌ Apenas os cargos administrativos configurados podem confirmar entregas.",
        )
        return False

    @discord.ui.button(
        label="Marcar como entregue",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="solicitacao_armas:marcar_entregue",
    )
    async def mark_delivered(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        order_id = self.cog.order_id_from_interaction_message(interaction)
        if order_id is None:
            await send_ephemeral(
                interaction,
                "❌ Não consegui identificar o número deste pedido.",
            )
            return
        await self.cog.prompt_delivery_confirmation(interaction, order_id)


class OrderPanelView(discord.ui.View):
    def __init__(self, cog: "SolicitacaoArmas") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if has_order_permission(interaction):
            return True
        await send_ephemeral(
            interaction,
            "❌ Você não possui permissão para usar o sistema de pedidos.",
        )
        return False

    @discord.ui.button(
        label="Solicitar pedido",
        emoji="➕",
        style=discord.ButtonStyle.success,
        custom_id="solicitacao_armas:novo_pedido",
        row=0,
    )
    async def new_order(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(NewOrderModal(self.cog))

    @discord.ui.button(
        label="Ver solicitados",
        emoji="📋",
        style=discord.ButtonStyle.primary,
        custom_id="solicitacao_armas:listar_solicitados",
        row=0,
    )
    async def requested_orders(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await self.cog.send_order_list(
            interaction,
            statuses=[STATUS_SOLICITADO],
            overdue_only=False,
            title="📋 Pedidos solicitados",
        )

    @discord.ui.button(
        label="Resumo",
        emoji="📊",
        style=discord.ButtonStyle.secondary,
        custom_id="solicitacao_armas:resumo",
        row=1,
    )
    async def summary(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await self.cog.send_summary(interaction)

    @discord.ui.button(
        label="Atrasados",
        emoji="⚠️",
        style=discord.ButtonStyle.danger,
        custom_id="solicitacao_armas:listar_atrasados",
        row=1,
    )
    async def overdue(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await self.cog.send_order_list(
            interaction,
            statuses=[STATUS_SOLICITADO],
            overdue_only=True,
            title="⚠️ Pedidos atrasados",
        )


# ============================================================
# COG
# ============================================================


class SolicitacaoArmas(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._write_lock = asyncio.Lock()
        self._restore_task: asyncio.Task[None] | None = None
        init_database()

    async def cog_load(self) -> None:
        self.bot.add_view(OrderPanelView(self))
        self.bot.add_view(DeliveryButtonView(self))
        self._restore_task = asyncio.create_task(
            self._restore_existing_order_messages()
        )

    def cog_unload(self) -> None:
        if self._restore_task and not self._restore_task.done():
            self._restore_task.cancel()

    async def _restore_existing_order_messages(self) -> None:
        await self.bot.wait_until_ready()

        for guild in self.bot.guilds:
            orders = await asyncio.to_thread(
                list_orders,
                guild.id,
                [STATUS_SOLICITADO],
                limit=10_000,
                offset=0,
            )
            for order in orders:
                try:
                    await self.refresh_public_order_message(guild, order)
                except Exception:
                    logger.exception(
                        "Falha ao restaurar a mensagem do pedido #%s",
                        order.get("id"),
                    )

    async def _run_write(self, func: Any, /, *args: Any, **kwargs: Any) -> Any:
        async with self._write_lock:
            return await asyncio.to_thread(func, *args, **kwargs)

    async def send_permission_error(
        self,
        interaction: discord.Interaction,
        *,
        delivery_only: bool = False,
    ) -> None:
        message = (
            "❌ Apenas os cargos administrativos configurados podem executar esta ação."
            if delivery_only
            else "❌ Você não possui permissão para gerenciar pedidos."
        )
        await send_ephemeral(interaction, message)

    def order_id_from_interaction_message(
        self,
        interaction: discord.Interaction,
    ) -> int | None:
        message = interaction.message
        if message is None or not message.embeds:
            return None

        title = message.embeds[0].title or ""
        match = re.search(r"#(\d+)", title)
        return int(match.group(1)) if match else None

    async def _fetch_text_channel(
        self,
        guild: discord.Guild,
        channel_id: int,
    ) -> discord.TextChannel | None:
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel

        try:
            fetched = await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

        return fetched if isinstance(fetched, discord.TextChannel) else None

    async def refresh_public_order_message(
        self,
        guild: discord.Guild,
        order: dict[str, Any],
    ) -> bool:
        channel_id = order.get("mensagem_canal_id")
        message_id = order.get("mensagem_id")
        if not channel_id or not message_id:
            return False

        channel = await self._fetch_text_channel(guild, int(channel_id))
        if channel is None:
            return False

        try:
            message = await channel.fetch_message(int(message_id))
            view: discord.ui.View | None = (
                None
                if str(order.get("status")) == STATUS_ENTREGUE
                else DeliveryButtonView(self)
            )
            await message.edit(
                embed=order_embed(order, compact=True),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False

    async def publish_new_order(
        self,
        guild: discord.Guild,
        order: dict[str, Any],
    ) -> None:
        orders_channel, _ = await ensure_order_channels(guild)
        if orders_channel is None:
            return

        message = await orders_channel.send(
            embed=order_embed(order, title_prefix="📦 Pedido solicitado", compact=True),
            view=DeliveryButtonView(self),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._run_write(
            set_order_message_reference,
            guild.id,
            int(order["id"]),
            orders_channel.id,
            message.id,
        )

    async def publish_delivery_log(
        self,
        guild: discord.Guild,
        order: dict[str, Any],
    ) -> bool:
        _, logs_channel = await ensure_order_channels(guild)
        if logs_channel is None:
            return False

        message = await logs_channel.send(
            embed=order_embed(order, title_prefix="✅ Pedido entregue"),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._run_write(
            set_order_log_reference,
            guild.id,
            int(order["id"]),
            logs_channel.id,
            message.id,
        )
        return True

    async def delete_delivery_log(
        self,
        guild: discord.Guild,
        order: dict[str, Any],
    ) -> bool:
        channel_id = order.get("log_canal_id")
        message_id = order.get("log_mensagem_id")

        if not channel_id or not message_id:
            await self._run_write(
                set_order_log_reference,
                guild.id,
                int(order["id"]),
                None,
                None,
            )
            return True

        channel = await self._fetch_text_channel(guild, int(channel_id))
        if channel is None:
            return False

        try:
            message = await channel.fetch_message(int(message_id))
            await message.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException):
            return False

        await self._run_write(
            set_order_log_reference,
            guild.id,
            int(order["id"]),
            None,
            None,
        )
        return True

    async def remove_discord_messages(
        self,
        guild: discord.Guild,
        order: dict[str, Any],
    ) -> None:
        for channel_key, message_key in (
            ("mensagem_canal_id", "mensagem_id"),
            ("log_canal_id", "log_mensagem_id"),
        ):
            channel_id = order.get(channel_key)
            message_id = order.get(message_key)
            if not channel_id or not message_id:
                continue

            channel = await self._fetch_text_channel(guild, int(channel_id))
            if channel is None:
                continue

            try:
                message = await channel.fetch_message(int(message_id))
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    async def register_order(
        self,
        *,
        interaction: discord.Interaction,
        faccao: str,
        arma: str,
        quantidade: str | int,
        valor_unitario: str,
        prazo_maximo: str,
    ) -> None:
        if not has_order_permission(interaction):
            await self.send_permission_error(interaction)
            return
        if not interaction.guild:
            await send_ephemeral(interaction, "❌ Use este sistema dentro de um servidor.")
            return

        try:
            parsed_faction = normalize_text(
                faccao,
                field_name="Facção",
                min_length=2,
                max_length=100,
            )
            parsed_weapon = normalize_text(
                arma,
                field_name="Arma/item",
                min_length=2,
                max_length=100,
            )
            parsed_quantity = parse_quantity(quantidade)
            parsed_value = parse_brl_to_cents(valor_unitario)
            parsed_deadline = parse_deadline(prazo_maximo)
            validate_deadline(parsed_deadline)
        except ValueError as error:
            await send_ephemeral(interaction, f"❌ {error}")
            return

        await defer_ephemeral(interaction)
        order_id = await self._run_write(
            create_order,
            guild_id=interaction.guild.id,
            faccao=str(parsed_faction),
            arma=str(parsed_weapon),
            quantidade=parsed_quantity,
            valor_unitario_centavos=parsed_value,
            prazo_maximo=parsed_deadline,
            criado_por_id=interaction.user.id,
            criado_por_nome=str(interaction.user),
        )
        order = await asyncio.to_thread(get_order, interaction.guild.id, order_id)

        if not order:
            await send_ephemeral(
                interaction,
                "❌ O pedido foi salvo, mas não consegui consultá-lo.",
            )
            return

        try:
            await self.publish_new_order(interaction.guild, order)
        except discord.HTTPException:
            logger.exception("Falha ao publicar o pedido #%s", order_id)

        await send_ephemeral(
            interaction,
            f"✅ Pedido **#{order_id}** solicitado e publicado no canal de pedidos.",
        )

    async def send_order_list(
        self,
        interaction: discord.Interaction,
        *,
        statuses: Sequence[str] | None,
        overdue_only: bool,
        title: str,
    ) -> None:
        if not has_order_permission(interaction):
            await self.send_permission_error(interaction)
            return
        if not interaction.guild:
            await send_ephemeral(interaction, "❌ Use este sistema dentro de um servidor.")
            return

        await defer_ephemeral(interaction)
        orders, total = await asyncio.gather(
            asyncio.to_thread(
                list_orders,
                interaction.guild.id,
                statuses,
                overdue_only=overdue_only,
                limit=page_size(),
                offset=0,
            ),
            asyncio.to_thread(
                count_orders,
                interaction.guild.id,
                statuses,
                overdue_only=overdue_only,
            ),
        )

        total_pages = max(1, (total + page_size() - 1) // page_size())
        view: discord.ui.View | None = None
        if total_pages > 1:
            view = OrderListView(
                self,
                guild_id=interaction.guild.id,
                statuses=statuses,
                overdue_only=overdue_only,
                title=title,
                total_count=total,
            )

        await send_ephemeral(
            interaction,
            embed=orders_list_embed(
                orders,
                title=title,
                total_count=total,
                page=0,
                total_pages=total_pages,
            ),
            view=view,
        )

    async def send_summary(self, interaction: discord.Interaction) -> None:
        if not has_order_permission(interaction):
            await self.send_permission_error(interaction)
            return
        if not interaction.guild:
            await send_ephemeral(interaction, "❌ Use este sistema dentro de um servidor.")
            return

        await defer_ephemeral(interaction)
        summary = await asyncio.to_thread(get_summary, interaction.guild.id)
        await send_ephemeral(interaction, embed=summary_embed(summary))

    async def prompt_delivery_confirmation(
        self,
        interaction: discord.Interaction,
        order_id: int,
    ) -> None:
        if not has_delivery_permission(interaction):
            await self.send_permission_error(interaction, delivery_only=True)
            return
        if not interaction.guild:
            await send_ephemeral(interaction, "❌ Use este sistema dentro de um servidor.")
            return

        order = await asyncio.to_thread(get_order, interaction.guild.id, order_id)
        if not order:
            await send_ephemeral(interaction, f"❌ Pedido **#{order_id}** não encontrado.")
            return
        if str(order.get("status")) == STATUS_ENTREGUE:
            await send_ephemeral(interaction, f"⚠️ O pedido **#{order_id}** já foi entregue.")
            return

        await send_ephemeral(
            interaction,
            (
                f"Confirma a entrega do pedido **#{order_id}**?\n"
                f"**Facção:** {safe_display(order.get('faccao'), 100)}\n"
                f"**Pedido:** {safe_display(order.get('arma'), 100)} "
                f"× **{int(order['quantidade'])}**\n"
                f"**Total:** {format_currency(int(order['valor_total_centavos']))}"
            ),
            view=ConfirmDeliveryView(self, order_id),
        )

    async def complete_order(
        self,
        interaction: discord.Interaction,
        order_id: int,
    ) -> None:
        if not has_delivery_permission(interaction):
            await self.send_permission_error(interaction, delivery_only=True)
            return
        if not interaction.guild:
            await send_ephemeral(interaction, "❌ Use este sistema dentro de um servidor.")
            return

        await defer_ephemeral(interaction)
        order, changed = await self._run_write(
            mark_order_delivered,
            interaction.guild.id,
            order_id,
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
        )

        if not order:
            await send_ephemeral(interaction, f"❌ Pedido **#{order_id}** não encontrado.")
            return
        if not changed:
            await send_ephemeral(interaction, f"⚠️ O pedido **#{order_id}** já foi entregue.")
            return

        public_updated = await self.refresh_public_order_message(
            interaction.guild,
            order,
        )

        log_created = False
        try:
            log_created = await self.publish_delivery_log(interaction.guild, order)
        except discord.HTTPException:
            logger.exception("Falha ao publicar entrega do pedido #%s", order_id)

        warnings: list[str] = []
        if not public_updated:
            warnings.append("não consegui editar a mensagem original")
        if not log_created:
            warnings.append("não consegui publicar no canal de logs")

        suffix = f"\n⚠️ Porém, {' e '.join(warnings)}." if warnings else ""
        await send_ephemeral(
            interaction,
            f"✅ Pedido **#{order_id}** marcado como entregue.{suffix}",
        )

    async def undo_delivery(
        self,
        interaction: discord.Interaction,
        order_id: int,
    ) -> None:
        if not has_delivery_permission(interaction):
            await self.send_permission_error(interaction, delivery_only=True)
            return
        if not interaction.guild:
            await send_ephemeral(interaction, "❌ Use este sistema dentro de um servidor.")
            return

        await defer_ephemeral(interaction)
        existing = await asyncio.to_thread(get_order, interaction.guild.id, order_id)
        if not existing:
            await send_ephemeral(interaction, f"❌ Pedido **#{order_id}** não encontrado.")
            return
        if str(existing.get("status")) != STATUS_ENTREGUE:
            await send_ephemeral(
                interaction,
                f"⚠️ O pedido **#{order_id}** ainda está como solicitado.",
            )
            return

        order, changed = await self._run_write(
            reopen_delivered_order,
            interaction.guild.id,
            order_id,
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
        )
        if not order or not changed:
            await send_ephemeral(interaction, "❌ Não foi possível desfazer a entrega.")
            return

        log_deleted = await self.delete_delivery_log(interaction.guild, existing)
        public_updated = await self.refresh_public_order_message(
            interaction.guild,
            order,
        )

        warnings: list[str] = []
        if not log_deleted:
            warnings.append("não consegui apagar o registro do canal de logs")
        if not public_updated:
            warnings.append("não consegui restaurar a mensagem original")

        suffix = f"\n⚠️ Porém, {' e '.join(warnings)}." if warnings else ""
        await send_ephemeral(
            interaction,
            f"↩️ Entrega do pedido **#{order_id}** desfeita.{suffix}",
        )

    # --------------------------------------------------------
    # COMANDOS
    # --------------------------------------------------------

    @app_commands.command(
        name="painelpedidos",
        description="Cria o painel do sistema de pedidos",
    )
    async def painelpedidos(self, interaction: discord.Interaction) -> None:
        if not has_order_permission(interaction):
            await self.send_permission_error(interaction)
            return
        if not interaction.guild:
            await send_ephemeral(interaction, "❌ Use este comando em um servidor.")
            return

        await defer_ephemeral(interaction)
        orders_channel, logs_channel = await ensure_order_channels(interaction.guild)

        channels = ""
        if orders_channel:
            channels += f"\n\n📋 Pedidos: {orders_channel.mention}"
        if logs_channel:
            channels += f"\n🧾 Entregas concluídas: {logs_channel.mention}"

        embed = discord.Embed(
            title="📦 Controle de pedidos — GTA RP",
            description=(
                "Use os quatro botões para solicitar e acompanhar pedidos.\n\n"
                "**Fluxo:** 🟡 Solicitado → ✅ Entregue"
                f"{channels}"
            ),
            color=discord.Color.dark_teal(),
        )
        embed.add_field(
            name="Comandos úteis",
            value=(
                "`/registrarpedido` — cria uma solicitação\n"
                "`/entregapedido` — confirma uma entrega\n"
                "`/desfazerentrega` — reabre uma entrega\n"
                "`/verpedido` — consulta detalhes\n"
                "`/editarpedido` — corrige o pedido\n"
                "`/listarpedidos` — lista pedidos\n"
                "`/historicopedido` — mostra o histórico"
            ),
            inline=False,
        )

        if isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.channel.send(
                embed=embed,
                view=OrderPanelView(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await send_ephemeral(interaction, "✅ Painel criado neste canal.")
        else:
            await send_ephemeral(interaction, "❌ Não identifiquei o canal atual.")

    @app_commands.command(
        name="registrarpedido",
        description="Registra um novo pedido",
    )
    @app_commands.describe(
        faccao="Facção solicitante e recebedora",
        arma="Arma ou item solicitado",
        quantidade="Quantidade solicitada",
        valor_unitario="Valor de cada unidade",
        prazo_maximo="Prazo no formato DD/MM/AAAA",
    )
    async def registrarpedido(
        self,
        interaction: discord.Interaction,
        faccao: str,
        arma: str,
        quantidade: app_commands.Range[int, 1, 100000],
        valor_unitario: str,
        prazo_maximo: str,
    ) -> None:
        await self.register_order(
            interaction=interaction,
            faccao=faccao,
            arma=arma,
            quantidade=int(quantidade),
            valor_unitario=valor_unitario,
            prazo_maximo=prazo_maximo,
        )

    @app_commands.command(
        name="entregapedido",
        description="Confirma que um pedido foi entregue",
    )
    @app_commands.describe(pedido_id="Número do pedido")
    async def entregapedido(
        self,
        interaction: discord.Interaction,
        pedido_id: app_commands.Range[int, 1, None],
    ) -> None:
        await self.prompt_delivery_confirmation(interaction, int(pedido_id))

    @app_commands.command(
        name="desfazerentrega",
        description="Desfaz a entrega e reabre o pedido",
    )
    @app_commands.describe(pedido_id="Número do pedido")
    async def desfazerentrega(
        self,
        interaction: discord.Interaction,
        pedido_id: app_commands.Range[int, 1, None],
    ) -> None:
        await self.undo_delivery(interaction, int(pedido_id))

    @app_commands.command(name="listarpedidos", description="Lista os pedidos")
    @app_commands.describe(status="Filtra por status")
    @app_commands.choices(status=LIST_STATUS_CHOICES)
    async def listarpedidos(
        self,
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
    ) -> None:
        selected = status.value if status else "todos"
        statuses = None if selected == "todos" else [selected]
        title = (
            "📋 Todos os pedidos"
            if selected == "todos"
            else f"📋 Pedidos — {STATUS_LABELS[selected]}"
        )
        await self.send_order_list(
            interaction,
            statuses=statuses,
            overdue_only=False,
            title=title,
        )

    @app_commands.command(name="verpedido", description="Mostra os detalhes de um pedido")
    @app_commands.describe(pedido_id="Número do pedido")
    async def verpedido(
        self,
        interaction: discord.Interaction,
        pedido_id: app_commands.Range[int, 1, None],
    ) -> None:
        if not has_order_permission(interaction):
            await self.send_permission_error(interaction)
            return
        if not interaction.guild:
            await send_ephemeral(interaction, "❌ Use este comando em um servidor.")
            return

        order = await asyncio.to_thread(get_order, interaction.guild.id, int(pedido_id))
        if not order:
            await send_ephemeral(interaction, f"❌ Pedido **#{pedido_id}** não encontrado.")
            return
        await send_ephemeral(interaction, embed=order_embed(order))

    @app_commands.command(name="editarpedido", description="Corrige os dados de um pedido")
    @app_commands.describe(
        pedido_id="Número do pedido",
        faccao="Nova facção",
        arma="Novo item/arma",
        quantidade="Nova quantidade",
        valor_unitario="Novo valor unitário",
        prazo_maximo="Novo prazo DD/MM/AAAA",
    )
    async def editarpedido(
        self,
        interaction: discord.Interaction,
        pedido_id: app_commands.Range[int, 1, None],
        faccao: str | None = None,
        arma: str | None = None,
        quantidade: app_commands.Range[int, 1, 100000] | None = None,
        valor_unitario: str | None = None,
        prazo_maximo: str | None = None,
    ) -> None:
        if not has_order_permission(interaction):
            await self.send_permission_error(interaction)
            return
        if not interaction.guild:
            await send_ephemeral(interaction, "❌ Use este comando em um servidor.")
            return
        if all(
            value is None
            for value in (faccao, arma, quantidade, valor_unitario, prazo_maximo)
        ):
            await send_ephemeral(interaction, "❌ Informe ao menos um campo para alterar.")
            return

        existing_order = await asyncio.to_thread(
            get_order,
            interaction.guild.id,
            int(pedido_id),
        )
        if not existing_order:
            await send_ephemeral(
                interaction,
                f"❌ Pedido **#{pedido_id}** não encontrado.",
            )
            return
        if str(existing_order.get("status")) == STATUS_ENTREGUE:
            await send_ephemeral(
                interaction,
                "⚠️ Um pedido entregue não pode ser editado. "
                "Use `/desfazerentrega` primeiro.",
            )
            return

        try:
            parsed_faction = normalize_text(
                faccao,
                field_name="Facção",
                min_length=2,
                max_length=100,
                optional=True,
            )
            parsed_weapon = normalize_text(
                arma,
                field_name="Arma/item",
                min_length=2,
                max_length=100,
                optional=True,
            )
            parsed_quantity = int(quantidade) if quantidade is not None else None
            parsed_value = (
                parse_brl_to_cents(valor_unitario)
                if valor_unitario is not None
                else None
            )
            parsed_deadline = (
                parse_deadline(prazo_maximo)
                if prazo_maximo is not None
                else None
            )
            if parsed_deadline:
                validate_deadline(parsed_deadline)
        except ValueError as error:
            await send_ephemeral(interaction, f"❌ {error}")
            return

        await defer_ephemeral(interaction)
        order, changes = await self._run_write(
            update_order_data,
            interaction.guild.id,
            int(pedido_id),
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
            faccao=parsed_faction,
            arma=parsed_weapon,
            quantidade=parsed_quantity,
            valor_unitario_centavos=parsed_value,
            prazo_maximo=parsed_deadline,
        )

        if not order:
            await send_ephemeral(interaction, f"❌ Pedido **#{pedido_id}** não encontrado.")
            return
        if not changes:
            await send_ephemeral(interaction, "ℹ️ Nenhuma informação foi alterada.")
            return

        await self.refresh_public_order_message(interaction.guild, order)
        await send_ephemeral(
            interaction,
            f"✅ Pedido **#{pedido_id}** atualizado na mensagem oficial.",
        )

    @app_commands.command(name="resumopedidos", description="Mostra o resumo dos pedidos")
    async def resumopedidos(self, interaction: discord.Interaction) -> None:
        await self.send_summary(interaction)

    @app_commands.command(
        name="historicopedido",
        description="Mostra o histórico de um pedido",
    )
    @app_commands.describe(pedido_id="Número do pedido")
    async def historicopedido(
        self,
        interaction: discord.Interaction,
        pedido_id: app_commands.Range[int, 1, None],
    ) -> None:
        if not has_order_permission(interaction):
            await self.send_permission_error(interaction)
            return
        if not interaction.guild:
            await send_ephemeral(interaction, "❌ Use este comando em um servidor.")
            return

        rows = await asyncio.to_thread(
            get_order_history,
            interaction.guild.id,
            int(pedido_id),
            limit=15,
        )
        await send_ephemeral(
            interaction,
            embed=history_embed(int(pedido_id), rows),
        )

    @app_commands.command(
        name="removerpedido",
        description="Remove definitivamente um pedido",
    )
    @app_commands.describe(
        pedido_id="Número do pedido",
        motivo="Motivo da remoção",
        confirmar="Marque verdadeiro para confirmar",
    )
    async def removerpedido(
        self,
        interaction: discord.Interaction,
        pedido_id: app_commands.Range[int, 1, None],
        motivo: str,
        confirmar: bool,
    ) -> None:
        if not has_delivery_permission(interaction):
            await self.send_permission_error(interaction, delivery_only=True)
            return
        if not interaction.guild:
            await send_ephemeral(interaction, "❌ Use este comando em um servidor.")
            return
        if not confirmar:
            await send_ephemeral(interaction, "ℹ️ Remoção cancelada.")
            return

        try:
            parsed_reason = normalize_text(
                motivo,
                field_name="Motivo",
                min_length=5,
                max_length=300,
            )
        except ValueError as error:
            await send_ephemeral(interaction, f"❌ {error}")
            return

        await defer_ephemeral(interaction)
        existing = await self._run_write(
            delete_order,
            interaction.guild.id,
            int(pedido_id),
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
            reason=str(parsed_reason),
        )
        if not existing:
            await send_ephemeral(interaction, f"❌ Pedido **#{pedido_id}** não encontrado.")
            return

        await self.remove_discord_messages(interaction.guild, existing)
        await send_ephemeral(interaction, f"🗑️ Pedido **#{pedido_id}** removido.")

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        original = getattr(error, "original", error)
        logger.error(
            "Erro não tratado em solicitacao_armas",
            exc_info=(type(original), original, original.__traceback__),
        )
        await send_ephemeral(
            interaction,
            "❌ Ocorreu um erro inesperado. Consulte o console do bot.",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SolicitacaoArmas(bot))
