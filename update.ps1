# Apply an update. Run from the project folder with the server stopped.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) { Write-Host "No .venv here. Wrong folder?" -Fore Red; exit 1 }

# 1. Back up the database first. Everything else is replaceable; this isn't.
New-Item -ItemType Directory -Force -Path backups | Out-Null
if (Test-Path "data\helpdesk.db") {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    Copy-Item "data\helpdesk.db" "backups\helpdesk-$stamp.db"
    Write-Host "  Backed up to backups\helpdesk-$stamp.db" -Fore Green
    # keep the 20 most recent
    Get-ChildItem backups\helpdesk-*.db | Sort-Object LastWriteTime -Desc |
        Select-Object -Skip 20 | Remove-Item -Force
}

# 2. Dependencies
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
Write-Host "  Dependencies current" -Fore Green

# 3. Schema
& .\.venv\Scripts\python.exe -m app.migrate

Write-Host "`n  Done. Start with: python run.py" -Fore Cyan
