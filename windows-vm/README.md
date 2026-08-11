# ClawManager Windows VM Runtime

This image wraps `dockurr/windows` with the shared `clawmanager-agent` so
ClawManager can treat a Windows VM as a minimal managed runtime.

The MVP lifecycle is intentionally small:

- runtime type: `windows-vm`
- capacity: `1`
- gateway/web desktop port: `8006`
- RDP port: `3389/tcp` and `3389/udp`
- Windows VM storage: `/storage`
- optional shared folder: `/shared`

The Windows guest does not run a ClawManager instance agent yet. The Linux
container layer reports runtime-pod health and starts the Windows VM when
ClawManager creates a gateway.

## Build

Run from the repository root:

```bash
docker build -f windows-vm/Dockerfile -t windows-vm:local .
```

To build an image that downloads the official WorkBuddy Windows installer,
downloads the Microsoft Edge for Business MSI, and passes both to the Windows
first-boot OEM installer:

```bash
docker build \
  -f windows-vm/Dockerfile.workbuddy \
  -t windows-vm-workbuddy:local \
  .
```

To build an image that downloads the official ChatGPT desktop app for Windows,
including its native Codex agent, and installs it during Windows first boot:

```bash
docker build \
  -f windows-vm/Dockerfile.codex \
  -t windows-vm-codex:local \
  .
```

The Codex image downloads OpenAI's current Store-signed x64 MSIX and offline
license from the stable URLs documented for enterprise Windows deployment. The
OEM script installs the package for the active desktop user, or provisions it
for Windows users when the OEM phase runs as `SYSTEM`. It also creates
`C:\Workspace` as the default location for user projects. New disks default to
Windows 11 in Chinese (`LANGUAGE=Chinese`, `REGION=zh-CN`, and
`KEYBOARD=zh-CN`). Git for Windows, Node.js LTS, Python, and ripgrep are
installed during the OEM phase, together with long-path and developer-mode
settings needed by coding agents.

To build and push directly to the ClawManager registry (no SSH registry tunnel
required):

```powershell
powershell -ExecutionPolicy Bypass -File windows-vm/fetch-codex-devtools.ps1
docker build `
  -f windows-vm/Dockerfile.codex `
  -t 10.130.14.23:5000/windows-vm-codex:2026.8.10-phase2-r2 `
  .
docker push 10.130.14.23:5000/windows-vm-codex:2026.8.10-phase2-r2
```

The Docker image contains the OEM payload, not the installed Windows system
disk. Reusing an existing `/storage` volume therefore keeps its old Windows
version, language, and installed tools. For the Windows 11 Chinese baseline,
create a fresh 80 GiB golden PVC, complete the first boot and OEM installation,
shut Windows down cleanly, and only then switch ClawManager from
`codex-golden-v1` to the new `codex-golden-v2` PVC.

The WorkBuddy installer is not stored in this repository. The Dockerfile reads
the current official URL from:

```text
https://www.workbuddy.cn/v2/update?platform=workbuddy-win32-x64-user
```

The Edge MSI is downloaded from Microsoft's stable Enterprise x64 fwlink:

```text
https://go.microsoft.com/fwlink/?LinkID=2093437
```

You can pin a specific installer URL when reproducibility matters:

```bash
docker build \
  -f windows-vm/Dockerfile.workbuddy \
  --build-arg WORKBUDDY_INSTALLER_URL=https://download.codebuddy.cn/workbuddy/saas/win32-x64-user/WorkBuddy-win32-x64-user-5.3.8.34705286-e9991e2b.exe \
  -t windows-vm-workbuddy:local \
  .
```

## Standalone smoke test

Without runtime-agent tokens, the image behaves like the upstream
`dockurr/windows` image and starts Windows immediately.

```powershell
docker run -it --rm `
  --name windows-vm-local `
  --device=/dev/kvm `
  --device=/dev/net/tun `
  --cap-add NET_ADMIN `
  -e RAM_SIZE=8G `
  -e CPU_CORES=4 `
  -e DISK_SIZE=128G `
  -v "D:\path\to\windows.iso:/custom.iso:ro" `
  -v "D:\agentsruntime-windows\storage:/storage" `
  -v "D:\agentsruntime-windows\shared:/shared" `
  -p 8006:8006 `
  -p 3389:3389/tcp `
  -p 3389:3389/udp `
  windows-vm:local
```

Open `http://localhost:8006`, or connect by RDP to `localhost:3389` after the
Windows installation finishes.

## Managed runtime mode

When ClawManager injects `RUNTIME_AGENT_CONTROL_TOKEN` and
`RUNTIME_AGENT_REPORT_TOKEN`, the container starts `clawmanager-agent` first.
The agent registers as `windows-vm`; a gateway create request then starts the
Windows VM process through `/usr/local/bin/windows-vm-gateway`.

The platform still needs to allow this runtime type and supply the container
requirements used by `dockurr/windows`:

- `/dev/kvm`
- `/dev/net/tun`
- `NET_ADMIN`
- persistent storage mounted at `/storage`, or a workspace volume large enough
  for Windows disks
- ports `8006`, `3389/tcp`, and `3389/udp`
