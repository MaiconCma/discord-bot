from __future__ import annotations

import sqlite3
import logging
from pathlib import Path
from typing import Optional, List, Dict, Tuple

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

DB_PATH = "data/bolao_copa.db"

# ==================== BANCO DE DADOS ====================
def get_db_connection() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jogos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time_a TEXT NOT NULL,
                time_b TEXT NOT NULL,
                placar_a INTEGER,
                placar_b INTEGER,
                status TEXT NOT NULL DEFAULT 'aberto' -- 'aberto', 'finalizado'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS palpites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                jogo_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                palpite_a INTEGER NOT NULL,
                palpite_b INTEGER NOT NULL,
                UNIQUE(jogo_id, user_id),
                FOREIGN KEY(jogo_id) REFERENCES jogos(id) ON DELETE CASCADE
            )
        """)
        conn.commit()

# ==================== MODAL DE PALPITE ====================
class PalpiteModal(discord.ui.Modal):
    def __init__(self, jogo_id: int, time_a: str, time_b: str):
        super().__init__(title=f"Palpite: {time_a} x {time_b}")
        self.jogo_id = jogo_id
        self.time_a = time_a
        self.time_b = time_b

        self.placar_a = discord.ui.TextInput(
            label=f"Gols do {time_a}",
            placeholder="Ex: 2",
            required=True,
            max_length=2,
        )
        self.placar_b = discord.ui.TextInput(
            label=f"Gols do {time_b}",
            placeholder="Ex: 1",
            required=True,
            max_length=2,
        )
        self.add_item(self.placar_a)
        self.add_item(self.placar_b)

    async def on_submit(self, interaction: discord.Interaction):
        # Validação simples
        if not self.placar_a.value.isdigit() or not self.placar_b.value.isdigit():
            return await interaction.response.send_message(
                "❌ Os placares devem ser números válidos!", ephemeral=True
            )

        gols_a = int(self.placar_a.value)
        gols_b = int(self.placar_b.value)

        # Verificar se o jogo ainda está aberto
        with get_db_connection() as conn:
            jogo = conn.execute("SELECT status FROM jogos WHERE id = ?", (self.jogo_id,)).fetchone()
            if not jogo:
                return await interaction.response.send_message("❌ Jogo não encontrado.", ephemeral=True)
            if jogo["status"] != "aberto":
                return await interaction.response.send_message("❌ Este jogo já está fechado para palpites!", ephemeral=True)

            # Salvar ou atualizar o palpite
            conn.execute(
                """
                INSERT INTO palpites (jogo_id, user_id, palpite_a, palpite_b)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(jogo_id, user_id) DO UPDATE SET palpite_a = excluded.palpite_a, palpite_b = excluded.palpite_b
                """,
                (self.jogo_id, interaction.user.id, gols_a, gols_b)
            )
            conn.commit()

        await interaction.response.send_message(
            f"✅ Seu palpite para **{self.time_a} {gols_a} x {gols_b} {self.time_b}** foi registrado com sucesso!",
            ephemeral=True
        )

# ==================== VIEW PERSISTENTE DO PAINEL ====================
class CopaPainelSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Escolha o jogo para dar seu palpite...",
            min_values=1,
            max_values=1,
            custom_id="copa_painel_select"
        )

    async def callback(self, interaction: discord.Interaction):
        jogo_id = int(self.values[0])

        with get_db_connection() as conn:
            jogo = conn.execute("SELECT * FROM jogos WHERE id = ?", (jogo_id,)).fetchone()

        if not jogo:
            return await interaction.response.send_message("❌ Jogo não encontrado.", ephemeral=True)

        if jogo["status"] != "aberto":
            return await interaction.response.send_message("❌ Este jogo já está fechado para palpites!", ephemeral=True)

        # Abre o formulário pop-up (Modal)
        await interaction.response.send_modal(
            PalpiteModal(jogo_id=jogo["id"], time_a=jogo["time_a"], time_b=jogo["time_b"])
        )

class CopaPainelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.select_menu = CopaPainelSelect()
        self.add_item(self.select_menu)

    def atualizar_opcoes(self) -> bool:
        """Atualiza as opções do select menu com os jogos abertos. Retorna True se houver jogos."""
        with get_db_connection() as conn:
            jogos_abertos = conn.execute(
                "SELECT * FROM jogos WHERE status = 'aberto' ORDER BY id DESC"
            ).fetchall()

        options = []
        for jogo in jogos_abertos:
            options.append(
                discord.SelectOption(
                    label=f"{jogo['time_a']} x {jogo['time_b']}",
                    value=str(jogo["id"]),
                    description=f"Dê o seu palpite para o jogo ID: {jogo['id']}",
                    emoji="⚽"
                )
            )

        self.select_menu.options = options
        # Desabilita se não houver opções
        if not options:
            self.select_menu.disabled = True
            self.select_menu.placeholder = "Não há jogos abertos para palpites no momento"
            return False
        else:
            self.select_menu.disabled = False
            self.select_menu.placeholder = "Escolha o jogo para dar seu palpite..."
            return True

# ==================== COG DO SISTEMA ====================
class BolaoCopaSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()
        # Registra a view persistente
        self.bot.add_view(CopaPainelView())

    # --- COMANDOS ADMINISTRATIVOS ---
    @app_commands.command(name="copa_criar_jogo", description="[ADMIN] Adiciona um novo jogo ao bolão da Copa.")
    @app_commands.checks.has_permissions(administrator=True)
    async def copa_criar_jogo(self, interaction: discord.Interaction, time_a: str, time_b: str):
        with get_db_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO jogos (time_a, time_b, status) VALUES (?, ?, 'aberto')",
                (time_a, time_b)
            )
            jogo_id = cursor.lastrowid
            conn.commit()

        await interaction.response.send_message(
            f"✅ Jogo **ID {jogo_id}**: **{time_a} x {time_b}** adicionado com sucesso!",
            ephemeral=True
        )

    @app_commands.command(name="copa_listar_jogos", description="Lista todos os jogos da Copa cadastrados.")
    async def copa_listar_jogos(self, interaction: discord.Interaction):
        with get_db_connection() as conn:
            jogos = conn.execute("SELECT * FROM jogos ORDER BY id DESC").fetchall()

        if not jogos:
            return await interaction.response.send_message("📭 Nenhum jogo cadastrado.", ephemeral=True)

        embed = discord.Embed(
            title="🏆 Jogos da Copa - Bolão",
            color=discord.Color.gold()
        )

        for jogo in jogos:
            status_str = "🟢 Aberto" if jogo["status"] == "aberto" else f"🔴 Finalizado ({jogo['placar_a']} x {jogo['placar_b']})"
            embed.add_field(
                name=f"ID {jogo['id']}: {jogo['time_a']} x {jogo['time_b']}",
                value=f"Status: **{status_str}**",
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="copa_painel", description="[ADMIN] Envia o painel de palpites para o canal atual.")
    @app_commands.checks.has_permissions(administrator=True)
    async def copa_painel(self, interaction: discord.Interaction):
        view = CopaPainelView()
        tem_jogos = view.atualizar_opcoes()

        embed = discord.Embed(
            title="🏆 BOLÃO DA COPA DO MUNDO 🏆",
            description=(
                "Participe do nosso bolão oficial! Selecione o jogo abaixo e dê seu palpite.\n\n"
                "⚽ **Como participar:**\n"
                "1. Escolha o jogo no menu abaixo.\n"
                "2. Digite o placar exato no formulário que se abrir.\n"
                "3. Você pode alterar seu palpite selecionando o jogo novamente enquanto ele estiver aberto.\n\n"
                "Boa sorte!"
            ),
            color=discord.Color.green()
        )
        embed.set_footer(text="Palpites fechados no início da partida.")

        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="copa_encerrar", description="[ADMIN] Encerra um jogo, define o placar e anuncia os ganhadores.")
    @app_commands.checks.has_permissions(administrator=True)
    async def copa_encerrar(self, interaction: discord.Interaction, jogo_id: int, placar_a: int, placar_b: int):
        await interaction.response.defer()

        with get_db_connection() as conn:
            jogo = conn.execute("SELECT * FROM jogos WHERE id = ?", (jogo_id,)).fetchone()

            if not jogo:
                return await interaction.followup.send("❌ Jogo não encontrado.")

            if jogo["status"] != "aberto":
                return await interaction.followup.send("❌ Este jogo já está finalizado.")

            # Atualiza status e placar do jogo
            conn.execute(
                "UPDATE jogos SET status = 'finalizado', placar_a = ?, placar_b = ? WHERE id = ?",
                (placar_a, placar_b, jogo_id)
            )

            # Busca todos os palpites
            palpites = conn.execute(
                "SELECT * FROM palpites WHERE jogo_id = ?",
                (jogo_id,)
            ).fetchall()

            conn.commit()

        vencedores = []
        for p in palpites:
            if p["palpite_a"] == placar_a and p["palpite_b"] == placar_b:
                vencedores.append(f"<@{p['user_id']}>")

        # Mensagem de encerramento
        embed = discord.Embed(
            title="🎉 FIM DE JOGO E APURAÇÃO 🎉",
            description=f"O confronto **{jogo['time_a']} x {jogo['time_b']}** terminou!",
            color=discord.Color.blue()
        )
        embed.add_field(name="Placar Final", value=f"⚽ **{jogo['time_a']} {placar_a} x {placar_b} {jogo['time_b']}**", inline=False)

        if vencedores:
            embed.add_field(name="🏆 Ganhadores do Placar Exato", value=", ".join(vencedores), inline=False)
            content = f"🏆 Parabéns aos vencedores do Bolão: {', '.join(vencedores)}!"
        else:
            embed.add_field(name="😢 Ganhadores", value="Ninguém acertou o placar exato deste jogo.", inline=False)
            content = "Ninguém acertou o placar exato deste jogo!"

        await interaction.followup.send(content=content, embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(BolaoCopaSystem(bot))
