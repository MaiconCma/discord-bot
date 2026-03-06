from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord.ext import commands
from discord import app_commands

import config
from datastore import JsonStore


_store = JsonStore(getattr(config, "ECONOMY_JSON_PATH", "data/economia.json"))
_lock = asyncio.Lock()


def _is_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    allowed = list(getattr(config, "ECONOMY_ADMIN_ROLE_IDS", [])) or list(getattr(config, "CARGOS_PERMITIDOS_IDS", []))
    ids = {r.id for r in member.roles}
    return any(rid in ids for rid in allowed)


def _get_guild_bucket(data: dict[str, Any], guild_id: int) -> dict[str, Any]:
    g = data.setdefault("guilds", {}).setdefault(str(guild_id), {})
    g.setdefault("balances", {})
    g.setdefault("caixa", 0)
    return g


async def credit_caixa_on_sale(guild_id: int, amount: int, actor_id: int | None = None) -> None:
    if not getattr(config, "ECONOMY_ENABLED", True):
        return
    if not getattr(config, "ECONOMY_CREDIT_CAIXA_ON_SALE", True):
        return
    if amount <= 0:
        return

    async with _lock:
        data = _store.read()
        g = _get_guild_bucket(data, guild_id)
        g["caixa"] = int(g.get("caixa", 0)) + int(amount)
        _store.write(data)


class EconomiaSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # /saldo
    @app_commands.command(name="saldo", description="Mostra seu saldo (economia do servidor).")
    async def saldo(self, interaction: discord.Interaction):
        if not getattr(config, "ECONOMY_ENABLED", True):
            return await interaction.response.send_message("⚠️ Economia está desativada no config.", ephemeral=True)

        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Comando só funciona no servidor.", ephemeral=True)

        async with _lock:
            data = _store.read()
            g = _get_guild_bucket(data, interaction.guild.id)
            bal = int(g["balances"].get(str(interaction.user.id), 0))

        await interaction.response.send_message(f"💳 Seu saldo: **{bal:,}**".replace(",", "."), ephemeral=True)

    # /caixa
    @app_commands.command(name="caixa", description="Mostra o caixa da organização (somatório das vendas).")
    async def caixa(self, interaction: discord.Interaction):
        if not getattr(config, "ECONOMY_ENABLED", True):
            return await interaction.response.send_message("⚠️ Economia está desativada no config.", ephemeral=True)

        if not interaction.guild:
            return await interaction.response.send_message("❌ Comando só funciona no servidor.", ephemeral=True)

        async with _lock:
            data = _store.read()
            g = _get_guild_bucket(data, interaction.guild.id)
            caixa = int(g.get("caixa", 0))

        await interaction.response.send_message(f"🏦 Caixa: **{caixa:,}**".replace(",", "."), ephemeral=True)

    # /addsaldo (admin)
    @app_commands.command(name="addsaldo", description="Admin: adiciona saldo para um membro.")
    @app_commands.describe(membro="Quem vai receber", valor="Valor a adicionar")
    async def addsaldo(self, interaction: discord.Interaction, membro: discord.Member, valor: int):
        if not getattr(config, "ECONOMY_ENABLED", True):
            return await interaction.response.send_message("⚠️ Economia está desativada no config.", ephemeral=True)

        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Comando só funciona no servidor.", ephemeral=True)

        if not _is_admin(interaction.user):
            return await interaction.response.send_message("❌ Você não tem permissão para isso.", ephemeral=True)

        if valor <= 0:
            return await interaction.response.send_message("❌ Valor precisa ser > 0.", ephemeral=True)

        async with _lock:
            data = _store.read()
            g = _get_guild_bucket(data, interaction.guild.id)
            b = g["balances"]
            b[str(membro.id)] = int(b.get(str(membro.id), 0)) + int(valor)
            _store.write(data)

        await interaction.response.send_message(f"✅ Adicionado **{valor:,}** para {membro.mention}.".replace(",", "."), ephemeral=True)

    # /remsaldo (admin)
    @app_commands.command(name="remsaldo", description="Admin: remove saldo de um membro.")
    @app_commands.describe(membro="Quem vai perder", valor="Valor a remover")
    async def remsaldo(self, interaction: discord.Interaction, membro: discord.Member, valor: int):
        if not getattr(config, "ECONOMY_ENABLED", True):
            return await interaction.response.send_message("⚠️ Economia está desativada no config.", ephemeral=True)

        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Comando só funciona no servidor.", ephemeral=True)

        if not _is_admin(interaction.user):
            return await interaction.response.send_message("❌ Você não tem permissão para isso.", ephemeral=True)

        if valor <= 0:
            return await interaction.response.send_message("❌ Valor precisa ser > 0.", ephemeral=True)

        async with _lock:
            data = _store.read()
            g = _get_guild_bucket(data, interaction.guild.id)
            b = g["balances"]
            cur = int(b.get(str(membro.id), 0))
            b[str(membro.id)] = max(0, cur - int(valor))
            _store.write(data)

        await interaction.response.send_message(f"✅ Removido **{valor:,}** de {membro.mention}.".replace(",", "."), ephemeral=True)

    # /pagar
    @app_commands.command(name="pagar", description="Transfere saldo para outro membro.")
    @app_commands.describe(membro="Quem recebe", valor="Quanto transferir")
    async def pagar(self, interaction: discord.Interaction, membro: discord.Member, valor: int):
        if not getattr(config, "ECONOMY_ENABLED", True):
            return await interaction.response.send_message("⚠️ Economia está desativada no config.", ephemeral=True)

        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Comando só funciona no servidor.", ephemeral=True)

        if valor <= 0:
            return await interaction.response.send_message("❌ Valor precisa ser > 0.", ephemeral=True)

        if membro.id == interaction.user.id:
            return await interaction.response.send_message("❌ Você não pode pagar você mesmo.", ephemeral=True)

        async with _lock:
            data = _store.read()
            g = _get_guild_bucket(data, interaction.guild.id)
            b = g["balances"]

            payer = str(interaction.user.id)
            payee = str(membro.id)

            cur = int(b.get(payer, 0))
            if cur < valor:
                return await interaction.response.send_message("❌ Saldo insuficiente.", ephemeral=True)

            b[payer] = cur - int(valor)
            b[payee] = int(b.get(payee, 0)) + int(valor)
            _store.write(data)

        await interaction.response.send_message(
            f"✅ Transferido **{valor:,}** para {membro.mention}.".replace(",", "."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomiaSystem(bot), guild=discord.Object(id=int(config.GUILD_ID)) if getattr(config, "GUILD_ID", None) else None)
