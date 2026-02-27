import os
import sys
import asyncio

import discord
from discord.ext import commands
from dotenv import load_dotenv

from config import GUILD_ID

load_dotenv(override=True)
sys.stdout.reconfigure(encoding="utf-8")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Carrega os cogs
        await self.load_extension("cogs.set_system")
        await self.load_extension("cogs.bau_system")
        await self.load_extension("cogs.music")
        await self.load_extension("cogs.vendas_system")  # ✅ painelvendas / exportvendas

        # Sincroniza slash commands na guild
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"[OK] Slash commands sincronizados na guild {GUILD_ID}")


bot = Bot()


@bot.event
async def on_ready():
    print(f"[OK] Bot online como {bot.user} (ID: {bot.user.id})")


async def main():
    token = os.getenv("TOKEN")
    print("[DEBUG] TOKEN len =", len(token or ""))

    if not token:
        raise RuntimeError("TOKEN não encontrado em variável de ambiente (.env).")

    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
