from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, time
from pathlib import Path
from typing import Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config

# ========== UTILITÁRIOS ==========
def _has_permission(interaction: discord.Interaction) -> bool:
    """Verifica se o usuário tem cargo permitido (ou é admin)."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    allowed = getattr(config, "ANIME_ALLOWED_ROLES", [])
    user_role_ids = {r.id for r in interaction.user.roles}
    return any(rid in allowed for rid in user_role_ids)


def _get_db_connection() -> sqlite3.Connection:
    db_path = getattr(config, "ANIME_DB_PATH", "data/animes.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _get_db_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS animes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                dia_semana TEXT NOT NULL,
                link TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ativo'
            )
        ''')
        conn.commit()


def add_anime(nome: str, dia_semana: str, link: str) -> int:
    with _get_db_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO animes (nome, dia_semana, link, status) VALUES (?, ?, ?, 'ativo')",
            (nome, dia_semana, link)
        )
        conn.commit()
        return cursor.lastrowid


def update_anime_status(nome: str, novo_status: str) -> bool:
    with _get_db_connection() as conn:
        cursor = conn.execute(
            "UPDATE animes SET status = ? WHERE nome = ?",
            (novo_status, nome)
        )
        conn.commit()
        return cursor.rowcount > 0


def list_animes() -> list[dict]:
    with _get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, nome, dia_semana, link, status FROM animes ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def get_animes_por_dia(dia_semana: str) -> list[dict]:
    with _get_db_connection() as conn:
        rows = conn.execute(
            "SELECT nome, link FROM animes WHERE dia_semana = ? AND status = 'ativo'",
            (dia_semana,)
        ).fetchall()
    return [dict(row) for row in rows]


def delete_anime(nome: str) -> bool:
    with _get_db_connection() as conn:
        cursor = conn.execute("DELETE FROM animes WHERE nome = ?", (nome,))
        conn.commit()
        return cursor.rowcount > 0


# ========== CRIAÇÃO AUTOMÁTICA DE CANAIS ==========
async def _ensure_anime_channels(guild: discord.Guild) -> Tuple[discord.TextChannel, discord.TextChannel]:
    """Garante que a categoria e os canais existam. Retorna (canal_feed, canal_output)."""
    cat_name = getattr(config, "ANIME_CATEGORY_NAME", "📺 - ANIMES")
    feed_name = getattr(config, "ANIME_FEED_CHANNEL_NAME", "🍱-alimentacao-animes")
    output_name = getattr(config, "ANIME_OUTPUT_CHANNEL_NAME", "📢-lembretes-animes")

    category = discord.utils.get(guild.categories, name=cat_name)
    if not category:
        category = await guild.create_category(cat_name)

    feed_ch = discord.utils.get(category.text_channels, name=feed_name)
    if not feed_ch:
        feed_ch = await guild.create_text_channel(feed_name, category=category)

    output_ch = discord.utils.get(category.text_channels, name=output_name)
    if not output_ch:
        output_ch = await guild.create_text_channel(output_name, category=category)

    return feed_ch, output_ch


# ========== MODAIS ==========
class AddAnimeModal(discord.ui.Modal, title="Adicionar Anime"):
    nome = discord.ui.TextInput(label="Nome do anime", placeholder="Ex: One Piece", max_length=100)
    dia = discord.ui.TextInput(
        label="Dia da semana (segunda a domingo)",
        placeholder="Ex: segunda, terca, quarta, ...",
        max_length=10
    )
    link = discord.ui.TextInput(label="Link (URL do episódio / imagem)", placeholder="https://...", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        if not _has_permission(interaction):
            return await interaction.response.send_message("❌ Você não tem permissão.", ephemeral=True)

        dia_valido = self.dia.value.lower().strip()
        dias_validos = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]
        if dia_valido not in dias_validos:
            return await interaction.response.send_message(
                f"❌ Dia inválido. Use: {', '.join(dias_validos)}", ephemeral=True
            )

        nome_anime = self.nome.value.strip()
        link = self.link.value.strip() or "Sem link"

        add_anime(nome_anime, dia_valido, link)
        await interaction.response.send_message(
            f"✅ Anime **{nome_anime}** adicionado com lembretes às **{dia_valido}s**.",
            ephemeral=True
        )


class EditStatusModal(discord.ui.Modal, title="Alterar Status do Anime"):
    nome = discord.ui.TextInput(label="Nome do anime", placeholder="Nome exato do anime", max_length=100)
    status = discord.ui.TextInput(
        label="Novo status",
        placeholder="ativo / acabou / hiato",
        max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not _has_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)

        nome_anime = self.nome.value.strip()
        novo_status = self.status.value.strip().lower()
        if novo_status not in ["ativo", "acabou", "hiato"]:
            return await interaction.response.send_message(
                "Status inválido. Use: ativo, acabou ou hiato.", ephemeral=True
            )

        if update_anime_status(nome_anime, novo_status):
            await interaction.response.send_message(
                f"✅ Anime **{nome_anime}** agora está **{novo_status}**.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ Anime **{nome_anime}** não encontrado.", ephemeral=True
            )


# ========== VIEW COM BOTÕES ==========
class AnimeReminderView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ Adicionar Anime", style=discord.ButtonStyle.success, custom_id="anime_reminder:add")
    async def add_button(self, interaction: discord.Interaction, _):
        if not _has_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await interaction.response.send_modal(AddAnimeModal())

    @discord.ui.button(label="✏️ Alterar Status", style=discord.ButtonStyle.primary, custom_id="anime_reminder:status")
    async def status_button(self, interaction: discord.Interaction, _):
        if not _has_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await interaction.response.send_modal(EditStatusModal())

    @discord.ui.button(label="📋 Listar Animes", style=discord.ButtonStyle.secondary, custom_id="anime_reminder:list")
    async def list_button(self, interaction: discord.Interaction, _):
        if not _has_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)

        animes = list_animes()
        if not animes:
            return await interaction.response.send_message("📭 Nenhum anime cadastrado.", ephemeral=True)

        msg = "**📺 Lista de Animes:**\n"
        for a in animes:
            status_emoji = {"ativo": "🟢", "acabou": "🔴", "hiato": "🟡"}.get(a["status"], "⚪")
            msg += f"{status_emoji} **{a['nome']}** – {a['dia_semana']}s – {a['status']}\n   Link: {a['link']}\n"
        await interaction.response.send_message(msg, ephemeral=True)


# ========== COG PRINCIPAL ==========
class AnimeReminderSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()
        self.view = AnimeReminderView()
        bot.add_view(self.view)

        self.output_channel_id: Optional[int] = None

        if bot.is_ready():
            self.lembrete_diario.start()
        else:
            bot.loop.create_task(self._start_reminder_when_ready())

    async def _start_reminder_when_ready(self):
        await self.bot.wait_until_ready()
        self.lembrete_diario.start()

    async def _get_output_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """Obtém o canal de saída (cria se não existir)."""
        _, output_ch = await _ensure_anime_channels(guild)
        return output_ch

    @tasks.loop(time=time(
        hour=getattr(config, "ANIME_REMINDER_HOUR", 12),
        minute=getattr(config, "ANIME_REMINDER_MINUTE", 0)
    ))
    async def lembrete_diario(self):
        await self.bot.wait_until_ready()
        
        hoje = datetime.now().strftime("%A").lower()
        traducoes = {
            "monday": "segunda", "tuesday": "terca", "wednesday": "quarta",
            "thursday": "quinta", "friday": "sexta", "saturday": "sabado", "sunday": "domingo",
            "segunda-feira": "segunda", "terça-feira": "terca", "quarta-feira": "quarta",
            "quinta-feira": "quinta", "sexta-feira": "sexta", "sábado": "sabado", "domingo": "domingo"
        }
        hoje = traducoes.get(hoje, hoje)

        # Executando a consulta DB fora do loop de guilds usando thread
        animes = await asyncio.to_thread(get_animes_por_dia, hoje)
        if not animes:
            return

        embed = discord.Embed(
            title=f"📺 Lembretes de hoje – {hoje.capitalize()}",
            description="Os seguintes episódios saem hoje:",
            color=discord.Color.purple()
        )
        for anime in animes:
            nome = anime["nome"]
            link = anime["link"]
            embed.add_field(name=nome, value=f"[Link]({link})", inline=False)

        # Correção: Iterar por todas as guilds para enviar os lembretes adequadamente
        for guild in self.bot.guilds:
            try:
                canal = await self._get_output_channel(guild)
                if canal:
                    await canal.send(embed=embed)
            except Exception as e:
                print(f"⚠️ Erro ao enviar lembrete na guild {guild.name}: {e}")

    @lembrete_diario.before_loop
    async def before_reminder(self):
        await self.bot.wait_until_ready()

    # ========== COMANDOS SLASH ==========
    @app_commands.command(name="painelanime", description="Cria o painel de gerenciamento de animes (administradores)")
    async def painelanime(self, interaction: discord.Interaction):
        if not _has_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão para criar o painel.", ephemeral=True)

        if not interaction.guild:
            return await interaction.response.send_message("❌ Este comando só funciona em servidores.", ephemeral=True)

        # Garante que os canais existam
        feed_ch, output_ch = await _ensure_anime_channels(interaction.guild)

        embed = discord.Embed(
            title="📺 Sistema de Lembretes de Animes",
            description=f"Use os botões abaixo para gerenciar os animes.\n\n"
                        f"📅 O bot enviará um lembrete diário no canal {output_ch.mention}.",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, view=self.view)

    @app_commands.command(name="listaranimes", description="Lista todos os animes cadastrados (alternativa ao botão)")
    async def listaranimes(self, interaction: discord.Interaction):
        if not _has_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)

        animes = list_animes()
        if not animes:
            return await interaction.response.send_message("📭 Nenhum anime cadastrado.", ephemeral=True)

        msg = "**📺 Lista de Animes:**\n"
        for a in animes:
            status_emoji = {"ativo": "🟢", "acabou": "🔴", "hiato": "🟡"}.get(a["status"], "⚪")
            msg += f"{status_emoji} **{a['nome']}** – {a['dia_semana']}s – {a['status']}\n   Link: {a['link']}\n"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="removeranime", description="Remove um anime do sistema (cuidado!)")
    @app_commands.describe(nome="Nome exato do anime a remover")
    async def removeranime(self, interaction: discord.Interaction, nome: str):
        if not _has_permission(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)

        if delete_anime(nome.strip()):
            await interaction.response.send_message(f"🗑️ Anime **{nome}** removido com sucesso.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Anime **{nome}** não encontrado.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AnimeReminderSystem(bot))