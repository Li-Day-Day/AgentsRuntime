param(
    [string]$ImageName = "workbuddy-linux:poc",
    [string]$DmgPath = "",
    [string]$Platform = "linux/amd64",
    [switch]$ForceDownload
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$downloadDir = Join-Path $repoRoot ".tmp\workbuddy"
New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null

if ([string]::IsNullOrWhiteSpace($DmgPath)) {
    $metadataUrl = "https://www.codebuddy.cn/v2/update?platform=workbuddy-darwin-x64"
    Write-Host "Resolving the official WorkBuddy Intel/x64 download..."
    $metadata = Invoke-RestMethod -Uri $metadataUrl -Headers @{ Accept = "application/json" }
    if ([string]::IsNullOrWhiteSpace($metadata.url)) {
        throw "The official update API did not return a download URL."
    }

    $dmgUrl = $metadata.url -replace '\.zip(?=$|\?)', '.dmg'
    $dmgName = [System.IO.Path]::GetFileName(([Uri]$dmgUrl).AbsolutePath)
    $DmgPath = Join-Path $downloadDir $dmgName

    if ($ForceDownload -or -not (Test-Path -LiteralPath $DmgPath)) {
        Write-Host "Downloading $dmgUrl"
        & curl.exe `
            --http1.1 `
            --fail `
            --location `
            --retry 3 `
            --retry-delay 2 `
            --connect-timeout 30 `
            --continue-at - `
            --output $DmgPath `
            $dmgUrl
        if ($LASTEXITCODE -ne 0) {
            throw "WorkBuddy DMG download failed with exit code $LASTEXITCODE."
        }
    } else {
        Write-Host "Using cached DMG: $DmgPath"
    }
}

$DmgPath = (Resolve-Path -LiteralPath $DmgPath).Path
$stream = [System.IO.File]::OpenRead($DmgPath)
try {
    if ($stream.Length -lt 512) {
        throw "The supplied file is too small to be a DMG: $DmgPath"
    }
    [void]$stream.Seek(-512, [System.IO.SeekOrigin]::End)
    $signature = New-Object byte[] 4
    [void]$stream.Read($signature, 0, $signature.Length)
} finally {
    $stream.Dispose()
}
$signatureText = [System.Text.Encoding]::ASCII.GetString($signature)
if ($signatureText -ne "koly") {
    throw "The supplied file does not have a DMG signature: $DmgPath"
}

# Give BuildKit a context containing only the DMG. Pointing it at the download
# directory would make unrelated cache/log files part of the build cache key.
$inputContext = Join-Path $downloadDir "build-context"
New-Item -ItemType Directory -Force -Path $inputContext | Out-Null
$contextDmg = Join-Path $inputContext ([System.IO.Path]::GetFileName($DmgPath))
if (-not [string]::Equals($contextDmg, $DmgPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    Get-ChildItem -LiteralPath $inputContext -File -Filter "*.dmg" | ForEach-Object {
        Remove-Item -LiteralPath $_.FullName -Force
    }
    try {
        New-Item -ItemType HardLink -Path $contextDmg -Target $DmgPath -ErrorAction Stop | Out-Null
    } catch {
        Copy-Item -LiteralPath $DmgPath -Destination $contextDmg -Force
    }
}

Write-Host "Building $ImageName from $DmgPath"
& docker buildx build `
    --platform $Platform `
    --build-context "workbuddy_input=$inputContext" `
    --file (Join-Path $PSScriptRoot "Dockerfile") `
    --tag $ImageName `
    --load `
    $repoRoot

if ($LASTEXITCODE -ne 0) {
    throw "Docker build failed with exit code $LASTEXITCODE."
}

docker image inspect $ImageName --format 'Built {{.RepoTags}} ({{.Size}} bytes)'
