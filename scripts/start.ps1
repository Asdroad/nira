param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python' }
    & $taskPython -m pip install -r backend/requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости' }
}
& $taskPython -m alembic -c backend/alembic.ini upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Не удалось обновить базу данных' }
Write-Host "NIRA: http://127.0.0.1:$Port"
Write-Host "API: http://127.0.0.1:$Port/api/docs"
Write-Host 'Для ИИ, поиска и чтения документов запустите scripts/worker.ps1 в отдельном терминале.'
& $taskPython -m uvicorn backend.app.main:app --host 127.0.0.1 --port $Port
