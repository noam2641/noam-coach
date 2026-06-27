param(
    [string]$LocalUrl = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    Write-Host "cloudflared לא נמצא." -ForegroundColor Yellow
    Write-Host "אפשר להתקין ב-Windows עם:" -ForegroundColor Yellow
    Write-Host "  winget install --id Cloudflare.cloudflared" -ForegroundColor Cyan
    exit 1
}

Write-Host "פותח Cloudflare Quick Tunnel אל $LocalUrl" -ForegroundColor Green
Write-Host "לאחר שמתקבלת כתובת HTTPS, העתק אותה ל-PUBLIC_BASE_URL בקובץ .env והפעל מחדש את הבוט." -ForegroundColor Yellow
cloudflared tunnel --url $LocalUrl
