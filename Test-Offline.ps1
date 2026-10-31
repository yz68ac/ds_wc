# 准备依赖：uv sync --locked --extra database --extra login --group test
# 在当前项目环境运行合成数据测试；不连接 SSH、微信或模型接口，不自动安装依赖。
param(
    [ValidateSet('models','wechat','bot','management')]
    [string[]]$Suite = @()
)

$ErrorActionPreference = 'Stop'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw '未找到 uv。请使用 pyproject.toml 要求的版本。'
}
Push-Location $PSScriptRoot
try {
    & uv sync --locked --check --inexact --extra database --extra login --group test
    if ($LASTEXITCODE -ne 0) {
        throw '测试环境与锁文件不一致。请先执行：uv sync --locked --extra database --extra login --group test'
    }
    $suiteArgs = foreach ($name in $Suite) { '--suite'; $name }
    & uv run --locked --no-sync python -X utf8 (Join-Path $PSScriptRoot 'scripts\test-offline.py') @suiteArgs
    $testExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $testExitCode
