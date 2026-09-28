# 用法: . .\scripts\env.ps1   (dot-source, 影响当前会话)
# 让 playwright CLI 与安装命令使用工作区内置的浏览器目录
$root = (Resolve-Path "$PSScriptRoot\..").Path
$env:PLAYWRIGHT_BROWSERS_PATH = "$root\.browsers"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Write-Host "PLAYWRIGHT_BROWSERS_PATH = $env:PLAYWRIGHT_BROWSERS_PATH"
Write-Host "PYTHONUTF8 = $env:PYTHONUTF8"
