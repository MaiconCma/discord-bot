"""
cogs/dev_info.py - Sistema de informações para devs.

Melhorias aplicadas:
  - Sessão aiohttp compartilhada (sem abrir/fechar por requisição)
  - Cache em memória dos configs por guild (evita I/O excessivo)
  - Operações de config atômicas (_check_and_mark substitui _is_new + _mark_posted)
  - Busca paralela com asyncio.gather
  - Cooldown nos comandos slash
  - Embeds com timestamp e footer padronizados
  - Truncamento seguro de campos (limite Discord = 1024 chars)
  - View persistente com botões (sobrevive a reinícios do bot)
  - Handler de erros de cooldown amigável
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from datastore import AsyncJsonStore

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────
REMOTIVE_API      = "https://remotive.com/api/remote-jobs"
DEVTO_API         = "https://dev.to/api/articles"
DEV_EVENTS_URL    = "https://dev.events/"

MAX_ITEMS_EMBED   = 5
MAX_POSTED_IDS    = 500
TRIM_POSTED_TO    = 300
EMBED_FIELD_LIMIT = 1000    # margem segura abaixo do limite de 1024 chars

STORE_PATH = "data/dev_info.json"

DEFAULT_GUILD_CONFIG: Dict[str, Any] = {
    "announce_channel_id":   None,
    "posted_job_ids":        [],
    "posted_event_ids":      [],
    "posted_article_ids":    [],
    "category_name":         config.DEV_CATEGORY_NAME,
    "feed_channel_name":     config.DEV_FEED_CHANNEL,
    "announce_channel_name": config.DEV_ANNOUNCE_CHANNEL,
}

# ─────────────────────────────────────────────
# Store + cache em memória
# ─────────────────────────────────────────────
_store: AsyncJsonStore = AsyncJsonStore(STORE_PATH)
_config_cache: Dict[str, Dict[str, Any]] = {}


async def _get_guild_config(guild_id: int) -> Dict[str, Any]:
    key = str(guild_id)
    if key in _config_cache:
        return _config_cache[key]
    data = await _store.read()
    if key not in data:
        data[key] = dict(DEFAULT_GUILD_CONFIG)
        await _store.write(data)
    _config_cache[key] = data[key]
    return _config_cache[key]


async def _save_guild_config(guild_id: int, cfg: Dict[str, Any]) -> None:
    key = str(guild_id)
    _config_cache[key] = cfg
    data = await _store.read()
    data[key] = cfg
    await _store.write(data)


async def _check_and_mark(
    guild_id: int, item_type: str, item_ids: List[str]
) -> List[str]:
    """
    Operação atômica: verifica quais IDs são novos, marca todos e retorna apenas os novos.
    Substitui o antigo par _is_new + _mark_posted (que faziam 2 leituras separadas).
    """
    key = str(guild_id)
    posted_key = f"posted_{item_type}_ids"

    data = await _store.read()
    cfg = data.setdefault(key, dict(DEFAULT_GUILD_CONFIG))
    posted: List[str] = cfg.get(posted_key, [])

    new_ids = [i for i in item_ids if i not in posted]
    if new_ids:
        posted.extend(new_ids)
        if len(posted) > MAX_POSTED_IDS:
            posted = posted[-TRIM_POSTED_TO:]
        cfg[posted_key] = posted
        _config_cache[key] = cfg
        await _store.write(data)

    return new_ids


# ─────────────────────────────────────────────
# Helpers de embed
# ─────────────────────────────────────────────
def _stamp(embed: discord.Embed) -> discord.Embed:
    embed.timestamp = datetime.now(tz=timezone.utc)
    embed.set_footer(text="🤖 Bot Dev")
    return embed


def _truncate(text: str, limit: int = EMBED_FIELD_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "…"


# ─────────────────────────────────────────────
# Coletores de dados
# ─────────────────────────────────────────────
async def fetch_jobs(
    session: aiohttp.ClientSession,
    category: str = "software-dev",
    limit: int = MAX_ITEMS_EMBED,
) -> List[Dict]:
    try:
        async with session.get(
            REMOTIVE_API,
            params={"category": category, "limit": limit},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                return (await resp.json()).get("jobs", [])
            log.warning("Remotive retornou status %s", resp.status)
    except Exception as e:
        log.error("Erro ao buscar vagas: %s", e)
    return []


async def fetch_devto_articles(
    session: aiohttp.ClientSession,
    tag: str = "programming",
    limit: int = MAX_ITEMS_EMBED,
) -> List[Dict]:
    try:
        async with session.get(
            DEVTO_API,
            params={"tag": tag, "per_page": limit},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            log.warning("DEV.to retornou status %s", resp.status)
    except Exception as e:
        log.error("Erro ao buscar artigos: %s", e)
    return []


async def fetch_events(
    session: aiohttp.ClientSession,
    limit: int = MAX_ITEMS_EMBED,
) -> List[Dict]:
    try:
        async with session.get(DEV_EVENTS_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        events: List[Dict] = []
        for card in soup.select(".event-card")[:limit]:
            title_el = card.select_one(".event-title")
            link_el  = card.select_one("a")
            date_el  = card.select_one(".event-date")
            events.append({
                "title": title_el.text.strip() if title_el else "Sem título",
                "url":   link_el["href"]        if link_el  else DEV_EVENTS_URL,
                "date":  date_el.text.strip()   if date_el  else "Data não informada",
            })
        return events
    except Exception as e:
        log.error("Erro ao buscar eventos: %s", e)
    return []


async def _gather_all(session: aiohttp.ClientSession) -> Tuple[List, List, List]:
    """Busca vagas, eventos e artigos em paralelo."""
    return await asyncio.gather(
        fetch_jobs(session),
        fetch_events(session),
        fetch_devto_articles(session),
    )


# ─────────────────────────────────────────────
# Builders de embed
# ─────────────────────────────────────────────
def build_jobs_embed(jobs: List[Dict]) -> discord.Embed:
    embed = discord.Embed(
        title="💼 Vagas Recentes em Tecnologia",
        color=discord.Color.blue(),
        description="Vagas remotas para devs:" if jobs else "Nenhuma vaga encontrada. Tente outra stack!",
    )
    for job in jobs[:MAX_ITEMS_EMBED]:
        embed.add_field(
            name=job["title"][:256],
            value=_truncate(f"🏢 [{job['company_name']}]({job['url']})"),
            inline=False,
        )
    return _stamp(embed)


def build_events_embed(events: List[Dict]) -> discord.Embed:
    embed = discord.Embed(
        title="📅 Próximos Eventos de Tecnologia",
        color=discord.Color.green(),
        description="Próximos eventos da comunidade:" if events else "Nenhum evento encontrado.",
    )
    for ev in events[:MAX_ITEMS_EMBED]:
        embed.add_field(
            name=ev["title"][:256],
            value=_truncate(f"📆 {ev['date']}\n🔗 [Ver evento]({ev['url']})"),
            inline=False,
        )
    return _stamp(embed)


def build_articles_embed(articles: List[Dict]) -> discord.Embed:
    embed = discord.Embed(
        title="📚 Artigos em Destaque no DEV.to",
        color=discord.Color.purple(),
        description="Os artigos mais relevantes de hoje:" if articles else "Nenhum artigo encontrado.",
    )
    for art in articles[:MAX_ITEMS_EMBED]:
        author = art.get("user", {}).get("name", "Desconhecido")
        embed.add_field(
            name=art["title"][:256],
            value=_truncate(f"✍️ {author}\n🔗 [Ler artigo]({art['url']})"),
            inline=False,
        )
    return _stamp(embed)


def build_daily_summary_embed(jobs: List, events: List, articles: List) -> discord.Embed:
    embed = discord.Embed(
        title="📢 Resumo Diário da Comunidade Dev",
        color=discord.Color.gold(),
        description="As melhores oportunidades e conteúdos de hoje!",
    )
    job_lines = [f"• [{j['title']} — {j['company_name']}]({j['url']})" for j in jobs[:3]] or ["Nenhuma vaga nova hoje."]
    embed.add_field(name="💼 Vagas", value=_truncate("\n".join(job_lines)), inline=False)
    if events:
        embed.add_field(name="📅 Eventos", value=_truncate("\n".join(f"• [{e['title']}]({e['url']}) — {e['date']}" for e in events[:3])), inline=False)
    if articles:
        embed.add_field(name="📚 Artigos", value=_truncate("\n".join(f"• [{a['title']}]({a['url']})" for a in articles[:3])), inline=False)
    return _stamp(embed)


# ─────────────────────────────────────────────
# View persistente
# ─────────────────────────────────────────────
class _DevPanelView(discord.ui.View):
    """timeout=None garante que os botões funcionem mesmo após reinício do bot."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    def _cog(self, interaction: discord.Interaction) -> "DevInfoSystem":
        return interaction.client.cogs["DevInfoSystem"]

    @discord.ui.button(label="💼 Vagas", style=discord.ButtonStyle.primary, custom_id="dev:vagas")
    async def btn_vagas(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        jobs = await fetch_jobs(await self._cog(interaction)._get_session())
        await interaction.followup.send(embed=build_jobs_embed(jobs), ephemeral=True)

    @discord.ui.button(label="📅 Eventos", style=discord.ButtonStyle.success, custom_id="dev:eventos")
    async def btn_eventos(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        events = await fetch_events(await self._cog(interaction)._get_session())
        await interaction.followup.send(embed=build_events_embed(events), ephemeral=True)

    @discord.ui.button(label="📚 Artigos", style=discord.ButtonStyle.secondary, custom_id="dev:artigos")
    async def btn_artigos(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        articles = await fetch_devto_articles(await self._cog(interaction)._get_session())
        await interaction.followup.send(embed=build_articles_embed(articles), ephemeral=True)


# ─────────────────────────────────────────────
# Cog principal
# ─────────────────────────────────────────────
class DevInfoSystem(commands.Cog):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self.daily_post.start()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "DevInfoBot/1.0 (discord bot)"}
            )
        return self._session

    def cog_unload(self) -> None:
        self.daily_post.cancel()
        if self._session and not self._session.closed:
            self.bot.loop.create_task(self._session.close())

    async def _ensure_channels(self, guild: discord.Guild) -> Tuple[discord.TextChannel, discord.TextChannel]:
        cfg = await _get_guild_config(guild.id)
        category_name = cfg.get("category_name",         config.DEV_CATEGORY_NAME)
        feed_name     = cfg.get("feed_channel_name",     config.DEV_FEED_CHANNEL)
        announce_name = cfg.get("announce_channel_name", config.DEV_ANNOUNCE_CHANNEL)

        category = discord.utils.get(guild.categories, name=category_name) \
                   or await guild.create_category(category_name)
        feed_ch  = discord.utils.get(category.text_channels, name=feed_name) \
                   or await guild.create_text_channel(feed_name, category=category)

        announce_ch = discord.utils.get(category.text_channels, name=announce_name)
        if not announce_ch:
            announce_ch = await guild.create_text_channel(announce_name, category=category)
            cfg["announce_channel_id"] = announce_ch.id
            await _save_guild_config(guild.id, cfg)

        return feed_ch, announce_ch

    @tasks.loop(time=time(hour=config.DEV_POST_HOUR, minute=config.DEV_POST_MINUTE))
    async def daily_post(self) -> None:
        log.info("Executando postagem diária dev...")
        session = await self._get_session()
        jobs, events, articles = await _gather_all(session)

        for guild in self.bot.guilds:
            cfg = await _get_guild_config(guild.id)
            announce_id = cfg.get("announce_channel_id")
            if not announce_id:
                try:
                    _, ch = await self._ensure_channels(guild)
                    announce_id = ch.id
                except Exception as e:
                    log.error("Erro ao criar canais em %s: %s", guild.name, e)
                    continue

            channel = guild.get_channel(announce_id)
            if not channel:
                continue

            new_job_ids     = await _check_and_mark(guild.id, "job",     [f"job_{j['id']}"  for j in jobs])
            new_event_ids   = await _check_and_mark(guild.id, "event",   [f"ev_{e['url']}"  for e in events])
            new_article_ids = await _check_and_mark(guild.id, "article", [f"art_{a['id']}"  for a in articles])

            new_jobs     = [j for j in jobs     if f"job_{j['id']}"  in new_job_ids]
            new_events   = [e for e in events   if f"ev_{e['url']}"  in new_event_ids]
            new_articles = [a for a in articles if f"art_{a['id']}"  in new_article_ids]

            if not (new_jobs or new_events or new_articles):
                continue

            try:
                await channel.send(embed=build_daily_summary_embed(new_jobs, new_events, new_articles))
                log.info("Resumo diário enviado para %s.", guild.name)
            except discord.Forbidden:
                log.warning("Sem permissão para enviar em #%s (%s).", channel.name, guild.name)
            except Exception as e:
                log.error("Erro ao enviar embed em %s: %s", guild.name, e)

    @daily_post.before_loop
    async def before_daily(self) -> None:
        await self.bot.wait_until_ready()

    # ── Comandos ─────────────────────────────────────────────────────────────
    @app_commands.command(name="vagas", description="Lista vagas recentes de tecnologia")
    @app_commands.describe(stack="Filtrar por tecnologia (ex: python, react, devops)")
    @app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
    async def vagas_cmd(self, interaction: discord.Interaction, stack: str = "software-dev") -> None:
        await interaction.response.defer()
        jobs = await fetch_jobs(await self._get_session(), category=stack)
        await interaction.followup.send(embed=build_jobs_embed(jobs))

    @app_commands.command(name="eventos", description="Próximos eventos de tecnologia")
    @app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
    async def eventos_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        events = await fetch_events(await self._get_session())
        await interaction.followup.send(embed=build_events_embed(events))

    @app_commands.command(name="artigos", description="Artigos recentes do DEV.to")
    @app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
    async def artigos_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        articles = await fetch_devto_articles(await self._get_session())
        await interaction.followup.send(embed=build_articles_embed(articles))

    @app_commands.command(name="configurar_dev", description="Define o canal de anúncios diários (admin)")
    @app_commands.default_permissions(administrator=True)
    async def configurar_dev(self, interaction: discord.Interaction, canal: discord.TextChannel) -> None:
        cfg = await _get_guild_config(interaction.guild_id)
        cfg["announce_channel_id"] = canal.id
        await _save_guild_config(interaction.guild_id, cfg)
        await interaction.response.send_message(
            f"✅ Canal de anúncios dev definido para {canal.mention}", ephemeral=True
        )

    @app_commands.command(name="paineldev", description="Cria o painel de comandos dev (admin)")
    @app_commands.default_permissions(administrator=True)
    async def painel_dev(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("❌ Apenas em servidores.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        _, announce_ch = await self._ensure_channels(interaction.guild)
        embed = discord.Embed(
            title="📰 Sistema de Informações Dev",
            description=(
                "Fique por dentro das novidades da comunidade dev!\n\n"
                "**Comandos disponíveis:**\n"
                "`/vagas [stack]` — Vagas remotas\n"
                "`/eventos` — Próximos eventos\n"
                "`/artigos` — Artigos do DEV.to\n\n"
                f"📢 Resumo diário em {announce_ch.mention} "
                f"às {config.DEV_POST_HOUR:02d}h{config.DEV_POST_MINUTE:02d}m"
            ),
            color=discord.Color.blurple(),
        )
        _stamp(embed)
        await interaction.followup.send(embed=embed, view=_DevPanelView(), ephemeral=False)

    @vagas_cmd.error
    @eventos_cmd.error
    @artigos_cmd.error
    async def _on_cooldown(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Aguarde **{error.retry_after:.0f}s** antes de usar este comando novamente."
        else:
            log.exception("Erro em comando dev: %r", error)
            msg = "❌ Ocorreu um erro inesperado. Tente novamente."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


# ─────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────
async def setup(bot: commands.Bot) -> None:
    bot.add_view(_DevPanelView())  # registra a view persistente ao iniciar
    await bot.add_cog(DevInfoSystem(bot))
    log.info("Cog DevInfoSystem carregado.")