$ErrorActionPreference = "Stop"

$logPath = "C:\OEM\workbuddy-install.log"
$workBuddyInstaller = "C:\OEM\WorkBuddySetup.exe"
$edgeInstaller = "C:\OEM\MicrosoftEdgeEnterpriseX64.msi"
$defaultAssociations = "C:\OEM\default-app-associations.xml"
$marker = "C:\OEM\workbuddy-installed.marker"

function Write-InstallLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$timestamp $Message" | Out-File -FilePath $logPath -Append -Encoding utf8
}

try {
    Write-InstallLog "OEM app install start"

    if (Test-Path $edgeInstaller) {
        Write-InstallLog "Installing Microsoft Edge"
        $edgeProcess = Start-Process -FilePath "msiexec.exe" -ArgumentList "/i `"$edgeInstaller`" /qn /norestart" -Wait -PassThru
        Write-InstallLog "Microsoft Edge installer exited with code $($edgeProcess.ExitCode)"
        if ($edgeProcess.ExitCode -notin @(0, 3010)) {
            throw "Microsoft Edge installer failed with exit code $($edgeProcess.ExitCode)"
        }
    } else {
        Write-InstallLog "Microsoft Edge installer not found: $edgeInstaller"
    }

    if (Test-Path $defaultAssociations) {
        Write-InstallLog "Importing default browser associations"
        & dism.exe /Online /Import-DefaultAppAssociations:$defaultAssociations | Out-File -FilePath $logPath -Append -Encoding utf8
    }

    if (-not (Test-Path $workBuddyInstaller)) {
        throw "Installer not found: $workBuddyInstaller"
    }

    Write-InstallLog "Installing WorkBuddy"
    $workBuddyArgs = "/S"
    $process = Start-Process -FilePath $workBuddyInstaller -ArgumentList $workBuddyArgs -PassThru
    $finished = $process.WaitForExit(600000)
    if ($finished) {
        Write-InstallLog "WorkBuddy installer exited with code $($process.ExitCode)"
        if ($process.ExitCode -ne 0) {
            throw "WorkBuddy installer failed with exit code $($process.ExitCode)"
        }
    } else {
        Write-InstallLog "WorkBuddy installer wait timed out after 10 minutes; continuing with installation verification"
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }

    $installedWorkBuddy = Join-Path $env:LOCALAPPDATA "Programs\WorkBuddy\WorkBuddy.exe"
    if (-not (Test-Path $installedWorkBuddy)) {
        throw "WorkBuddy executable not found after installation: $installedWorkBuddy"
    }

    New-Item -ItemType File -Path $marker -Force | Out-Null
    Write-InstallLog "OEM app install complete"
} catch {
    Write-InstallLog "ERROR: $($_.Exception.Message)"
}
