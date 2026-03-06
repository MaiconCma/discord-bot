import os
import sys
import asyncio
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

import config

# Carrega .env (não sobrescreve variáveis do sistema por padrão)
load_dotenv(override=True)

# Windows/console: evita problemas com emoji
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LOG_FILE = os.getenv("BOT_LOG_FILE", "bot.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)

discord.utils.setup_logging(level=logging.INFO, root=False)


def _build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.members = True  # necessário pro SET (nickname) e checagem de cargos
    intents.message_content = True  # necessário pro prefixo "!" (comandos de texto)
    return intents


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=_build_intents())

    async def setup_hook(self):
        # Carregamento seguro: se não existir um cog, só avisa e segue.
        extensions = [
            "cogs.set_system",
            "cogs.bau_system",
            "cogs.vendas_system",
            "cogs.help_system",
            "cogs.economia_system",
            "cogs.music",  # opcional (stub incluso)
        ]

        for ext in extensions:
            try:
                await self.load_extension(ext)
                logging.info("[OK] Cog carregado: %s", ext)
            except commands.ExtensionNotFound:
                logging.warning("[SKIP] Cog não encontrado: %s", ext)
            except commands.ExtensionFailed as e:
                logging.exception("[ERRO] Falha ao carregar %s: %r", ext, e)
                raise

        guild = discord.Object(id=config.GUILD_ID)
        self.tree.clear_commands(guild=guild)  # limpa comandos antigos/duplicados na guild
        await self.tree.sync(guild=guild)
        logging.info("[OK] Slash commands sincronizados na guild %s", config.GUILD_ID)

    async def on_command_error(self, context: commands.Context, exception: commands.CommandError) -> None:
        logging.exception("[CMD ERROR] %s -> %r", getattr(context.command, "qualified_name", "unknown"), exception)
        try:
            await context.send("❌ Deu erro ao executar o comando. (veja o console / bot.log)")
        except Exception:
            pass

    async def on_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        # Erros de Slash Commands
        logging.exception("[SLASH ERROR] %r", error)
        msg = "❌ Deu erro ao executar o comando. (veja o console / bot.log)"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


bot = Bot()


@bot.event
async def on_ready():
    logging.info("[OK] Bot online como %s (ID: %s)", bot.user, bot.user.id)


async def main():
    token = os.getenv("TOKEN")
    logging.info("[DEBUG] TOKEN len = %s", len(token or ""))

    if not token:
        raise RuntimeError("TOKEN não encontrado em variável de ambiente (.env).")

    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
