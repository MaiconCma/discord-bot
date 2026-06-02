from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord.ext import commands
from discord import app_commands

import config
from datastore import AsyncJsonStore

# Store assíncrono (thread-safe, não bloqueia o event loop)
_store = AsyncJsonStore(getattr(config, "ECONOMY_JSON_PATH", "data/economia.json"))
_lock = asyncio.Lock()  # Lock adicional para operações compostas de negócio

def _is_admin(member: discord.Member) -> bool:
    """Verifica se o membro tem permissão administrativa para economia."""
    if member.guild_permissions.administrator:
        return True
    allowed = list(getattr(config, "ECONOMY_ADMIN_ROLE_IDS", [])) or list(
        getattr(config, "CARGOS_PERMITIDOS_IDS", [])
    )
    user_role_ids = {r.id for r in member.roles}
    return any(rid in user_role_ids for rid in allowed)

def _get_guild_bucket(data: dict[str, Any], guild_id: int) -> dict[str, Any]:
    """Obtém ou cria o bucket de dados da guild."""
    g = data.setdefault("guilds", {}).setdefault(str(guild_id), {})
    g.setdefault("balances", {})
    g.setdefault("caixa", 0)
    return g

async def credit_caixa_on_sale(guild_id: int, amount: int, actor_id: int | None = None) -> None:
    """Adiciona valor ao caixa da organização quando uma venda é registrada."""
    if not getattr(config, "ECONOMY_ENABLED", True):
        return
    if not getattr(config, "ECONOMY_CREDIT_CAIXA_ON_SALE", True):
        return
    if amount <= 0:
        return

    async with _lock:
        # Tenta usar thread para não bloquear o event loop caso a leitura seja síncrona por baixo dos panos
        data = await asyncio.to_thread(_store.read_sync) if hasattr(_store, 'read_sync') else await _store.read()
        g = _get_guild_bucket(data, guild_id)
        g["caixa"] = int(g.get("caixa", 0)) + int(amount)
        if hasattr(_store, 'write_sync'):
            await asyncio.to_thread(_store.write_sync, data)
        else:
            await _store.write(data)

class EconomiaSystem(commands.Cog):
    """Sistema de economia (saldo de usuários e caixa da organização)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="saldo", description="Mostra seu saldo atual.")
    async def saldo(self, interaction: discord.Interaction):
        if not getattr(config, "ECONOMY_ENABLED", True):
            return await interaction.response.send_message(
                "⚠️ Economia está desativada no config.", ephemeral=True
            )

        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "❌ Comando só funciona no servidor.", ephemeral=True
            )

        async with _lock:
            data = await _store.read()
            g = _get_guild_bucket(data, interaction.guild.id)
            bal = int(g["balances"].get(str(interaction.user.id), 0))

        await interaction.response.send_message(
            f"💳 Seu saldo: **{bal:,}**".replace(",", "."), ephemeral=True
        )

    @app_commands.command(name="caixa", description="Mostra o caixa da organização (total de vendas).")
    async def caixa(self, interaction: discord.Interaction):
        if not getattr(config, "ECONOMY_ENABLED", True):
            return await interaction.response.send_message(
                "⚠️ Economia está desativada no config.", ephemeral=True
            )

        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ Comando só funciona no servidor.", ephemeral=True
            )

        async with _lock:
            data = await _store.read()
            g = _get_guild_bucket(data, interaction.guild.id)
            caixa = int(g.get("caixa", 0))

        await interaction.response.send_message(
            f"🏦 Caixa: **{caixa:,}**".replace(",", "."), ephemeral=True
        )

    @app_commands.command(name="addsaldo", description="[ADMIN] Adiciona saldo a um membro.")
    @app_commands.describe(membro="Membro que receberá o saldo", valor="Quantia a adicionar")
    async def addsaldo(self, interaction: discord.Interaction, membro: discord.Member, valor: int):
        if not getattr(config, "ECONOMY_ENABLED", True):
            return await interaction.response.send_message(
                "⚠️ Economia está desativada no config.", ephemeral=True
            )

        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "❌ Comando só funciona no servidor.", ephemeral=True
            )

        if not _is_admin(interaction.user):
            return await interaction.response.send_message(
                "❌ Você não tem permissão para isso.", ephemeral=True
            )

        if valor <= 0:
            return await interaction.response.send_message(
                "❌ Valor precisa ser maior que zero.", ephemeral=True
            )

        async with _lock:
            data = await _store.read()
            g = _get_guild_bucket(data, interaction.guild.id)
            balances = g["balances"]
            uid = str(membro.id)
            balances[uid] = int(balances.get(uid, 0)) + int(valor)
            await _store.write(data)

        await interaction.response.send_message(
            f"✅ Adicionado **{valor:,}** para {membro.mention}.".replace(",", "."),
            ephemeral=True
        )

    @app_commands.command(name="remsaldo", description="[ADMIN] Remove saldo de um membro.")
    @app_commands.describe(membro="Membro que perderá o saldo", valor="Quantia a remover")
    async def remsaldo(self, interaction: discord.Interaction, membro: discord.Member, valor: int):
        if not getattr(config, "ECONOMY_ENABLED", True):
            return await interaction.response.send_message(
                "⚠️ Economia está desativada no config.", ephemeral=True
            )

        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "❌ Comando só funciona no servidor.", ephemeral=True
            )

        if not _is_admin(interaction.user):
            return await interaction.response.send_message(
                "❌ Você não tem permissão para isso.", ephemeral=True
            )

        if valor <= 0:
            return await interaction.response.send_message(
                "❌ Valor precisa ser maior que zero.", ephemeral=True
            )

        async with _lock:
            data = await _store.read()
            g = _get_guild_bucket(data, interaction.guild.id)
            balances = g["balances"]
            uid = str(membro.id)
            current = int(balances.get(uid, 0))
            balances[uid] = max(0, current - int(valor))
            await _store.write(data)

        await interaction.response.send_message(
            f"✅ Removido **{valor:,}** de {membro.mention}.".replace(",", "."),
            ephemeral=True
        )

    @app_commands.command(name="pagar", description="Transfere saldo para outro membro.")
    @app_commands.describe(membro="Quem vai receber", valor="Valor a transferir")
    async def pagar(self, interaction: discord.Interaction, membro: discord.Member, valor: int):
        if not getattr(config, "ECONOMY_ENABLED", True):
            return await interaction.response.send_message(
                "⚠️ Economia está desativada no config.", ephemeral=True
            )

        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "❌ Comando só funciona no servidor.", ephemeral=True
            )

        if valor <= 0:
            return await interaction.response.send_message(
                "❌ Valor precisa ser maior que zero.", ephemeral=True
            )

        if membro.id == interaction.user.id:
            return await interaction.response.send_message(
                "❌ Você não pode pagar a si mesmo.", ephemeral=True
            )

        async with _lock:
            data = await _store.read()
            g = _get_guild_bucket(data, interaction.guild.id)
            balances = g["balances"]

            payer_id = str(interaction.user.id)
            payee_id = str(membro.id)

            payer_balance = int(balances.get(payer_id, 0))
            if payer_balance < valor:
                return await interaction.response.send_message(
                    "❌ Saldo insuficiente.", ephemeral=True
                )

            balances[payer_id] = payer_balance - int(valor)
            balances[payee_id] = int(balances.get(payee_id, 0)) + int(valor)
            await _store.write(data)

        await interaction.response.send_message(
            f"✅ Transferido **{valor:,}** para {membro.mention}.".replace(",", "."),
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(EconomiaSystem(bot))