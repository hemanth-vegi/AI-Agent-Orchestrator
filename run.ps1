$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python .\orchestrator.py --check
