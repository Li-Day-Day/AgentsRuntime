# WorkBuddy Linux Webtop image (unofficial POC)

This image uses the archived community
[`JipZeonGit/workbuddy-linux`](https://github.com/JipZeonGit/workbuddy-linux)
conversion scripts to run a user-supplied official WorkBuddy macOS Intel/x64
DMG inside the repository's existing Ubuntu KDE Webtop desktop.

This is a local proof of concept, not an official Tencent Linux release.
The conversion disables WorkBuddy's native code sandbox, Tencent Docs engine,
and automatic updater. Do not run the container as privileged, mount the Docker
socket, or expose the Webtop ports directly to an untrusted network.

## Build on Windows

Docker Desktop must be running in Linux-container mode. From the repository
root:

```powershell
powershell -ExecutionPolicy Bypass -File .\workbuddy\build-local.ps1
```

The script resolves the current official Intel/x64 DMG URL from Tencent's
update API, caches the DMG under `.tmp/workbuddy`, and supplies its directory
to BuildKit as a read-only named build context. The DMG is not copied into an
image layer.

To use an existing DMG:

```powershell
.\workbuddy\build-local.ps1 -DmgPath D:\Downloads\WorkBuddy.dmg
```

The archived conversion project was validated against WorkBuddy 4.22.10.
Current upstream releases may require updates to its runtime patches.

## Run

```powershell
docker volume create workbuddy-config
docker volume create workbuddy-workspace

docker run -d `
  --name workbuddy-poc `
  --shm-size=1g `
  -e PUID=1000 `
  -e PGID=1000 `
  -e TZ=Asia/Shanghai `
  -v workbuddy-config:/config `
  -v workbuddy-workspace:/workspace `
  -p 127.0.0.1:3000:3000 `
  -p 127.0.0.1:3001:3001 `
  workbuddy-linux:poc
```

Open `https://127.0.0.1:3001` and accept Webtop's local self-signed
certificate. WorkBuddy starts automatically in the KDE session. Complete the
first login using the browser inside that same desktop so the `workbuddy://`
callback stays inside the container.

WorkBuddy is supervised as a container service: it waits for the KDE desktop,
runs as the unprivileged `abc` desktop user, and restarts if the application
exits unexpectedly. Its output is available through `docker logs workbuddy-poc`.

This image defaults to a pure X11 KDE session (`PIXELFLUX_WAYLAND=false`).
That avoids Selkies' KWin/Wayland Unicode clipboard fallback, which can paste
stale clipboard content when a host Chinese IME confirms a candidate with
Enter. The original Wayland mode remains available with
`-e PIXELFLUX_WAYLAND=true` for comparison.

If the automatic launch fails, open Konsole in Webtop and run:

```bash
/usr/local/bin/start-workbuddy --verbose
```

Runtime data remains in the `workbuddy-config` volume. To remove the test
without deleting the data volume:

```powershell
docker rm -f workbuddy-poc
```
