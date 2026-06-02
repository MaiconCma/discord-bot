# =============================================
# Reiniciar Discord Bot
# =============================================

$BOT_PATH = "C:\Users\secre\Documents\bot\discord-bot"

Write-Host "Encerrando processos Python do Discord Bot..." -ForegroundColor Yellow

# Mata apenas processos python relacionados ao bot
Get-CimInstance Win32_Process |
Where-Object {
    ($_.Name -like "python*.exe") -and $_.CommandLine -like "*discord-bot*"
} |
ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 2

Write-Host "Iniciando Discord bot em segundo plano..." -ForegroundColor Green

Set-Location $BOT_PATH

# Caminhos possíveis do Python
$pythonw = Join-Path $BOT_PATH ".venv\Scripts\pythonw.exe"
$python = Join-Path $BOT_PATH ".venv\Scripts\python.exe"

# Verifica qual existe
if (Test-Path $pythonw) {

    Start-Process -WindowStyle Hidden `
        -FilePath $pythonw `
        -ArgumentList "main.py"

}
elseif (Test-Path $python) {

    Start-Process -WindowStyle Hidden `
        -FilePath $python `
        -ArgumentList "main.py"

}
else {

    Write-Host "❌ Nenhum Python encontrado na .venv" -ForegroundColor Red
    Write-Host "Esperado em:" -ForegroundColor Yellow
    Write-Host $pythonw
    Write-Host $python
    exit

}

Start-Sleep -Seconds 3

# Verifica se iniciou
$process = Get-CimInstance Win32_Process |
Where-Object {
    ($_.Name -like "python*.exe") -and $_.CommandLine -like "*main.py*"
}

if ($process) {
    Write-Host "✅ Discord bot iniciado com sucesso." -ForegroundColor Green
}
else {
    Write-Host "❌ Falha ao iniciar Discord bot." -ForegroundColor Red
}