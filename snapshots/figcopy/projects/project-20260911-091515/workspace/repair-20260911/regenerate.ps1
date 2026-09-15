# 在已配置 YIBU_API_KEY 或 YIBU_SHARED_PATH 的 PowerShell 中运行。
# 复用现有背景及新照片裁切参数，仅重新生成七个装饰素材。
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath 'D:\codes\figcopy'
try {
    python -m collage build --spec "$PSScriptRoot\reviewed.regenerate.json" --out "$PSScriptRoot\regenerated-template" --work "$PSScriptRoot\regenerated-build" --provider collage.providers.yibu:YibuImageProvider --force
    if ($LASTEXITCODE -ne 0) { throw '装饰构建失败，请根据上方业务错误码处理后重试。' }
    python -m collage render --template "$PSScriptRoot\regenerated-template" --bindings "$PSScriptRoot\bindings.json" --out "$PSScriptRoot\result.regenerated.png" --allow-unreviewed
    if ($LASTEXITCODE -ne 0) { throw '本地渲染失败，请检查上方错误。' }
    Write-Output "完成：$PSScriptRoot\result.regenerated.png"
} finally {
    Pop-Location
}
