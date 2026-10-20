# 在 Windows 上运行合成数据测试；不连接 SSH、微信或 DeepSeek。
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$testPath = Join-Path $PSScriptRoot 'wechat-group-bot\scripts\test-offline.py'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw '未找到本项目原有的 .venv。请先检查 Python 环境；此脚本不会自动安装依赖。'
}
& $pythonPath -X utf8 $testPath
exit $LASTEXITCODE
