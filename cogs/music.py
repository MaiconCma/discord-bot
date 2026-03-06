import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Dict, List

import discord
from discord.ext import commands

import config

try:
    import yt_dlp
except Exception:
    yt_dlp = None


log = logging.getLogger("music")

YTDLP_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_BEFORE_OPTS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = "-vn"


@dataclass
class Track:
    title: str
    webpage_url: str
    stream_url: str
    requested_by: int
    duration: Optional[int] = None  # seconds


class GuildPlayer:
    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.current: Optional[Track] = None
        self.loop: bool = False

        self._task: Optional[asyncio.Task] = None
        self._next = asyncio.Event()

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
        self._task = None
        self.current = None
        self.loop = False
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except Exception:
                break

    def signal_next(self):
        self._next.set()

    def _get_vc(self) -> Optional[discord.VoiceClient]:
        guild = self.bot.get_guild(self.guild_id)
        if not guild:
            return None
        return guild.voice_client

    async def _loop(self):
        while True:
            self._next.clear()
            vc = self._get_vc()
            if not vc or not vc.is_connected():
                self.current = None
                await asyncio.sleep(1)
                continue

            if self.loop and self.current is not None:
                track = self.current
            else:
                track = await self.queue.get()
                self.current = track

            try:
                await self._play(vc, track)
                await self._next.wait()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Player loop error: %r", e)
                await asyncio.sleep(1)
                self.signal_next()

            if not self.loop:
                try:
                    self.queue.task_done()
                except Exception:
                    pass

    async def _play(self, vc: discord.VoiceClient, track: Track):
        ffmpeg_path = getattr(config, "FFMPEG_PATH", "ffmpeg")

        source = discord.FFmpegPCMAudio(
            track.stream_url,
            before_options=FFMPEG_BEFORE_OPTS,
            options=FFMPEG_OPTS,
            executable=ffmpeg_path,
        )

        def _after(err: Optional[Exception]):
            if err:
                log.warning("Audio error: %r", err)
            try:
                self.bot.loop.call_soon_threadsafe(self.signal_next)
            except Exception:
                pass

        vc.play(source, after=_after)


class Music(commands.Cog):
    """Sistema de música por prefixo ! (sem Lavalink)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: Dict[int, GuildPlayer] = {}

    def _player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(self.bot, guild_id)
        return self.players[guild_id]

    def _ytdlp(self):
        if yt_dlp is None:
            raise RuntimeError("yt-dlp não está instalado. Rode: pip install -U yt-dlp")
        return yt_dlp.YoutubeDL(YTDLP_OPTS)

    async def _extract(self, query: str) -> Track:
        def _run():
            with self._ytdlp() as ydl:
                info = ydl.extract_info(query, download=False)

            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            title = info.get("title") or "Sem título"
            webpage_url = info.get("webpage_url") or info.get("original_url") or query
            stream_url = info.get("url")
            duration = info.get("duration")
            return title, webpage_url, stream_url, duration

        title, webpage_url, stream_url, duration = await asyncio.to_thread(_run)
        if not stream_url:
            raise RuntimeError("Não consegui obter o link de áudio (stream_url vazio).")
        return Track(title=title, webpage_url=webpage_url, stream_url=stream_url, requested_by=0, duration=duration)

    def _fmt_dur(self, seconds: Optional[int]) -> str:
        if not seconds:
            return "?"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    async def _ensure_voice(self, ctx: commands.Context) -> Optional[discord.VoiceClient]:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            await ctx.reply("❌ Use isso dentro de um servidor.")
            return None

        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.reply("❌ Entre em um canal de voz primeiro.")
            return None

        channel = ctx.author.voice.channel
        vc = ctx.guild.voice_client

        if vc and vc.is_connected() and vc.channel and vc.channel.id != channel.id:
            await ctx.reply("❌ Eu já estou em outro canal de voz.")
            return None

        if vc is None or not vc.is_connected():
            try:
                vc = await channel.connect(self_deaf=True)
            except discord.Forbidden:
                await ctx.reply("❌ Sem permissão para conectar/falar no canal.")
                return None
            except Exception as e:
                await ctx.reply(f"❌ Erro ao conectar: `{e}`")
                return None

        return vc

    # ----------------- COMMANDS (!prefix) -----------------

    @commands.command(name="entrar")
    async def entrar(self, ctx: commands.Context):
        vc = await self._ensure_voice(ctx)
        if vc:
            await ctx.reply(f"✅ Conectado em **{vc.channel.name}**.")

    @commands.command(name="sair")
    async def sair(self, ctx: commands.Context):
        if not ctx.guild:
            return
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            return await ctx.reply("❌ Eu não estou em voz.")

        player = self._player(ctx.guild.id)
        await player.stop()
        await vc.disconnect(force=True)
        await ctx.reply("✅ Saí do canal e limpei a fila.")

    @commands.command(name="play")
    async def play(self, ctx: commands.Context, *, query: str):
        """!play <texto ou link>"""
        if not ctx.guild:
            return

        msg = await ctx.reply("🔎 Buscando...")

        vc = await self._ensure_voice(ctx)
        if not vc:
            return

        player = self._player(ctx.guild.id)
        player.start()

        try:
            track = await self._extract(query)
            track.requested_by = ctx.author.id
        except Exception as e:
            return await msg.edit(content=f"❌ Não consegui carregar: `{e}`")

        await player.queue.put(track)

        emb = discord.Embed(title="✅ Adicionado na fila", description=f"[{track.title}]({track.webpage_url})")
        emb.add_field(name="Duração", value=self._fmt_dur(track.duration), inline=True)
        emb.add_field(name="Pedido por", value=f"<@{track.requested_by}>", inline=True)
        await msg.edit(content=None, embed=emb)

        if not vc.is_playing() and not vc.is_paused():
            player.signal_next()

    @commands.command(name="skip")
    async def skip(self, ctx: commands.Context):
        if not ctx.guild:
            return
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            return await ctx.reply("❌ Eu não estou em voz.")
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await ctx.reply("⏭️ Pulei.")
        else:
            await ctx.reply("❌ Não tem nada tocando.")

    @commands.command(name="pause")
    async def pause(self, ctx: commands.Context):
        if not ctx.guild:
            return
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            return await ctx.reply("❌ Eu não estou em voz.")
        if vc.is_playing():
            vc.pause()
            await ctx.reply("⏸️ Pausado.")
        else:
            await ctx.reply("❌ Nada tocando para pausar.")

    @commands.command(name="resume")
    async def resume(self, ctx: commands.Context):
        if not ctx.guild:
            return
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            return await ctx.reply("❌ Eu não estou em voz.")
        if vc.is_paused():
            vc.resume()
            await ctx.reply("▶️ Retomado.")
        else:
            await ctx.reply("❌ Não está pausado.")

    @commands.command(name="stop")
    async def stop(self, ctx: commands.Context):
        if not ctx.guild:
            return
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            return await ctx.reply("❌ Eu não estou em voz.")
        player = self._player(ctx.guild.id)
        await player.stop()
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await ctx.reply("⏹️ Parei e limpei a fila.")

    @commands.command(name="queue")
    async def queue_cmd(self, ctx: commands.Context):
        if not ctx.guild:
            return
        player = self._player(ctx.guild.id)
        items: List[Track] = list(player.queue._queue)  # ok só pra leitura

        desc = ""
        if player.current:
            desc += f"🎶 **Agora:** {player.current.title}\n\n"

        if not items:
            desc += "📭 Fila vazia."
        else:
            for i, t in enumerate(items[:10], start=1):
                desc += f"{i}. {t.title} (`{self._fmt_dur(t.duration)}`)\n"
            if len(items) > 10:
                desc += f"\n… e mais **{len(items) - 10}** na fila."

        emb = discord.Embed(title="📜 Fila", description=desc)
        emb.set_footer(text=f"Loop: {'ON' if player.loop else 'OFF'}")
        await ctx.reply(embed=emb)

    @commands.command(name="now")
    async def now(self, ctx: commands.Context):
        if not ctx.guild:
            return
        player = self._player(ctx.guild.id)
        if not player.current:
            return await ctx.reply("❌ Nada tocando.")
        t = player.current
        emb = discord.Embed(title="🎶 Tocando agora", description=f"[{t.title}]({t.webpage_url})")
        emb.add_field(name="Duração", value=self._fmt_dur(t.duration), inline=True)
        emb.add_field(name="Pedido por", value=f"<@{t.requested_by}>", inline=True)
        await ctx.reply(embed=emb)

    @commands.command(name="loop")
    async def loop(self, ctx: commands.Context):
        if not ctx.guild:
            return
        player = self._player(ctx.guild.id)
        player.loop = not player.loop
        await ctx.reply(f"🔁 Loop: **{'ON' if player.loop else 'OFF'}**")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
