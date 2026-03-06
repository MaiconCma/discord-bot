from __future__ import annotations

import csv
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands
from discord import app_commands

import config

# Integração com economia (opcional)
try:
    from cogs.economia_system import credit_caixa_on_sale
except Exception:
    credit_caixa_on_sale = None  # type: ignore


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


def _items() -> dict[str, dict[str, Any]]:
    items = getattr(config, "VENDAS_ITENS", {}) or {}
    return dict(items)


def _get_item_names() -> list[str]:
    return list(_items().keys())


def _get_price(item_name: str, tipo: str) -> int:
    it = _items().get(item_name, {})
    if "preco" in it:
        return int(it.get("preco", 0))
    if tipo == "familia":
        return int(it.get("preco_familia", 0))
    return int(it.get("preco_pista", 0))


def _get_emoji(item_name: str) -> str:
    it = _items().get(item_name, {})
    return str(it.get("emoji", "🔫"))


async def _ensure_category_and_channels(guild: discord.Guild) -> tuple[discord.CategoryChannel, discord.TextChannel, discord.TextChannel]:
    cat_name = getattr(config, "VENDAS_CATEGORIA_NOME", "💰 - VENDAS")
    channel_name = getattr(config, "VENDAS_CHANNEL_NAME", "💰-vendas")
    log_name = getattr(config, "VENDAS_LOG_CHANNEL_NAME", "📋-logs-vendas")

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
    return Path(getattr(config, "VENDAS_EXPORT_CSV_PATH", "data/vendas.csv"))


def _append_csv(row: dict[str, str]) -> None:
    path = _csv_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    header = [
        "timestamp_utc",
        "guild_id",
        "canal_id",
        "tipo",
        "arma",
        "quantidade",
        "preco_unit",
        "total",
        "player",
        "registrado_por_id",
    ]

    file_exists = path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if not file_exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in header})


def _read_last_rows(limit: int = 20) -> list[dict[str, str]]:
    path = _csv_path()
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        r = list(csv.DictReader(f))
    return r[-limit:]


def _build_panel_embed() -> discord.Embed:
    emb = discord.Embed(
        title="💰 Painel de Vendas",
        description=(
            "**Passo a passo**\n"
            "1) Família / Pista\n"
            "2) Arma\n"
            "3) Quantidade + Player\n\n"
            "✅ Cada venda vira **uma linha** no canal de vendas."
        ),
    )
    emb.set_footer(text="Use /exportvendas para baixar o CSV (e XLSX, se habilitado)")
    return emb


class VendaStep1View(discord.ui.View):
    def __init__(self, cog: "VendasSystem"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Família", style=discord.ButtonStyle.primary, custom_id="vendas:step1:familia")
    async def familia(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog._step2(interaction, "familia")

    @discord.ui.button(label="Pista", style=discord.ButtonStyle.danger, custom_id="vendas:step1:pista")
    async def pista(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog._step2(interaction, "pista")


class VendaStep2View(discord.ui.View):
    def __init__(self, cog: "VendasSystem", tipo: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.tipo = tipo

        options = [
            discord.SelectOption(label=f"{_get_emoji(name)} {name}", value=name)
            for name in _get_item_names()
        ]
        if not options:
            options = [discord.SelectOption(label="(Configure VENDAS_ITENS no config.py)", value="__none__")]

        self.select = discord.ui.Select(
            placeholder="Escolha a arma",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"vendas:step2:arma:{tipo}",
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        arma = self.select.values[0]
        if arma == "__none__":
            return await interaction.response.send_message("❌ Configure `VENDAS_ITENS` no `config.py`.", ephemeral=True)

        modal = VendaStep3Modal(self.cog, tipo=self.tipo, arma=arma)
        await interaction.response.send_modal(modal)


class VendaStep3Modal(discord.ui.Modal, title="Registrar venda"):
    player = discord.ui.TextInput(label="ID DO PLAYER", placeholder="Ex: 12345", max_length=80)
    quantidade = discord.ui.TextInput(label="Quantidade Vendida", placeholder="Ex: 1", max_length=6)

    def __init__(self, cog: "VendasSystem", tipo: str, arma: str):
        super().__init__(timeout=None, custom_id=f"vendas:step3:{tipo}:{arma}")
        self.cog = cog
        self.tipo = tipo
        self.arma = arma

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qtd = int(str(self.quantidade.value).strip())
            if qtd <= 0:
                raise ValueError()
        except Exception:
            return await interaction.response.send_message("❌ Quantidade inválida. Use um número inteiro > 0.", ephemeral=True)

        await self.cog._registrar(
            interaction,
            tipo=self.tipo,
            arma=self.arma,
            qtd=qtd,
            player=str(self.player.value).strip(),
        )


class VendasSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.export_lock = asyncio.Lock()
        self.allowed_roles = list(getattr(config, "VENDAS_CARGOS_PERMITIDOS_IDS", [])) or list(getattr(config, "CARGOS_PERMITIDOS_IDS", []))

        # view persistente
        bot.add_view(VendaStep1View(self))

    def _check_perm(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return _has_any_role(interaction.user, self.allowed_roles)

    async def _step2(self, interaction: discord.Interaction, tipo: str) -> None:
        if not self._check_perm(interaction):
            return await interaction.response.send_message("❌ Você não tem permissão para registrar vendas.", ephemeral=True)

        emb = discord.Embed(
            title="💰 Registrar venda",
            description=f"**1) Tipo:** `{tipo}`\n\nAgora selecione a arma (Passo 2).",
        )
        await interaction.response.edit_message(embed=emb, view=VendaStep2View(self, tipo))

    async def _registrar(self, interaction: discord.Interaction, tipo: str, arma: str, qtd: int, player: str) -> None:
        if not self._check_perm(interaction):
            return await interaction.response.send_message("❌ Você não tem permissão para registrar vendas.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("❌ Sem guild.", ephemeral=True)

        _, vendas_ch, log_ch = await _ensure_category_and_channels(interaction.guild)

        unit = _get_price(arma, tipo)
        total = unit * qtd
        tipo_label = "Família" if tipo == "familia" else "Pista"

        # Linha única no canal de vendas
        line = (
            f" Tipo de Venda:  **[{tipo_label}]**{_get_emoji(arma)} \n| Tipo de arma: **{arma}** \n| Quantidade: x{qtd} \n| "
            f"**Total: {_money(total)}** \n| ID do Player: `{player}` \n| Vendido por <@{interaction.user.id}>"
        )
        await vendas_ch.send(line)

        # Log detalhado
        emb = discord.Embed(title="📋 Venda registrada", color=discord.Color.green())
        emb.add_field(name="Tipo", value=tipo_label, inline=True)
        emb.add_field(name="Arma", value=arma, inline=True)
        emb.add_field(name="Quantidade", value=str(qtd), inline=True)
        emb.add_field(name="Preço unit.", value=_money(unit), inline=True)
        emb.add_field(name="Total", value=_money(total), inline=True)
        emb.add_field(name="Player", value=player or "-", inline=False)
        emb.add_field(name="Registrado por", value=f"{interaction.user} ({interaction.user.id})", inline=False)
        emb.set_footer(text=_now_iso())
        await log_ch.send(embed=emb)

        # CSV
        _append_csv(
            {
                "timestamp_utc": _now_iso(),
                "guild_id": str(interaction.guild.id),
                "canal_id": str(vendas_ch.id),
                "tipo": tipo,
                "arma": arma,
                "quantidade": str(qtd),
                "preco_unit": str(unit),
                "total": str(total),
                "player": player,
                "registrado_por_id": str(interaction.user.id),
            }
        )

        # ECONOMIA: adiciona no caixa automaticamente (se habilitado)
        if credit_caixa_on_sale is not None:
            try:
                await credit_caixa_on_sale(interaction.guild.id, total, interaction.user.id)
            except Exception:
                pass

        # volta pro step1
        await interaction.response.edit_message(embed=_build_panel_embed(), view=VendaStep1View(self))

    # /painelvendas
    @app_commands.command(name="painelvendas", description="Posta o painel de vendas (passo a passo).")
    async def painelvendas(self, interaction: discord.Interaction):
        if not self._check_perm(interaction):
            return await interaction.response.send_message("❌ Você não tem permissão para usar isso.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("❌ Sem guild.", ephemeral=True)

        _, vendas_ch, _ = await _ensure_category_and_channels(interaction.guild)

        await interaction.channel.send(embed=_build_panel_embed(), view=VendaStep1View(self))
        await interaction.response.send_message(
            f"✅ Painel enviado aqui. A lista de vendas continua em {vendas_ch.mention}.",
            ephemeral=True,
        )

    # /exportvendas
    @app_commands.command(name="exportvendas", description="Exporta as vendas (CSV e XLSX se habilitado).")
    async def exportvendas(self, interaction: discord.Interaction):
        if not self._check_perm(interaction):
            return await interaction.response.send_message("❌ Você não tem permissão para exportar.", ephemeral=True)

        async with self.export_lock:
            csv_path = _csv_path()
            if not csv_path.exists():
                return await interaction.response.send_message("❌ Ainda não existe CSV (registre uma venda primeiro).", ephemeral=True)

            await interaction.response.send_message("📤 Preparando arquivos...", ephemeral=True)

            # CSV
            await interaction.followup.send(file=discord.File(str(csv_path)), ephemeral=True)

            # XLSX (opcional)
            if getattr(config, "VENDAS_EXPORT_XLSX", False):
                try:
                    xlsx_path = Path(getattr(config, "VENDAS_EXPORT_XLSX_PATH", "data/vendas.xlsx"))
                    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
                    self._csv_to_xlsx(csv_path, xlsx_path)
                    await interaction.followup.send(file=discord.File(str(xlsx_path)), ephemeral=True)
                except Exception:
                    await interaction.followup.send("⚠️ Não consegui gerar o XLSX. (veja o console / bot.log)", ephemeral=True)

    def _csv_to_xlsx(self, csv_path: Path, xlsx_path: Path) -> None:
        # Gera XLSX simples (sem depender de pandas)
        from openpyxl import Workbook

        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))

        wb = Workbook()
        ws = wb.active
        ws.title = "vendas"
        for r in rows:
            ws.append(r)
        wb.save(xlsx_path)

    # /listavendas (últimas vendas)
    @app_commands.command(name="listavendas", description="Mostra as últimas vendas registradas.")
    @app_commands.describe(quantidade="Quantas linhas mostrar (1 a 30)")
    async def listavendas(self, interaction: discord.Interaction, quantidade: int = 10):
        if not self._check_perm(interaction):
            return await interaction.response.send_message("❌ Você não tem permissão.", ephemeral=True)

        quantidade = max(1, min(int(quantidade), 30))
        rows = _read_last_rows(quantidade)

        if not rows:
            return await interaction.response.send_message("⚠️ Ainda não tem vendas registradas.", ephemeral=True)

        emb = discord.Embed(title=f"🧾 Últimas {len(rows)} vendas", color=discord.Color.gold())
        desc = []
        for r in rows:
            tipo = "Fam" if r.get("tipo") == "familia" else "Pista"
            arma = r.get("arma", "-")
            qtd = r.get("quantidade", "0")
            total = r.get("total", "0")
            player = r.get("player", "-")
            desc.append(f"`{tipo}` **{arma}** x{qtd} • Total {_money(int(total))} • `{player}`")

        emb.description = "\n".join(desc[:30])
        emb.set_footer(text="Use /exportvendas para baixar a planilha")
        await interaction.response.send_message(embed=emb, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VendasSystem(bot), guild=discord.Object(id=int(config.GUILD_ID)) if getattr(config, "GUILD_ID", None) else None)
