# Codeguard CI Gateway 本地开发启动脚本
# 用法: .\start-ci.ps1

$ErrorActionPreference = "Stop"

# 仅用项目 .env 补齐当前 shell 未显式设置的 CODEGUARD_* 配置。
$envFile = Join-Path $PSScriptRoot "..\..\.env"
if (Test-Path $envFile) {
    Write-Host ">>> 加载 .env 配置..." -ForegroundColor Yellow
    Get-Content $envFile | Where-Object { $_ -match '^\s*CODEGUARD_[^=]+=' -and $_ -notmatch '^\s*#' } | ForEach-Object {
        if ($_ -match '^\s*(CODEGUARD_[^=]+)=(.*)$') {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim()
            if (-not (Test-Path "Env:$name")) {
                Set-Item -Path "Env:$name" -Value $value
            }
        }
    }
} else {
    Write-Warning ".env 文件不存在: $envFile；将只使用当前 shell 环境变量。"
}

$jar = Join-Path $PSScriptRoot "target\codeguard-gateway.jar"
if (-not (Test-Path $jar)) {
    throw "Gateway JAR 不存在，请先在 services/gateway 执行 mvn package"
}

Write-Host ">>> 启动 Gateway..." -ForegroundColor Green
java -jar $jar
