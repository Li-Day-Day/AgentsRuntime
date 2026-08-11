$ErrorActionPreference = "Stop"

$source = "\\host.lan\Data\codex-devtools.ps1"
$target = "C:\OEM\codex\devtools.ps1"
$result = "\\host.lan\Data\repair-result.txt"

try {
    Copy-Item -LiteralPath $source -Destination $target -Force
    & "C:\OEM\codex\install.ps1"
    if ($LASTEXITCODE -ne 0) {
        throw "Codex OEM installer exited with code $LASTEXITCODE"
    }
    "REPAIR=SUCCESS" | Set-Content $result -Encoding UTF8
} catch {
    "REPAIR=FAILED`r`nERROR=$($_.Exception.Message)" | Set-Content $result -Encoding UTF8
    exit 1
}
