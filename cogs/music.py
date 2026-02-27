import os
import re
import shutil
import asyncio
from typing import Optional

import discord
from discord.ext import commands

import yt_dlp
from yt_dlp.utils import DownloadError

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

import config


YOUTUBE_PLAYLIST_HINT_RE = re.compile(r"(?:[?&]list=|/playlist\?)", re.IGNORECASE)


class MusicCog(commands.Cog):
    """
    Melhorias incluídas:
    - after() thread-safe: agenda a continuação no event loop.
    - Lock por guild (evita race conditions em play/skip/jump).
    - Volume por guild (!volume 0-100).
    - Timeout quando a fila fica vazia (desconecta após alguns minutos).
    - Detecção de playlist em !play e comando dedicado !playpl para playlist (com limite).
    - Mensagens de erro mais claras para yt-dlp.
    """

    # Ajuste aqui se quiser
    IDLE_DISCONNECT_SECONDS = 180  # 3 min sem música (fila vazia) -> desconecta

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

        # volume por servidor (0.0 a 1.0 default)
        self.volume: dict[int, float] = {}

        # lock por servidor (evita conflito)
        self._locks: dict[int, asyncio.Lock] = {}

        # idle task por servidor (fila vazia)
        self._idle_tasks: dict[int, asyncio.Task] = {}

        # Spotify (opcional, só se tiver credenciais)
        self.sp = None
        if getattr(config, "SPOTIFY_CLIENT_ID", "") and getattr(config, "SPOTIFY_CLIENT_SECRET", ""):
            auth_manager = SpotifyClientCredentials(
                client_id=config.SPOTIFY_CLIENT_ID,
                client_secret=config.SPOTIFY_CLIENT_SECRET,
            )
            self.sp = spotipy.Spotify(auth_manager=auth_manager)

        # yt-dlp (single)
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

        # yt-dlp (playlist)
        self.ytdl_playlist = yt_dlp.YoutubeDL(
            {
                **self.ytdl_format_options,
                "noplaylist": False,
                # mais rápido p/ listar itens; depois extraímos cada item completo
                "extract_flat": "in_playlist",
            }
        )

        # ffmpeg (reconexão)
        self.ffmpeg_base_before = (
            "-reconnect 1 "
            "-reconnect_streamed 1 "
            "-reconnect_delay_max 5 "
            "-reconnect_on_network_error 1 "
            "-reconnect_on_http_error 4xx,5xx "
            "-rw_timeout 15000000 "
            "-timeout 15000000 "
        )

    # ---------------- helpers ----------------

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[guild_id] = lock
        return lock

    def _cancel_idle(self, guild_id: int) -> None:
        t = self._idle_tasks.pop(guild_id, None)
        if t and not t.done():
            t.cancel()

    def _ensure_defaults(self, guild_id: int) -> None:
        self.music_queue.setdefault(guild_id, [])
        self.auto_queue.setdefault(guild_id, False)
        self.volume.setdefault(guild_id, 0.6)

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
        """Retorna um URL direto tocável pelo ffmpeg."""
        if info.get("url") and str(info["url"]).startswith(("http://", "https://")):
            return info["url"]

        formats = info.get("formats") or []
        for fmt in reversed(formats):
            u = fmt.get("url")
            if u and str(u).startswith(("http://", "https://")):
                return u
        return None

    def _call_soon(self, fn, /, *args, **kwargs) -> None:
        """Agenda uma função para rodar com segurança no event loop do bot."""
        self.bot.loop.call_soon_threadsafe(fn, *args, **kwargs)

    def _is_likely_playlist_url(self, query: str) -> bool:
        q = query.strip()
        if "spotify.com" in q:
            return "playlist" in q or "album" in q
        if "youtube" in q or "youtu.be" in q:
            return bool(YOUTUBE_PLAYLIST_HINT_RE.search(q))
        return False

    async def _schedule_idle_disconnect(self, guild: discord.Guild) -> None:
        """Agenda desconexão se a fila ficar vazia e o bot não estiver tocando."""
        guild_id = guild.id
        self._cancel_idle(guild_id)

        async def _job():
            try:
                await asyncio.sleep(self.IDLE_DISCONNECT_SECONDS)

                vc = guild.voice_client
                if not vc or not vc.is_connected():
                    return

                # se voltou a tocar ou tem fila, não desconecta
                if vc.is_playing() or vc.is_paused():
                    return
                if self.music_queue.get(guild_id):
                    return

                # desconecta mesmo com humanos (fila vazia há muito tempo)
                await vc.disconnect()
                self.now_playing.pop(guild_id, None)
                self.show_queue_after_skip.pop(guild_id, None)
            except asyncio.CancelledError:
                return

        self._idle_tasks[guild_id] = asyncio.create_task(_job())

    async def _disconnect_if_alone(self, guild: discord.Guild, force: bool = False):
        """
        force=False: desconecta apenas se não houver humanos no canal
        force=True: desconecta mesmo se tiver humanos (ex.: comando stop)
        """
        vc = guild.voice_client
        if not vc or not vc.is_connected() or not vc.channel:
            return

        humans = [m for m in vc.channel.members if not m.bot]
        if force or len(humans) == 0:
            self.music_queue.get(guild.id, []).clear()
            self.now_playing.pop(guild.id, None)
            self.show_queue_after_skip.pop(guild.id, None)
            self._cancel_idle(guild.id)
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

        embed = discord.Embed(title=title, description="\n".join(linhas), color=discord.Color.blurple())
        await ctx.send(embed=embed)

    def _make_ffmpeg_options_for_guild(self, guild_id: int) -> dict:
        # aplica volume via filtro
        vol = self.volume.get(guild_id, 0.6)
        # clamp de segurança
        if vol < 0.0:
            vol = 0.0
        if vol > 2.0:
            vol = 2.0

        # -filter:a volume=<float>
        return {
            "before_options": self.ffmpeg_base_before,
            "options": f"-vn -loglevel warning -filter:a volume={vol}",
        }

    def _play_next(self, ctx: commands.Context):
        """
        ⚠️ Deve ser chamado a partir do event loop do bot (ou agendado).
        """
        guild_id = ctx.guild.id
        self._ensure_defaults(guild_id)
        self._cancel_idle(guild_id)

        # Se a fila acabou: agenda timeout p/ desconectar
        if not self.music_queue.get(guild_id):
            self.now_playing.pop(guild_id, None)
            asyncio.create_task(self._schedule_idle_disconnect(ctx.guild))
            return

        next_song = self.music_queue[guild_id].pop(0)
        audio_url = next_song["url"]
        title = next_song["title"]

        self.now_playing[guild_id] = title

        source = discord.FFmpegPCMAudio(
            audio_url,
            executable=getattr(config, "FFMPEG_PATH", "ffmpeg"),
            **self._make_ffmpeg_options_for_guild(guild_id),
        )

        ctx.voice_client.play(source, after=lambda e: self._after_play(ctx, e))

        asyncio.run_coroutine_threadsafe(ctx.send(f"▶️ Tocando agora: **{title}**"), ctx.bot.loop)

        if self.show_queue_after_skip.get(guild_id):
            self.show_queue_after_skip[guild_id] = False
            asyncio.run_coroutine_threadsafe(self._send_queue_embed(ctx), ctx.bot.loop)

    def _after_play(self, ctx: commands.Context, error):
        """
        Callback do discord.py: roda fora do loop async.
        Aqui só agendamos a continuação no event loop.
        """
        if error:
            print(f"[Music] Erro ao tocar: {error}")

        guild_id = ctx.guild.id

        def _continue():
            # autofila ON: mostrar quando trocar naturalmente (e ainda houver fila)
            if self.auto_queue.get(guild_id, False) and self.music_queue.get(guild_id):
                asyncio.create_task(self._send_queue_embed(ctx))

            self._play_next(ctx)

        self._call_soon(_continue)

    async def _extract_single(self, query: str) -> tuple[str, str] | None:
        """
        Extrai (title, audio_url) para uma query (nome ou link).
        """
        loop = asyncio.get_running_loop()

        def _work():
            # sempre procura como ytsearch quando não é URL
            # se for URL, yt-dlp também entende, mas o ytsearch não atrapalha se vier como URL.
            return self.ytdl.extract_info(f"ytsearch:{query}", download=False)

        info = await loop.run_in_executor(None, _work)

        if "entries" in info and info["entries"]:
            info = info["entries"][0]

        title = info.get("title", "Sem título")
        audio_url = self._extract_audio_url(info)
        if not audio_url:
            return None
        return title, audio_url

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

        if self._is_likely_playlist_url(query):
            return await ctx.send(
                "📃 Parece que isso é **playlist/album**.\n"
                "Use **`!playpl <link>`** para adicionar playlist (com limite), ou cole um link de track/vídeo."
            )

        voice_channel = ctx.author.voice.channel

        # conecta / move
        if not ctx.voice_client:
            await voice_channel.connect()
        elif ctx.voice_client.channel != voice_channel:
            await ctx.voice_client.move_to(voice_channel)

        guild_id = ctx.guild.id
        self._ensure_defaults(guild_id)
        self._cancel_idle(guild_id)

        # Spotify -> converter para busca no YouTube (somente track)
        search_query = query
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

        lock = self._get_lock(guild_id)
        async with lock:
            try:
                result = await self._extract_single(search_query)
                if not result:
                    return await ctx.send("❌ Não consegui obter um link de áudio tocável dessa música.")

                title, audio_url = result
                self.music_queue[guild_id].append({"url": audio_url, "title": title})

                if not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused():
                    self._play_next(ctx)
                else:
                    await ctx.send(f"✅ Adicionado à fila: **{title}**")

            except DownloadError as e:
                msg = str(e).lower()
                if "private" in msg or "unavailable" in msg:
                    return await ctx.send("❌ Esse vídeo parece **indisponível/privado**.")
                if "sign in" in msg or "age" in msg:
                    return await ctx.send("❌ Esse vídeo parece ter **restrição de idade/login**.")
                return await ctx.send("❌ Falha ao buscar no YouTube (yt-dlp). Tente outro termo/link.")
            except Exception as e:
                print(e)
                await ctx.send("❌ Ocorreu um erro ao tentar buscar/tocar a música.")

    @commands.command(name="playpl", aliases=["playlist"], help="Adiciona músicas de uma playlist do YouTube (com limite)")
    async def playpl(self, ctx: commands.Context, url: str, limit: Optional[int] = 15):
        if ctx.guild is None:
            return await ctx.send("❌ Este comando só funciona em servidor.")

        if not self._ensure_ffmpeg():
            return await ctx.send(
                "❌ **FFmpeg não encontrado**.\n"
                "Se `ffmpeg -version` funciona no terminal, coloque no `.env`: `FFMPEG_PATH=ffmpeg`."
            )

        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("🎧 Você precisa estar em um canal de voz para tocar música!")

        if limit is None:
            limit = 15
        limit = max(1, min(int(limit), 50))

        voice_channel = ctx.author.voice.channel
        if not ctx.voice_client:
            await voice_channel.connect()
        elif ctx.voice_client.channel != voice_channel:
            await ctx.voice_client.move_to(voice_channel)

        guild_id = ctx.guild.id
        self._ensure_defaults(guild_id)
        self._cancel_idle(guild_id)

        await ctx.send(f"📥 Lendo playlist... (vou adicionar até **{limit}** músicas)")

        loop = asyncio.get_running_loop()

        def _get_playlist():
            return self.ytdl_playlist.extract_info(url, download=False)

        try:
            pl_info = await loop.run_in_executor(None, _get_playlist)

            entries = pl_info.get("entries") or []
            # pode vir com None em itens
            entries = [e for e in entries if e]

            if not entries:
                return await ctx.send("❌ Não consegui ler a playlist (sem itens).")

            # limita
            entries = entries[:limit]

            lock = self._get_lock(guild_id)
            async with lock:
                added = 0
                failed = 0

                for e in entries:
                    # extract_flat pode vir com 'url' ou 'id'
                    item_url = e.get("url") or e.get("webpage_url") or e.get("id")
                    if not item_url:
                        failed += 1
                        continue

                    # se vier só ID, monta url
                    if isinstance(item_url, str) and not item_url.startswith(("http://", "https://")):
                        item_url = f"https://www.youtube.com/watch?v={item_url}"

                    try:
                        single = await self._extract_single(str(item_url))
                        if not single:
                            failed += 1
                            continue
                        title, audio_url = single
                        self.music_queue[guild_id].append({"url": audio_url, "title": title})
                        added += 1
                    except Exception:
                        failed += 1

                if added == 0:
                    return await ctx.send("❌ Não consegui adicionar nenhuma música dessa playlist.")

                await ctx.send(f"✅ Playlist adicionada: **{added}** música(s). (falhas: {failed})")

                if ctx.voice_client and not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused():
                    self._play_next(ctx)

        except DownloadError:
            await ctx.send("❌ Não consegui acessar essa playlist (yt-dlp). Verifique se o link é público.")
        except Exception as e:
            print(e)
            await ctx.send("❌ Erro ao processar playlist.")

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

        title = self.now_playing.get(ctx.guild.id)

        if vc.is_paused():
            return await ctx.send(f"⏸️ Pausado em: **{title or 'Sem título'}**")

        if not vc.is_playing():
            fila = self.music_queue.get(ctx.guild.id, [])
            if fila:
                return await ctx.send("⏸️ Não estou tocando agora, mas existe música na fila (`!queue`).")
            return await ctx.send("⏸️ Não estou tocando nada no momento.")

        return await ctx.send(f"🎧 Tocando agora: **{title or 'Sem título'}**")

    @commands.command(name="volume", aliases=["vol"], help="Define o volume (0 a 100)")
    async def volume_cmd(self, ctx: commands.Context, value: Optional[int] = None):
        if ctx.guild is None:
            return await ctx.send("❌ Este comando só funciona em servidor.")

        guild_id = ctx.guild.id
        self._ensure_defaults(guild_id)

        if value is None:
            current = int(self.volume.get(guild_id, 0.6) * 100)
            return await ctx.send(f"🔊 Volume atual: **{current}%**. Use `!volume 0-100`.")

        value = max(0, min(int(value), 200))  # permite até 200% se quiser
        self.volume[guild_id] = value / 100.0

        await ctx.send(f"🔊 Volume definido para **{value}%**.\n⚠️ Ele aplica na **próxima música** (a atual não muda).")

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
        await self._schedule_idle_disconnect(ctx.guild)

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

        guild_id = ctx.guild.id
        fila = self.music_queue.get(guild_id, [])
        if not fila:
            return await ctx.send("📭 A fila está vazia.")

        if pos < 1 or pos > len(fila):
            return await ctx.send("⚠️ Posição inválida. Use `!queue` para ver as posições.")

        lock = self._get_lock(guild_id)
        async with lock:
            chosen = fila.pop(pos - 1)
            fila.insert(0, chosen)

            await ctx.send(f"⏩ Indo para: **{chosen.get('title','Sem título')}**")

            if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
                self.show_queue_after_skip[guild_id] = True
                ctx.voice_client.stop()
            else:
                self._play_next(ctx)

    @commands.command(name="autofila", aliases=["aq"], help="Liga/desliga mostrar fila automaticamente ao trocar música")
    async def autofila(self, ctx: commands.Context, arg: str | None = None):
        if ctx.guild is None:
            return await ctx.send("❌ Este comando só funciona em servidor.")

        guild_id = ctx.guild.id
        self._ensure_defaults(guild_id)
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

        guild_id = ctx.guild.id
        lock = self._get_lock(guild_id)

        async with lock:
            if ctx.voice_client and ctx.voice_client.is_playing():
                self.show_queue_after_skip[guild_id] = True
                ctx.voice_client.stop()
                await ctx.send("⏭️ Música pulada!")
            else:
                await ctx.send("⚠️ Não há música tocando no momento.")

    @commands.command(name="stop", aliases=["parar", "sair"], help="Limpa a fila, para a música e desconecta")
    async def stop(self, ctx: commands.Context):
        if ctx.guild is None:
            return await ctx.send("❌ Este comando só funciona em servidor.")

        guild_id = ctx.guild.id
        self.music_queue.get(guild_id, []).clear()
        self.now_playing.pop(guild_id, None)
        self.show_queue_after_skip.pop(guild_id, None)
        self._cancel_idle(guild_id)

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
                "`!play <nome/link>` (aliases: `!p`, `!tocar`) — toca uma música (YouTube ou Spotify track)\n"
                "`!playpl <link> [limite]` — adiciona playlist do YouTube (limite padrão 15)\n"
                "`!pause` — pausa\n"
                "`!resume` — continua\n"
                "`!skip` — pula\n"
                "`!stop` — desconecta e limpa\n"
                "`!now` — mostra a atual\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="📃 Fila",
            value=(
                "`!queue` — mostra fila\n"
                "`!remove <pos>` — remove item\n"
                "`!clear` — limpa fila\n"
                "`!jump <pos>` — toca direto da posição\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="⚙️ Extras",
            value=(
                "`!volume` — ver volume\n"
                "`!volume 0-100` — define volume (aplica na próxima música)\n"
                "`!autofila` / `!autofila on/off` — fila automática\n"
                f"Desconecta se fila ficar vazia por **{self.IDLE_DISCONNECT_SECONDS}s**\n"
            ),
            inline=False,
        )

        embed.set_footer(text="Dica: use !queue para ver as posições e depois !jump <pos>.")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
