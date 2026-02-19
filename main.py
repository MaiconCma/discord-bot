import os
import discord
from discord.ext import commands
import asyncio
from config import GUILD_ID

intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",  # não use "/" (conflita com slash commands)
            intents=intents
        )

    async def setup_hook(self):
        # Carrega seus cogs
        await self.load_extension("cogs.set_system")
        await self.load_extension("cogs.bau_system")
        await self.load_extension("cogs.music")

        # Faz os slash commands aparecerem na hora (guild sync)
        guild = discord.Object(id=GUILD_ID)

        # Copia comandos globais para a guild (necessário com sync(guild=...))
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

        print(f"Slash commands sincronizados para guild {GUILD_ID}")


bot = Bot()


@bot.event
async def on_ready():
    print(f"Bot online como {bot.user} (ID: {bot.user.id})")


async def main():
    token = os.getenv("TOKEN")
    if not token:
        raise RuntimeError("TOKEN não encontrado em variável de ambiente.")
    async with bot:
        await bot.start(token)


asyncio.run(main())
