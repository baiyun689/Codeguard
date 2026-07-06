# Codeguard CI Gateway 一键启动脚本
# 用法: .\start-ci.ps1

$ErrorActionPreference = "Stop"

# 1. 杀掉旧进程
Write-Host ">>> 清理旧 Java 进程..." -ForegroundColor Yellow
Get-Process java -ErrorAction SilentlyContinue | Stop-Process -Force

# 2. 从项目根 .env 加载 LLM 配置
Write-Host ">>> 加载 .env 配置..." -ForegroundColor Yellow
$envFile = Join-Path $PSScriptRoot "..\..\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | Where-Object { $_ -match '^CODEGUARD_' -and $_ -notmatch '^\s*#' } | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2])
        }
    }
} else {
    Write-Warning ".env 文件不存在: $envFile，请先配好 LLM 密钥"
}

# 3. CI 专用变量（本地测试用，生产放 docker-compose）
$env:CODEGUARD_WEBHOOK_SECRET = "test123"
$env:CODEGUARD_PYTHON = "E:\ProgramData\Anaconda3\envs\codeguard\python.exe"
$env:CODEGUARD_REACT_RECURSION_LIMIT = "8"

# 4. GitHub App（从独立文件读，避免硬编码在脚本里）
$appIdFile = Join-Path $PSScriptRoot "github-app-id.txt"
$pemFile = Join-Path $PSScriptRoot "github-app.pem"
if ((Test-Path $appIdFile) -and (Test-Path $pemFile)) {
    $env:CODEGUARD_GITHUB_APP_ID = (Get-Content $appIdFile).Trim()
    $env:CODEGUARD_GITHUB_PRIVATE_KEY = Get-Content $pemFile -Raw
    Write-Host ">>> GitHub App 配置已加载 (App ID: $env:CODEGUARD_GITHUB_APP_ID)" -ForegroundColor Green
} else {
    Write-Warning "GitHub App 未配置，审查结果不会回写到 PR。"
    Write-Warning "把 App ID 写入 $appIdFile，私钥写入 $pemFile"
}

# 5. 清理上次的 H2 数据（测试阶段每次启动清空）
$h2db = Join-Path $PSScriptRoot "data\codeguard-jobs.mv.db"
if (Test-Path $h2db) {
    Remove-Item $h2db -Force
    Write-Host ">>> 已清理旧的 H2 数据库" -ForegroundColor Yellow
}

# 6. 启动
Write-Host ">>> 启动 Gateway..." -ForegroundColor Green
java -jar target\codeguard-gateway.jar
