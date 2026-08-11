$ErrorActionPreference = "Stop"

$codexDirectory = "C:\OEM\codex"
$logPath = Join-Path $codexDirectory "install.log"
$msixPath = Join-Path $codexDirectory "ChatGPT-x64.msix"
$licensePath = Join-Path $codexDirectory "ChatGPT-License.xml"
$markerPath = Join-Path $codexDirectory "installed.marker"
$workspacePath = "C:\Workspace"
$bootstrapSourcePath = Join-Path $codexDirectory "bootstrap.ps1"
$devtoolsScriptPath = Join-Path $codexDirectory "devtools.ps1"
$clawManagerDirectory = "C:\ProgramData\ClawManager"
$bootstrapTargetPath = Join-Path $clawManagerDirectory "codex-bootstrap.ps1"
$bootstrapTaskName = "ClawManager Codex Bootstrap"

function Write-InstallLog {
    param([string]$Message)

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$timestamp $Message" | Out-File -FilePath $logPath -Append -Encoding utf8
}

function Find-UserPackage {
    Get-AppxPackage |
        Where-Object { $_.Name -match "ChatGPT|OpenAI" } |
        Select-Object -First 1
}

function Find-ProvisionedPackage {
    Get-AppxProvisionedPackage -Online |
        Where-Object { $_.DisplayName -match "ChatGPT|OpenAI" } |
        Select-Object -First 1
}

function Configure-SystemLocale {
    Write-InstallLog "Configuring Chinese system locale"
    Set-WinSystemLocale -SystemLocale "zh-CN"
    Set-TimeZone -Id "China Standard Time"
    Write-InstallLog "Chinese system locale configured; a reboot may be required"
}

function Install-DeveloperTools {
    if (-not (Test-Path $devtoolsScriptPath)) {
        throw "Codex developer tools script not found: $devtoolsScriptPath"
    }
    & $devtoolsScriptPath
    Write-InstallLog "Codex developer tools installed"
}

function Install-CodexBootstrap {
    if (-not (Test-Path $bootstrapSourcePath)) {
        throw "Codex bootstrap script not found: $bootstrapSourcePath"
    }

    New-Item -ItemType Directory -Path $clawManagerDirectory -Force | Out-Null
    Copy-Item -LiteralPath $bootstrapSourcePath -Destination $bootstrapTargetPath -Force

    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$bootstrapTargetPath`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "Docker"
    $principal = New-ScheduledTaskPrincipal -UserId "Docker" -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

    Register-ScheduledTask `
        -TaskName $bootstrapTaskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Force | Out-Null
    Write-InstallLog "Registered per-user Codex bootstrap task"
}

try {
    New-Item -ItemType Directory -Path $codexDirectory -Force | Out-Null
    Write-InstallLog "Codex desktop OEM install start"

    if (-not (Test-Path $msixPath)) {
        throw "MSIX package not found: $msixPath"
    }

    New-Item -ItemType Directory -Path $workspacePath -Force | Out-Null
    Write-InstallLog "Workspace ready: $workspacePath"
    Configure-SystemLocale
    Install-DeveloperTools

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if ($identity.IsSystem) {
        $package = Find-ProvisionedPackage
        if (-not $package) {
            if (-not (Test-Path $licensePath)) {
                throw "Offline license not found: $licensePath"
            }

            Write-InstallLog "Provisioning ChatGPT/Codex MSIX for Windows users"
            $provisioningArguments = @{
                Online = $true
                PackagePath = $msixPath
                LicensePath = $licensePath
                ErrorAction = "Stop"
            }
            Add-AppxProvisionedPackage @provisioningArguments |
                Out-File -FilePath $logPath -Append -Encoding utf8
            $package = Find-ProvisionedPackage
        }

        if (-not $package) {
            throw "Provisioned ChatGPT/Codex package was not detected"
        }

        Write-InstallLog "Provisioned package: $($package.DisplayName) $($package.Version)"
    } else {
        $package = Find-UserPackage
        if (-not $package) {
            Write-InstallLog "Installing ChatGPT/Codex MSIX for user $($identity.Name)"
            Add-AppxPackage -Path $msixPath -ForceApplicationShutdown
            $package = Find-UserPackage
        }

        if (-not $package) {
            throw "ChatGPT/Codex user package was not detected"
        }

        Write-InstallLog "Installed package: $($package.Name) $($package.Version)"
    }

    Install-CodexBootstrap

    New-Item -ItemType File -Path $markerPath -Force | Out-Null
    Write-InstallLog "Codex desktop OEM install complete"
} catch {
    Write-InstallLog "ERROR: $($_.Exception.Message)"
    exit 1
}
