import os
import shutil
import asyncio

import discord
from discord.ext import commands

import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

import config


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # fila por servidor: {guild_id: [{"url": "...", "title": "..."}]}
        self.music_queue: dict[int, list[dict[str, str]]] = {}

        # título atual por servidor (para o !now)
        self.now_playing: dict[int, str] = {}

        # flag: após um skip/jump, mostrar fila quando a próxima começar
        self.show_queue_after_skip: dict[int, bool] = {}

        # autofila por servidor (default OFF)
        self.auto_queue: dict[int, bool] = {}

        # Spotify (opcional, só se tiver credenciais)
        self.sp = None
        if config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET:
            auth_manager = SpotifyClientCredentials(
                client_id=config.SPOTIFY_CLIENT_ID,
                client_secret=config.SPOTIFY_CLIENT_SECRET,
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
        self.ytdl = yt_dlp.YoutubeDL(self.ytdl_format_options)

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
            "options": "-vn -loglevel warning",
        }

    # ---------------- helpers ----------------

    def _ensure_ffmpeg(self) -> bool:
        """
        Se FFMPEG_PATH = 'ffmpeg', verifica no PATH.
        Se for caminho, verifica se existe.
        """
        ff = (getattr(config, "FFMPEG_PATH", "") or "").strip()
        if not ff:
            return False

        if ff.lower() == "ffmpeg":
            return shutil.which("ffmpeg") is not None

        return os.path.exists(ff)

    def _extract_audio_url(self, info: dict) -> str | None:
        """Retorna um URL direto tocável pelo ffmpeg."""  # noqa
        if info.get("url") and str(info["url"]).startswith(("http://", "https://")):
            return info["url"]

        formats = info.get("formats") or []
        for fmt in reversed(formats):
            u = fmt.get("url")
            if u and str(u).startswith(("http://", "https://")):
                return u

        return None

    async def _disconnect_if_alone(self, guild: discord.Guild, force: bool = False):
        """
        force=False: desconecta apenas se não houver humanos no canal
        force=True: desconecta mesmo se tiver humanos (ex.: fila acabou)
        """
        vc = guild.voice_client
        if not vc or not vc.is_connected() or not vc.channel:
            return

        humans = [m for m in vc.channel.members if not m.bot]
        if force or len(humans) == 0:
            # limpa estado
            self.music_queue.get(guild.id, []).clear()
            self.now_playing.pop(guild.id, None)
            self.show_queue_after_skip.pop(guild.id, None)
            # auto_queue mantém (pref. do servidor), se quiser limpar: self.auto_queue.pop(guild.id, None)

            await vc.disconnect()

    async def _send_queue_embed(self, ctx: commands.Context, title: str = "🎶 Próximas na fila"):
        fila = self.music_queue.get(ctx.guild.id, [])

        if not fila:
            return await ctx.send("📭 A fila está vazia.")

        max_itens = 15
        linhas: list[str] = []
        for i, item in enumerate(fila[:max_itens], start=1):
            song_title = item.get("title", "Sem título")
            linhas.append(f"**{i}.** {song_title}")

        resto = len(fila) - max_itens
        if resto > 0:
            linhas.append(f"... e mais **{resto}** música(s).")

        embed = discord.Embed(
            title=title,
            description="\n".join(linhas),
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

    def _play_next(self, ctx: commands.Context):
        guild_id = ctx.guild.id

        # Fila acabou -> desconecta (forçado)
        if guild_id not in self.music_queue or not self.music_queue[guild_id]:
            self.now_playing.pop(guild_id, None)
            asyncio.run_coroutine_threadsafe(
                self._disconnect_if_alone(ctx.guild, force=True),
                ctx.bot.loop,
            )
            return

        next_song = self.music_queue[guild_id].pop(0)
        audio_url = next_song["url"]
        title = next_song["title"]

        # guarda "agora tocando"
        self.now_playing[guild_id] = title

        source = discord.FFmpegPCMAudio(
            audio_url,
            executable=config.FFMPEG_PATH,  # recomendado: "ffmpeg"
            **self.ffmpeg_options,
        )

        ctx.voice_client.play(source, after=lambda e: self._after_play(ctx, e))

        # avisa "tocando agora" via threadsafe
        asyncio.run_coroutine_threadsafe(
            ctx.send(f"▶️ Tocando agora: **{title}**"),
            ctx.bot.loop,
        )

        # se foi skip/jump, mostra a fila depois que a próxima começar (não duplica)
        if self.show_queue_after_skip.get(guild_id):
            self.show_queue_after_skip[guild_id] = False
            asyncio.run_coroutine_threadsafe(
                self._send_queue_embed(ctx),
                ctx.bot.loop,
            )

    def _after_play(self, ctx: commands.Context, error):
        if error:
            print(f"[Music] Erro ao tocar: {error}")

        guild_id = ctx.guild.id

        # Se acabou por causa de skip/jump, a fila é mostrada no _play_next (evita duplicar)
        if self.show_queue_after_skip.get(guild_id):
            self._play_next(ctx)
            return

        # Se autofila ON, mostra fila quando trocar naturalmente (e ainda tiver algo na fila)
        if self.auto_queue.get(guild_id, False) and self.music_queue.get(guild_id):
            asyncio.run_coroutine_threadsafe(
                self._send_queue_embed(ctx),
                ctx.bot.loop,
            )

        self._play_next(ctx)

    # ---------------- listeners ----------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        guild = member.guild
        vc = guild.voice_client
        if not vc or not vc.is_connected() or not vc.channel:
            return

        bot_channel = vc.channel
        if before.channel != bot_channel and after.channel != bot_channel:
            return

        # pequeno delay para atualizar lista de membros corretamente
        await asyncio.sleep(180)
        await self._disconnect_if_alone(guild, force=False)

    # ---------------- commands ----------------

    @commands.command(name="play", aliases=["p", "tocar"], help="Toca uma música (YouTube ou Spotify track)")
    async def play(self, ctx: commands.Context, *, query: str):
        if ctx.guild is None:
            return await ctx.send("❌ Este comando só funciona em servidor.")

        if not self._ensure_ffmpeg():
            return await ctx.send(
                "❌ **FFmpeg não encontrado**.\n"
                "Se `ffmpeg -version` funciona no terminal, coloque no `.env`: `FFMPEG_PATH=ffmpeg`."
            )

        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("🎧 Você precisa estar em um canal de voz para tocar música!")

        voice_channel = ctx.author.voice.channel

        # conecta / move
        if not ctx.voice_client:
            await voice_channel.connect()
        elif ctx.voice_client.channel != voice_channel:
            await ctx.voice_client.move_to(voice_channel)

        # inicializa fila e autofila default
        self.music_queue.setdefault(ctx.guild.id, [])
        self.auto_queue.setdefault(ctx.guild.id, False)

        search_query = query

        # Spotify -> converter para busca no YouTube (somente track)
        if "spotify.com" in query:
            if not self.sp:
                return await ctx.send("❌ Spotify não configurado (SPOTIFY_CLIENT_ID/SECRET).")

            try:
                if "track" not in query:
                    return await ctx.send("⚠️ Por enquanto, só suportamos link de **track** do Spotify.")

                track_info = self.sp.track(query)
                artist = track_info["artists"][0]["name"]
                song_name = track_info["name"]
                search_query = f"{song_name} {artist} audio"
                await ctx.send(f"🎵 Spotify: **{song_name}** - **{artist}**. Buscando no YouTube...")
            except Exception as e:
                print(e)
                return await ctx.send("❌ Erro ao ler o link do Spotify. Confira suas credenciais.")

        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(
                None,
                lambda: self.ytdl.extract_info(f"ytsearch:{search_query}", download=False),
            )

            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            title = info.get("title", "Sem título")
            audio_url = self._extract_audio_url(info)

            if not audio_url:
                return await ctx.send("❌ Não consegui obter um link de áudio tocável dessa música.")

            self.music_queue[ctx.guild.id].append({"url": audio_url, "title": title})

            if not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused():
                self._play_next(ctx)
            else:
                await ctx.send(f"✅ Adicionado à fila: **{title}**")

        except Exception as e:
            print(e)
            await ctx.send("❌ Ocorreu um erro ao tentar buscar/tocar a música.")

    @commands.command(name="queue", aliases=["q", "fila"], help="Mostra a fila de músicas")
    async def queue(self, ctx: commands.Context):
        if ctx.guild is None:
            return await ctx.send("❌ Este comando só funciona em servidor.")
        await self._send_queue_embed(ctx)

    @commands.command(name="now", aliases=["np", "tocando"], help="Mostra a música que está tocando agora")
    async def now(self, ctx: commands.Context):
        if ctx.guild is None:
            return await ctx.send("❌ Este comando só funciona em servidor.")

        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send("🔇 Eu não estou conectado em nenhum canal de voz.")

        if vc.is_paused():
            title = self.now_playing.get(ctx.guild.id)
            return await ctx.send(f"⏸️ Pausado em: **{title or 'Sem título'}**")

        if not vc.is_playing():
            fila = self.music_queue.get(ctx.guild.id, [])
            if fila:
                return await ctx.send("⏸️ Não estou tocando agora, mas existe música na fila (`!queue`).")
            return await ctx.send("⏸️ Não estou tocando nada no momento.")

        title = self.now_playing.get(ctx.guild.id)
        if title:
            return await ctx.send(f"🎧 Tocando agora: **{title}**")
        return await ctx.send("🎧 Estou tocando agora (sem título disponível).")

    @commands.command(name="remove", aliases=["rm"], help="Remove uma música da fila pela posição")
    async def remove(self, ctx: commands.Context, pos: int):
        if ctx.guild is None:
            return await ctx.send("❌ Este comando só funciona em servidor.")

        fila = self.music_queue.get(ctx.guild.id, [])
        if not fila:
            return await ctx.send("📭 A fila está vazia.")

        if pos < 1 or pos > len(fila):
            return await ctx.send("⚠️ Posição inválida. Use `!queue` para ver as posições.")

        removed = fila.pop(pos - 1)
        await ctx.send(f"🗑️ Removido da fila: **{removed.get('title','Sem título')}**")

    @commands.command(name="clear", aliases=["limparfila"], help="Limpa a fila de músicas")
    async def clear(self, ctx: commands.Context):
        if ctx.guild is None:
            return await ctx.send("❌ Este comando só funciona em servidor.")
        self.music_queue.get(ctx.guild.id, []).clear()
        await ctx.send("🧹 Fila limpa!")

    @commands.command(name="pause", aliases=["pausar"], help="Pausa a música atual")
    async def pause(self, ctx: commands.Context):
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send("🔇 Eu não estou conectado em nenhum canal de voz.")
        if vc.is_playing():
            vc.pause()
            return await ctx.send("⏸️ Pausado!")
        return await ctx.send("⚠️ Não há música tocando agora.")

    @commands.command(name="resume", aliases=["continuar"], help="Retoma a música pausada")
    async def resume(self, ctx: commands.Context):
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send("🔇 Eu não estou conectado em nenhum canal de voz.")
        if vc.is_paused():
            vc.resume()
            return await ctx.send("▶️ Voltou a tocar!")
        return await ctx.send("⚠️ Não está pausado.")

    @commands.command(name="jump", aliases=["jmp", "tocarpos", "playpos"], help="Toca imediatamente a música da posição N da fila")
    async def jump(self, ctx: commands.Context, pos: int):
        if ctx.guild is None:
            return await ctx.send("❌ Este comando só funciona em servidor.")

        fila = self.music_queue.get(ctx.guild.id, [])
        if not fila:
            return await ctx.send("📭 A fila está vazia.")

        if pos < 1 or pos > len(fila):
            return await ctx.send("⚠️ Posição inválida. Use `!queue` para ver as posições.")

        chosen = fila.pop(pos - 1)
        fila.insert(0, chosen)

        await ctx.send(f"⏩ Indo para: **{chosen.get('title','Sem título')}**")

        if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            self.show_queue_after_skip[ctx.guild.id] = True
            ctx.voice_client.stop()
        else:
            self._play_next(ctx)

    @commands.command(name="autofila", aliases=["aq"], help="Liga/desliga mostrar fila automaticamente ao trocar música")
    async def autofila(self, ctx: commands.Context, arg: str | None = None):
        if ctx.guild is None:
            return await ctx.send("❌ Este comando só funciona em servidor.")

        guild_id = ctx.guild.id
        atual = self.auto_queue.get(guild_id, False)

        if arg is None:
            status = "ON ✅" if atual else "OFF ❌"
            return await ctx.send(f"📌 AutoFila está: **{status}**. Use `!autofila on` ou `!autofila off`.")

        arg = arg.lower().strip()
        if arg in ("on", "1", "true", "ligar", "sim"):
            self.auto_queue[guild_id] = True
            return await ctx.send("✅ AutoFila ativada!")
        if arg in ("off", "0", "false", "desligar", "nao", "não"):
            self.auto_queue[guild_id] = False
            return await ctx.send("❌ AutoFila desativada!")

        return await ctx.send("⚠️ Use: `!autofila on` ou `!autofila off`.")

    @commands.command(name="skip", aliases=["pular", "passar"], help="Pula a música atual")
    async def skip(self, ctx: commands.Context):
        if ctx.guild is None:
            return await ctx.send("❌ Este comando só funciona em servidor.")

        if ctx.voice_client and ctx.voice_client.is_playing():
            self.show_queue_after_skip[ctx.guild.id] = True
            ctx.voice_client.stop()
            await ctx.send("⏭️ Música pulada!")
        else:
            await ctx.send("⚠️ Não há música tocando no momento.")

    @commands.command(name="stop", aliases=["parar", "sair"], help="Limpa a fila, para a música e desconecta")
    async def stop(self, ctx: commands.Context):
        if ctx.guild and ctx.guild.id in self.music_queue:
            self.music_queue[ctx.guild.id].clear()

        if ctx.guild:
            self.now_playing.pop(ctx.guild.id, None)
            self.show_queue_after_skip.pop(ctx.guild.id, None)

        if ctx.voice_client:
            await ctx.voice_client.disconnect()
            await ctx.send("🛑 Desconectado e fila limpa.")
        else:
            await ctx.send("Já estou desconectado. 😒")

    @commands.command(name="ajuda", aliases=["comandos", "cmds"], help="Mostra os comandos do bot de música")
    async def ajuda(self, ctx: commands.Context):
        embed = discord.Embed(
            title="📌 Comandos do Bot (Música)",
            description="Prefixo: **!**",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="▶️ Tocar / Controle",
            value=(
                "`!play <nome/link>` (aliases: `!p`, `!tocar`) — toca uma música (YouTube ou Spotify track) \n"  
                "`!pause` (alias: `!pausar`) — pausa a música atual \n" 
                "`!resume` (alias: `!continuar`) — retoma a música pausada \n"  
                "`!skip` (aliases: `!pular`, `!passar`) — pula a música atual \n" 
                "`!stop` (aliases: `!parar`, `!sair`) — para a música e limpa a fila \n"  
                "`!now` (aliases: `!np`, `!tocando`) — mostra informações da música atual \n"  
            ),
            inline=False,
        )

        embed.add_field(
            name="📃 Fila",
            value=(
                "`!queue` (aliases: `!q`, `!fila`) — mostra as próximas músicas na fila \n"  
                "`!remove <pos>` (alias: `!rm`) — remove uma música da fila \n"  
                "`!clear` (alias: `!limparfila`) — limpa a fila \n"  
                "`!jump <pos>` (aliases: `!jmp`, `!tocarpos`, `!playpos`) — toca direto a música da posição \n"  
            ),
            inline=False,
        )

        embed.add_field(
            name="⚙️ Extras",
            value=(
                "`!autofila` (alias: `!aq`) — ver status \n"
                "`!autofila on` — ativa \n"
                "`!autofila off` — desativa \n"
            ),
            inline=False,
        )

        embed.set_footer(text="Dica: use !queue para ver as posições e depois !jump <pos>.")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
