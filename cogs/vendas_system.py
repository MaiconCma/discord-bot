import os
import csv
import asyncio
import traceback
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord import app_commands

import config

"""
VENDAS (GTA RP) — Painel passo a passo + EXPORT automático (CSV/XLSX)

✅ Fluxo:
1) Tipo (Família / Pista)
2) Arma/Item
3) Player + Quantidade

✅ Registro:
- Um canal único (config.VENDAS_CHANNEL_NAME), 1 mensagem por venda.

✅ Export:
- CSV sempre (config.VENDAS_EXPORT_CSV_PATH)
- XLSX opcional (config.VENDAS_EXPORT_XLSX / config.VENDAS_EXPORT_XLSX_PATH)

Correções:
- Se não existir nenhum item em config.VENDAS_ITENS, o passo 2 mostra aviso e não cria Select vazio
  (isso era a causa do erro 400: "options field is required").
"""


def _has_allowed_role(member: discord.Member) -> bool:
    allowed = getattr(config, "VENDAS_CARGOS_PERMITIDOS_IDS", None)
    if allowed is None:
        allowed = getattr(config, "CARGOS_PERMITIDOS_IDS", []) or []
    if not allowed:
        return True
    return any(r.id in allowed for r in member.roles)


def _fmt_money(value: int) -> str:
    currency = getattr(config, "VENDAS_CURRENCY", "R$")
    s = f"{value:,}".replace(",", ".")
    return f"{currency} {s}"


def _normalize_tipo(tipo: str) -> str:
    t = (tipo or "").strip().lower()
    if t in ("familia", "família", "family", "f"):
        return "familia"
    return "pista"


def _needs_tipo(meta: dict) -> bool:
    return ("preco_familia" in meta) or ("preco_pista" in meta)


def _get_price_from_meta(meta: dict, tipo: str) -> int:
    if _needs_tipo(meta):
        if _normalize_tipo(tipo) == "familia":
            return int(meta.get("preco_familia", 0))
        return int(meta.get("preco_pista", 0))
    return int(meta.get("preco", 0))


async def _get_or_create_sales_channel(guild: discord.Guild) -> discord.TextChannel:
    ch_name = getattr(config, "VENDAS_CHANNEL_NAME", "💰-vendas")
    cat_name = getattr(config, "VENDAS_CATEGORIA_NOME", "💰 - VENDAS")

    existing = discord.utils.get(guild.text_channels, name=ch_name)
    if existing:
        return existing

    categoria = discord.utils.get(guild.categories, name=cat_name)
    if not categoria:
        categoria = await guild.create_category(name=cat_name, reason="Categoria de vendas (auto)")

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=True),  # type: ignore[arg-type]
    }

    for cargo_id in (getattr(config, "VENDAS_CARGOS_PERMITIDOS_IDS", None) or getattr(config, "CARGOS_PERMITIDOS_IDS", []) or []):
        cargo = guild.get_role(cargo_id)
        if cargo:
            overwrites[cargo] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=True)

    canal = await guild.create_text_channel(
        name=ch_name,
        category=categoria,
        overwrites=overwrites,
        reason="Canal único de vendas (auto)",
    )
    return canal


CSV_HEADERS = [
    "timestamp_iso",
    "guild_id",
    "guild_name",
    "sale_message_id",
    "seller_id",
    "seller_name",
    "seller_tag",
    "tipo",
    "item",
    "quantidade",
    "preco_unit",
    "total",
    "player",
]


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _append_csv_row(path: str, row: dict) -> None:
    _ensure_parent_dir(path)
    file_exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in CSV_HEADERS})


def _upsert_xlsx_row(path: str, row: dict) -> None:
    try:
        from openpyxl import Workbook, load_workbook  # type: ignore
    except Exception:
        print("[VENDAS] openpyxl não instalado; XLSX não será atualizado. (pip install openpyxl)")
        return

    _ensure_parent_dir(path)

    if os.path.exists(path):
        wb = load_workbook(path)
        ws = wb.active
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Vendas"
        ws.append(CSV_HEADERS)

    ws.append([row.get(h, "") for h in CSV_HEADERS])
    wb.save(path)


def _build_step1_embed() -> discord.Embed:
    embed = discord.Embed(
        title="💰 Painel de Vendas — passo 1/3",
        description="Escolha o **tipo** da venda para continuar.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="1) Tipo", value="🏠 Família ou 🛣️ Pista", inline=False)
    embed.add_field(name="2) Arma/Item", value="Você escolhe no próximo passo", inline=False)
    embed.add_field(name="3) Quantidade + Player", value="Você preenche no final", inline=False)
    embed.add_field(name="Export", value="CSV/XLSX atualiza automaticamente", inline=False)
    return embed


def _build_step2_embed(tipo: str) -> discord.Embed:
    tipo_n = _normalize_tipo(tipo)
    tipo_txt = "Família" if tipo_n == "familia" else "Pista"
    embed = discord.Embed(
        title="💰 Painel de Vendas — passo 2/3",
        description=f"Tipo selecionado: **{tipo_txt}**\nAgora escolha a **arma/item**.",
        color=discord.Color.blurple(),
    )
    return embed


def _build_no_items_embed() -> discord.Embed:
    embed = discord.Embed(
        title="⚠️ Nenhuma arma/item configurado",
        description="O `config.py` está sem `VENDAS_ITENS` (ou está vazio).\n"
                    "Adicione os itens e tente novamente.",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Dica", value="Abra o `config.py` e confira o bloco `VENDAS_ITENS = {...}`.", inline=False)
    return embed


async def _err(interaction: discord.Interaction, where: str, e: Exception):
    code = f"{where}:{type(e).__name__}"
    print(f"[VENDAS] ERRO {code} -> {repr(e)}")
    traceback.print_exc()
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ Erro ({code}). Veja o console do bot.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Erro ({code}). Veja o console do bot.", ephemeral=True)
    except Exception:
        pass


class VendaStep3Modal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, tipo: str, item_nome: str, export_lock: asyncio.Lock):
        super().__init__(title="Registrar venda — passo 3/3")
        self.bot = bot
        self.tipo = _normalize_tipo(tipo)
        self.item_nome = item_nome
        self.export_lock = export_lock

        self.player = discord.ui.TextInput(
            label="ID DO COMPRADOR",
            placeholder="Ex: ID 20090",
            required=True,
            max_length=64,
        )
        self.quantidade = discord.ui.TextInput(
            label="Quantidade vendida",
            placeholder="Ex: 1",
            required=True,
            max_length=6,
        )

        self.add_item(self.player)
        self.add_item(self.quantidade)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.guild:
                return await interaction.response.send_message("❌ Use isso dentro de um servidor.", ephemeral=True)

            member = interaction.user
            if not isinstance(member, discord.Member):
                return await interaction.response.send_message("❌ Use isso dentro de um servidor.", ephemeral=True)

            if not _has_allowed_role(member):
                return await interaction.response.send_message("❌ Você não tem permissão para registrar vendas.", ephemeral=True)

            q_raw = str(self.quantidade.value).strip()
            if not q_raw.isdigit():
                return await interaction.response.send_message("❌ Quantidade deve ser um número.", ephemeral=True)

            qtd = int(q_raw)
            if qtd <= 0:
                return await interaction.response.send_message("❌ Quantidade precisa ser maior que zero.", ephemeral=True)

            itens = getattr(config, "VENDAS_ITENS", {}) or {}
            meta = itens.get(self.item_nome)
            if not meta:
                return await interaction.response.send_message("❌ Item não encontrado no config.", ephemeral=True)

            preco_unit = _get_price_from_meta(meta, self.tipo)
            if preco_unit <= 0:
                return await interaction.response.send_message("❌ Preço inválido no config.", ephemeral=True)

            total = preco_unit * qtd
            tipo_txt = "Família" if self.tipo == "familia" else "Pista"

            sales_ch = await _get_or_create_sales_channel(interaction.guild)

            ts = discord.utils.format_dt(discord.utils.utcnow(), style="f")
            line = (
                f"🧾 **Venda** • {ts}\n"
                f"👤 **Vendedor:** {member.mention}\n"
                f"🎯 **Tipo:** {tipo_txt}\n"
                f"🔫 **Item:** **{self.item_nome}** × **{qtd}**\n"
                f"🧑‍💼 **Player:** `{self.player.value}`\n"
                f"💵 **Unit:** {_fmt_money(preco_unit)}  •  **Total:** {_fmt_money(total)}"
            )

            sale_msg = await sales_ch.send(line)

            row = {
                "timestamp_iso": datetime.now(timezone.utc).isoformat(),
                "guild_id": str(interaction.guild.id),
                "guild_name": interaction.guild.name,
                "sale_message_id": str(sale_msg.id),
                "seller_id": str(member.id),
                "seller_name": member.name,
                "seller_tag": str(member),
                "tipo": tipo_txt,
                "item": self.item_nome,
                "quantidade": str(qtd),
                "preco_unit": str(preco_unit),
                "total": str(total),
                "player": str(self.player.value),
            }

            csv_path = getattr(config, "VENDAS_EXPORT_CSV_PATH", "data/vendas.csv")
            xlsx_path = getattr(config, "VENDAS_EXPORT_XLSX_PATH", "data/vendas.xlsx")
            export_xlsx = bool(getattr(config, "VENDAS_EXPORT_XLSX", True))

            async with self.export_lock:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _append_csv_row, csv_path, row)
                if export_xlsx:
                    await loop.run_in_executor(None, _upsert_xlsx_row, xlsx_path, row)

            await interaction.response.send_message(
                f"✅ Registrado em {sales_ch.mention} • Total: **{_fmt_money(total)}**\n"
                f"📁 Export: `{csv_path}`" + (f" e `{xlsx_path}`" if export_xlsx else ""),
                ephemeral=True,
            )
        except Exception as e:
            await _err(interaction, "modal", e)


class VendaStep2Select(discord.ui.Select):
    def __init__(self, bot: commands.Bot, tipo: str, export_lock: asyncio.Lock):
        self.bot = bot
        self.tipo = _normalize_tipo(tipo)
        self.export_lock = export_lock

        itens = getattr(config, "VENDAS_ITENS", {}) or {}
        options: list[discord.SelectOption] = []

        for item_nome, meta in list(itens.items())[:25]:
            emoji = meta.get("emoji", "🔫")
            preco = _get_price_from_meta(meta, self.tipo)
            desc = f"{_fmt_money(preco)} cada" if preco > 0 else "preço inválido"
            options.append(
                discord.SelectOption(
                    label=item_nome[:100],
                    value=item_nome,
                    description=desc[:100],
                    emoji=emoji,
                )
            )

        # IMPORTANTE: options não pode ser vazio
        super().__init__(
            placeholder="Passo 2/3 — escolha a arma/item",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.send_modal(VendaStep3Modal(self.bot, self.tipo, self.values[0], self.export_lock))
        except Exception as e:
            await _err(interaction, "select", e)


class VendaStep2View(discord.ui.View):
    def __init__(self, bot: commands.Bot, tipo: str, export_lock: asyncio.Lock):
        super().__init__(timeout=300)
        self.bot = bot
        self.tipo = _normalize_tipo(tipo)
        self.export_lock = export_lock

        itens = getattr(config, "VENDAS_ITENS", {}) or {}
        if itens:
            self.add_item(VendaStep2Select(bot, self.tipo, export_lock))
        # Se não tiver itens, a view só vai ter o botão voltar

    @discord.ui.button(label="Voltar", style=discord.ButtonStyle.secondary, emoji="⬅️")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.edit_message(embed=_build_step1_embed(), view=VendaStep1View(self.bot, self.export_lock))
        except Exception as e:
            await _err(interaction, "back", e)


class VendaStep1View(discord.ui.View):
    def __init__(self, bot: commands.Bot, export_lock: asyncio.Lock):
        super().__init__(timeout=300)
        self.bot = bot
        self.export_lock = export_lock

    @discord.ui.button(label="Família", style=discord.ButtonStyle.success, emoji="🏠")
    async def familia(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            itens = getattr(config, "VENDAS_ITENS", {}) or {}
            if not itens:
                return await interaction.response.edit_message(embed=_build_no_items_embed(), view=VendaStep2View(self.bot, "familia", self.export_lock))
            await interaction.response.edit_message(embed=_build_step2_embed("familia"), view=VendaStep2View(self.bot, "familia", self.export_lock))
        except Exception as e:
            await _err(interaction, "familia", e)

    @discord.ui.button(label="Pista", style=discord.ButtonStyle.primary, emoji="🛣️")
    async def pista(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            itens = getattr(config, "VENDAS_ITENS", {}) or {}
            if not itens:
                return await interaction.response.edit_message(embed=_build_no_items_embed(), view=VendaStep2View(self.bot, "pista", self.export_lock))
            await interaction.response.edit_message(embed=_build_step2_embed("pista"), view=VendaStep2View(self.bot, "pista", self.export_lock))
        except Exception as e:
            await _err(interaction, "pista", e)


class VendasSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.export_lock = asyncio.Lock()

    @app_commands.command(name="painelvendas", description="Cria o painel passo a passo para registrar vendas (GTA RP).")
    @app_commands.checks.has_permissions(administrator=True)
    async def painelvendas(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Use isso dentro de um servidor.", ephemeral=True)

        await interaction.response.send_message(embed=_build_step1_embed(), view=VendaStep1View(self.bot, self.export_lock))

    @app_commands.command(name="exportvendas", description="Envia o CSV/XLSX atual de vendas (admin).")
    @app_commands.checks.has_permissions(administrator=True)
    async def exportvendas(self, interaction: discord.Interaction):
        csv_path = getattr(config, "VENDAS_EXPORT_CSV_PATH", "data/vendas.csv")
        xlsx_path = getattr(config, "VENDAS_EXPORT_XLSX_PATH", "data/vendas.xlsx")
        export_xlsx = bool(getattr(config, "VENDAS_EXPORT_XLSX", True))

        files: list[discord.File] = []
        if os.path.exists(csv_path):
            files.append(discord.File(csv_path, filename=os.path.basename(csv_path)))
        if export_xlsx and os.path.exists(xlsx_path):
            files.append(discord.File(xlsx_path, filename=os.path.basename(xlsx_path)))

        if not files:
            return await interaction.response.send_message("📭 Ainda não existe export de vendas.", ephemeral=True)

        await interaction.response.send_message("📦 Export atual:", files=files, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VendasSystem(bot))
