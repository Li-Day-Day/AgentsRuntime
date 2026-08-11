$ErrorActionPreference = "Stop"

$result = "\\host.lan\Data\launch-result.txt"
try {
    $package = Get-AppxPackage -Name "OpenAI.Codex" | Select-Object -First 1
    if (-not $package) {
        throw "OpenAI.Codex package was not found"
    }
    $manifest = Get-AppxPackageManifest -Package $package.PackageFullName
    $applicationId = @($manifest.Package.Applications.Application)[0].Id
    $aumid = "$($package.PackageFamilyName)!$applicationId"
    Start-Process -FilePath "explorer.exe" -ArgumentList "shell:AppsFolder\$aumid"
    Start-Sleep -Seconds 12
    $processes = Get-Process |
        Where-Object { $_.ProcessName -match "ChatGPT|Codex" } |
        Select-Object -ExpandProperty ProcessName -Unique
    @(
        "LAUNCH=SUCCESS"
        "AUMID=$aumid"
        "PROCESSES=$($processes -join ',')"
    ) | Set-Content $result -Encoding UTF8
} catch {
    "LAUNCH=FAILED`r`nERROR=$($_.Exception.Message)" | Set-Content $result -Encoding UTF8
    exit 1
}
