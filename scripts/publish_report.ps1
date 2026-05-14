param(
  [string]$Message = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  throw "Git command was not found. Install Git or add it to PATH, then run this script again."
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  throw "Python command was not found. Install Python or add it to PATH, then run this script again."
}

python report.py

$LatestReport = Get-ChildItem "report_out" -Filter "report_*.html" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1

if ($null -eq $LatestReport) {
  throw "No report_*.html file was found in report_out."
}

Copy-Item $LatestReport.FullName "report_out/index.html" -Force
Write-Host "Prepared report_out/index.html from $($LatestReport.Name)"

git add report.py requirements.txt .github/workflows/pages.yml scripts/publish_report.ps1 report_out

git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
  Write-Host "No changes to commit."
  exit 0
}

if ([string]::IsNullOrWhiteSpace($Message)) {
  $Message = "Update report $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
}

git commit -m $Message
git push
