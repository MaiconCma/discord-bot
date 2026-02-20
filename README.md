# discord-bot (MC Cma Bot)

Bot de Discord em Python usando **discord.py** com:
- ✅ Painel de **SET** (modal para definir Nickname)
- ✅ Painel de **BAÚ** (cria canal privado por usuário)
- ✅ Sistema de **Música** (YouTube + Spotify *track* → busca no YouTube)
- ✅ Fila, now playing, skip, stop e **autofila**

---

## ✅ Requisitos

- **Python 3.10+** (recomendado)
- **FFmpeg** instalado e disponível no terminal (`ffmpeg -version`)
- Dependências Python:
  - `discord.py`
  - `python-dotenv`
  - `yt-dlp`
  - `spotipy`
  - `PyNaCl` (necessário para voz)

---

## 📁 Estrutura do projeto
├─ main.py  <br>
├─ config.example.py  <br>
├─ .env (NÃO subir no git)  <br>
└─ cogs/  <br>
├─ music.py  <br>
├─ set_system.py  <br>
└─ bau_system.py  <br>


---

## ⚙️ Instalação (Windows / PowerShell)

1) Clone o repositório:
```bash
git clone https://github.com/MaiconCma/discord-bot
cd discord-bot

2) Criar ambiente virtual (venv)
python -m venv .venv

3) Ativar venv (PowerShell)
Se o PowerShell bloquear scripts, libere apenas nesta janela:
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
Agora ative:
.\.venv\Scripts\Activate.ps1

✅ Alternativa (CMD):
.\.venv\Scripts\activate.bat

4) Instalar dependências
python -m pip install -U pip
pip install -U discord.py python-dotenv yt-dlp spotipy PyNaCl

🎧 Instalando FFmpeg (Windows)
Instale via winget:
winget install Gyan.FFmpeg

Teste:
ffmpeg -version

✅ Se funcionar, use no .env:
FFMPEG_PATH=ffmpeg

🔐 Variáveis de ambiente (.env)
Crie um arquivo .env na raiz do projeto (mesmo nível do main.py):
TOKEN=SEU_TOKEN_DO_DISCORD
SPOTIFY_CLIENT_ID=SEU_CLIENT_ID
SPOTIFY_CLIENT_SECRET=SEU_CLIENT_SECRET
FFMPEG_PATH=ffmpeg

Importante: não coloque os.getenv(...) dentro do .env.
O .env é apenas CHAVE=VALOR.

🧾 Configuração (config.py)
✅ No repositório, mantenha apenas o config.example.py.
No seu PC/servidor, crie um arquivo config.py baseado no exemplo:

import os

GUILD_ID = 0
CATEGORIA_NOME = "📦 - BAÚ"
LOG_CHANNEL_NAME = "📋-logs-bau"
SET_LOG_CHANNEL = "📋-logs-set"

CARGOS_PERMITIDOS_IDS = []

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
Como pegar os IDs dos cargos (CARGOS_PERMITIDOS_IDS)

Ative Developer Mode no Discord: Settings → Advanced → Developer Mode

Clique com botão direito no cargo → Copy ID
Cole os IDs na lista:
CARGOS_PERMITIDOS_IDS = [111111111111111111, 222222222222222222]

🤖 Rodando o bot
Com venv ativo e .env configurado:

python main.py
🧰 Comandos disponíveis
🎵 Música (prefixo !)
!play <nome ou link> (aliases: !p, !tocar)
!skip (aliases: !pular, !passar)
!stop (aliases: !parar, !sair)
!queue (aliases: !q, !fila)
!now (aliases: !np, !tocando)
!autofila (alias: !aq)
!autofila on
!autofila off
!ajuda (aliases: !comandos, !cmds)

🧰 Slash Commands (painéis)
/painelset → cria o painel do SET
/painelbau → cria o painel do BAÚ

🛡️ Permissões necessárias no Discord
Para SET (alterar apelido)
Bot precisa de: Manage Nicknames
O cargo do bot deve estar acima dos cargos dos usuários (hierarquia)

Para BAÚ (criar canais privados)
Bot precisa de:
Manage Channels
View Channels
Send Messages
(recomendado) Permissões para setar overwrites

Para Música (voz) Bot precisa de:
Connect
Speak
Use Voice Activity

🔒 Segurança (GitHub)
Nunca suba:
.env
config.py
__pycache__/
.venv/

Exemplo de .gitignore:
.venv/
__pycache__/
*.pyc
.env
.env.*
config.py

🧩 Problemas comuns
401 Unauthorized (token inválido)
Gere um novo token no Discord Developer Portal → Bot → Reset Token

Coloque no .env sem aspas:
TOKEN=SEU_TOKEN

FFmpeg não encontrado
Confirme: ffmpeg -version

Use no .env: FFMPEG_PATH=ffmpeg
pip não é reconhecido

Use:
python -m pip install -U pip
🚀 Deploy (ideia rápida)

Você pode rodar em:
PC local
VPS/Hostinger (Linux/Windows)
Serviços free (com limites): Railway/Render (dependendo do plano)
📌 Licença
Uso pessoal.
