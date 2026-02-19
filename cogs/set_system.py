import discord
from discord.ext import commands
from discord import app_commands
from config import SET_LOG_CHANNEL


class SetModal(discord.ui.Modal, title="Solicitar SET"):
    nome = discord.ui.TextInput(label="Seu nome in-game", required=True, max_length=20)
    id = discord.ui.TextInput(label="Seu ID", required=True, max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        membro = interaction.user
        guild = interaction.guild

        if guild is None:
            return await interaction.response.send_message(
                "❌ Este comando só funciona dentro de um servidor.",
                ephemeral=True
            )

        if not self.id.value.isdigit():
            return await interaction.response.send_message(
                "❌ O ID deve conter apenas números.",
                ephemeral=True
            )

        novo_nick = f"{self.nome.value} | {self.id.value}"

        me = guild.me
        if me is None:
            return await interaction.response.send_message(
                "❌ Não consegui identificar o bot no servidor.",
                ephemeral=True
            )

        if not me.guild_permissions.manage_nicknames:
            return await interaction.response.send_message(
                "❌ Eu não tenho permissão **Manage Nicknames** (Gerenciar apelidos).",
                ephemeral=True
            )

        if membro.top_role >= me.top_role and guild.owner_id != membro.id:
            return await interaction.response.send_message(
                "❌ Não consigo alterar seu apelido por causa da **hierarquia de cargos**.\n"
                "➡️ Suba o cargo do bot acima do seu cargo no servidor.",
                ephemeral=True
            )

        try:
            await membro.edit(nick=novo_nick, reason="SET solicitado via bot")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ Discord negou a alteração do nickname.\n"
                "Verifique **Manage Nicknames** e a **hierarquia de cargos**.",
                ephemeral=True
            )
        except discord.HTTPException as e:
            return await interaction.response.send_message(
                f"❌ Discord recusou a alteração do nickname. Detalhe: `{e}`",
                ephemeral=True
            )

        log_channel = discord.utils.get(guild.text_channels, name=SET_LOG_CHANNEL)
        if log_channel:
            await log_channel.send(
                f"📋 SET realizado\n👤 {membro.mention}\n📝 {novo_nick}"
            )

        await interaction.response.send_message(
            f"✅ Nome atualizado: **{novo_nick}**",
            ephemeral=True
        )


class SetView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Solicitar SET", style=discord.ButtonStyle.blurple, custom_id="set_button")
    async def button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetModal())


class SetSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(SetView())

    @app_commands.command(name="painelset", description="Criar painel SET")
    @app_commands.checks.has_permissions(administrator=True)
    async def painelset(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📋 Sistema de SET",
            description="Clique no botão abaixo para registrar seu nome e ID.",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, view=SetView())


async def setup(bot):
    await bot.add_cog(SetSystem(bot))
