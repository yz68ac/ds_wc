# 在 Windows 上运行合成数据测试；不连接 SSH、微信或 DeepSeek。
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    # 沿用已存在的测试环境；迁移 Git 根目录不搬动带绝对路径的 venv。
    $pythonPath = Join-Path (Split-Path $PSScriptRoot -Parent) '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw '未找到 .venv。请先准备 Python 测试环境；此脚本不会自动安装依赖。'
}
Push-Location $PSScriptRoot
try {
    & $pythonPath -X utf8 (Join-Path $PSScriptRoot 'scripts\test-offline.py')
    $testExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $testExitCode
