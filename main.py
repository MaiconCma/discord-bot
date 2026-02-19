import os
import sys
import discord
from discord.ext import commands
import asyncio
from config import GUILD_ID

sys.stdout.reconfigure(encoding="utf-8")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.load_extension("cogs.set_system")
        await self.load_extension("cogs.bau_system")
        await self.load_extension("cogs.music")

        guild = discord.Object(id=GUILD_ID)

        # ✅ Registra comandos apenas na guild (sem global)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

        print(f"[OK] Slash commands sincronizados na guild {GUILD_ID}")


bot = Bot()


@bot.event
async def on_ready():
    print(f"[OK] Bot online como {bot.user} (ID: {bot.user.id})")


async def main():
    token = os.getenv("TOKEN")
    if not token:
        raise RuntimeError("TOKEN não encontrado em variável de ambiente.")
    async with bot:
        await bot.start(token)


asyncio.run(main())
