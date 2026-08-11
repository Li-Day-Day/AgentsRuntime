param(
    [string]$GitVersion = "2.51.0",
    [string]$NodeVersion = "22.18.0",
    [string]$PythonVersion = "3.13.7",
    [string]$RipgrepVersion = "14.1.1",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$cacheDirectory = Join-Path $PSScriptRoot ".build-cache\codex"
New-Item -ItemType Directory -Path $cacheDirectory -Force | Out-Null

function Save-CodexAsset {
    param(
        [string]$Url,
        [string]$FileName
    )

    $destination = Join-Path $cacheDirectory $FileName
    if ((Test-Path $destination) -and -not $Force) {
        Write-Output "Using cached $FileName"
        return
    }

    $partial = "$destination.partial"
    Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
    Write-Output "Downloading $FileName"
    & curl.exe `
        --fail `
        --location `
        --retry 5 `
        --retry-all-errors `
        --retry-delay 2 `
        --connect-timeout 20 `
        --output $partial `
        $Url
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        throw "Failed to download $Url"
    }
    Move-Item -LiteralPath $partial -Destination $destination -Force
}

Save-CodexAsset `
    -Url "https://github.com/git-for-windows/git/releases/download/v$GitVersion.windows.1/Git-$GitVersion-64-bit.exe" `
    -FileName "Git-64-bit.exe"
Save-CodexAsset `
    -Url "https://nodejs.org/dist/v$NodeVersion/node-v$NodeVersion-x64.msi" `
    -FileName "node-x64.msi"
Save-CodexAsset `
    -Url "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" `
    -FileName "python-amd64.exe"
Save-CodexAsset `
    -Url "https://github.com/BurntSushi/ripgrep/releases/download/$RipgrepVersion/ripgrep-$RipgrepVersion-x86_64-pc-windows-msvc.zip" `
    -FileName "ripgrep.zip"

Get-ChildItem -LiteralPath $cacheDirectory -File |
    Select-Object Name, Length
