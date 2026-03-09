<#
.SYNOPSIS
  修复混合行尾（CRLF/LF 混用）为统一 LF。

.DESCRIPTION
  仅处理包含 CRLF 与 LF 混用的文本文件。
  默认会跳过常见的缓存/虚拟环境/静态目录。

.PARAMETER Root
  扫描根目录，默认当前目录。

.PARAMETER Extensions
  处理的扩展名列表（包含点），默认常见文本类型。

.PARAMETER DryRun
  仅输出将会处理的文件，不写入。
#>

param(
    [string]$Root = ".",
    [string[]]$Extensions = @(".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".env", ".ps1"),
    [switch]$DryRun
)

$excludeDirs = @(
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "public",
    "migrations"
)

function Test-ExcludedPath {
    param([string]$Path)
    foreach ($dir in $excludeDirs) {
        if ($Path -match ("[\\/]" + [regex]::Escape($dir) + "[\\/]")) {
            return $true
        }
    }
    return $false
}

$fixed = New-Object System.Collections.Generic.List[string]
$skipped = New-Object System.Collections.Generic.List[string]

$files = Get-ChildItem -Path $Root -Recurse -File -Force |
    Where-Object {
        $name = $_.Name
        $ext = $_.Extension
        ($Extensions -contains $ext) -or ($Extensions -contains "." + $name)
    } |
    Where-Object { -not (Test-ExcludedPath $_.FullName) }

foreach ($file in $files) {
    try {
        $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
        if ($bytes -contains 0) {
            $skipped.Add($file.FullName) | Out-Null
            continue
        }

        $hasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        if ($hasBom) {
            $text = $text.TrimStart([char]0xFEFF)
        }

        $hasCrlf = $text.Contains("`r`n")
        $hasLf = [regex]::IsMatch($text, '(?<!\r)\n')
        if (-not ($hasCrlf -and $hasLf)) {
            continue
        }

        $normalized = $text -replace "`r`n", "`n"
        $normalized = $normalized -replace "`r", "`n"

        if ($DryRun) {
            $fixed.Add($file.FullName) | Out-Null
            continue
        }

        $outBytes = [System.Text.Encoding]::UTF8.GetBytes($normalized)
        if ($hasBom) {
            $bom = [byte[]](0xEF, 0xBB, 0xBF)
            $outBytes = $bom + $outBytes
        }
        [System.IO.File]::WriteAllBytes($file.FullName, $outBytes)
        $fixed.Add($file.FullName) | Out-Null
    } catch {
        $skipped.Add($file.FullName) | Out-Null
    }
}

Write-Host "已修复混合行尾文件数：$($fixed.Count)"
if ($DryRun -and $fixed.Count -gt 0) {
    Write-Host "以下文件将被修复："
    $fixed | ForEach-Object { Write-Host " - $_" }
}
if ($skipped.Count -gt 0) {
    Write-Host "跳过文件数：$($skipped.Count)"
}
