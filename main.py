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
# Tenta ajustar o encoding do stdout de forma compatível
# com ambientes que não expõem `reconfigure()` (IDEs, pipes, etc.).
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    else:
        import io

        try:
            # Recria um wrapper de texto usando o buffer subjacente, quando disponível.
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
        except Exception:
            # Alguns ambientes não expõem `.buffer` — ignora nesse caso.
            pass
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
            "cogs.solicitacao_armas",
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

        if isinstance(exception, commands.CommandOnCooldown):
            try:
                await context.send(
                    f"⏳ Aguarde **{exception.retry_after:.0f}s** antes de usar "
                    f"`!{context.command}` novamente."
                )
            except Exception:
                pass
            return

        logging.error(
            "[CMD ERROR] %s -> %r",
            getattr(context.command, "qualified_name", "unknown"),
            exception,
            exc_info=(type(exception), exception, exception.__traceback__),
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
        logging.error(
            "[SLASH ERROR] %r",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )

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


async def _edit_or_send_command_result(
    ctx: commands.Context,
    message: discord.Message,
    content: str,
) -> None:
    """Edita a mensagem de progresso ou envia outra caso ela não exista mais."""
    try:
        await message.edit(content=content)
    except discord.NotFound:
        # A mensagem pode ter sido apagada manualmente, por AutoMod ou outro bot.
        try:
            await ctx.send(content)
        except discord.HTTPException:
            logging.exception("Não foi possível enviar o resultado do comando.")
    except discord.HTTPException:
        logging.exception("Não foi possível editar a mensagem de progresso.")
        try:
            await ctx.send(content)
        except discord.HTTPException:
            pass


# ==================================================
# COMANDO PREFIXO: !sync
# ==================================================
@bot.command(
    name="sync",
    description="[ADMIN] Sincroniza comandos slash. Use: !sync (global), !sync ~ (local), !sync * (copia global pro local), !sync ^ (limpa local)"
)
@commands.has_permissions(administrator=True)
@commands.cooldown(1, 300, commands.BucketType.guild)  # 5 min de cooldown — o Discord limita ~2 syncs/10min por guild
async def sync_cmds(
    ctx: commands.Context, guilds: commands.Greedy[discord.Object], spec: str | None = None
) -> None:
    if ctx.guild is None:
        return await ctx.send("❌ Use este comando dentro de um servidor.")

    msg = await ctx.send("🔄 Sincronizando comandos slash...")

    try:
        if not guilds:
            if spec == "~":
                # Sync apenas neste servidor — instantâneo, ideal para testes
                synced = await bot.tree.sync(guild=ctx.guild)
            elif spec == "*":
                # Copia globais para o servidor atual e sincroniza — ainda 1 chamada só
                bot.tree.copy_global_to(guild=ctx.guild)
                synced = await bot.tree.sync(guild=ctx.guild)
            elif spec == "^":
                # Limpa comandos presos neste servidor (resolve duplicidade)
                bot.tree.clear_commands(guild=ctx.guild)
                await bot.tree.sync(guild=ctx.guild)
                synced = []
            else:
                # Sync global — pode levar até 1h para aparecer para todos os usuários
                synced = await bot.tree.sync()

            target = "globalmente" if spec is None else "neste servidor"
            await _edit_or_send_command_result(
                ctx, msg,
                f"✅ Sincronização concluída!\n"
                f"**{len(synced)}** comandos atualizados {target}.\n"
                f"_(Dica: use `!sync ~` para testar comandos novos instantaneamente)_"
            )
            return

        ret = 0
        for guild in guilds:
            try:
                await bot.tree.sync(guild=guild)
            except discord.HTTPException:
                pass
            else:
                ret += 1

        await _edit_or_send_command_result(
            ctx, msg, f"✅ Árvore de comandos sincronizada em {ret}/{len(guilds)} servidores."
        )

    except discord.HTTPException as error:
        # Erro 429 = rate limit do Discord no endpoint de comandos
        if error.status == 429:
            wait = getattr(error, 'retry_after', 300)
            await _edit_or_send_command_result(
                ctx, msg,
                f"⏳ **Rate limit do Discord (429).**\n"
                f"O Discord limita sincronizações de comandos. Aguarde **{wait:.0f}s** e tente novamente.\n"
                f"_Dica: evite usar `!sync` várias vezes seguidas. Prefira `!sync ~` para testar._"
            )
        else:
            logging.exception("[SYNC HTTP ERROR] %r", error)
            await _edit_or_send_command_result(
                ctx, msg, f"❌ O Discord recusou a sincronização (HTTP {error.status}). Veja o `bot.log`."
            )
    except Exception as error:
        logging.exception("[SYNC ERROR] %r", error)
        await _edit_or_send_command_result(ctx, msg, f"❌ Erro ao sincronizar: `{error}`")

# ==================================================
# COMANDO PREFIXO: !limparslash
# ==================================================
@bot.command(
    name="limparslash",
    description="[ADMIN] Remove comandos slash globais (use !sync ^ para limpar local)"
)
@commands.has_permissions(administrator=True)
@commands.cooldown(1, 600, commands.BucketType.guild)  # 10 min — essa operação consome 2 chamadas à API
async def limpar_slash(ctx: commands.Context):
    if ctx.guild is None:
        return await ctx.send("❌ Use este comando dentro de um servidor.")

    msg = await ctx.send("🧹 Limpando comandos slash globais...")

    try:
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync(guild=None)

        await _edit_or_send_command_result(
            ctx,
            msg,
            (
                "✅ Comandos slash globais removidos.\n\n"
                "Agora use `!sync ~` para registrar os comandos novamente neste servidor\n"
                "ou `!sync ^` para limpar comandos presos APENAS neste servidor."
            ),
        )

    except discord.HTTPException as e:
        if e.status == 429:
            await _edit_or_send_command_result(
                ctx, msg,
                f"⏳ **Rate limit do Discord (429).** Aguarde alguns minutos e tente novamente."
            )
        else:
            logging.exception("[LIMPAR SLASH ERROR] %r", e)
            await _edit_or_send_command_result(ctx, msg, f"❌ Erro HTTP {e.status}: `{e}`")
    except Exception as e:
        logging.exception("[LIMPAR SLASH ERROR] %r", e)
        await _edit_or_send_command_result(
            ctx,
            msg,
            f"❌ Erro ao limpar comandos slash: `{e}`",
        )


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