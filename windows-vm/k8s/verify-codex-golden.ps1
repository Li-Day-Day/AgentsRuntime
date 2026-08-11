$ErrorActionPreference = "Continue"

$lines = [System.Collections.Generic.List[string]]::new()
$os = Get-CimInstance Win32_OperatingSystem
$codex = Get-AppxPackage -Name "OpenAI.Codex" -ErrorAction SilentlyContinue
$systemLocale = Get-WinSystemLocale
$userLanguages = Get-WinUserLanguageList

$lines.Add("OS=$($os.Caption)")
$lines.Add("OS_VERSION=$($os.Version)")
$lines.Add("SYSTEM_LOCALE=$($systemLocale.Name)")
$lines.Add("USER_LANGUAGE=$($userLanguages[0].LanguageTag)")
$lines.Add("CODEX_VERSION=$($codex.Version)")
$lines.Add("OEM_MARKER=$(Test-Path 'C:\OEM\codex\installed.marker')")
$lines.Add("BOOTSTRAP=$(Test-Path 'C:\ProgramData\ClawManager\codex-bootstrap.ps1')")
$lines.Add("GIT=$(& git --version 2>&1)")
$lines.Add("NODE=$(& node --version 2>&1)")
$lines.Add("NPM=$(& npm --version 2>&1)")
$lines.Add("PYTHON=$(& python --version 2>&1)")
$lines.Add("RIPGREP=$((& rg --version 2>&1 | Select-Object -First 1))")
$lines.Add("LONG_PATHS=$((Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem').LongPathsEnabled)")
$lines.Add("DEVELOPER_MODE=$((Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock').AllowDevelopmentWithoutDevLicense)")
$lines.Add("BOOTSTRAP_TASK=$(Get-ScheduledTask -TaskName 'ClawManager Codex Bootstrap' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty TaskName)")
$lines.Add("INSTALL_LOG_BEGIN")
if (Test-Path "C:\OEM\codex\install.log") {
    $lines.AddRange([string[]](Get-Content "C:\OEM\codex\install.log" -Tail 20))
}
$lines.Add("INSTALL_LOG_END")

$lines | Set-Content "\\host.lan\Data\verify-result.txt" -Encoding UTF8
