import os
import discord
from discord.ext import commands
from discord import app_commands

# ===== INTENTS =====
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ==========================
# ===== MODAL (INPUT) =====
# ==========================
class BauModal(discord.ui.Modal, title="Criar Baú"):

    numero = discord.ui.TextInput(
        label="Qual é o número do seu baú in-game?",
        placeholder="Ex: 14",
        required=True,
        max_length=5
    )

    async def on_submit(self, interaction: discord.Interaction):

        guild = interaction.guild
        membro = interaction.user
        numero_digitado = self.numero.value.strip()

        # Buscar categoria
        categoria = discord.utils.get(guild.categories, name="📦 - BAÚ")

        if categoria is None:
            return await interaction.response.send_message(
                "❌ Categoria 📦 - BAÚ não encontrada.",
                ephemeral=True
            )

        # Verifica se já existe canal com esse número
        for canal in categoria.text_channels:
            if canal.name.startswith(f"💸-{numero_digitado}-"):
                return await interaction.response.send_message(
                    "⚠️ Já existe um baú com esse número!",
                    ephemeral=True
                )

        nome_canal = f"💸-{numero_digitado}-{membro.name.lower()}"

        # IDs dos cargos que também podem ver os baús
        cargos_permitidos_ids = [
            1469341592530845756,
            1469341592497422579,
            1469341592497422577
        ]

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            membro: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        # Adiciona permissões para os cargos
        for cargo_id in cargos_permitidos_ids:
            cargo = guild.get_role(cargo_id)
            if cargo:
                overwrites[cargo] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        novo_canal = await guild.create_text_channel(
            name=nome_canal,
            category=categoria,
            overwrites=overwrites
        )

        await interaction.response.send_message(
            f"✅ Baú criado com sucesso: {novo_canal.mention}",
            ephemeral=True
        )


# ==========================
# ===== BOTÃO =====
# ==========================
class BauButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Criar Baú", style=discord.ButtonStyle.green)
    async def criar_bau(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BauModal())


# ==========================
# ===== SLASH COMMAND =====
# ==========================
@tree.command(name="painelbau", description="Criar painel do sistema de baús")
@app_commands.checks.has_permissions(administrator=True)
async def painelbau(interaction: discord.Interaction):

    embed = discord.Embed(
        title="📦 Sistema de Baús",
        description="Clique no botão abaixo para criar seu baú privado.",
        color=discord.Color.green()
    )

    await interaction.response.send_message(embed=embed, view=BauButton())


# ==========================
# ===== EVENTO READY =====
# ==========================
@bot.event
async def on_ready():
    synced = await tree.sync()
    print(f"Sincronizados {len(synced)} comandos.")
    print(f"Bot online como {bot.user}")


# ===== RODAR BOT =====
bot.run(os.getenv("TOKEN"))
