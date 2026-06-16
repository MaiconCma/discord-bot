from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

import discord
from discord.ext import commands, tasks

import config

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

log = logging.getLogger(__name__)

YTDLP_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": False,
    "postprocessors": [{"key": "FFmpegExtractAudio"}],
    "youtube_include_dash_manifest": False,
}

FFMPEG_BEFORE_OPTS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = "-vn"

class TrackCache:
    def __init__(self, expiry: int = 3600):
        self._cache: Dict[str, tuple[float, Track]] = {}
        self.expiry = expiry

    def get(self, key: str) -> Optional[Track]:
        if key in self._cache:
            timestamp, track = self._cache[key]
            if time.time() - timestamp < self.expiry:
                return track
            else:
                del self._cache[key]
        return None

    def set(self, key: str, track: Track):
        self._cache[key] = (time.time(), track)

def get_queue_file(guild_id: int) -> Path:
    return Path(f"data/queue_{guild_id}.json")

def save_queue(guild_id: int, items: List[Track]):
    if not getattr(config, "MUSIC_PERSIST_QUEUE", True):
        return
    path = get_queue_file(guild_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [asdict(t) for t in items]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_queue(guild_id: int) -> List[Track]:
    if not getattr(config, "MUSIC_PERSIST_QUEUE", True):
        return []
    path = get_queue_file(guild_id)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            items = json.load(f)
        return [Track(**t) for t in items]
    except Exception:
        return []

def clear_queue_file(guild_id: int):
    path = get_queue_file(guild_id)
    if path.exists():
        path.unlink()

@dataclass
class Track:
    title: str
    webpage_url: str
    stream_url: str
    requested_by: int
    duration: Optional[int] = None

    def format_duration(self) -> str:
        if not self.duration:
            return "?"
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def _resolve_ffmpeg_path() -> str:
    ffmpeg_path = getattr(config, "FFMPEG_PATH", "ffmpeg")
    if os.path.isabs(ffmpeg_path) or os.path.exists(ffmpeg_path):
        return ffmpeg_path
    resolved = shutil.which(ffmpeg_path)
    if resolved:
        return resolved
    raise FileNotFoundError(
        f"FFmpeg não encontrado em '{ffmpeg_path}'. Instale o FFmpeg e adicione ao PATH, ou defina FFMPEG_PATH no .env."
    )


class GuildPlayer:
    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.current: Optional[Track] = None
        self.loop: bool = False
        self.volume: float = 1.0
        self._task: Optional[asyncio.Task] = None
        self._next = asyncio.Event()
        self._stop_loop = False
        self._new_track_event = asyncio.Event()
        self._idle_task: Optional[asyncio.Task] = None
        self._idle_timeout_seconds = getattr(config, "MUSIC_IDLE_TIMEOUT", 60)
        self._max_duration = getattr(config, "MUSIC_MAX_DURATION", 3600)
        self._current_start_time = None
        self.text_channel: Optional[discord.TextChannel] = None  # Canal de texto associado

        for t in load_queue(guild_id):
            self.queue.put_nowait(t)

    def start(self):
        if self._task is None or self._task.done():
            self._stop_loop = False
            self._task = asyncio.create_task(self._player_loop())

    async def stop(self, keep_queue: bool = False):
        self._stop_loop = True
        self._cancel_idle_timer()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self.current = None
        self.loop = False
        if not keep_queue:
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                    self.queue.task_done()
                except asyncio.QueueEmpty:
                    break
        if keep_queue:
            items = list(self.queue._queue)
            save_queue(self.guild_id, items)
        else:
            clear_queue_file(self.guild_id)

    def signal_next(self):
        self._next.set()

    def _get_vc(self) -> Optional[discord.VoiceClient]:
        guild = self.bot.get_guild(self.guild_id)
        return guild.voice_client if guild else None

    def _cancel_idle_timer(self):
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            self._idle_task = None

    async def _start_idle_timer(self):
        self._cancel_idle_timer()
        self._idle_task = asyncio.create_task(self._idle_worker())

    async def _idle_worker(self):
        try:
            await asyncio.sleep(self._idle_timeout_seconds)
        except asyncio.CancelledError:
            return
        log.info("Fila vazia por %d segundos, saindo do canal.", self._idle_timeout_seconds)
        await self._leave_voice()

    async def _leave_voice(self):
        vc = self._get_vc()
        if vc and vc.is_connected():
            await vc.disconnect()
        await self.stop(keep_queue=False)
        music_cog = self.bot.get_cog("Music")
        if music_cog and self.guild_id in music_cog.players:
            del music_cog.players[self.guild_id]

    async def _add_track(self, track: Track):
        await self.queue.put(track)
        self._new_track_event.set()
        save_queue(self.guild_id, list(self.queue._queue))

    async def _player_loop(self):
        while not self._stop_loop:
            self._next.clear()
            vc = self._get_vc()
            if not vc or not vc.is_connected():
                await asyncio.sleep(2)
                continue

            track = None
            if self.loop and self.current:
                track = self.current
            else:
                if self.queue.empty():
                    self._cancel_idle_timer()
                    await self._start_idle_timer()
                    try:
                        await self._new_track_event.wait()
                    except asyncio.CancelledError:
                        break
                    self._new_track_event.clear()
                    if self.queue.empty():
                        if not vc.is_connected():
                            break
                        continue
                try:
                    track = await self.queue.get()
                except asyncio.CancelledError:
                    break

            if track is None:
                continue

            self._cancel_idle_timer()
            self.current = track
            self._current_start_time = time.time()

            try:
                await self._play(vc, track)
                await self._next.wait()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Erro no loop de reprodução: %r", e)
                await asyncio.sleep(1)
                self.signal_next()
            finally:
                if not self.loop:
                    try:
                        self.queue.task_done()
                    except Exception:
                        pass
                if not self.loop:
                    items = list(self.queue._queue)
                    save_queue(self.guild_id, items)

    async def _play(self, vc: discord.VoiceClient, track: Track):
        if not vc or not vc.is_connected():
            log.warning("Tentativa de reprodução sem conexão de voz.")
            return

        ffmpeg_path = _resolve_ffmpeg_path()
        volume_filter = f"volume={self.volume:.2f}" if self.volume != 1.0 else None
        options = FFMPEG_OPTS
        if volume_filter:
            options = f"{FFMPEG_OPTS} -af {volume_filter}"

        source = discord.FFmpegPCMAudio(
            track.stream_url,
            before_options=FFMPEG_BEFORE_OPTS,
            options=options,
            executable=ffmpeg_path,
        )

        def _after(err: Optional[Exception]):
            if err:
                log.warning("Erro de áudio: %r", err)
            self.bot.loop.call_soon_threadsafe(self.signal_next)

        try:
            vc.play(source, after=_after)
            await self._update_now_playing(track)
        except discord.ClientException as e:
            log.error("Falha ao iniciar reprodução: %r", e)
            self.signal_next()

    async def _update_now_playing(self, track: Track):
        music_cog = self.bot.get_cog("Music")
        if music_cog and music_cog.now_playing_message.get(self.guild_id):
            await music_cog._update_now_playing_message(self.guild_id, track)
        else:
            await music_cog._send_now_playing_message(self.guild_id, track)

    async def set_volume(self, volume: float):
        self.volume = max(0.0, min(2.0, volume))
        log.info("Volume ajustado para %s na guild %s", self.volume, self.guild_id)

class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: Dict[int, GuildPlayer] = {}
        self._ytdl = None
        self._cache = TrackCache(expiry=getattr(config, "MUSIC_CACHE_EXPIRY", 3600))
        self.now_playing_message: Dict[int, discord.Message] = {}
        self._user_cooldown: Dict[int, float] = {}
        self._cooldown_seconds = getattr(config, "MUSIC_PLAYER_COOLDOWN", 5)
        self.update_now_playing.start()

    def cog_unload(self):
        self.update_now_playing.cancel()

    def _get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(self.bot, guild_id)
        return self.players[guild_id]

    def _get_ytdl(self):
        if yt_dlp is None:
            raise RuntimeError("yt-dlp não está instalado. Instale com: pip install yt-dlp")
        if self._ytdl is None:
            self._ytdl = yt_dlp.YoutubeDL(YTDLP_OPTS)
        return self._ytdl

    async def _extract(self, query: str) -> List[Track]:
        cache_key = query
        cached = self._cache.get(cache_key)
        if cached:
            return [cached]

        def _run():
            ydl = self._get_ytdl()
            try:
                info = ydl.extract_info(query, download=False)
            except Exception as e:
                log.exception("Erro ao extrair info: %s", e)
                raise

            tracks = []
            if "entries" in info:
                for entry in info["entries"]:
                    if entry is None:
                        continue
                    title = entry.get("title") or "Sem título"
                    webpage_url = entry.get("webpage_url") or entry.get("original_url") or query
                    stream_url = entry.get("url")
                    duration = entry.get("duration")
                    if stream_url:
                        tracks.append(Track(title, webpage_url, stream_url, 0, duration))
            else:
                title = info.get("title") or "Sem título"
                webpage_url = info.get("webpage_url") or info.get("original_url") or query
                stream_url = info.get("url")
                duration = info.get("duration")
                if stream_url:
                    tracks.append(Track(title, webpage_url, stream_url, 0, duration))
            return tracks

        try:
            tracks = await asyncio.to_thread(_run)
        except Exception as e:
            log.error("Falha na extração: %s", e)
            raise RuntimeError(f"Não foi possível carregar: {e}")

        if not tracks:
            raise RuntimeError("Nenhuma faixa encontrada.")

        self._cache.set(cache_key, tracks[0])
        return tracks

    async def _ensure_voice(self, ctx: commands.Context) -> Optional[discord.VoiceClient]:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            await ctx.reply("❌ Este comando só funciona em servidores.")
            return None

        if ctx.author.voice is None:
            await ctx.reply("❌ Você precisa estar em um canal de voz.")
            return None

        channel = ctx.author.voice.channel
        vc = ctx.guild.voice_client

        if vc and vc.is_connected():
            if vc.channel.id != channel.id:
                await ctx.reply("❌ Já estou em outro canal de voz.")
                return None
        else:
            try:
                vc = await channel.connect(self_deaf=True)
                log.info("Conectado ao canal %s na guild %s", channel.name, ctx.guild.id)
            except discord.Forbidden:
                await ctx.reply("❌ Sem permissão para conectar/falar no canal.")
                return None
            except Exception as e:
                await ctx.reply(f"❌ Erro ao conectar: {e}")
                return None

        return vc

    async def _send_now_playing_message(self, guild_id: int, track: Track):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        player = self._get_player(guild_id)
        if not player.text_channel:
            return
        channel = player.text_channel
        embed = self._build_now_playing_embed(track, player)
        msg = await channel.send(embed=embed)
        self.now_playing_message[guild_id] = msg
        await self._add_reactions(msg)

    async def _update_now_playing_message(self, guild_id: int, track: Track):
        msg = self.now_playing_message.get(guild_id)
        if msg:
            player = self._get_player(guild_id)
            embed = self._build_now_playing_embed(track, player)
            await msg.edit(embed=embed)

    def _build_now_playing_embed(self, track: Track, player: GuildPlayer) -> discord.Embed:
        embed = discord.Embed(
            title="🎶 Tocando agora",
            description=f"[{track.title}]({track.webpage_url})",
        )
        embed.add_field(name="Duração", value=track.format_duration(), inline=True)
        embed.add_field(name="Pedido por", value=f"<@{track.requested_by}>", inline=True)
        if player.current == track and player._current_start_time and track.duration:
            elapsed = time.time() - player._current_start_time
            elapsed = min(elapsed, track.duration)
            percent = elapsed / track.duration
            bar_len = 20
            filled = int(bar_len * percent)
            bar = "▬" * filled + "🔘" + "▬" * (bar_len - filled - 1)
            embed.add_field(
                name="Progresso",
                value=f"{bar}\n`{self._format_time(elapsed)} / {track.format_duration()}`",
                inline=False,
            )
        embed.set_footer(text=f"Volume: {int(player.volume * 100)}% | Loop: {'ON' if player.loop else 'OFF'}")
        return embed

    def _format_time(self, seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    async def _add_reactions(self, msg: discord.Message):
        await msg.add_reaction("⏸️")
        await msg.add_reaction("▶️")
        await msg.add_reaction("⏭️")
        await msg.add_reaction("🔁")
        await msg.add_reaction("🔊")
        await msg.add_reaction("🔉")

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        msg_id = payload.message_id
        guild_id = payload.guild_id
        if not guild_id:
            return
        if self.now_playing_message.get(guild_id) and self.now_playing_message[guild_id].id == msg_id:
            guild = self.bot.get_guild(guild_id)
            member = guild.get_member(payload.user_id)
            if not member:
                return
            player = self._get_player(guild_id)
            vc = player._get_vc()
            if not vc or not vc.is_connected():
                return
            emoji = str(payload.emoji)
            if emoji == "⏸️" and vc.is_playing():
                vc.pause()
                await self._update_now_playing_message(guild_id, player.current)
            elif emoji == "▶️" and vc.is_paused():
                vc.resume()
                await self._update_now_playing_message(guild_id, player.current)
            elif emoji == "⏭️":
                if vc.is_playing():
                    vc.stop()
                else:
                    player.signal_next()
            elif emoji == "🔁":
                player.loop = not player.loop
                await self._update_now_playing_message(guild_id, player.current)
            elif emoji == "🔊":
                new_vol = min(2.0, player.volume + 0.1)
                await player.set_volume(new_vol)
                await self._update_now_playing_message(guild_id, player.current)
            elif emoji == "🔉":
                new_vol = max(0.0, player.volume - 0.1)
                await player.set_volume(new_vol)
                await self._update_now_playing_message(guild_id, player.current)
            try:
                await self.now_playing_message[guild_id].remove_reaction(emoji, member)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction(payload)

    @commands.command(name="entrar")
    async def entrar(self, ctx: commands.Context):
        vc = await self._ensure_voice(ctx)
        if vc:
            player = self._get_player(ctx.guild.id)
            player.text_channel = ctx.channel
            await ctx.reply(f"✅ Conectado em **{vc.channel.name}**.")

    @commands.command(name="sair")
    async def sair(self, ctx: commands.Context):
        if not ctx.guild:
            return
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            await ctx.reply("❌ Não estou em um canal de voz.")
            return

        player = self._get_player(ctx.guild.id)
        await player.stop(keep_queue=False)
        await vc.disconnect()
        if ctx.guild.id in self.players:
            del self.players[ctx.guild.id]
        if ctx.guild.id in self.now_playing_message:
            await self.now_playing_message[ctx.guild.id].delete()
            del self.now_playing_message[ctx.guild.id]
        await ctx.reply("✅ Desconectado e fila limpa.")

    @commands.command(name="play")
    async def play(self, ctx: commands.Context, *, query: str):
        if not ctx.guild:
            return

        user_id = ctx.author.id
        now = time.time()
        last = self._user_cooldown.get(user_id, 0)
        if now - last < self._cooldown_seconds:
            await ctx.reply(f"❌ Aguarde {self._cooldown_seconds:.0f} segundos antes de usar o comando novamente.")
            return
        self._user_cooldown[user_id] = now

        msg = await ctx.reply("🔎 Buscando...")

        vc = await self._ensure_voice(ctx)
        if not vc:
            return

        player = self._get_player(ctx.guild.id)
        try:
            _resolve_ffmpeg_path()
        except FileNotFoundError as exc:
            await msg.edit(content=f"❌ {exc}")
            return

        player.start()
        player.text_channel = ctx.channel

        try:
            tracks = await self._extract(query)
        except Exception as e:
            await msg.edit(content=f"❌ {e}")
            return

        max_dur = getattr(config, "MUSIC_MAX_DURATION", 3600)
        valid_tracks = [t for t in tracks if t.duration is None or t.duration <= max_dur]
        if len(valid_tracks) != len(tracks):
            skipped = len(tracks) - len(valid_tracks)
            await ctx.send(f"⚠️ {skipped} música(s) ignorada(s) por excederem {max_dur//60} minutos.")

        if not valid_tracks:
            await msg.edit(content="❌ Nenhuma música válida encontrada (duração muito longa ou inválida).")
            return

        for track in valid_tracks:
            track.requested_by = ctx.author.id
            await player._add_track(track)

        if len(valid_tracks) == 1:
            embed = discord.Embed(
                title="✅ Adicionado na fila",
                description=f"[{valid_tracks[0].title}]({valid_tracks[0].webpage_url})",
            )
            embed.add_field(name="Duração", value=valid_tracks[0].format_duration(), inline=True)
            embed.add_field(name="Pedido por", value=ctx.author.mention, inline=True)
            await msg.edit(content=None, embed=embed)
        else:
            await msg.edit(content=f"✅ Adicionadas **{len(valid_tracks)}** músicas à fila.")

        if not vc.is_playing() and not vc.is_paused():
            player.signal_next()

    @commands.command(name="skip")
    async def skip(self, ctx: commands.Context):
        if not ctx.guild:
            return
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            await ctx.reply("❌ Não estou em um canal de voz.")
            return
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await ctx.reply("⏭️ Pulei para a próxima música.")
        else:
            await ctx.reply("❌ Nada tocando no momento.")

    @commands.command(name="pause")
    async def pause(self, ctx: commands.Context):
        if not ctx.guild:
            return
        vc = ctx.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await ctx.reply("⏸️ Pausado.")
            player = self._get_player(ctx.guild.id)
            await self._update_now_playing_message(ctx.guild.id, player.current)
        else:
            await ctx.reply("❌ Nada tocando para pausar.")

    @commands.command(name="resume")
    async def resume(self, ctx: commands.Context):
        if not ctx.guild:
            return
        vc = ctx.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await ctx.reply("▶️ Retomado.")
            player = self._get_player(ctx.guild.id)
            await self._update_now_playing_message(ctx.guild.id, player.current)
        else:
            await ctx.reply("❌ Nada pausado para retomar.")

    @commands.command(name="stop")
    async def stop(self, ctx: commands.Context):
        if not ctx.guild:
            return
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            await ctx.reply("❌ Não estou em um canal de voz.")
            return
        player = self._get_player(ctx.guild.id)
        await player.stop(keep_queue=False)
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await ctx.reply("⏹️ Parado e fila limpa.")
        if ctx.guild.id in self.now_playing_message:
            await self.now_playing_message[ctx.guild.id].delete()
            del self.now_playing_message[ctx.guild.id]

    @commands.command(name="queue")
    async def queue_cmd(self, ctx: commands.Context):
        if not ctx.guild:
            return
        player = self._get_player(ctx.guild.id)
        items = list(player.queue._queue)
        desc = ""
        if player.current:
            desc += f"🎶 **Agora:** {player.current.title}\n\n"
        if not items:
            desc += "📭 Fila vazia."
        else:
            for i, t in enumerate(items[:10], start=1):
                desc += f"{i}. {t.title} (`{t.format_duration()}`)\n"
            if len(items) > 10:
                desc += f"\n… e mais **{len(items) - 10}** na fila."

        embed = discord.Embed(title="📜 Fila de reprodução", description=desc)
        embed.set_footer(text=f"Loop: {'ON' if player.loop else 'OFF'}  |  Volume: {int(player.volume * 100)}%")
        await ctx.reply(embed=embed)

    @commands.command(name="now")
    async def now(self, ctx: commands.Context):
        if not ctx.guild:
            return
        player = self._get_player(ctx.guild.id)
        if not player.current:
            await ctx.reply("❌ Nada tocando no momento.")
            return
        embed = self._build_now_playing_embed(player.current, player)
        await ctx.reply(embed=embed)

    @commands.command(name="loop")
    async def loop(self, ctx: commands.Context):
        if not ctx.guild:
            return
        player = self._get_player(ctx.guild.id)
        player.loop = not player.loop
        await ctx.reply(f"🔁 Loop: **{'ATIVADO' if player.loop else 'DESATIVADO'}**")
        await self._update_now_playing_message(ctx.guild.id, player.current)

    @commands.command(name="volume")
    async def volume(self, ctx: commands.Context, vol: int):
        if not ctx.guild:
            return
        if not 0 <= vol <= 200:
            await ctx.reply("❌ O volume deve estar entre 0 e 200.")
            return
        player = self._get_player(ctx.guild.id)
        await player.set_volume(vol / 100.0)
        await ctx.reply(f"🔊 Volume ajustado para **{vol}%**.")
        await self._update_now_playing_message(ctx.guild.id, player.current)

    @commands.command(name="shuffle")
    async def shuffle(self, ctx: commands.Context):
        if not ctx.guild:
            return
        player = self._get_player(ctx.guild.id)
        if player.queue.empty():
            await ctx.reply("❌ A fila está vazia.")
            return
        items = list(player.queue._queue)
        random.shuffle(items)
        while not player.queue.empty():
            try:
                player.queue.get_nowait()
                player.queue.task_done()
            except asyncio.QueueEmpty:
                break
        for item in items:
            await player.queue.put(item)
        save_queue(ctx.guild.id, items)
        await ctx.reply("🔀 Fila embaralhada.")

    @commands.command(name="clear")
    async def clear(self, ctx: commands.Context):
        if not ctx.guild:
            return
        player = self._get_player(ctx.guild.id)
        count = 0
        while not player.queue.empty():
            try:
                player.queue.get_nowait()
                player.queue.task_done()
                count += 1
            except asyncio.QueueEmpty:
                break
        clear_queue_file(ctx.guild.id)
        await ctx.reply(f"🧹 Limpei **{count}** músicas da fila (a música atual continua).")

    @commands.command(name="remove")
    async def remove(self, ctx: commands.Context, pos: int):
        if not ctx.guild:
            return
        player = self._get_player(ctx.guild.id)
        if pos < 1:
            await ctx.reply("❌ Posição deve ser maior que 0.")
            return
        items = list(player.queue._queue)
        if pos > len(items):
            await ctx.reply("❌ Posição inválida.")
            return
        removed = items.pop(pos - 1)
        while not player.queue.empty():
            try:
                player.queue.get_nowait()
                player.queue.task_done()
            except asyncio.QueueEmpty:
                break
        for item in items:
            await player.queue.put(item)
        save_queue(ctx.guild.id, items)
        await ctx.reply(f"🗑️ Removido: **{removed.title}**")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.id != self.bot.user.id:
            return
        if after.channel is None:
            guild = member.guild
            if guild.id in self.players:
                player = self.players[guild.id]
                await player.stop(keep_queue=False)
                del self.players[guild.id]
                if guild.id in self.now_playing_message:
                    await self.now_playing_message[guild.id].delete()
                    del self.now_playing_message[guild.id]
                log.info("Bot desconectado do canal, player removido para guild %s", guild.id)

    @tasks.loop(seconds=5.0)  # Reduzido para 5 segundos para evitar spam
    async def update_now_playing(self):
        for guild_id, msg in list(self.now_playing_message.items()):
            player = self.players.get(guild_id)
            if player and player.current:
                try:
                    embed = self._build_now_playing_embed(player.current, player)
                    await msg.edit(embed=embed)
                except Exception:
                    pass

    @update_now_playing.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    cog = Music(bot)
    await bot.add_cog(cog)