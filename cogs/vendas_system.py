from __future__ import annotations

import csv
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands
from discord import app_commands

import config

try:
    from cogs.economia_system import credit_caixa_on_sale
except Exception:
    credit_caixa_on_sale = None

# Lock para escrita segura no CSV
_csv_lock = asyncio.Lock()


def _has_any_role(member: discord.Member, role_ids: list[int]) -> bool:
    if member.guild_permissions.administrator:
        return True

    ids = {r.id for r in member.roles}
    return any(rid in ids for rid in role_ids)


def _money(n: int) -> str:
    cur = getattr(config, "VENDAS_CURRENCY", "R$")
    return f"{cur} {n:,}".replace(",", ".")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_produtos() -> dict[str, dict[str, int | str]]:
    """
    Produtos virtuais/in-game do RP.
    """
    return getattr(config, "VENDAS_ARMAS_PRECOS", {
        "ia2": {"nome": "IA2", "preco": 130000},
        "mtar": {"nome": "MTAR", "preco": 70000},
        "five_familia": {"nome": "Five - Família", "preco": 30000},
        "five_pista": {"nome": "Five - Pista", "preco": 40000},
    })


async def _ensure_category_and_channels(guild: discord.Guild):
    cat_name = getattr(config, "VENDAS_ARMAS_CATEGORIA_NOME", "💰 - VENDAS")
    channel_name = getattr(config, "VENDAS_ARMAS_CHANNEL_NAME", "💰-vendas-armas")
    log_name = getattr(config, "VENDAS_ARMAS_LOG_CHANNEL_NAME", "📋-logs-vendas-armas")

    category = discord.utils.get(guild.categories, name=cat_name)
    if not category:
        category = await guild.create_category(cat_name)

    vendas_ch = discord.utils.get(category.text_channels, name=channel_name)
    if not vendas_ch:
        vendas_ch = await guild.create_text_channel(channel_name, category=category)

    log_ch = discord.utils.get(category.text_channels, name=log_name)
    if not log_ch:
        log_ch = await guild.create_text_channel(log_name, category=category)

    return category, vendas_ch, log_ch


def _csv_path() -> Path:
    return Path(getattr(config, "VENDAS_ARMAS_EXPORT_CSV_PATH", "data/vendas_armas.csv"))


def _write_csv_sync(path: Path, row: dict[str, str]):
    # Isolamento da operação de disco para rodar em thread separada
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "timestamp_utc", "guild_id", "canal_id", "item", "quantidade", 
        "preco_unit", "total", "player", "registrado_por_id"
    ]
    file_exists = path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if not file_exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in header})


async def _append_csv(row: dict[str, str]) -> None:
    async with _csv_lock:
        path = _csv_path()
        await asyncio.to_thread(_write_csv_sync, path, row)


def _build_panel_embed() -> discord.Embed:
    produtos = _get_produtos()

    linhas = []
    for produto in produtos.values():
        nome = str(produto["nome"])
        preco = int(produto["preco"])
        linhas.append(f"• **{nome}:** {_money(preco)}")

    emb = discord.Embed(
        title="🔫 Sistema de Vendas - Armas RP",
        description=(
            "**Tabela de preços:**\n"
            f"{chr(10).join(linhas)}\n\n"
            "Clique no botão do item vendido para registrar a venda.\n"
            "Você informará a **quantidade** e o **ID do player**."
        ),
        color=discord.Color.dark_gold(),
    )
    emb.set_footer(text="Use /exportvendas para baixar a planilha")
    return emb


class VendaArmaModal(discord.ui.Modal):
    quantidade = discord.ui.TextInput(
        label="Quantidade",
        placeholder="Ex: 1",
        min_length=1,
        max_length=6,
    )
    player_id = discord.ui.TextInput(
        label="ID do Player",
        placeholder="Ex: 12345",
        max_length=80,
    )

    def __init__(self, cog: "VendasSystem", item_nome: str, preco_unit: int):
        super().__init__(title=f"Registrar venda - {item_nome}", timeout=None)
        self.cog = cog
        self.item_nome = item_nome
        self.preco_unit = preco_unit

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qtd = int(self.quantidade.value.strip())
            if qtd <= 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "❌ Quantidade inválida. Use um número inteiro positivo.",
                ephemeral=True,
            )

        player = self.player_id.value.strip()
        if not player:
            return await interaction.response.send_message(
                "❌ Você precisa informar o ID do player.",
                ephemeral=True,
            )

        if not interaction.guild:
            return await interaction.response.send_message("❌ Sem servidor.", ephemeral=True)

        if not self.cog._check_perm(interaction):
            return await interaction.response.send_message(
                "❌ Você não tem permissão para registrar vendas.",
                ephemeral=True,
            )

        total = qtd * self.preco_unit

        _, vendas_ch, log_ch = await _ensure_category_and_channels(interaction.guild)

        line = (
            f"🔫 **Venda de Arma RP**\n"
            f"└ Item: **{self.item_nome}**\n"
            f"└ Quantidade: **x{qtd}**\n"
            f"└ Preço unit.: **{_money(self.preco_unit)}**\n"
            f"└ Total: **{_money(total)}**\n"
            f"└ ID do Player: `{player}`\n"
            f"└ Vendido por: {interaction.user.mention}"
        )
        await vendas_ch.send(line)

        emb = discord.Embed(title="📋 Venda registrada", color=discord.Color.green())
        emb.add_field(name="Item", value=self.item_nome, inline=True)
        emb.add_field(name="Quantidade", value=str(qtd), inline=True)
        emb.add_field(name="Preço unit.", value=_money(self.preco_unit), inline=True)
        emb.add_field(name="Total", value=_money(total), inline=True)
        emb.add_field(name="ID do Player", value=player, inline=False)
        emb.add_field(name="Registrado por", value=f"{interaction.user} ({interaction.user.id})", inline=False)
        emb.set_footer(text=_now_iso())
        await log_ch.send(embed=emb)

        await _append_csv({
            "timestamp_utc": _now_iso(),
            "guild_id": str(interaction.guild.id),
            "canal_id": str(vendas_ch.id),
            "item": self.item_nome,
            "quantidade": str(qtd),
            "preco_unit": str(self.preco_unit),
            "total": str(total),
            "player": player,
            "registrado_por_id": str(interaction.user.id),
        })

        if credit_caixa_on_sale is not None:
            try:
                await credit_caixa_on_sale(interaction.guild.id, total, interaction.user.id)
            except Exception:
                pass

        await interaction.response.send_message(
            (
                f"✅ Venda registrada: **x{qtd} {self.item_nome}** "
                f"por **{_money(total)}** para `{player}`."
            ),
            ephemeral=True,
        )


class VendaArmasView(discord.ui.View):
    def __init__(self, cog: "VendasSystem"):
        super().__init__(timeout=None)
        self.cog = cog

    async def _abrir_modal(self, interaction: discord.Interaction, produto_key: str):
        if not self.cog._check_perm(interaction):
            return await interaction.response.send_message(
                "❌ Você não tem permissão para registrar vendas.",
                ephemeral=True,
            )

        produto = _get_produtos()[produto_key]
        await interaction.response.send_modal(
            VendaArmaModal(
                self.cog,
                item_nome=str(produto["nome"]),
                preco_unit=int(produto["preco"]),
            )
        )

    @discord.ui.button(
        label="IA2 - R$ 130.000",
        style=discord.ButtonStyle.danger,
        custom_id="vendas_armas:ia2",
    )
    async def ia2(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._abrir_modal(interaction, "ia2")

    @discord.ui.button(
        label="MTAR - R$ 70.000",
        style=discord.ButtonStyle.danger,
        custom_id="vendas_armas:mtar",
    )
    async def mtar(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._abrir_modal(interaction, "mtar")

    @discord.ui.button(
        label="Five Família - R$ 30.000",
        style=discord.ButtonStyle.primary,
        custom_id="vendas_armas:five_familia",
    )
    async def five_familia(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._abrir_modal(interaction, "five_familia")

    @discord.ui.button(
        label="Five Pista - R$ 40.000",
        style=discord.ButtonStyle.primary,
        custom_id="vendas_armas:five_pista",
    )
    async def five_pista(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._abrir_modal(interaction, "five_pista")


class VendasSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.export_lock = asyncio.Lock()

        self.allowed_roles = (
            list(getattr(config, "VENDAS_ARMAS_CARGOS_PERMITIDOS_IDS", []))
            or list(getattr(config, "VENDAS_CARGOS_PERMITIDOS_IDS", []))
            or list(getattr(config, "CARGOS_PERMITIDOS_IDS", []))
        )

        bot.add_view(VendaArmasView(self))

    def _check_perm(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False

        return _has_any_role(interaction.user, self.allowed_roles)

    @app_commands.command(name="painelvendas", description="Posta o painel de vendas de armas RP.")
    async def painelvendas(self, interaction: discord.Interaction):
        if not self._check_perm(interaction):
            return await interaction.response.send_message(
                "❌ Você não tem permissão para usar isso.",
                ephemeral=True,
            )

        if not interaction.guild:
            return await interaction.response.send_message("❌ Sem guild.", ephemeral=True)

        if not interaction.channel:
            return await interaction.response.send_message("❌ Canal inválido.", ephemeral=True)

        await interaction.channel.send(embed=_build_panel_embed(), view=VendaArmasView(self))
        await interaction.response.send_message("✅ Painel de vendas enviado neste canal.", ephemeral=True)

    @app_commands.command(name="exportvendas", description="Exporta as vendas de armas RP.")
    async def exportvendas(self, interaction: discord.Interaction):
        if not self._check_perm(interaction):
            return await interaction.response.send_message(
                "❌ Você não tem permissão para exportar.",
                ephemeral=True,
            )

        async with self.export_lock:
            csv_path = _csv_path()
            if not csv_path.exists():
                return await interaction.response.send_message(
                    "❌ Ainda não existe CSV. Registre uma venda primeiro.",
                    ephemeral=True,
                )

            await interaction.response.send_message("📤 Preparando arquivos...", ephemeral=True)
            await interaction.followup.send(file=discord.File(str(csv_path)), ephemeral=True)

            if getattr(config, "VENDAS_ARMAS_EXPORT_XLSX", getattr(config, "VENDAS_EXPORT_XLSX", False)):
                try:
                    xlsx_path = Path(getattr(config, "VENDAS_ARMAS_EXPORT_XLSX_PATH", "data/vendas_armas.xlsx"))
                    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
                    self._csv_to_xlsx(csv_path, xlsx_path)
                    await interaction.followup.send(file=discord.File(str(xlsx_path)), ephemeral=True)
                except Exception:
                    await interaction.followup.send(
                        "⚠️ Não foi possível gerar o XLSX. Veja o console.",
                        ephemeral=True,
                    )

    def _csv_to_xlsx(self, csv_path: Path, xlsx_path: Path) -> None:
        from openpyxl import Workbook

        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))

        wb = Workbook()
        ws = wb.active
        ws.title = "vendas_armas"

        for r in rows:
            ws.append(r)

        wb.save(xlsx_path)

    @app_commands.command(name="listavendas", description="Mostra as últimas vendas de armas RP registradas.")
    @app_commands.describe(quantidade="Quantas linhas mostrar (1 a 30)")
    async def listavendas(self, interaction: discord.Interaction, quantidade: int = 10):
        if not self._check_perm(interaction):
            return await interaction.response.send_message(
                "❌ Você não tem permissão.",
                ephemeral=True,
            )

        quantidade = max(1, min(int(quantidade), 30))
        path = _csv_path()

        if not path.exists():
            return await interaction.response.send_message(
                "⚠️ Ainda não tem vendas registradas.",
                ephemeral=True,
            )

        with open(path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))

        ultimas = rows[-quantidade:]
        if not ultimas:
            return await interaction.response.send_message(
                "⚠️ Nenhuma venda encontrada.",
                ephemeral=True,
            )

        emb = discord.Embed(
            title=f"🧾 Últimas {len(ultimas)} vendas de armas RP",
            color=discord.Color.gold(),
        )

        desc = []
        for r in ultimas:
            item = r.get("item", "Item")
            qtd = r.get("quantidade", "0")
            total_raw = r.get("total", "0")
            player = r.get("player", "-")

            try:
                total_formatado = _money(int(total_raw))
            except ValueError:
                total_formatado = total_raw

            desc.append(f"🔫 **{item}** x{qtd} • Total {total_formatado} • Player `{player}`")

        emb.description = "\n".join(desc[:30])
        emb.set_footer(text="Use /exportvendas para baixar a planilha")
        await interaction.response.send_message(embed=emb, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VendasSystem(bot))