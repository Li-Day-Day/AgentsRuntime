param([switch]$InstallOnly)

$ErrorActionPreference = "Stop"

$sourceDirectory = "\\host.lan\Data\.clawmanager"
$codexHome = Join-Path $env:USERPROFILE ".codex"
$logDirectory = "C:\ProgramData\ClawManager"
$logPath = Join-Path $logDirectory "codex-bootstrap.log"
$waitDeadline = (Get-Date).AddMinutes(3)
$installedScriptPath = Join-Path $logDirectory "codex-bootstrap.ps1"

function Install-SelfForLogon {
    $sourcePath = $PSCommandPath
    if ([string]::IsNullOrWhiteSpace($sourcePath)) {
        return
    }
    if ([string]::Equals($sourcePath, $installedScriptPath, [StringComparison]::OrdinalIgnoreCase)) {
        return
    }

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    Copy-Item -LiteralPath $sourcePath -Destination $installedScriptPath -Force
    $runCommand = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$installedScriptPath`""
    New-ItemProperty `
        -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
        -Name "ClawManagerCodexBootstrap" `
        -Value $runCommand `
        -PropertyType String `
        -Force | Out-Null
}

function Write-BootstrapLog {
    param([string]$Message)

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$timestamp $Message" | Out-File -FilePath $logPath -Append -Encoding utf8
}

function Copy-Atomic {
    param(
        [string]$Source,
        [string]$Destination
    )

    $temporary = "$Destination.$PID.tmp"
    Copy-Item -LiteralPath $Source -Destination $temporary -Force
    Move-Item -LiteralPath $temporary -Destination $Destination -Force
}

function Protect-CredentialFile {
    param([string]$Path)

    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    foreach ($identity in @($currentUser, $system)) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $identity,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Configure-CodexUserLocale {
    $languageList = New-WinUserLanguageList -Language "zh-CN"
    $languageList.Add("en-US")
    Set-WinUserLanguageList -LanguageList $languageList -Force
    Set-WinUILanguageOverride -Language "zh-CN"
    Set-Culture -CultureInfo "zh-CN"
    Set-WinHomeLocation -GeoId 45
    Write-BootstrapLog "Chinese user language, region, and Microsoft Pinyin configured"
}

function Find-CodexPackage {
    Get-AppxPackage -Name "OpenAI.Codex" -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Start-CodexDesktop {
    if (Get-Process -Name "ChatGPT" -ErrorAction SilentlyContinue) {
        Write-BootstrapLog "Codex desktop is already running"
        return
    }

    $packageDeadline = (Get-Date).AddMinutes(1)
    $package = Find-CodexPackage
    while (-not $package -and (Get-Date) -lt $packageDeadline) {
        Start-Sleep -Seconds 2
        $package = Find-CodexPackage
    }
    if (-not $package) {
        throw "OpenAI.Codex package is not registered for the current user"
    }

    $manifest = Get-AppxPackageManifest -Package $package.PackageFullName
    $applicationId = @($manifest.Package.Applications.Application)[0].Id
    if ([string]::IsNullOrWhiteSpace($applicationId)) {
        throw "OpenAI.Codex application id was not found"
    }
    $aumid = "$($package.PackageFamilyName)!$applicationId"
    Start-Process -FilePath "explorer.exe" -ArgumentList "shell:AppsFolder\$aumid"
    Write-BootstrapLog "Codex desktop launch requested"
}

try {
    Install-SelfForLogon
    if ($InstallOnly) {
        Write-BootstrapLog "Windows Codex bootstrap installed for future logons"
        exit 0
    }
    Write-BootstrapLog "Windows Codex bootstrap start"
    Configure-CodexUserLocale
    $configSource = Join-Path $sourceDirectory "config.toml"
    $authSource = Join-Path $sourceDirectory "auth.json"
    while ((-not (Test-Path $configSource) -or -not (Test-Path $authSource)) -and (Get-Date) -lt $waitDeadline) {
        Start-Sleep -Seconds 2
    }

    if ((Test-Path $configSource) -and (Test-Path $authSource)) {
        New-Item -ItemType Directory -Path $codexHome -Force | Out-Null
        $configTarget = Join-Path $codexHome "config.toml"
        $authTarget = Join-Path $codexHome "auth.json"
        Copy-Atomic -Source $configSource -Destination $configTarget
        Copy-Atomic -Source $authSource -Destination $authTarget
        Protect-CredentialFile -Path $authTarget
        Write-BootstrapLog "Codex configuration installed for the current user"
    } else {
        Write-BootstrapLog "Codex bootstrap files were not available before the timeout"
    }

    Start-CodexDesktop
    Write-BootstrapLog "Windows Codex bootstrap complete"
} catch {
    Write-BootstrapLog "ERROR: $($_.Exception.Message)"
}
