import os

GUILD_ID = 1469341591805366475

CATEGORIA_NOME = "📦 - BAÚ"
LOG_CHANNEL_NAME = "📋-logs-bau"
SET_LOG_CHANNEL = "📋-logs-set"

CARGOS_PERMITIDOS_IDS = [
    1469341592530845756,
    1469341592497422579,
    1469341592497422577,
    1469341592497422576,
    1469341592207757414
]
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

# Se o ffmpeg estiver instalado e no PATH, isso funciona:
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")


# Quem pode REGISTRAR vendas (IDs de cargos)
VENDAS_CARGOS_PERMITIDOS_IDS = CARGOS_PERMITIDOS_IDS
# --- EXPORT VENDAS (CSV / XLSX) ---
VENDAS_EXPORT_CSV_PATH = "data/vendas.csv"
VENDAS_EXPORT_XLSX = True
VENDAS_EXPORT_XLSX_PATH = "data/vendas.xlsx"

# --- VENDAS (GTA RP) ---
VENDAS_CHANNEL_NAME = "💰-vendas"
VENDAS_CATEGORIA_NOME = "💰 - VENDAS"
VENDAS_CURRENCY = "R$"

# Itens (armas) e preços
# - Se tiver preço diferente para família/pista, use preco_familia e preco_pista
# - Se for preço único, use preco
VENDAS_ITENS = {
    "Pistola Five-Seven": {"emoji": "🔫", "preco_familia": 30000, "preco_pista": 40000},
    "Pistola Deagle": {"emoji": "🔫", "preco_familia": 35000, "preco_pista": 45000},
    "Sub Tec-9": {"emoji": "🔫", "preco": 50000},
    "Sub Thompson": {"emoji": "🔫", "preco": 50000},
    "Sub M-Tar X": {"emoji": "🔫", "preco": 70000},
    "SPAS-12": {"emoji": "🔫", "preco": 120000},
    "Fuzil AK-47": {"emoji": "🔫", "preco": 110000},
    "Fuzil AUG": {"emoji": "🔫", "preco": 130000},
    "M16": {"emoji": "🔫", "preco": 160000},
    "Parafal": {"emoji": "🔫", "preco": 160000},
    "Pistola AP (weapon_appistol)": {"emoji": "🔫", "preco": 50000},
}

