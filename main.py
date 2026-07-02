import os
import sys
import asyncio
import logging
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import portalocker

# ==================================================
# BASE DO PROJETO
# ==================================================
BASE_DIR = Path(__file__).resolve().parent

# Garante que imports como repositories/, services/ e utils/ funcionem
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Garante que caminhos relativos como data/animes.db funcionem
os.chdir(BASE_DIR)

# Carrega .env ANTES do config.py
load_dotenv(BASE_DIR / ".env", override=True)

import config  # noqa: E402


# ==================================================
# LOCK PARA EVITAR DUPLICIDADE
# ==================================================
def acquire_lock():
    try:
        lock_file = open(BASE_DIR / "bot.lock", "w", encoding="utf-8")
        portalocker.lock(lock_file, portalocker.LOCK_EX | portalocker.LOCK_NB)
        return lock_file
    except portalocker.LockException:
        return None


lock_file = acquire_lock()

if lock_file is None:
    print("Bot já está rodando. Saindo.")
    sys.exit(0)


# ==================================================
# ENCODING / LOGS
# ==================================================
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LOG_FILE = os.getenv("BOT_LOG_FILE", "bot.log")
LOG_PATH = BASE_DIR / LOG_FILE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)

discord.utils.setup_logging(level=logging.INFO, root=False)


# ==================================================
# INTENTS
# ==================================================
def _build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = True
    return intents


# ==================================================
# BOT
# ==================================================
class Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=_build_intents(),
            help_command=None,
        )

        # Faz o discord.py usar nosso handler de erro para slash commands
        self.tree.on_error = self.on_tree_error

    async def setup_hook(self):
        extensions = [
            "cogs.set_system",
            "cogs.bau_system",
            "cogs.vendas_system",
            "cogs.help_system",
            "cogs.economia_system",
            "cogs.anime_reminder_system",
            "cogs.music",
            "cogs.dev_info",
            "cogs.ponto_system",
            "cogs.bolao_copa_system",
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

            except Exception as e:
                logging.exception("[ERRO] Erro inesperado ao carregar %s: %r", ext, e)
                raise

        logging.info(
            "[INFO] Setup Hook finalizado. "
            "Use !sync no Discord se tiver comandos slash novos."
        )

    async def on_command_error(
        self,
        context: commands.Context,
        exception: commands.CommandError,
    ):
        if isinstance(exception, commands.CommandNotFound):
            return

        if isinstance(exception, commands.MissingPermissions):
            try:
                await context.send("❌ Você não tem permissão para usar este comando.")
            except Exception:
                pass
            return

        logging.exception(
            "[CMD ERROR] %s -> %r",
            getattr(context.command, "qualified_name", "unknown"),
            exception,
        )

        try:
            await context.send("❌ Deu erro ao executar o comando. Veja o console ou `bot.log`.")
        except Exception:
            pass

    async def on_tree_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        logging.exception("[SLASH ERROR] %r", error)

        if isinstance(error, app_commands.errors.CommandSignatureMismatch):
            msg = (
                "❌ Este comando slash está desatualizado no Discord.\n\n"
                "Faça nesta ordem:\n"
                "1. Digite `!limparslash`\n"
                "2. Pare o bot com `CTRL + C`\n"
                "3. Rode `python main.py`\n"
                "4. Digite `!sync`\n"
                "5. Recarregue o Discord com `Ctrl + R`"
            )
        elif isinstance(error, app_commands.errors.MissingPermissions):
            msg = "❌ Você não tem permissão para usar este comando."
        else:
            msg = "❌ Deu erro ao executar o comando. Veja o console ou `bot.log`."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


bot = Bot()


# ==================================================
# EVENTOS
# ==================================================
@bot.event
async def on_ready():
    if bot.user:
        logging.info("[OK] Bot online como %s (ID: %s)", bot.user, bot.user.id)
    else:
        logging.info("[OK] Bot online.")


# ==================================================
# COMANDO PREFIXO: !sync
# ==================================================
@bot.command(
    name="sync",
    description="[ADMIN] Sincroniza comandos slash com o servidor atual",
)
@commands.has_permissions(administrator=True)
async def sync_cmds(ctx: commands.Context):
    if ctx.guild is None:
        return await ctx.send("❌ Use este comando dentro de um servidor.")

    msg = await ctx.send("🔄 Sincronizando comandos slash neste servidor...")

    try:
        guild_obj = discord.Object(id=ctx.guild.id)

        # 1. Remove comandos antigos específicos deste servidor
        bot.tree.clear_commands(guild=guild_obj)
        await bot.tree.sync(guild=guild_obj)

        # 2. Copia comandos atuais carregados no código para o servidor
        bot.tree.copy_global_to(guild=guild_obj)

        # 3. Sincroniza comandos atuais no servidor
        synced = await bot.tree.sync(guild=guild_obj)

        await msg.edit(
            content=(
                f"✅ Sincronização concluída!\n"
                f"**{len(synced)}** comandos slash atualizados neste servidor.\n\n"
                f"Se ainda aparecer comando duplicado, use `!limparslash`."
            )
        )

    except Exception as e:
        logging.exception("[SYNC ERROR] %r", e)
        await msg.edit(content=f"❌ Erro ao sincronizar: `{e}`")


# ==================================================
# COMANDO PREFIXO: !limparslash
# ==================================================
@bot.command(
    name="limparslash",
    description="[ADMIN] Remove comandos slash globais e do servidor atual",
)
@commands.has_permissions(administrator=True)
async def limpar_slash(ctx: commands.Context):
    if ctx.guild is None:
        return await ctx.send("❌ Use este comando dentro de um servidor.")

    msg = await ctx.send("🧹 Limpando comandos slash antigos...")

    try:
        guild_obj = discord.Object(id=ctx.guild.id)

        # 1. Limpa comandos específicos deste servidor no Discord
        bot.tree.clear_commands(guild=guild_obj)
        await bot.tree.sync(guild=guild_obj)

        # 2. Limpa comandos globais antigos no Discord
        # Isso remove comandos antigos que causam duplicidade.
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync(guild=None)

        await msg.edit(
            content=(
                "✅ Comandos slash antigos removidos.\n\n"
                "Agora faça exatamente nesta ordem:\n"
                "1. Pare o bot com `CTRL + C`\n"
                "2. Inicie novamente com `python main.py`\n"
                "3. Digite `!sync` no Discord\n"
                "4. Recarregue o Discord com `Ctrl + R` se ainda aparecer comando antigo"
            )
        )

    except Exception as e:
        logging.exception("[LIMPAR SLASH ERROR] %r", e)
        await msg.edit(content=f"❌ Erro ao limpar comandos slash: `{e}`")


# ==================================================
# COMANDO SLASH: /ping
# ==================================================
@bot.tree.command(name="ping", description="Verificar se o bot está respondendo")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(
        f"🏓 Pong! Latência: {latency}ms",
        ephemeral=True,
    )


# ==================================================
# MAIN
# ==================================================
async def main():
    token = os.getenv("TOKEN")

    if not token:
        raise RuntimeError("TOKEN não encontrado em variável de ambiente (.env).")

    retry_count = 0
    max_retries = 5

    while True:
        try:
            async with bot:
                await bot.start(token)

        except (
            aiohttp.ClientError,
            discord.GatewayNotFound,
            asyncio.TimeoutError,
            discord.ConnectionClosed,
        ) as e:
            retry_count += 1

            if retry_count > max_retries:
                logging.error("Número máximo de tentativas de reconexão atingido. Encerrando.")
                break

            wait = min(30 * retry_count, 300)
            logging.error("Erro de conexão: %s. Tentando novamente em %s segundos...", e, wait)
            await asyncio.sleep(wait)

        except Exception as e:
            logging.exception("Erro fatal: %s", e)
            break


# ==================================================
# START / CLEANUP
# ==================================================
if __name__ == "__main__":
    try:
        asyncio.run(main())

    finally:
        if lock_file:
            try:
                portalocker.unlock(lock_file)
                lock_file.close()

                lock_path = BASE_DIR / "bot.lock"
                if lock_path.exists():
                    lock_path.unlink()

            except Exception:
                pass