# Mata processos pythonw existentes
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force

# Aguarda um segundo
Start-Sleep -Seconds 1

# Inicia o bot em segundo plano
.venv\Scripts\pythonw.exe main.py

Write-Host "Bot iniciado em segundo plano. PID:" (Get-Process pythonw).Id