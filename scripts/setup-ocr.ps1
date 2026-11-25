# Portable Tesseract: extract trusted binaries; no system installation or elevation.
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskSevenZip = @('C:/Program Files/7-Zip/7z.exe', 'C:/Program Files/NVIDIA Corporation/NVIDIA app/7z.exe', (Get-Command 7z -ErrorAction SilentlyContinue).Source) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $taskSevenZip) { throw 'Для распаковки нужен 7-Zip. Установите его с https://www.7-zip.org/ и повторите запуск.' }
$taskDownloadDir = Join-Path $taskRoot '.tmp/tesseract-install'
New-Item -ItemType Directory -Path $taskDownloadDir -Force | Out-Null
$taskInstaller = Join-Path $taskDownloadDir 'tesseract-ocr-w64-setup-5.4.0.20240606.exe'
$taskExpectedHash = 'C885FFF6998E0608BA4BB8AB51436E1C6775C2BAFC2559A19B423E18678B60C9'
if (-not (Test-Path -LiteralPath $taskInstaller)) {
    Invoke-WebRequest -Uri 'https://github.com/UB-Mannheim/tesseract/releases/download/v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe' -OutFile $taskInstaller
}
if ((Get-FileHash -LiteralPath $taskInstaller -Algorithm SHA256).Hash -ne $taskExpectedHash) { throw 'Контрольная сумма Tesseract не совпадает. Файл не распакован.' }
$taskOcrDir = Join-Path $taskRoot '.tools/Tesseract-OCR'
& $taskSevenZip x -tNSIS -y "-o$taskOcrDir" -r $taskInstaller '*.dll' 'tesseract.exe' 'tessdata/*' 'doc/*' '-x!$PLUGINSDIR/*'
if ($LASTEXITCODE -ne 0) { throw 'Не удалось распаковать Tesseract' }
$taskRussianModel = Join-Path $taskOcrDir 'tessdata/rus.traineddata'
if (-not (Test-Path -LiteralPath $taskRussianModel)) {
    Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/rus.traineddata' -OutFile $taskRussianModel
}
if ((Get-FileHash -LiteralPath $taskRussianModel -Algorithm SHA256).Hash -ne 'E16E5E036CCE1D9EC2B00063CF8B54472625B9E14D893A169E2B0DEDEB4DF225') { throw 'Контрольная сумма русского OCR-модуля не совпадает' }
& (Join-Path $taskRoot '.venv/Scripts/python.exe') -m pip install -r backend/requirements.lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости OCR' }
& (Join-Path $taskOcrDir 'tesseract.exe') --list-langs
if ($LASTEXITCODE -ne 0) { throw 'Tesseract не запускается' }
Write-Host 'Локальный OCR готов. Перезапустите локальный исполнитель.'
