# Generates local font specimen sheets using the fonts installed on Windows.

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

function Get-UsableStyle {
    param([System.Drawing.FontFamily]$Family)

    foreach ($style in @(
        [System.Drawing.FontStyle]::Regular,
        [System.Drawing.FontStyle]::Bold,
        [System.Drawing.FontStyle]::Italic
    )) {
        if ($Family.IsStyleAvailable($style)) {
            return $style
        }
    }

    return [System.Drawing.FontStyle]::Regular
}

function New-SpecimenFont {
    param(
        [System.Drawing.FontFamily]$Family,
        [single]$Size
    )

    $style = Get-UsableStyle -Family $Family
    return [System.Drawing.Font]::new(
        $Family,
        $Size,
        $style,
        [System.Drawing.GraphicsUnit]::Pixel
    )
}

function New-Canvas {
    param(
        [int]$Width,
        [int]$Height
    )

    $bitmap = [System.Drawing.Bitmap]::new(
        $Width,
        $Height,
        [System.Drawing.Imaging.PixelFormat]::Format24bppRgb
    )
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
    $graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $graphics.Clear([System.Drawing.Color]::FromArgb(248, 247, 243))

    return @($bitmap, $graphics)
}

try {
    Write-Host 'INFO [FONT_PREVIEW_DISCOVERY] Reading installed Windows fonts'

    $collection = [System.Drawing.Text.InstalledFontCollection]::new()
    $families = @($collection.Families | Sort-Object Name)
    $familyByName = @{}
    foreach ($family in $families) {
        $familyByName[$family.Name] = $family
    }

    $uiFamily = [System.Drawing.FontFamily]::new('Segoe UI')
    $titleFont = [System.Drawing.Font]::new(
        $uiFamily, 42, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel
    )
    $subtitleFont = [System.Drawing.Font]::new(
        $uiFamily, 20, [System.Drawing.FontStyle]::Regular, [System.Drawing.GraphicsUnit]::Pixel
    )
    $labelFont = [System.Drawing.Font]::new(
        $uiFamily, 17, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel
    )
    $darkBrush = [System.Drawing.SolidBrush]::new(
        [System.Drawing.Color]::FromArgb(31, 35, 40)
    )
    $mutedBrush = [System.Drawing.SolidBrush]::new(
        [System.Drawing.Color]::FromArgb(100, 105, 110)
    )
    $linePen = [System.Drawing.Pen]::new(
        [System.Drawing.Color]::FromArgb(222, 218, 210), 1
    )

    Write-Host 'INFO [FONT_PREVIEW_CHINESE] Rendering selected Chinese and common fonts'

    $preferredNames = @(
        'Noto Sans SC',
        'Noto Serif SC',
        '微软雅黑',
        '微软雅黑 Light',
        '等线',
        '等线 Light',
        '黑体',
        '宋体',
        '新宋体',
        '楷体',
        '仿宋',
        '微軟正黑體',
        'Microsoft JhengHei UI',
        'Yu Gothic',
        'Malgun Gothic',
        'Arial',
        'Bahnschrift',
        'Calibri',
        'Cambria',
        'Georgia'
    )
    $selectedFamilies = @(
        foreach ($name in $preferredNames) {
            if ($familyByName.ContainsKey($name)) {
                $familyByName[$name]
            }
        }
    )

    $columns = 2
    $cellWidth = 1050
    $cellHeight = 184
    $headerHeight = 150
    $rows = [int][Math]::Ceiling($selectedFamilies.Count / $columns)
    $canvas = New-Canvas -Width ($cellWidth * $columns) -Height ($headerHeight + $rows * $cellHeight + 30)
    $bitmap = $canvas[0]
    $graphics = $canvas[1]

    $graphics.DrawString('本机字体样张 · 中文与常用字体', $titleFont, $darkBrush, 48, 32)
    $graphics.DrawString(
        ('统一字号实机渲染｜共 {0} 款精选字体' -f $selectedFamilies.Count),
        $subtitleFont,
        $mutedBrush,
        50,
        92
    )

    for ($index = 0; $index -lt $selectedFamilies.Count; $index++) {
        $column = $index % $columns
        $row = [Math]::Floor($index / $columns)
        $x = $column * $cellWidth
        $y = $headerHeight + $row * $cellHeight

        if ($column -gt 0) {
            $graphics.DrawLine($linePen, $x, $y + 12, $x, $y + $cellHeight - 12)
        }
        $graphics.DrawLine(
            $linePen,
            $x + 34,
            $y + $cellHeight - 1,
            $x + $cellWidth - 34,
            $y + $cellHeight - 1
        )

        $family = $selectedFamilies[$index]
        $graphics.DrawString($family.Name, $labelFont, $mutedBrush, $x + 46, $y + 22)

        $sampleFont = New-SpecimenFont -Family $family -Size 35
        $smallFont = New-SpecimenFont -Family $family -Size 23
        $graphics.DrawString('山川异域，风月同天。Aa 123', $sampleFont, $darkBrush, $x + 46, $y + 59)
        $graphics.DrawString('标题 · 正文 · 设计 DESIGN 2026', $smallFont, $darkBrush, $x + 48, $y + 116)
        $sampleFont.Dispose()
        $smallFont.Dispose()
    }

    $chineseOutput = Join-Path $PSScriptRoot 'font-preview-chinese.png'
    $bitmap.Save($chineseOutput, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose()
    $bitmap.Dispose()

    Write-Host 'INFO [FONT_PREVIEW_ALL] Rendering complete installed-font catalog'

    $allColumns = 3
    $allCellWidth = 720
    $allCellHeight = 96
    $allHeaderHeight = 144
    $allRows = [int][Math]::Ceiling($families.Count / $allColumns)
    $canvas = New-Canvas -Width ($allCellWidth * $allColumns) -Height ($allHeaderHeight + $allRows * $allCellHeight + 26)
    $bitmap = $canvas[0]
    $graphics = $canvas[1]

    $graphics.DrawString('本机完整字体总览', $titleFont, $darkBrush, 42, 28)
    $graphics.DrawString(
        ('共 {0} 个字体家族/变体｜样张：Aa Bb 123 中文' -f $families.Count),
        $subtitleFont,
        $mutedBrush,
        44,
        87
    )

    $textFormat = [System.Drawing.StringFormat]::new()
    $textFormat.Trimming = [System.Drawing.StringTrimming]::EllipsisCharacter
    $textFormat.FormatFlags = [System.Drawing.StringFormatFlags]::NoWrap

    for ($index = 0; $index -lt $families.Count; $index++) {
        $column = $index % $allColumns
        $row = [Math]::Floor($index / $allColumns)
        $x = $column * $allCellWidth
        $y = $allHeaderHeight + $row * $allCellHeight

        if (($row % 2) -eq 0) {
            $shadeBrush = [System.Drawing.SolidBrush]::new(
                [System.Drawing.Color]::FromArgb(243, 241, 235)
            )
            $graphics.FillRectangle($shadeBrush, $x, $y, $allCellWidth, $allCellHeight)
            $shadeBrush.Dispose()
        }
        if ($column -gt 0) {
            $graphics.DrawLine($linePen, $x, $y + 8, $x, $y + $allCellHeight - 8)
        }

        $family = $families[$index]
        $labelRectangle = [System.Drawing.RectangleF]::new(
            $x + 24, $y + 10, $allCellWidth - 48, 24
        )
        $graphics.DrawString(
            $family.Name,
            $labelFont,
            $mutedBrush,
            $labelRectangle,
            $textFormat
        )

        $sampleFont = New-SpecimenFont -Family $family -Size 27
        $sampleRectangle = [System.Drawing.RectangleF]::new(
            $x + 23, $y + 43, $allCellWidth - 46, 42
        )
        $graphics.DrawString(
            'Aa Bb Cc 123 · 字体样张',
            $sampleFont,
            $darkBrush,
            $sampleRectangle,
            $textFormat
        )
        $sampleFont.Dispose()
    }

    $allOutput = Join-Path $PSScriptRoot 'font-preview-all.png'
    $bitmap.Save($allOutput, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose()
    $bitmap.Dispose()

    Write-Host ('INFO [FONT_PREVIEW_COMPLETE] Generated {0} files' -f 2)
    Get-Item -LiteralPath $chineseOutput, $allOutput |
        Select-Object Name, Length, LastWriteTime
}
catch {
    Write-Error ('[FONT_PREVIEW_FAILED] {0}' -f $_.Exception.Message)
    exit 1
}
finally {
    foreach ($resourceName in @(
        'textFormat',
        'titleFont',
        'subtitleFont',
        'labelFont',
        'darkBrush',
        'mutedBrush',
        'linePen',
        'uiFamily',
        'collection'
    )) {
        $resource = Get-Variable -Name $resourceName -ValueOnly -ErrorAction SilentlyContinue
        if ($null -ne $resource) {
            $resource.Dispose()
        }
    }
}
