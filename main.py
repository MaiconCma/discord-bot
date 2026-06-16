import os
import sys
import asyncio
import logging
import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

import config
import portalocker

# ========== LOCK PARA EVITAR DUPLICIDADE ==========
def acquire_lock():
    try:
        lock_file = open("bot.lock", "w")
        portalocker.lock(lock_file, portalocker.LOCK_EX | portalocker.LOCK_NB)
        return lock_file
    except portalocker.LockException:
        return None

lock_file = acquire_lock()
if lock_file is None:
    print("Bot já está rodando. Saindo.")
    sys.exit(0)
# ==================================================

load_dotenv(override=True)

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
    intents.members = True
    intents.message_content = True
    return intents

class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=_build_intents())

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

        # NOTA DO ENGENHEIRO:
        # Removi a sincronização automática de comandos (self.tree.sync) do boot.
        # Fazer isso em todo boot causa Rate Limit severo no Discord e lentidão.
        # Sempre que adicionar um comando novo no código, reinicie o bot e digite o comando !sync no Discord.
        logging.info("[INFO] Setup Hook finalizado. Use o comando de sync in-game se tiver novos comandos.")

    async def on_command_error(self, context: commands.Context, exception: commands.CommandError):
        logging.exception("[CMD ERROR] %s -> %r", getattr(context.command, "qualified_name", "unknown"), exception)
        try:
            await context.send("❌ Deu erro ao executar o comando. (veja o console / bot.log)")
        except Exception:
            pass

    async def on_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
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

# ========== COMANDO DE SINC FORÇADA (ADMIN) ==========
# Agora transformado em comando de prefixo (!sync) para funcionar imediatamente
# mesmo que os comandos slash antigos não estejam sincronizados.
@bot.command(name="sync", description="[ADMIN] Sincroniza comandos slash com o servidor atual")
@commands.has_permissions(administrator=True)
async def sync_cmds(ctx: commands.Context):
    msg = await ctx.send("🔄 Iniciando sincronização da árvore de comandos...")
    try:
        # Sincroniza apenas para o servidor atual para ser instantâneo
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await msg.edit(content=f"✅ Sincronização concluída! **{len(synced)}** comandos slash foram atualizados neste servidor.")
    except Exception as e:
        await msg.edit(content=f"❌ Erro ao sincronizar: {e}")
# =====================================================

@bot.tree.command(name="ping", description="Verificar se o bot está respondendo")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! Latência: {latency}ms", ephemeral=True)

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
        except (aiohttp.ClientError, discord.GatewayNotFound, asyncio.TimeoutError, discord.ConnectionClosed) as e:
            retry_count += 1
            if retry_count > max_retries:
                logging.error("Número máximo de tentativas de reconexão atingido. Encerrando.")
                break
            wait = min(30 * retry_count, 300)
            logging.error(f"Erro de conexão: {e}. Tentando novamente em {wait} segundos...")
            await asyncio.sleep(wait)
        except Exception as e:
            logging.exception(f"Erro fatal: {e}")
            break

if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        if lock_file:
            try:
                portalocker.unlock(lock_file)
                lock_file.close()
                if os.path.exists("bot.lock"):
                    os.remove("bot.lock")
            except Exception:
                pass