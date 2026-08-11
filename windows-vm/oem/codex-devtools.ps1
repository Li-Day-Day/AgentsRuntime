$ErrorActionPreference = "Stop"

$codexDirectory = "C:\OEM\codex"
$payloadDirectory = Join-Path $codexDirectory "devtools"
$logPath = Join-Path $codexDirectory "devtools-install.log"

function Write-DevtoolsLog {
    param([string]$Message)

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$timestamp $Message" | Out-File -FilePath $logPath -Append -Encoding utf8
}

function Invoke-Installer {
    param(
        [string]$FilePath,
        [string]$Arguments,
        [string]$Name
    )

    if (-not (Test-Path $FilePath)) {
        $command = Get-Command $FilePath -ErrorAction SilentlyContinue
        if (-not $command) {
            throw "$Name installer not found: $FilePath"
        }
        $FilePath = $command.Source
    }
    Write-DevtoolsLog "Installing $Name"
    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -Wait `
        -PassThru
    if ($process.ExitCode -notin @(0, 1641, 3010)) {
        throw "$Name installer exited with code $($process.ExitCode)"
    }
    Write-DevtoolsLog "$Name installation complete (exit $($process.ExitCode))"
}

function Add-MachinePath {
    param([string]$Directory)

    if (-not (Test-Path $Directory)) {
        return
    }
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $segments = @($machinePath -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($segments -notcontains $Directory) {
        [Environment]::SetEnvironmentVariable("Path", (($segments + $Directory) -join ";"), "Machine")
    }
    if (@($env:Path -split ";") -notcontains $Directory) {
        $env:Path = "$env:Path;$Directory"
    }
}

function Install-Ripgrep {
    $archivePath = Join-Path $payloadDirectory "ripgrep.zip"
    if (-not (Test-Path $archivePath)) {
        throw "ripgrep archive not found: $archivePath"
    }

    $temporaryDirectory = Join-Path $env:TEMP "clawmanager-ripgrep"
    $targetDirectory = "C:\Program Files\ripgrep"
    Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $temporaryDirectory -Force | Out-Null
    Expand-Archive -LiteralPath $archivePath -DestinationPath $temporaryDirectory -Force
    $sourceDirectory = Get-ChildItem -LiteralPath $temporaryDirectory -Directory |
        Select-Object -First 1
    if (-not $sourceDirectory) {
        throw "ripgrep archive did not contain a directory"
    }

    Remove-Item -LiteralPath $targetDirectory -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
    Copy-Item -Path (Join-Path $sourceDirectory.FullName "*") -Destination $targetDirectory -Recurse -Force
    Add-MachinePath -Directory $targetDirectory
    Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    Write-DevtoolsLog "ripgrep installation complete"
}

try {
    Write-DevtoolsLog "Codex developer tools install start"
    Invoke-Installer `
        -FilePath (Join-Path $payloadDirectory "Git-64-bit.exe") `
        -Arguments "/VERYSILENT /NORESTART /NOCANCEL /SP- /SUPPRESSMSGBOXES" `
        -Name "Git for Windows"
    Invoke-Installer `
        -FilePath "msiexec.exe" `
        -Arguments "/i `"$(Join-Path $payloadDirectory 'node-x64.msi')`" /qn /norestart" `
        -Name "Node.js LTS"
    Invoke-Installer `
        -FilePath (Join-Path $payloadDirectory "python-amd64.exe") `
        -Arguments "/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1 Include_test=0 Shortcuts=0" `
        -Name "Python"
    Install-Ripgrep

    foreach ($directory in @(
        "C:\Program Files\Git\cmd",
        "C:\Program Files\nodejs",
        "C:\Program Files\Python313",
        "C:\Program Files\Python313\Scripts"
    )) {
        Add-MachinePath -Directory $directory
    }

    New-ItemProperty `
        -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
        -Name "LongPathsEnabled" `
        -PropertyType DWord `
        -Value 1 `
        -Force | Out-Null
    New-Item -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock" -Force | Out-Null
    New-ItemProperty `
        -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock" `
        -Name "AllowDevelopmentWithoutDevLicense" `
        -PropertyType DWord `
        -Value 1 `
        -Force | Out-Null
    try {
        Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope LocalMachine -Force
    } catch {
        # Some Windows images enforce execution policy through Group Policy and
        # reject changing LocalMachine with a SecurityException. The OEM entry
        # point already launches these trusted local scripts with Bypass, so a
        # policy refusal must not prevent Codex provisioning.
        Write-DevtoolsLog "WARNING: Could not set LocalMachine execution policy: $($_.Exception.Message)"
    }

    $git = "C:\Program Files\Git\cmd\git.exe"
    if (Test-Path $git) {
        & $git config --system core.longpaths true
    }
    Write-DevtoolsLog "Codex developer tools install complete"
} catch {
    Write-DevtoolsLog "ERROR: $($_.Exception.Message)"
    throw
}
