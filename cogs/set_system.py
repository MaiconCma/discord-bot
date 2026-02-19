import discord
from discord.ext import commands
from discord import app_commands
from config import SET_LOG_CHANNEL


class SetModal(discord.ui.Modal, title="Solicitar SET"):
    nome = discord.ui.TextInput(label="Seu nome in-game", required=True, max_length=20)
    player_id = discord.ui.TextInput(label="Seu ID", required=True, max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        membro = interaction.user

        if guild is None:
            return await interaction.response.send_message(
                "❌ Este comando só funciona dentro de um servidor.",
                ephemeral=True
            )

        # pega o "Member" do bot de forma confiável
        bot_member = guild.me
        if bot_member is None and interaction.client.user is not None:
            bot_member = guild.get_member(interaction.client.user.id)

        if bot_member is None:
            return await interaction.response.send_message(
                "❌ Não consegui identificar o bot no servidor (guild.me).",
                ephemeral=True
            )

        # valida ID
        if not self.player_id.value.isdigit():
            return await interaction.response.send_message(
                "❌ O ID deve conter apenas números.",
                ephemeral=True
            )

        novo_nick = f"{self.nome.value} | {self.player_id.value}"

        # permissão
        if not bot_member.guild_permissions.manage_nicknames:
            return await interaction.response.send_message(
                "❌ Eu não tenho a permissão **Manage Nicknames**.\n"
                "➡️ Vá em *Server Settings → Roles → (cargo do bot) → Permissions* e habilite.",
                ephemeral=True
            )

        # hierarquia de cargos (bot precisa estar acima do usuário)
        # OBS: dono do servidor sempre pode tudo, mas o bot ainda respeita hierarquia
        if membro.top_role >= bot_member.top_role and guild.owner_id != membro.id:
            return await interaction.response.send_message(
                "❌ Não consigo alterar seu apelido por causa da **hierarquia de cargos**.\n"
                f"➡️ Seu cargo: **{membro.top_role.name}**\n"
                f"➡️ Cargo do bot: **{bot_member.top_role.name}**\n\n"
                "✅ Solução: coloque o **cargo do bot acima** do seu cargo em *Server Settings → Roles*.",
                ephemeral=True
            )

        try:
            await membro.edit(nick=novo_nick, reason="SET solicitado via bot")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ O Discord bloqueou a alteração do nickname.\n"
                "➡️ Confira: permissão **Manage Nicknames** + hierarquia de cargos.",
                ephemeral=True
            )
        except discord.HTTPException as e:
            return await interaction.response.send_message(
                f"❌ Erro do Discord ao alterar o nickname: `{e}`",
                ephemeral=True
            )

        # log
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

    @discord.ui.button(
        label="Solicitar SET",
        style=discord.ButtonStyle.blurple,
        custom_id="set_button"
    )
    async def button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetModal())


class SetSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
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


async def setup(bot: commands.Bot):
    await bot.add_cog(SetSystem(bot))
