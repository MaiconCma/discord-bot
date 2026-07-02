from __future__ import annotations

import asyncio
from datetime import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from repositories.anime_repository import AnimeRepository
from repositories.database import init_db
from services.anilist_service import AniListError, AniListService
from utils.dates import DIAS_VALIDOS, format_timestamp, get_timezone, today_weekday_pt, weekday_from_timestamp
from utils.permissions import has_anime_permission
from utils.text import safe_join

DAY_CHOICES = [app_commands.Choice(name=dia, value=dia) for dia in DIAS_VALIDOS]
STATUS_CHOICES = [
    app_commands.Choice(name="ativo", value="ativo"),
    app_commands.Choice(name="acabou", value="acabou"),
    app_commands.Choice(name="hiato", value="hiato"),
]
COMMON_GENRES = [
    "Action", "Adventure", "Comedy", "Drama", "Fantasy", "Romance", "Sci-Fi", "Slice of Life",
    "Sports", "Supernatural", "Mystery", "Psychological", "Thriller", "Horror", "Music",
]
GENRE_CHOICES = [app_commands.Choice(name=g, value=g) for g in COMMON_GENRES]


async def ensure_anime_channels(guild: discord.Guild, repo: AnimeRepository) -> tuple[discord.TextChannel, discord.TextChannel]:
    cat_name = getattr(config, "ANIME_CATEGORY_NAME", "📺 - ANIMES")
    feed_name = getattr(config, "ANIME_FEED_CHANNEL_NAME", "🍱-alimentacao-animes")
    output_name = getattr(config, "ANIME_OUTPUT_CHANNEL_NAME", "📢-lembretes-animes")

    category = discord.utils.get(guild.categories, name=cat_name)
    if category is None:
        category = await guild.create_category(cat_name)

    feed_ch = discord.utils.get(category.text_channels, name=feed_name)
    if feed_ch is None:
        feed_ch = await guild.create_text_channel(feed_name, category=category)

    output_ch = discord.utils.get(category.text_channels, name=output_name)
    if output_ch is None:
        output_ch = await guild.create_text_channel(output_name, category=category)

    repo.upsert_guild_channels(guild.id, category.id, feed_ch.id, output_ch.id)
    return feed_ch, output_ch


def anime_embed(anime: dict[str, Any], title_prefix: str = "📺") -> discord.Embed:
    score = anime.get("averageScore") or "N/A"
    popularity = anime.get("popularity") or "N/A"
    genres = safe_join(anime.get("genres"))
    next_ep = anime.get("nextEpisode") or "N/A"
    next_air = format_timestamp(anime.get("nextAiringAt"))

    embed = discord.Embed(
        title=f"{title_prefix} {anime['title']}",
        description=anime.get("description") or "Sem descrição disponível.",
        url=anime.get("siteUrl"),
        color=discord.Color.purple(),
    )
    if anime.get("coverImage"):
        embed.set_thumbnail(url=anime["coverImage"])

    embed.add_field(name="Gêneros", value=genres, inline=False)
    embed.add_field(name="Nota", value=str(score), inline=True)
    embed.add_field(name="Popularidade", value=str(popularity), inline=True)
    embed.add_field(name="Próximo episódio", value=f"Ep. {next_ep}\n{next_air}", inline=True)
    return embed


class RecommendationView(discord.ui.View):
    def __init__(self, cog: "AnimeReminderSystemV2", anime: dict[str, Any], guild_id: int, user_id: int | None):
        super().__init__(timeout=180)
        self.cog = cog
        self.anime = anime
        self.guild_id = guild_id
        self.user_id = user_id

    @discord.ui.button(label="✅ Seguir", style=discord.ButtonStyle.success)
    async def follow_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not has_anime_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)

        dia = weekday_from_timestamp(self.anime.get("nextAiringAt"))
        created, _ = await asyncio.to_thread(
            self.cog.repo.add_anilist_anime,
            self.guild_id,
            self.anime,
            dia,
            interaction.user.id,
        )
        if created:
            await interaction.response.send_message(
                f"✅ **{self.anime['title']}** foi adicionado aos animes seguidos.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ **{self.anime['title']}** já estava cadastrado. Atualizei os dados externos.",
                ephemeral=True,
            )

    @discord.ui.button(label="🚫 Ignorar", style=discord.ButtonStyle.danger)
    async def ignore_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not has_anime_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)

        await asyncio.to_thread(
            self.cog.repo.ignore_anime,
            self.guild_id,
            int(self.anime["id"]),
            self.anime["title"],
            interaction.user.id,
        )
        await interaction.response.send_message(
            f"🚫 **{self.anime['title']}** não será mais recomendado para este servidor.",
            ephemeral=True,
        )


class AnimeReminderView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ Adicionar Anime", style=discord.ButtonStyle.success, custom_id="anime_v2:add_hint")
    async def add_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not has_anime_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await interaction.response.send_message(
            "Use `/anime_adicionar` para cadastrar manualmente ou `/seguir` para buscar no AniList e acompanhar.",
            ephemeral=True,
        )

    @discord.ui.button(label="🔥 Radar", style=discord.ButtonStyle.primary, custom_id="anime_v2:radar_hint")
    async def radar_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "Use `/radar` para ver animes em alta ou `/recomendar` para receber uma sugestão com botão de seguir.",
            ephemeral=True,
        )

    @discord.ui.button(label="📋 Listar", style=discord.ButtonStyle.secondary, custom_id="anime_v2:list_hint")
    async def list_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Use `/listaranimes` para ver os animes cadastrados.", ephemeral=True)


class AnimeReminderSystemV2(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.repo = AnimeRepository()
        self.anilist = AniListService()
        init_db()
        bot.add_view(AnimeReminderView())

        if bot.is_ready():
            self._start_loops()
        else:
            bot.loop.create_task(self._start_when_ready())

    async def _start_when_ready(self):
        await self.bot.wait_until_ready()
        self._start_loops()

    def _start_loops(self) -> None:
        if not self.lembrete_diario.is_running():
            self.lembrete_diario.start()
        if not self.alertas_anilist.is_running():
            self.alertas_anilist.start()

    async def _get_output_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        settings = await asyncio.to_thread(self.repo.get_guild_settings, guild.id)
        if settings and settings.get("output_channel_id"):
            channel = guild.get_channel(int(settings["output_channel_id"]))
            if isinstance(channel, discord.TextChannel):
                return channel

        _, output_ch = await ensure_anime_channels(guild, self.repo)
        return output_ch

    @tasks.loop(time=time(
        hour=getattr(config, "ANIME_REMINDER_HOUR", 12),
        minute=getattr(config, "ANIME_REMINDER_MINUTE", 0),
        tzinfo=get_timezone(),
    ))
    async def lembrete_diario(self):
        await self.bot.wait_until_ready()
        hoje = today_weekday_pt()

        for guild in self.bot.guilds:
            animes = await asyncio.to_thread(self.repo.get_animes_by_day, guild.id, hoje)
            if not animes:
                continue

            embed = discord.Embed(
                title=f"📺 Lembretes de hoje — {hoje.capitalize()}",
                description="Animes cadastrados para hoje:",
                color=discord.Color.purple(),
            )
            for anime in animes[:20]:
                link = anime.get("link") or anime.get("siteUrl") or "Sem link"
                embed.add_field(
                    name=f"#{anime['id']} — {anime['nome']}",
                    value=f"Status: **{anime['status']}**\nLink: {link}",
                    inline=False,
                )

            canal = await self._get_output_channel(guild)
            if canal:
                await canal.send(embed=embed)

    @tasks.loop(minutes=60)
    async def alertas_anilist(self):
        """Verifica animes seguidos via AniList e avisa quando houver próximo episódio registrado."""
        await self.bot.wait_until_ready()

        for guild in self.bot.guilds:
            canal = await self._get_output_channel(guild)
            if not canal:
                continue

            tracked = await asyncio.to_thread(self.repo.get_active_anilist_animes, guild.id)
            for local_anime in tracked:
                external_id = local_anime.get("external_id")
                if not external_id:
                    continue

                try:
                    anime = await self.anilist.get_anime_by_id(int(external_id))
                except Exception:
                    continue
                if not anime:
                    continue

                await asyncio.to_thread(
                    self.repo.update_anilist_airing_data,
                    guild.id,
                    int(local_anime["id"]),
                    anime.get("nextEpisode"),
                    anime.get("nextAiringAt"),
                )

                next_ep = anime.get("nextEpisode")
                next_airing = anime.get("nextAiringAt")
                if not next_ep or not next_airing:
                    continue

                # Alerta informativo do próximo episódio, sem repetir.
                unique_key = f"anilist-next:{guild.id}:{external_id}:{next_ep}:{next_airing}"
                created = await asyncio.to_thread(
                    self.repo.create_alert_if_not_exists,
                    guild.id,
                    int(local_anime["id"]),
                    int(external_id),
                    "anilist_next_episode",
                    unique_key,
                )
                if not created:
                    continue

                embed = anime_embed(anime, title_prefix="⏰ Próximo episódio")
                await canal.send(embed=embed)

    @lembrete_diario.before_loop
    @alertas_anilist.before_loop
    async def before_loops(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="painelanime", description="Cria o painel e os canais do sistema de animes")
    async def painelanime(self, interaction: discord.Interaction):
        if not has_anime_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão para criar o painel.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("❌ Este comando só funciona em servidores.", ephemeral=True)

        feed_ch, output_ch = await ensure_anime_channels(interaction.guild, self.repo)
        embed = discord.Embed(
            title="📺 Radar de Animes",
            description=(
                "Gerencie lembretes manuais e recomendações automáticas.\n\n"
                f"📥 Canal de alimentação: {feed_ch.mention}\n"
                f"📢 Canal de alertas: {output_ch.mention}\n\n"
                "Comandos principais:\n"
                "`/anime_adicionar`, `/seguir`, `/radar`, `/recomendar`, `/listaranimes`, `/removeranime`."
            ),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed, view=AnimeReminderView())

    @app_commands.command(name="anime_adicionar", description="Adiciona um anime manualmente ao lembrete")
    @app_commands.describe(nome="Nome do anime", dia_semana="Dia de lançamento", link="Link opcional")
    @app_commands.choices(dia_semana=DAY_CHOICES)
    async def anime_adicionar(
        self,
        interaction: discord.Interaction,
        nome: str,
        dia_semana: app_commands.Choice[str],
        link: str | None = None,
    ):
        if not has_anime_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("❌ Este comando só funciona em servidores.", ephemeral=True)

        created, anime_id = await asyncio.to_thread(
            self.repo.add_manual_anime,
            interaction.guild.id,
            nome,
            dia_semana.value,
            link,
            interaction.user.id,
        )
        if not created:
            return await interaction.response.send_message(
                "⚠️ Este anime já existe neste servidor ou ocorreu erro ao cadastrar.",
                ephemeral=True,
            )
        await interaction.response.send_message(
            f"✅ Anime **{nome}** cadastrado com ID **{anime_id}** para **{dia_semana.value}**.",
            ephemeral=True,
        )

    @app_commands.command(name="listaranimes", description="Lista os animes cadastrados neste servidor")
    async def listaranimes(self, interaction: discord.Interaction):
        if not has_anime_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("❌ Este comando só funciona em servidores.", ephemeral=True)

        animes = await asyncio.to_thread(self.repo.list_animes, interaction.guild.id, 25, 0)
        if not animes:
            return await interaction.response.send_message("📭 Nenhum anime cadastrado neste servidor.", ephemeral=True)

        embed = discord.Embed(title="📋 Animes cadastrados", color=discord.Color.purple())
        for anime in animes:
            status_emoji = {"ativo": "🟢", "acabou": "🔴", "hiato": "🟡"}.get(anime["status"], "⚪")
            genres = safe_join(anime.get("generos"), fallback="Manual")
            embed.add_field(
                name=f"{status_emoji} #{anime['id']} — {anime['nome']}",
                value=(
                    f"Dia: **{anime.get('dia_semana') or 'não definido'}**\n"
                    f"Fonte: **{anime.get('fonte')}**\n"
                    f"Gêneros: {genres}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="removeranime", description="Remove um anime pelo ID")
    @app_commands.describe(anime_id="ID exibido no /listaranimes")
    async def removeranime(self, interaction: discord.Interaction, anime_id: int):
        if not has_anime_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("❌ Este comando só funciona em servidores.", ephemeral=True)

        deleted = await asyncio.to_thread(self.repo.delete_anime, interaction.guild.id, anime_id)
        if deleted:
            await interaction.response.send_message(f"🗑️ Anime ID **{anime_id}** removido.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Anime ID **{anime_id}** não encontrado.", ephemeral=True)

    @app_commands.command(name="anime_status", description="Altera o status de um anime pelo ID")
    @app_commands.describe(anime_id="ID exibido no /listaranimes", status="Novo status")
    @app_commands.choices(status=STATUS_CHOICES)
    async def anime_status(self, interaction: discord.Interaction, anime_id: int, status: app_commands.Choice[str]):
        if not has_anime_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("❌ Este comando só funciona em servidores.", ephemeral=True)

        updated = await asyncio.to_thread(self.repo.update_anime_status, interaction.guild.id, anime_id, status.value)
        if updated:
            await interaction.response.send_message(f"✅ Anime ID **{anime_id}** agora está **{status.value}**.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Anime ID **{anime_id}** não encontrado.", ephemeral=True)

    @app_commands.command(name="radar", description="Mostra animes em alta/em lançamento pelo AniList")
    @app_commands.describe(genero="Gênero opcional", quantidade="Quantidade de resultados, até 10")
    @app_commands.choices(genero=GENRE_CHOICES)
    async def radar(
        self,
        interaction: discord.Interaction,
        genero: app_commands.Choice[str] | None = None,
        quantidade: app_commands.Range[int, 1, 10] = 5,
    ):
        await interaction.response.defer(ephemeral=False)
        genre_value = genero.value if genero else None

        try:
            animes = await self.anilist.get_season_radar(genre=genre_value, per_page=int(quantidade))
        except AniListError as exc:
            return await interaction.followup.send(f"❌ Erro ao consultar AniList: `{exc}`", ephemeral=True)

        if not animes:
            return await interaction.followup.send("📭 Nenhum anime encontrado para esse filtro.", ephemeral=True)

        embed = discord.Embed(
            title="🔥 Radar da Temporada" + (f" — {genre_value}" if genre_value else ""),
            color=discord.Color.orange(),
        )
        for idx, anime in enumerate(animes, start=1):
            next_info = f"Ep. {anime.get('nextEpisode') or 'N/A'} — {format_timestamp(anime.get('nextAiringAt'))}"
            embed.add_field(
                name=f"{idx}. {anime['title']}",
                value=(
                    f"Nota: **{anime.get('averageScore') or 'N/A'}** | Popularidade: **{anime.get('popularity') or 'N/A'}**\n"
                    f"Gêneros: {safe_join(anime.get('genres'))}\n"
                    f"Próximo: {next_info}\n"
                    f"[Ver no AniList]({anime.get('siteUrl')})"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="recomendar", description="Recomenda um anime com botão para seguir ou ignorar")
    @app_commands.describe(genero="Gênero opcional")
    @app_commands.choices(genero=GENRE_CHOICES)
    async def recomendar(self, interaction: discord.Interaction, genero: app_commands.Choice[str] | None = None):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Este comando só funciona em servidores.", ephemeral=True)

        await interaction.response.defer(ephemeral=False)
        ignored_ids = await asyncio.to_thread(self.repo.get_ignored_external_ids, interaction.guild.id)
        genre_value = genero.value if genero else None

        try:
            anime = await self.anilist.recommend(genre=genre_value, ignored_ids=ignored_ids)
        except AniListError as exc:
            return await interaction.followup.send(f"❌ Erro ao consultar AniList: `{exc}`", ephemeral=True)

        if not anime:
            return await interaction.followup.send("📭 Não encontrei uma recomendação boa com esse filtro.", ephemeral=True)

        embed = anime_embed(anime, title_prefix="✨ Recomendação")
        view = RecommendationView(self, anime, interaction.guild.id, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="seguir", description="Busca um anime no AniList e adiciona aos seguidos")
    @app_commands.describe(nome="Nome do anime", dia_semana="Opcional: força um dia manual para o lembrete")
    @app_commands.choices(dia_semana=DAY_CHOICES)
    async def seguir(
        self,
        interaction: discord.Interaction,
        nome: str,
        dia_semana: app_commands.Choice[str] | None = None,
    ):
        if not has_anime_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("❌ Este comando só funciona em servidores.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            results = await self.anilist.search_anime(nome, per_page=1)
        except AniListError as exc:
            return await interaction.followup.send(f"❌ Erro ao consultar AniList: `{exc}`", ephemeral=True)

        if not results:
            return await interaction.followup.send("📭 Anime não encontrado no AniList.", ephemeral=True)

        anime = results[0]
        dia = dia_semana.value if dia_semana else weekday_from_timestamp(anime.get("nextAiringAt"))
        created, _ = await asyncio.to_thread(
            self.repo.add_anilist_anime,
            interaction.guild.id,
            anime,
            dia,
            interaction.user.id,
        )
        msg = "adicionado" if created else "atualizado"
        await interaction.followup.send(
            f"✅ **{anime['title']}** foi {msg}. Dia de lembrete: **{dia or 'não definido'}**.",
            ephemeral=True,
        )

    @app_commands.command(name="ignorar", description="Ignora um anime nas próximas recomendações")
    @app_commands.describe(nome="Nome do anime")
    async def ignorar(self, interaction: discord.Interaction, nome: str):
        if not has_anime_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("❌ Este comando só funciona em servidores.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        results = await self.anilist.search_anime(nome, per_page=1)
        if not results:
            return await interaction.followup.send("📭 Anime não encontrado no AniList.", ephemeral=True)
        anime = results[0]
        await asyncio.to_thread(self.repo.ignore_anime, interaction.guild.id, int(anime["id"]), anime["title"], interaction.user.id)
        await interaction.followup.send(f"🚫 **{anime['title']}** foi ignorado nas recomendações.", ephemeral=True)

    @app_commands.command(name="preferencia", description="Define peso de preferência de gênero para o servidor")
    @app_commands.describe(genero="Gênero", peso="Peso de 1 a 5")
    @app_commands.choices(genero=GENRE_CHOICES)
    async def preferencia(
        self,
        interaction: discord.Interaction,
        genero: app_commands.Choice[str],
        peso: app_commands.Range[int, 1, 5],
    ):
        if not has_anime_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("❌ Este comando só funciona em servidores.", ephemeral=True)

        await asyncio.to_thread(self.repo.set_preference, interaction.guild.id, genero.value, int(peso))
        await interaction.response.send_message(
            f"✅ Preferência salva: **{genero.value}** com peso **{peso}**.",
            ephemeral=True,
        )

    @app_commands.command(name="preferencias", description="Lista as preferências de gênero do servidor")
    async def preferencias(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Este comando só funciona em servidores.", ephemeral=True)

        prefs = await asyncio.to_thread(self.repo.list_preferences, interaction.guild.id)
        if not prefs:
            return await interaction.response.send_message("📭 Nenhuma preferência cadastrada.", ephemeral=True)

        msg = "\n".join(f"• **{p['genre']}** — peso {p['weight']}" for p in prefs)
        await interaction.response.send_message(f"🎯 Preferências do servidor:\n{msg}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AnimeReminderSystemV2(bot))
