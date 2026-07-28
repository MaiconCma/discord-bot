from __future__ import annotations

"""
Sistema de solicitações de armas/itens para GTA RP.

Recursos:
- Registro de pedidos feitos por outras facções.
- Data do pedido automática.
- Prazo máximo, quantidade, item/arma, valor e facção.
- Contato e observações opcionais.
- Controle de fabricação e entrega por status.
- Painel com botões.
- Banco SQLite separado por servidor.
- Exportação em CSV.
- Permissões por cargos configurados no config.py.

Este módulo é destinado ao gerenciamento de itens dentro de servidor de GTA RP.
"""

import asyncio
import csv
import io
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


# ============================================================
# CONFIGURAÇÕES E CONSTANTES
# ============================================================

STATUS_AGUARDANDO = "aguardando_fabricacao"
STATUS_FABRICACAO = "em_fabricacao"
STATUS_PRONTO = "pronto_para_entrega"
STATUS_ENTREGUE = "entregue"
STATUS_CANCELADO = "cancelado"

STATUS_ATIVOS = (
    STATUS_AGUARDANDO,
    STATUS_FABRICACAO,
    STATUS_PRONTO,
)

STATUS_LABELS = {
    STATUS_AGUARDANDO: "Aguardando fabricação",
    STATUS_FABRICACAO: "Em fabricação",
    STATUS_PRONTO: "Pronto para entrega",
    STATUS_ENTREGUE: "Entregue",
    STATUS_CANCELADO: "Cancelado",
}

STATUS_EMOJIS = {
    STATUS_AGUARDANDO: "🟡",
    STATUS_FABRICACAO: "🛠️",
    STATUS_PRONTO: "🟢",
    STATUS_ENTREGUE: "✅",
    STATUS_CANCELADO: "❌",
}

STATUS_COLORS = {
    STATUS_AGUARDANDO: discord.Color.gold(),
    STATUS_FABRICACAO: discord.Color.orange(),
    STATUS_PRONTO: discord.Color.green(),
    STATUS_ENTREGUE: discord.Color.blue(),
    STATUS_CANCELADO: discord.Color.red(),
}

STATUS_CHOICES = [
    app_commands.Choice(name="Aguardando fabricação", value=STATUS_AGUARDANDO),
    app_commands.Choice(name="Em fabricação", value=STATUS_FABRICACAO),
    app_commands.Choice(name="Pronto para entrega", value=STATUS_PRONTO),
    app_commands.Choice(name="Entregue", value=STATUS_ENTREGUE),
    app_commands.Choice(name="Cancelado", value=STATUS_CANCELADO),
]

LIST_STATUS_CHOICES = [
    app_commands.Choice(name="Todos", value="todos"),
    *STATUS_CHOICES,
]

MAX_LIST_ITEMS = 25


def cfg(name: str, default: Any) -> Any:
    """Lê uma configuração do config.py e usa um padrão se ela não existir."""
    return getattr(config, name, default)


def get_timezone() -> ZoneInfo:
    timezone_name = str(cfg("SOLICITACAO_ARMAS_TIMEZONE", "America/Bahia"))
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def now_local() -> datetime:
    return datetime.now(get_timezone())


def today_local() -> date:
    return now_local().date()


def allowed_role_ids() -> set[int]:
    configured = cfg(
        "SOLICITACAO_ARMAS_CARGOS_PERMITIDOS_IDS",
        cfg("CARGOS_PERMITIDOS_IDS", []),
    )
    return {int(role_id) for role_id in configured}


def has_order_permission(interaction: discord.Interaction) -> bool:
    """
    Permite:
    - Administradores;
    - Usuários com um dos cargos configurados.
    """
    if not interaction.guild:
        return False

    if not isinstance(interaction.user, discord.Member):
        return False

    if interaction.user.guild_permissions.administrator:
        return True

    permitted = allowed_role_ids()
    if not permitted:
        return False

    user_roles = {role.id for role in interaction.user.roles}
    return bool(user_roles.intersection(permitted))


def ensure_guild_interaction(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None


# ============================================================
# FORMATAÇÃO E VALIDAÇÃO
# ============================================================

def clean_text(value: str, max_length: int) -> str:
    cleaned = " ".join(value.strip().split())
    return cleaned[:max_length]


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
    """
    Aceita exemplos:
    - 25000
    - 25.000
    - 25.000,50
    - 25000,50
    - R$ 25.000,50
    """
    value = str(raw_value).strip()
    value = (
        value.replace("R$", "")
        .replace("$", "")
        .replace(" ", "")
        .replace("\u00a0", "")
    )

    if not value:
        raise ValueError("Informe o valor unitário.")

    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    elif value.count(".") > 1:
        value = value.replace(".", "")
    elif "." in value:
        integer_part, decimal_part = value.rsplit(".", 1)
        # Em pt-BR, um ponto seguido de três dígitos normalmente é milhar.
        if len(decimal_part) == 3 and integer_part.replace("-", "").isdigit():
            value = integer_part + decimal_part

    try:
        decimal_value = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(
            "Valor inválido. Exemplos aceitos: 25000 ou 25.000,00."
        ) from exc

    if decimal_value < 0:
        raise ValueError("O valor não pode ser negativo.")

    cents = int(
        (decimal_value * 100).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )

    if cents > 9_999_999_999_999:
        raise ValueError("O valor informado é muito alto.")

    return cents


def format_currency(cents: int) -> str:
    currency = str(cfg("SOLICITACAO_ARMAS_CURRENCY", "R$"))
    amount = Decimal(cents) / Decimal(100)
    formatted = f"{amount:,.2f}"
    formatted = formatted.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{currency} {formatted}"


def parse_deadline(raw_value: str) -> date:
    value = raw_value.strip()
    accepted_formats = ("%d/%m/%Y", "%Y-%m-%d")

    for date_format in accepted_formats:
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue

    raise ValueError("Prazo inválido. Use o formato DD/MM/AAAA.")


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

    return parsed.strftime("%d/%m/%Y às %H:%M")


def truncate(value: str | None, limit: int, fallback: str = "Não informado") -> str:
    text = value or fallback
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def order_is_overdue(order: dict[str, Any]) -> bool:
    if order["status"] not in STATUS_ATIVOS:
        return False

    try:
        deadline = date.fromisoformat(order["prazo_maximo"])
    except (TypeError, ValueError):
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
    connection = sqlite3.connect(
        database_path(),
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def init_database() -> None:
    with get_connection() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS solicitacoes_armas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                faccao TEXT NOT NULL,
                contato TEXT,
                arma TEXT NOT NULL,
                quantidade INTEGER NOT NULL CHECK (quantidade > 0),
                valor_unitario_centavos INTEGER NOT NULL CHECK (valor_unitario_centavos >= 0),
                valor_total_centavos INTEGER NOT NULL CHECK (valor_total_centavos >= 0),
                data_pedido TEXT NOT NULL,
                prazo_maximo TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'aguardando_fabricacao',
                observacoes TEXT,
                criado_por_id INTEGER NOT NULL,
                criado_por_nome TEXT NOT NULL,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL,
                entregue_em TEXT
            )
            """
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
        connection.commit()


def create_order(
    *,
    guild_id: int,
    faccao: str,
    contato: str | None,
    arma: str,
    quantidade: int,
    valor_unitario_centavos: int,
    prazo_maximo: date,
    observacoes: str | None,
    criado_por_id: int,
    criado_por_nome: str,
) -> int:
    current_time = now_local()
    data_pedido = current_time.date().isoformat()
    created_at = current_time.isoformat()
    total_cents = quantidade * valor_unitario_centavos

    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO solicitacoes_armas (
                guild_id,
                faccao,
                contato,
                arma,
                quantidade,
                valor_unitario_centavos,
                valor_total_centavos,
                data_pedido,
                prazo_maximo,
                status,
                observacoes,
                criado_por_id,
                criado_por_nome,
                criado_em,
                atualizado_em
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                faccao,
                contato,
                arma,
                quantidade,
                valor_unitario_centavos,
                total_cents,
                data_pedido,
                prazo_maximo.isoformat(),
                STATUS_AGUARDANDO,
                observacoes,
                criado_por_id,
                criado_por_nome,
                created_at,
                created_at,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def get_order(guild_id: int, order_id: int) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM solicitacoes_armas
            WHERE guild_id = ? AND id = ?
            """,
            (guild_id, order_id),
        ).fetchone()

    return dict(row) if row else None


def list_orders(
    guild_id: int,
    statuses: Sequence[str] | None = None,
    *,
    limit: int = MAX_LIST_ITEMS,
    offset: int = 0,
) -> list[dict[str, Any]]:
    params: list[Any] = [guild_id]
    where = "WHERE guild_id = ?"

    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        where += f" AND status IN ({placeholders})"
        params.extend(statuses)

    params.extend([limit, offset])

    query = f"""
        SELECT *
        FROM solicitacoes_armas
        {where}
        ORDER BY
            CASE status
                WHEN '{STATUS_AGUARDANDO}' THEN 1
                WHEN '{STATUS_FABRICACAO}' THEN 2
                WHEN '{STATUS_PRONTO}' THEN 3
                WHEN '{STATUS_ENTREGUE}' THEN 4
                WHEN '{STATUS_CANCELADO}' THEN 5
                ELSE 6
            END,
            prazo_maximo ASC,
            id DESC
        LIMIT ? OFFSET ?
    """

    with get_connection() as connection:
        rows = connection.execute(query, params).fetchall()

    return [dict(row) for row in rows]


def count_orders(guild_id: int, statuses: Sequence[str] | None = None) -> int:
    params: list[Any] = [guild_id]
    where = "WHERE guild_id = ?"

    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        where += f" AND status IN ({placeholders})"
        params.extend(statuses)

    with get_connection() as connection:
        row = connection.execute(
            f"SELECT COUNT(*) AS total FROM solicitacoes_armas {where}",
            params,
        ).fetchone()

    return int(row["total"]) if row else 0


def update_order_status(
    guild_id: int,
    order_id: int,
    new_status: str,
) -> dict[str, Any] | None:
    current_time = now_local().isoformat()
    delivered_at = current_time if new_status == STATUS_ENTREGUE else None

    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE solicitacoes_armas
            SET
                status = ?,
                atualizado_em = ?,
                entregue_em = ?
            WHERE guild_id = ? AND id = ?
            """,
            (
                new_status,
                current_time,
                delivered_at,
                guild_id,
                order_id,
            ),
        )
        connection.commit()

        if cursor.rowcount == 0:
            return None

    return get_order(guild_id, order_id)


def update_order_data(
    guild_id: int,
    order_id: int,
    *,
    faccao: str | None = None,
    contato: str | None = None,
    arma: str | None = None,
    quantidade: int | None = None,
    valor_unitario_centavos: int | None = None,
    prazo_maximo: date | None = None,
    observacoes: str | None = None,
    clear_contact: bool = False,
    clear_notes: bool = False,
) -> dict[str, Any] | None:
    current = get_order(guild_id, order_id)
    if not current:
        return None

    new_faction = faccao if faccao is not None else current["faccao"]
    new_contact = (
        None
        if clear_contact
        else contato if contato is not None else current["contato"]
    )
    new_weapon = arma if arma is not None else current["arma"]
    new_quantity = quantidade if quantidade is not None else current["quantidade"]
    new_unit_value = (
        valor_unitario_centavos
        if valor_unitario_centavos is not None
        else current["valor_unitario_centavos"]
    )
    new_deadline = (
        prazo_maximo.isoformat()
        if prazo_maximo is not None
        else current["prazo_maximo"]
    )
    new_notes = (
        None
        if clear_notes
        else observacoes if observacoes is not None else current["observacoes"]
    )
    new_total = int(new_quantity) * int(new_unit_value)

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE solicitacoes_armas
            SET
                faccao = ?,
                contato = ?,
                arma = ?,
                quantidade = ?,
                valor_unitario_centavos = ?,
                valor_total_centavos = ?,
                prazo_maximo = ?,
                observacoes = ?,
                atualizado_em = ?
            WHERE guild_id = ? AND id = ?
            """,
            (
                new_faction,
                new_contact,
                new_weapon,
                new_quantity,
                new_unit_value,
                new_total,
                new_deadline,
                new_notes,
                now_local().isoformat(),
                guild_id,
                order_id,
            ),
        )
        connection.commit()

    return get_order(guild_id, order_id)


def delete_order(guild_id: int, order_id: int) -> bool:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            DELETE FROM solicitacoes_armas
            WHERE guild_id = ? AND id = ?
            """,
            (guild_id, order_id),
        )
        connection.commit()
        return cursor.rowcount > 0


def get_summary(guild_id: int) -> dict[str, Any]:
    with get_connection() as connection:
        status_rows = connection.execute(
            """
            SELECT
                status,
                COUNT(*) AS quantidade_pedidos,
                COALESCE(SUM(quantidade), 0) AS quantidade_itens,
                COALESCE(SUM(valor_total_centavos), 0) AS valor_total_centavos
            FROM solicitacoes_armas
            WHERE guild_id = ?
            GROUP BY status
            """,
            (guild_id,),
        ).fetchall()

        overdue_row = connection.execute(
            """
            SELECT COUNT(*) AS total
            FROM solicitacoes_armas
            WHERE
                guild_id = ?
                AND status IN (?, ?, ?)
                AND prazo_maximo < ?
            """,
            (
                guild_id,
                STATUS_AGUARDANDO,
                STATUS_FABRICACAO,
                STATUS_PRONTO,
                today_local().isoformat(),
            ),
        ).fetchone()

    by_status = {
        row["status"]: {
            "pedidos": int(row["quantidade_pedidos"]),
            "itens": int(row["quantidade_itens"]),
            "valor": int(row["valor_total_centavos"]),
        }
        for row in status_rows
    }

    return {
        "por_status": by_status,
        "atrasados": int(overdue_row["total"]) if overdue_row else 0,
    }


def all_orders_for_export(guild_id: int) -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM solicitacoes_armas
            WHERE guild_id = ?
            ORDER BY id ASC
            """,
            (guild_id,),
        ).fetchall()

    return [dict(row) for row in rows]


# ============================================================
# EMBEDS
# ============================================================

def status_text(status: str) -> str:
    emoji = STATUS_EMOJIS.get(status, "⚪")
    label = STATUS_LABELS.get(status, status)
    return f"{emoji} {label}"


def order_embed(
    order: dict[str, Any],
    *,
    title_prefix: str = "📦 Pedido",
) -> discord.Embed:
    overdue = order_is_overdue(order)
    status = order["status"]

    embed = discord.Embed(
        title=f"{title_prefix} #{order['id']}",
        description=(
            "⚠️ **PRAZO ATRASADO**"
            if overdue
            else f"Status: **{status_text(status)}**"
        ),
        color=discord.Color.red() if overdue else STATUS_COLORS.get(
            status,
            discord.Color.blurple(),
        ),
        timestamp=now_local(),
    )

    embed.add_field(
        name="Facção",
        value=truncate(order["faccao"], 100),
        inline=True,
    )
    embed.add_field(
        name="Contato",
        value=truncate(order.get("contato"), 100),
        inline=True,
    )
    embed.add_field(
        name="Item/arma",
        value=truncate(order["arma"], 100),
        inline=False,
    )
    embed.add_field(
        name="Quantidade",
        value=str(order["quantidade"]),
        inline=True,
    )
    embed.add_field(
        name="Valor unitário",
        value=format_currency(order["valor_unitario_centavos"]),
        inline=True,
    )
    embed.add_field(
        name="Valor total",
        value=format_currency(order["valor_total_centavos"]),
        inline=True,
    )
    embed.add_field(
        name="Data do pedido",
        value=format_date(order["data_pedido"]),
        inline=True,
    )
    embed.add_field(
        name="Prazo máximo",
        value=format_date(order["prazo_maximo"]),
        inline=True,
    )
    embed.add_field(
        name="Status",
        value=status_text(status),
        inline=True,
    )

    if order.get("observacoes"):
        embed.add_field(
            name="Observações",
            value=truncate(order["observacoes"], 900),
            inline=False,
        )

    embed.add_field(
        name="Registrado por",
        value=truncate(order["criado_por_nome"], 100),
        inline=True,
    )
    embed.add_field(
        name="Última atualização",
        value=format_datetime(order["atualizado_em"]),
        inline=True,
    )

    if order.get("entregue_em"):
        embed.add_field(
            name="Entregue em",
            value=format_datetime(order["entregue_em"]),
            inline=True,
        )

    embed.set_footer(text="Sistema de pedidos GTA RP")
    return embed


def orders_list_embed(
    orders: Iterable[dict[str, Any]],
    *,
    title: str,
    total_count: int,
) -> discord.Embed:
    orders_list = list(orders)
    embed = discord.Embed(
        title=title,
        description=(
            f"Exibindo **{len(orders_list)}** de **{total_count}** pedido(s)."
            if orders_list
            else "Nenhum pedido encontrado."
        ),
        color=discord.Color.blurple(),
        timestamp=now_local(),
    )

    for order in orders_list[:MAX_LIST_ITEMS]:
        overdue = " | ⚠️ ATRASADO" if order_is_overdue(order) else ""
        contact = f" — {order['contato']}" if order.get("contato") else ""

        embed.add_field(
            name=(
                f"{STATUS_EMOJIS.get(order['status'], '⚪')} "
                f"#{order['id']} — {truncate(order['faccao'], 55)}{overdue}"
            ),
            value=(
                f"**Item:** {truncate(order['arma'], 85)}\n"
                f"**Quantidade:** {order['quantidade']}\n"
                f"**Total:** {format_currency(order['valor_total_centavos'])}\n"
                f"**Prazo:** {format_date(order['prazo_maximo'])}\n"
                f"**Contato:** {truncate(contact.lstrip(' —'), 85)}"
            ),
            inline=False,
        )

    embed.set_footer(
        text="Use /verpedido para consultar todos os detalhes de um pedido."
    )
    return embed


def summary_embed(summary: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title="📊 Resumo dos pedidos",
        color=discord.Color.dark_teal(),
        timestamp=now_local(),
    )

    active_value = 0
    active_orders = 0
    active_items = 0

    for status in (
        STATUS_AGUARDANDO,
        STATUS_FABRICACAO,
        STATUS_PRONTO,
        STATUS_ENTREGUE,
        STATUS_CANCELADO,
    ):
        data = summary["por_status"].get(
            status,
            {"pedidos": 0, "itens": 0, "valor": 0},
        )

        if status in STATUS_ATIVOS:
            active_orders += data["pedidos"]
            active_items += data["itens"]
            active_value += data["valor"]

        embed.add_field(
            name=status_text(status),
            value=(
                f"Pedidos: **{data['pedidos']}**\n"
                f"Itens: **{data['itens']}**\n"
                f"Valor: **{format_currency(data['valor'])}**"
            ),
            inline=True,
        )

    embed.add_field(
        name="📌 Em aberto",
        value=(
            f"Pedidos: **{active_orders}**\n"
            f"Itens: **{active_items}**\n"
            f"Valor: **{format_currency(active_value)}**"
        ),
        inline=False,
    )

    embed.add_field(
        name="⚠️ Prazos atrasados",
        value=f"**{summary['atrasados']}** pedido(s)",
        inline=False,
    )

    embed.set_footer(text="Valores referentes aos pedidos registrados no bot.")
    return embed


# ============================================================
# CANAIS
# ============================================================

async def ensure_order_channels(
    guild: discord.Guild,
) -> tuple[discord.TextChannel | None, discord.TextChannel | None]:
    """
    Cria, quando possível:
    - Categoria de pedidos;
    - Canal principal;
    - Canal de logs.

    Caso o bot não tenha permissão para criar canais, o sistema continua
    funcionando normalmente pelos comandos.
    """
    category_name = str(
        cfg("SOLICITACAO_ARMAS_CATEGORIA_NOME", "📦 - PEDIDOS RP")
    )
    orders_channel_name = str(
        cfg("SOLICITACAO_ARMAS_CHANNEL_NAME", "📋-pedidos-armas")
    )
    logs_channel_name = str(
        cfg("SOLICITACAO_ARMAS_LOG_CHANNEL_NAME", "🧾-logs-pedidos")
    )

    try:
        category = discord.utils.get(guild.categories, name=category_name)
        if category is None:
            category = await guild.create_category(
                category_name,
                reason="Sistema de solicitações de armas do GTA RP",
            )

        orders_channel = discord.utils.get(
            category.text_channels,
            name=orders_channel_name,
        )
        if orders_channel is None:
            orders_channel = await guild.create_text_channel(
                orders_channel_name,
                category=category,
                topic="Controle de pedidos de armas e fabricação do GTA RP.",
                reason="Sistema de solicitações de armas do GTA RP",
            )

        logs_channel = discord.utils.get(
            category.text_channels,
            name=logs_channel_name,
        )
        if logs_channel is None:
            logs_channel = await guild.create_text_channel(
                logs_channel_name,
                category=category,
                topic="Histórico de alterações dos pedidos do GTA RP.",
                reason="Sistema de solicitações de armas do GTA RP",
            )

        return orders_channel, logs_channel

    except (discord.Forbidden, discord.HTTPException):
        return None, None


# ============================================================
# MODAL DE NOVO PEDIDO
# ============================================================

class NewOrderModal(discord.ui.Modal, title="Registrar novo pedido"):
    faction = discord.ui.TextInput(
        label="Facção",
        placeholder="Ex.: Máfia Italiana",
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
        label="Prazo máximo",
        placeholder="DD/MM/AAAA",
        min_length=10,
        max_length=10,
    )

    def __init__(self, cog: "SolicitacaoArmas"):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.register_order(
            interaction=interaction,
            faccao=self.faction.value,
            contato=None,
            arma=self.weapon.value,
            quantidade=self.quantity.value,
            valor_unitario=self.unit_value.value,
            prazo_maximo=self.deadline.value,
            observacoes=None,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        message = "❌ Ocorreu um erro ao registrar o pedido."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        raise error


# ============================================================
# PAINEL COM BOTÕES
# ============================================================

class OrderPanelView(discord.ui.View):
    def __init__(self, cog: "SolicitacaoArmas"):
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if has_order_permission(interaction):
            return True

        await interaction.response.send_message(
            "❌ Você não possui permissão para usar o sistema de pedidos.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="Novo pedido",
        emoji="➕",
        style=discord.ButtonStyle.success,
        custom_id="solicitacao_armas:novo_pedido",
        row=0,
    )
    async def new_order_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(NewOrderModal(self.cog))

    @discord.ui.button(
        label="Aguardando",
        emoji="🟡",
        style=discord.ButtonStyle.secondary,
        custom_id="solicitacao_armas:listar_aguardando",
        row=0,
    )
    async def waiting_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await self.cog.send_order_list(
            interaction,
            statuses=[STATUS_AGUARDANDO],
            title="🟡 Pedidos aguardando fabricação",
        )

    @discord.ui.button(
        label="Em fabricação",
        emoji="🛠️",
        style=discord.ButtonStyle.primary,
        custom_id="solicitacao_armas:listar_fabricacao",
        row=0,
    )
    async def manufacturing_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await self.cog.send_order_list(
            interaction,
            statuses=[STATUS_FABRICACAO],
            title="🛠️ Pedidos em fabricação",
        )

    @discord.ui.button(
        label="Prontos",
        emoji="🟢",
        style=discord.ButtonStyle.success,
        custom_id="solicitacao_armas:listar_prontos",
        row=1,
    )
    async def ready_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await self.cog.send_order_list(
            interaction,
            statuses=[STATUS_PRONTO],
            title="🟢 Pedidos prontos para entrega",
        )

    @discord.ui.button(
        label="Resumo",
        emoji="📊",
        style=discord.ButtonStyle.secondary,
        custom_id="solicitacao_armas:resumo",
        row=1,
    )
    async def summary_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await self.cog.send_summary(interaction)


# ============================================================
# COG
# ============================================================

class SolicitacaoArmas(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_database()

    async def cog_load(self) -> None:
        # Mantém o painel funcionando mesmo após o bot reiniciar.
        self.bot.add_view(OrderPanelView(self))

    async def send_permission_error(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.send_message(
            "❌ Você não possui permissão para gerenciar pedidos.",
            ephemeral=True,
        )

    async def publish_new_order(
        self,
        guild: discord.Guild,
        order: dict[str, Any],
    ) -> None:
        orders_channel, logs_channel = await ensure_order_channels(guild)
        embed = order_embed(order, title_prefix="📦 Novo pedido")

        if orders_channel:
            await orders_channel.send(embed=embed)

        if logs_channel and logs_channel != orders_channel:
            log_embed = order_embed(order, title_prefix="🧾 Pedido registrado")
            await logs_channel.send(embed=log_embed)

    async def publish_update(
        self,
        guild: discord.Guild,
        order: dict[str, Any],
        *,
        action: str,
        user: discord.abc.User,
    ) -> None:
        _, logs_channel = await ensure_order_channels(guild)
        if not logs_channel:
            return

        embed = order_embed(order, title_prefix=f"🧾 {action}")
        embed.add_field(
            name="Alterado por",
            value=f"{user} (`{user.id}`)",
            inline=False,
        )
        await logs_channel.send(embed=embed)

    async def register_order(
        self,
        *,
        interaction: discord.Interaction,
        faccao: str,
        contato: str | None,
        arma: str,
        quantidade: str | int,
        valor_unitario: str,
        prazo_maximo: str,
        observacoes: str | None,
    ) -> None:
        if not has_order_permission(interaction):
            return await self.send_permission_error(interaction)

        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ Este comando só funciona dentro de um servidor.",
                ephemeral=True,
            )

        try:
            parsed_faction = clean_text(faccao, 100)
            parsed_weapon = clean_text(arma, 100)
            parsed_contact = clean_text(contato, 100) if contato else None
            parsed_notes = clean_text(observacoes, 1000) if observacoes else None
            parsed_quantity = parse_quantity(quantidade)
            parsed_unit_value = parse_brl_to_cents(valor_unitario)
            parsed_deadline = parse_deadline(prazo_maximo)

            if len(parsed_faction) < 2:
                raise ValueError("Informe o nome da facção.")

            if len(parsed_weapon) < 2:
                raise ValueError("Informe o nome da arma/item.")

            if parsed_deadline < today_local():
                raise ValueError(
                    "O prazo máximo não pode ser anterior à data de hoje."
                )

        except ValueError as error:
            return await interaction.response.send_message(
                f"❌ {error}",
                ephemeral=True,
            )

        order_id = await asyncio.to_thread(
            create_order,
            guild_id=interaction.guild.id,
            faccao=parsed_faction,
            contato=parsed_contact,
            arma=parsed_weapon,
            quantidade=parsed_quantity,
            valor_unitario_centavos=parsed_unit_value,
            prazo_maximo=parsed_deadline,
            observacoes=parsed_notes,
            criado_por_id=interaction.user.id,
            criado_por_nome=str(interaction.user),
        )

        order = await asyncio.to_thread(
            get_order,
            interaction.guild.id,
            order_id,
        )

        if not order:
            return await interaction.response.send_message(
                "❌ O pedido foi salvo, mas não consegui consultar seus dados.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            content=f"✅ Pedido **#{order_id}** registrado com sucesso.",
            embed=order_embed(order),
            ephemeral=True,
        )

        try:
            await self.publish_new_order(interaction.guild, order)
        except discord.HTTPException:
            # O pedido já foi salvo. Falha ao publicar não deve desfazer o registro.
            pass

    async def send_order_list(
        self,
        interaction: discord.Interaction,
        *,
        statuses: Sequence[str] | None,
        title: str,
    ) -> None:
        if not has_order_permission(interaction):
            return await self.send_permission_error(interaction)

        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ Este comando só funciona dentro de um servidor.",
                ephemeral=True,
            )

        orders, total = await asyncio.gather(
            asyncio.to_thread(
                list_orders,
                interaction.guild.id,
                statuses,
                limit=MAX_LIST_ITEMS,
            ),
            asyncio.to_thread(
                count_orders,
                interaction.guild.id,
                statuses,
            ),
        )

        embed = orders_list_embed(
            orders,
            title=title,
            total_count=total,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def send_summary(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if not has_order_permission(interaction):
            return await self.send_permission_error(interaction)

        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ Este comando só funciona dentro de um servidor.",
                ephemeral=True,
            )

        summary = await asyncio.to_thread(
            get_summary,
            interaction.guild.id,
        )
        await interaction.response.send_message(
            embed=summary_embed(summary),
            ephemeral=True,
        )

    # --------------------------------------------------------
    # COMANDOS SLASH
    # --------------------------------------------------------

    @app_commands.command(
        name="painelpedidos",
        description="Cria o painel de controle dos pedidos de armas do GTA RP",
    )
    async def painelpedidos(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if not has_order_permission(interaction):
            return await self.send_permission_error(interaction)

        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ Este comando só funciona dentro de um servidor.",
                ephemeral=True,
            )

        orders_channel, logs_channel = await ensure_order_channels(
            interaction.guild
        )

        channels_description = ""
        if orders_channel:
            channels_description += (
                f"\n\n📋 Novos pedidos serão publicados em "
                f"{orders_channel.mention}."
            )
        if logs_channel:
            channels_description += (
                f"\n🧾 Alterações serão registradas em {logs_channel.mention}."
            )

        embed = discord.Embed(
            title="📦 Controle de pedidos — GTA RP",
            description=(
                "Use os botões abaixo para registrar e acompanhar pedidos.\n\n"
                "**Fluxo recomendado:**\n"
                "🟡 Aguardando fabricação → 🛠️ Em fabricação → "
                "🟢 Pronto para entrega → ✅ Entregue"
                f"{channels_description}"
            ),
            color=discord.Color.dark_teal(),
        )
        embed.add_field(
            name="Comandos úteis",
            value=(
                "`/registrarpedido` — registra com contato e observações\n"
                "`/verpedido` — consulta um pedido\n"
                "`/statuspedido` — altera o andamento\n"
                "`/editarpedido` — corrige informações\n"
                "`/exportarpedidos` — exporta o histórico"
            ),
            inline=False,
        )

        await interaction.response.send_message(
            embed=embed,
            view=OrderPanelView(self),
        )

    @app_commands.command(
        name="registrarpedido",
        description="Registra um pedido de arma/item feito por outra facção",
    )
    @app_commands.describe(
        faccao="Facção que realizou o pedido",
        arma="Arma ou item solicitado",
        quantidade="Quantidade solicitada",
        valor_unitario="Valor de cada unidade, por exemplo 25.000",
        prazo_maximo="Prazo no formato DD/MM/AAAA",
        contato="Nome ou identificação do contato da facção",
        observacoes="Informações adicionais sobre o pedido",
    )
    async def registrarpedido(
        self,
        interaction: discord.Interaction,
        faccao: str,
        arma: str,
        quantidade: app_commands.Range[int, 1, 100000],
        valor_unitario: str,
        prazo_maximo: str,
        contato: str | None = None,
        observacoes: str | None = None,
    ) -> None:
        await self.register_order(
            interaction=interaction,
            faccao=faccao,
            contato=contato,
            arma=arma,
            quantidade=int(quantidade),
            valor_unitario=valor_unitario,
            prazo_maximo=prazo_maximo,
            observacoes=observacoes,
        )

    @app_commands.command(
        name="listarpedidos",
        description="Lista os pedidos registrados",
    )
    @app_commands.describe(
        status="Filtra os pedidos por status",
    )
    @app_commands.choices(status=LIST_STATUS_CHOICES)
    async def listarpedidos(
        self,
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
    ) -> None:
        selected_status = status.value if status else "todos"
        statuses = None if selected_status == "todos" else [selected_status]

        title = (
            "📋 Todos os pedidos"
            if selected_status == "todos"
            else f"📋 Pedidos — {STATUS_LABELS[selected_status]}"
        )

        await self.send_order_list(
            interaction,
            statuses=statuses,
            title=title,
        )

    @app_commands.command(
        name="verpedido",
        description="Mostra todos os detalhes de um pedido",
    )
    @app_commands.describe(
        pedido_id="ID numérico do pedido",
    )
    async def verpedido(
        self,
        interaction: discord.Interaction,
        pedido_id: app_commands.Range[int, 1, None],
    ) -> None:
        if not has_order_permission(interaction):
            return await self.send_permission_error(interaction)

        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ Este comando só funciona dentro de um servidor.",
                ephemeral=True,
            )

        order = await asyncio.to_thread(
            get_order,
            interaction.guild.id,
            int(pedido_id),
        )

        if not order:
            return await interaction.response.send_message(
                f"❌ Pedido **#{pedido_id}** não encontrado.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            embed=order_embed(order),
            ephemeral=True,
        )

    @app_commands.command(
        name="statuspedido",
        description="Altera o status de fabricação ou entrega de um pedido",
    )
    @app_commands.describe(
        pedido_id="ID numérico do pedido",
        status="Novo status do pedido",
    )
    @app_commands.choices(status=STATUS_CHOICES)
    async def statuspedido(
        self,
        interaction: discord.Interaction,
        pedido_id: app_commands.Range[int, 1, None],
        status: app_commands.Choice[str],
    ) -> None:
        if not has_order_permission(interaction):
            return await self.send_permission_error(interaction)

        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ Este comando só funciona dentro de um servidor.",
                ephemeral=True,
            )

        order = await asyncio.to_thread(
            update_order_status,
            interaction.guild.id,
            int(pedido_id),
            status.value,
        )

        if not order:
            return await interaction.response.send_message(
                f"❌ Pedido **#{pedido_id}** não encontrado.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            content=(
                f"✅ Pedido **#{pedido_id}** atualizado para "
                f"**{STATUS_LABELS[status.value]}**."
            ),
            embed=order_embed(order),
            ephemeral=True,
        )

        try:
            await self.publish_update(
                interaction.guild,
                order,
                action=f"Status alterado para {STATUS_LABELS[status.value]}",
                user=interaction.user,
            )
        except discord.HTTPException:
            pass

    @app_commands.command(
        name="editarpedido",
        description="Corrige dados de um pedido já registrado",
    )
    @app_commands.describe(
        pedido_id="ID numérico do pedido",
        faccao="Novo nome da facção",
        arma="Novo nome da arma/item",
        quantidade="Nova quantidade",
        valor_unitario="Novo valor unitário",
        prazo_maximo="Novo prazo no formato DD/MM/AAAA",
        contato="Novo contato",
        observacoes="Novas observações",
        limpar_contato="Remove o contato já registrado",
        limpar_observacoes="Remove as observações já registradas",
    )
    async def editarpedido(
        self,
        interaction: discord.Interaction,
        pedido_id: app_commands.Range[int, 1, None],
        faccao: str | None = None,
        arma: str | None = None,
        quantidade: int | None = None,
        valor_unitario: str | None = None,
        prazo_maximo: str | None = None,
        contato: str | None = None,
        observacoes: str | None = None,
        limpar_contato: bool = False,
        limpar_observacoes: bool = False,
    ) -> None:
        if not has_order_permission(interaction):
            return await self.send_permission_error(interaction)

        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ Este comando só funciona dentro de um servidor.",
                ephemeral=True,
            )

        if all(
            value is None or value is False
            for value in (
                faccao,
                arma,
                quantidade,
                valor_unitario,
                prazo_maximo,
                contato,
                observacoes,
                limpar_contato,
                limpar_observacoes,
            )
        ):
            return await interaction.response.send_message(
                "❌ Informe pelo menos um campo para alterar.",
                ephemeral=True,
            )

        try:
            parsed_faction = clean_text(faccao, 100) if faccao else None
            parsed_weapon = clean_text(arma, 100) if arma else None
            parsed_quantity = (
                parse_quantity(quantidade)
                if quantidade is not None
                else None
            )
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
            parsed_contact = clean_text(contato, 100) if contato else None
            parsed_notes = (
                clean_text(observacoes, 1000)
                if observacoes
                else None
            )

            if parsed_deadline and parsed_deadline < today_local():
                raise ValueError(
                    "O novo prazo não pode ser anterior à data de hoje."
                )

        except ValueError as error:
            return await interaction.response.send_message(
                f"❌ {error}",
                ephemeral=True,
            )

        order = await asyncio.to_thread(
            update_order_data,
            interaction.guild.id,
            int(pedido_id),
            faccao=parsed_faction,
            contato=parsed_contact,
            arma=parsed_weapon,
            quantidade=parsed_quantity,
            valor_unitario_centavos=parsed_value,
            prazo_maximo=parsed_deadline,
            observacoes=parsed_notes,
            clear_contact=limpar_contato,
            clear_notes=limpar_observacoes,
        )

        if not order:
            return await interaction.response.send_message(
                f"❌ Pedido **#{pedido_id}** não encontrado.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            content=f"✅ Pedido **#{pedido_id}** atualizado.",
            embed=order_embed(order),
            ephemeral=True,
        )

        try:
            await self.publish_update(
                interaction.guild,
                order,
                action="Dados do pedido alterados",
                user=interaction.user,
            )
        except discord.HTTPException:
            pass

    @app_commands.command(
        name="resumopedidos",
        description="Mostra o total de pedidos, itens, valores e atrasos",
    )
    async def resumopedidos(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self.send_summary(interaction)

    @app_commands.command(
        name="removerpedido",
        description="Remove definitivamente um pedido registrado",
    )
    @app... (6 KB restante(s))