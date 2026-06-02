import discord
from discord.ext import commands
from discord import app_commands
from config import SET_LOG_CHANNEL, CITIES_CONFIG

class SetModal(discord.ui.Modal):
    nome = discord.ui.TextInput(
        label="Seu nome in-game", 
        required=True, 
        max_length=20, 
        placeholder="Ex: João Silva"
    )
    player_id = discord.ui.TextInput(
        label="Seu Passaporte/ID", 
        required=True, 
        max_length=10, 
        placeholder="Ex: 12345"
    )

    def __init__(self, selected_city_key: str):
        self.city_info = CITIES_CONFIG[selected_city_key]
        super().__init__(title=f"SET - {self.city_info['label']}")

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        membro = interaction.user
        bot_member = guild.me

        if not self.player_id.value.isdigit():
            return await interaction.response.send_message(
                "❌ O ID deve conter apenas números.", ephemeral=True
            )

        novo_nick = f"{self.nome.value} | {self.player_id.value}"
        cargo_cidade = guild.get_role(self.city_info["role_id"])

        if not bot_member.guild_permissions.manage_nicknames or not bot_member.guild_permissions.manage_roles:
            return await interaction.response.send_message(
                "❌ O bot precisa das permissões **Manage Nicknames** e **Manage Roles**.", ephemeral=True
            )

        if membro.top_role >= bot_member.top_role and guild.owner_id != membro.id:
            return await interaction.response.send_message(
                "❌ Meu cargo é menor ou igual ao seu. Não posso alterar seus dados.", ephemeral=True
            )

        if cargo_cidade and cargo_cidade >= bot_member.top_role:
            return await interaction.response.send_message(
                f"❌ Não posso dar o cargo **{cargo_cidade.name}** pois ele está acima do meu cargo.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            await membro.edit(nick=novo_nick, reason="SET solicitado via bot")
            if cargo_cidade:
                await membro.add_roles(cargo_cidade, reason=f"SET: Cidade {self.city_info['label']}")

            mensagem_sucesso = f"✅ SET concluído com sucesso!\n👤 Nome: **{novo_nick}**\n🏙️ Cidade: **{self.city_info['label']}**"
            await interaction.followup.send(mensagem_sucesso, ephemeral=True)

            log_channel = discord.utils.get(guild.text_channels, name=SET_LOG_CHANNEL)
            if log_channel:
                embed_log = discord.Embed(title="📋 Novo SET Realizado", color=discord.Color.green())
                embed_log.add_field(name="Membro", value=membro.mention, inline=False)
                embed_log.add_field(name="Novo Nick", value=f"`{novo_nick}`", inline=True)
                embed_log.add_field(name="Cidade", value=self.city_info['label'], inline=True)
                embed_log.set_thumbnail(url=membro.display_avatar.url)
                await log_channel.send(embed=embed_log)

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Falha de permissão ao tentar aplicar o SET. Contate a administração.", ephemeral=True
            )
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"❌ Ocorreu um erro de conexão com o Discord: `{e}`", ephemeral=True
            )

class CitySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=data["label"], 
                description=data["description"], 
                value=key
            ) for key, data in CITIES_CONFIG.items()
        ]
        
        super().__init__(
            placeholder="Escolha a cidade onde vai jogar...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="city_select_menu"
        )

    async def callback(self, interaction: discord.Interaction):
        selected_key = self.values[0]
        await interaction.response.send_modal(SetModal(selected_city_key=selected_key))

class SetView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CitySelect())

class SetSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(SetView())

    @app_commands.command(name="painelset", description="Cria o painel de registro (SET)")
    @app_commands.checks.has_permissions(administrator=True)
    async def painelset(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🛂 Passaporte e Registro",
            description="Seja bem-vindo!\nPor favor, **selecione a cidade** abaixo para iniciar seu registro e receber seus cargos.",
            color=discord.Color.dark_theme()
        )
        embed.set_footer(text="Sistema de SET Automatizado")
        
        await interaction.response.send_message(embed=embed, view=SetView())

async def setup(bot: commands.Bot):
    await bot.add_cog(SetSystem(bot))