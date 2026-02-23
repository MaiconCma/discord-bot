import discord
from discord.ext import commands
from discord import app_commands
from config import CATEGORIA_NOME, LOG_CHANNEL_NAME, CARGOS_PERMITIDOS_IDS


class BauModal(discord.ui.Modal, title="Criar Baú"):
    numero = discord.ui.TextInput(label="Número do seu baú", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        membro = interaction.user

        if guild is None:
            return await interaction.response.send_message(
                "❌ Este comando só funciona dentro de um servidor.",
                ephemeral=True
            )

        if not self.numero.value.isdigit():
            return await interaction.response.send_message(
                "❌ O número deve conter apenas números.",
                ephemeral=True
            )

        categoria = discord.utils.get(guild.categories, name=CATEGORIA_NOME)
        if not categoria:
            return await interaction.response.send_message(
                f"❌ Categoria `{CATEGORIA_NOME}` não encontrada.",
                ephemeral=True
            )

        for canal in categoria.text_channels:
            if canal.topic == str(membro.id):
                return await interaction.response.send_message(
                    "⚠️ Você já possui um baú.",
                    ephemeral=True
                )

        nome_canal = f"💸-{self.numero.value}-{membro.name.lower()}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            membro: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        for cargo_id in CARGOS_PERMITIDOS_IDS:
            cargo = guild.get_role(cargo_id)
            if cargo:
                overwrites[cargo] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        novo_canal = await guild.create_text_channel(
            name=nome_canal,
            category=categoria,
            overwrites=overwrites,
            topic=str(membro.id)
        )

        log_channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        if log_channel:
            await log_channel.send(
                f"📦 Baú criado\n👤 {membro.mention}\n📁 {novo_canal.mention}"
            )

        await interaction.response.send_message(
            f"✅ Baú criado: {novo_canal.mention}",
            ephemeral=True
        )


class BauView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Criar Baú",
        style=discord.ButtonStyle.green,
        custom_id="criar_bau_button"
    )
    async def button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BauModal())


class BauSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(BauView())

    @app_commands.command(name="painelbau", description="Criar painel de baús")
    @app_commands.checks.has_permissions(administrator=True)
    async def painelbau(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📦 Sistema de Baús",
            description="Clique para criar seu baú privado.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, view=BauView())


async def setup(bot):
    await bot.add_cog(BauSystem(bot))
