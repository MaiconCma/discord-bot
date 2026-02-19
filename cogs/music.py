import discord
from discord.ext import commands
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import asyncio

import config


class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.music_queue = {}

        auth_manager = SpotifyClientCredentials(
            client_id=config.SPOTIFY_CLIENT_ID,
            client_secret=config.SPOTIFY_CLIENT_SECRET
        )
        self.sp = spotipy.Spotify(auth_manager=auth_manager)

        self.ytdl_format_options = {
            "format": "bestaudio/best",
            "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
            "restrictfilenames": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "ignoreerrors": False,
            "logtostderr": False,
            "quiet": True,
            "no_warnings": True,
            "default_search": "auto",
            "source_address": "0.0.0.0"
        }
        self.ffmpeg_options = {
            "options": "-vn",
            "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
        }
        self.ytdl = yt_dlp.YoutubeDL(self.ytdl_format_options)

    def _play_next(self, ctx: commands.Context):
        """Toca a próxima música da fila (chamado pelo after=...)"""
        guild_id = ctx.guild.id

        if guild_id not in self.music_queue or len(self.music_queue[guild_id]) == 0:
            return

        next_song = self.music_queue[guild_id].pop(0)
        audio_url = next_song["url"]
        title = next_song["title"]

        source = discord.FFmpegPCMAudio(audio_url, **self.ffmpeg_options)

        ctx.voice_client.play(
            source,
            after=lambda e: self._after_play(ctx, e)
        )

        asyncio.run_coroutine_threadsafe(
            ctx.send(f"▶️ Tocando agora: **{title}**"),
            ctx.bot.loop
        )

    def _after_play(self, ctx: commands.Context, error: Exception | None):
        if error:
            print(f"[Music] Erro ao tocar: {error}")

        # Chama a próxima música
        self._play_next(ctx)

    @commands.command(name="play", aliases=["p", "tocar"], help="Toca uma música do Spotify ou YouTube")
    async def play(self, ctx: commands.Context, *, url: str):
        if ctx.guild is None:
            return await ctx.send("❌ Este comando só funciona em servidor.")

        # Verifica se o usuário está em um canal de voz
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("Você precisa estar em um canal de voz para tocar música!")
            return

        voice_channel = ctx.author.voice.channel

        # Conecta ao canal de voz
        if not ctx.voice_client:
            await voice_channel.connect()

        # Inicializa fila
        if ctx.guild.id not in self.music_queue:
            self.music_queue[ctx.guild.id] = []

        search_query = url

        # Spotify track -> converte em busca no YouTube
        if "spotify.com" in url:
            try:
                if "track" in url:
                    track_info = self.sp.track(url)
                    artist = track_info["artists"][0]["name"]
                    song_name = track_info["name"]
                    search_query = f"{song_name} {artist} audio"
                    await ctx.send(f"🎵 Spotify: **{song_name}** - **{artist}**. Buscando no YouTube...")
                else:
                    await ctx.send("No momento, só suportamos link de **track** do Spotify.")
                    return
            except Exception as e:
                await ctx.send("❌ Erro ao ler link do Spotify (confira credenciais no config.py).")
                print(e)
                return

        # Busca no YouTube via yt-dlp
        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(
                None,
                lambda: self.ytdl.extract_info(f"ytsearch:{search_query}", download=False)
            )

            if "entries" in data and data["entries"]:
                data = data["entries"][0]

            audio_url = data["url"]
            title = data["title"]

            self.music_queue[ctx.guild.id].append({"url": audio_url, "title": title})

            # Se não estiver tocando, inicia
            if not ctx.voice_client.is_playing():
                self._play_next(ctx)
            else:
                await ctx.send(f"✅ Adicionado à fila: **{title}**")

        except Exception as e:
            await ctx.send("❌ Ocorreu um erro ao tentar buscar a música.")
            print(e)

    @commands.command(name="skip", aliases=["pular", "passar"], help="Pula a música atual")
    async def skip(self, ctx: commands.Context):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await ctx.send("⏭️ Música pulada!")
        else:
            await ctx.send("⚠️ Não há música tocando no momento.")

    @commands.command(name="stop", aliases=["parar", "sair"], help="Limpa a fila, para a música e desconecta")
    async def stop(self, ctx: commands.Context):
        if ctx.voice_client:
            if ctx.guild and ctx.guild.id in self.music_queue:
                self.music_queue[ctx.guild.id].clear()

            await ctx.voice_client.disconnect()
            await ctx.send("🛑 Desconectado e fila limpa.")
        else:
            await ctx.send("Já estou desconectado.")


async def setup(bot):
    await bot.add_cog(MusicCog(bot))
