import discord
from discord.ext import commands
from discord import app_commands

def _embed_help() -> discord.Embed:
    emb = discord.Embed(
        title="🧭 Comandos do Bot",
        description=(
            "Lista rápida de comandos.\n\n"
            "📌 **/ = Slash Commands**\n"
            "🎵 **! = Música** (ex: `!play ...`)"
        ),
        color=discord.Color.blurple(),
    )

    emb.add_field(
        name="📋 SET",
        value="`/painelset` → cria o painel de SET (admin)\n"
              "Use o botão do painel para registrar nome/ID e atualizar nickname.",
        inline=False,
    )
    emb.add_field(
        name="📦 BAÚ",
        value="`/painelbau` → cria o painel de baús (admin)\n"
              "Use o botão do painel para criar seu canal privado.",
        inline=False,
    )
    emb.add_field(
        name="💰 VENDAS",
        value="`/painelvendas` → painel interativo para registrar vendas\n"
              "`/registrarvenda` → registra uma venda (modo comando)\n"
              "`/listavendas` → lista vendas (no canal atual)\n"
              "`/exportvendas` → exporta vendas (CSV/XLSX)",
        inline=False,
    )
    emb.add_field(
        name="🏦 ECONOMIA",
        value="`/saldo` → seu saldo\n"
              "`/pagar @membro valor` → transfere saldo\n"
              "`/caixa` → caixa da organização\n"
              "`/addsaldo @membro valor` → (admin) adiciona saldo\n"
              "`/remsaldo @membro valor` → (admin) remove saldo",
        inline=False,
    )
    emb.add_field(
        name="📌 PONTO",
        value="`/painelponto` → cria o painel de ponto com botões\n"
              "`/ponto_abertos` → lista pontos abertos/pausados/pendentes\n"
              "`/ponto_fechar @membro motivo` → fecha ponto com motivo\n"
              "`/ponto_pendentes` → lista pontos pendentes de revisão\n"
              "`/ponto_relatorio_dia` → relatório diário\n"
              "`/ponto_relatorio_semana` → relatório semanal\n"
              "`/ponto_exportar` → exporta registros em CSV",
        inline=False,
    )
    emb.add_field(
        name="🎵 MÚSICA (prefixo !)",
        value="`!entrar` • `!play <nome/link>` • `!pause` • `!resume`\n"
              "`!skip` • `!stop` • `!queue` • `!now` • `!loop` • `!sair`",
        inline=False,
    )
    emb.set_footer(text="Se não aparecer no /, feche e abra o Discord (ou Ctrl+R).")
    return emb

class HelpSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="comandos", description="Mostra a lista de comandos do bot.")
    async def comandos(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=_embed_help(), ephemeral=True)

    @commands.command(name="ajuda")
    async def ajuda_prefix(self, ctx: commands.Context):
        await ctx.reply(embed=_embed_help())

async def setup(bot: commands.Bot):
    await bot.add_cog(HelpSystem(bot))
