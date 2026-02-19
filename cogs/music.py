import discord
from discord.ext import commands
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import asyncio
import config
import os


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.music_queue: dict[int, list[dict[str, str]]] = {}

        # Spotify
        auth_manager = SpotifyClientCredentials(
            client_id=config.SPOTIFY_CLIENT_ID,
            client_secret=config.SPOTIFY_CLIENT_SECRET
        )
        self.sp = spotipy.Spotify(auth_manager=auth_manager)

        # yt-dlp
        self.ytdl_format_options = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "nocheckcertificate": True,
            "ignoreerrors": False,
            "quiet": True,
            "no_warnings": True,
            "default_search": "auto",
            "source_address": "0.0.0.0",
        }

        # ffmpeg (reconexão)
        self.ffmpeg_options = {
            "before_options": (
                "-reconnect 1 "
                "-reconnect_streamed 1 "
                "-reconnect_delay_max 5 "
                "-reconnect_on_network_error 1 "
                "-reconnect_on_http_error 4xx,5xx "
                "-rw_timeout 15000000 "
                "-timeout 15000000 "
            ),
            "options": "-vn -loglevel warning"
        }


        self.ytdl = yt_dlp.YoutubeDL(self.ytdl_format_options)

    # -------- helpers --------


    def _ensure_ffmpeg(self) -> bool:
        return bool(getattr(config, "FFMPEG_PATH", "")) and os.path.exists(config.FFMPEG_PATH)

    def _extract_audio_url(self, info: dict) -> str | None:
        """
        Retorna um URL direto tocável pelo ffmpeg.
        Tenta 'url' e depois formatos.
        """
        # Caso já venha pronto
        if info.get("url") and str(info["url"]).startswith(("http://", "https://")):
            return info["url"]

        # Procura em formatos (mais comum ser aqui)
        formats = info.get("formats") or []
        # Pega o último formato com url (normalmente melhor)
        for fmt in reversed(formats):
            u = fmt.get("url")
            if u and str(u).startswith(("http://", "https://")):
                return u

        return None

    def _play_next(self, ctx: commands.Context):
        guild_id = ctx.guild.id

        if guild_id not in self.music_queue or not self.music_queue[guild_id]:
            return

        next_song = self.music_queue[guild_id].pop(0)
        audio_url = next_song["url"]
        title = next_song["title"]

        source = discord.FFmpegPCMAudio(
            audio_url,
            executable=config.FFMPEG_PATH,
            **self.ffmpeg_options
        )

        ctx.voice_client.play(
            source,
            after=lambda e: self._after_play(ctx, e)
        )

        asyncio.run_coroutine_threadsafe(
            ctx.send(f"▶️ Tocando agora: **{title}**"),
            ctx.bot.loop
        )


    def _after_play(self, ctx: commands.Context, error):
        if error:
            print(f"[Music] Erro ao tocar: {error}")
        self._play_next(ctx)

    # -------- commands --------

    @commands.command(name="play", aliases=["p", "tocar"], help="Toca uma música do Spotify ou YouTube")
    async def play(self, ctx: commands.Context, *, query: str):
        if ctx.guild is None:
            return await ctx.send("❌ Este comando só funciona em servidor.")

        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("Você precisa estar em um canal de voz para tocar música!")

        voice_channel = ctx.author.voice.channel

        # conecta/move
        if not ctx.voice_client:
            await voice_channel.connect()
        elif ctx.voice_client.channel != voice_channel:
            await ctx.voice_client.move_to(voice_channel)

        # inicializa fila
        self.music_queue.setdefault(ctx.guild.id, [])

        # Spotify track -> converter para busca
        search_query = query
        if "spotify.com" in query:
            try:
                if "track" in query:
                    track_info = self.sp.track(query)
                    artist = track_info["artists"][0]["name"]
                    song_name = track_info["name"]
                    search_query = f"{song_name} {artist} audio"
                    await ctx.send(f"🎵 Spotify: **{song_name}** - **{artist}**. Buscando no YouTube...")
                else:
                    return await ctx.send("No momento, só suportamos link de **track** do Spotify.")
            except Exception as e:
                print(e)
                return await ctx.send("❌ Erro ao ler Spotify (confira SPOTIFY_CLIENT_ID/SECRET).")

        # yt-dlp: busca e pega URL direto tocável
        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(
                None,
                lambda: self.ytdl.extract_info(f"ytsearch:{search_query}", download=False)
            )

            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            title = info.get("title", "Sem título")
            audio_url = self._extract_audio_url(info)

            if not audio_url:
                return await ctx.send("❌ Não consegui obter um link de áudio tocável dessa música.")

            # adiciona na fila
            self.music_queue[ctx.guild.id].append({"url": audio_url, "title": title})

            # toca se estiver parado
            if not ctx.voice_client.is_playing():
                self._play_next(ctx)
            else:
                await ctx.send(f"✅ Adicionado à fila: **{title}**")

        except Exception as e:
            print(e)
            await ctx.send("❌ Ocorreu um erro ao tentar buscar/tocar a música.")

    @commands.command(name="skip", aliases=["pular", "passar"], help="Pula a música atual")
    async def skip(self, ctx: commands.Context):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await ctx.send("⏭️ Música pulada!")
        else:
            await ctx.send("⚠️ Não há música tocando no momento.")

    @commands.command(name="stop", aliases=["parar", "sair"], help="Limpa a fila, para a música e desconecta")
    async def stop(self, ctx: commands.Context):
        if ctx.guild and ctx.guild.id in self.music_queue:
            self.music_queue[ctx.guild.id].clear()

        if ctx.voice_client:
            await ctx.voice_client.disconnect()
            await ctx.send("🛑 Desconectado e fila limpa.")
        else:
            await ctx.send("Já estou desconectado.")


async def setup(bot):
    await bot.add_cog(MusicCog(bot))
