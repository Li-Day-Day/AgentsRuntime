# WorkBuddy、Codex 与 Claude Code 接入 ClawManager 部署手册

本文面向需要从零部署 ClawManager 智能体运行环境的开发、测试和运维人员。按照本文可以完成镜像构建、Registry 推送、ClawManager 镜像卡配置、Windows 黄金 PVC 制作、实例创建和上线验收。

本文聚焦智能体 runtime 接入，假定 ClawManager 控制面已经按照其 `deployments/k8s/single-node` 或 `deployments/k8s/cluster` 清单完成安装，数据库、AI Gateway、用户体系和 Kubernetes RBAC 均可正常使用。如果控制面尚未安装，应先完成 ClawManager 自身部署和首次管理员登录，再继续本文。

本文以 Kubernetes、Longhorn 和私有 Registry 为基准。示例环境使用：

```text
Registry:          10.130.14.23:5000
系统 namespace:   clawmanager-ltt-system
用户 namespace:   clawmanager-ltt-user-1
StorageClass:      longhorn
Windows 节点标签: clawmanager.io/windows-runtime=true
```

部署到其他环境时必须替换这些值，不要原样复制 namespace、镜像标签或服务器地址。

## 1. 支持矩阵

| 智能体 | 推荐运行形态 | 入口端口 | 持久化目录 | 是否需要黄金 PVC | 模型配置注入 |
| --- | --- | ---: | --- | --- | --- |
| WorkBuddy Windows | Windows VM / 官方 Windows 客户端 | 8006，RDP 3389 | `/storage` | 是，80Gi | 当前 Windows MVP 不自动注入 WorkBuddy 模型，应用内配置 |
| WorkBuddy Linux | Linux Webtop / 非官方兼容 POC | 3001 | `/config` | 否 | 支持 ClawManager AI Gateway |
| Codex Windows | Windows 11 VM / 官方 ChatGPT-Codex Desktop | 8006，RDP 3389 | `/storage` | 是，80Gi | 支持，通过 Secret 写入 `.codex` |
| Codex Linux | Linux Webtop / Codex CLI | 3001 | `/config` | 否 | 镜像支持；当前 ClawManager 的 `codex` 类型默认按 Windows 处理，不作为正式创建路径 |
| Claude Code | Linux Webtop / Claude Code CLI | 3001 | `/config` | 否 | 支持 ClawManager AI Gateway |

重要说明：

- WorkBuddy Linux 使用社区转换方案，不是腾讯官方 Linux 版本，不应作为高安全等级生产基线。
- Codex Windows 和 WorkBuddy Windows 是完整 Windows 虚拟机，必须有 KVM 节点和可克隆的 CSI 存储。
- Claude Code 当前没有 Windows 黄金盘实现。需要 Windows 版时，应单独增加 OEM 安装脚本、平台 runtime variant 和黄金盘配置，不能直接把 Linux 镜像改名使用。
- Windows 黄金 PVC 是预安装好的 Windows 系统母盘，不是共享工作目录，也不是 Docker 镜像。

## 2. 整体架构

Linux Webtop 实例的运行链路为：

```text
ClawManager
  -> 创建 PVC（/config）
  -> 创建 Webtop Pod
  -> 注入 Agent 与 AI Gateway 环境变量
  -> 容器启动 Codex CLI、Claude Code 或 WorkBuddy
  -> 浏览器通过 3001 访问桌面
```

Windows 实例的运行链路为：

```text
ClawManager
  -> 从同 namespace 的黄金 PVC 发起 CSI Clone
  -> 等待 Longhorn 完成数据复制和副本恢复
  -> 创建 privileged Windows VM Pod
  -> QEMU/KVM 从克隆盘启动 Windows
  -> 浏览器通过 8006/noVNC 或 3389/RDP 访问桌面
```

Docker 镜像与黄金 PVC 的职责不同：

| 对象 | 保存内容 |
| --- | --- |
| Windows runtime Docker 镜像 | QEMU、noVNC、ClawManager 容器 Agent、OEM 安装包和脚本 |
| 黄金 PVC | 已安装的 Windows、桌面应用、开发工具和系统设置 |
| 实例 PVC | 从黄金 PVC 克隆出来的独立可写系统盘 |
| `/shared` | Linux 容器和 Windows 客体之间的共享目录，在 Windows 中对应 `\\host.lan\Data` |
| Codex Bootstrap Secret | 每个实例独立的 `config.toml` 和 `auth.json`，挂载到 `/shared/.clawmanager` |

## 3. 部署前置条件

### 3.1 本机构建环境

需要安装：

- Git
- Docker Desktop 或 Docker Engine
- Docker Buildx
- PowerShell 7（Windows 构建机推荐）
- `kubectl`
- 可访问目标 Kubernetes 集群的 kubeconfig

检查：

```powershell
docker version
docker buildx version
kubectl version --client
kubectl cluster-info
```

所有 `docker build` 命令都必须在 AgentsRuntime 仓库根目录执行，因为 Dockerfile 会复制共享的 `clawmanager-agent/`。

### 3.2 通过 SSH 隧道推送 Registry

当前环境的 Registry 只监听服务器本机 `127.0.0.1:5000`，构建机通过 SSH 本地端口转发上传。单独打开一个终端并保持运行：

```powershell
ssh -N `
  -L 0.0.0.0:15000:127.0.0.1:5000 `
  root@10.130.14.23
```

在另一个 PowerShell 终端检查隧道：

```powershell
Test-NetConnection 127.0.0.1 -Port 15000
curl.exe http://127.0.0.1:15000/v2/
```

Registry 无认证时第二条命令通常返回 `{}`；启用认证时返回 `401` 也说明 Registry 已经可达。

Docker Desktop 的 daemon 运行在虚拟机中，推送时使用 `host.docker.internal:15000` 访问 Windows 主机上的转发端口：

```powershell
$Registry = "10.130.14.23:5000"
$TunnelRegistry = "host.docker.internal:15000"
$Tag = "2026.8.10"
```

如果 Registry 需要认证：

```powershell
docker login $TunnelRegistry
```

为了避免每个镜像重复写 tag/push 命令，可以在当前 PowerShell 会话定义：

```powershell
function Push-TunneledImage {
  param(
    [Parameter(Mandatory = $true)][string]$Repository,
    [Parameter(Mandatory = $true)][string]$ImageTag
  )

  $serverImage = "$Registry/${Repository}:$ImageTag"
  $tunnelImage = "$TunnelRegistry/${Repository}:$ImageTag"
  docker tag $serverImage $tunnelImage
  if ($LASTEXITCODE -ne 0) { throw "docker tag failed: $serverImage" }
  docker push $tunnelImage
  if ($LASTEXITCODE -ne 0) { throw "docker push failed: $tunnelImage" }
}
```

完整流程始终是：

```text
本地构建: 10.130.14.23:5000/<image>:<tag>
临时 tag: host.docker.internal:15000/<image>:<tag>
隧道 push: host.docker.internal:15000/<image>:<tag>
集群拉取: 10.130.14.23:5000/<image>:<tag>
```

隧道地址只用于构建机上传。ClawManager 镜像卡、黄金盘 YAML 和 Kubernetes Pod 必须填写节点能够访问的服务器地址 `10.130.14.23:5000/...`，不能填写 `host.docker.internal:15000/...`。

Registry 是纯 HTTP 时，Docker Desktop daemon 需要把 `host.docker.internal:15000` 配置为 insecure registry；Kubernetes 节点的 containerd 需要信任节点实际拉取地址 `10.130.14.23:5000`。两边配置的地址不同，不要遗漏。

`0.0.0.0:15000` 会在构建机所有网卡上监听。只在可信网络和防火墙保护下使用，上传完成后在 SSH 终端按 `Ctrl+C` 关闭隧道。若 Docker daemon 能通过回环地址访问主机端口，优先改成更安全的 `127.0.0.1:15000`。

### 3.3 Kubernetes Windows VM 节点

Windows VM Pod 实际运行在 Linux 节点上的 QEMU/KVM 中。节点至少需要：

- CPU 开启 VT-x 或 AMD-V。
- `/dev/kvm` 存在且可用。
- `/dev/net/tun` 存在。
- 允许 privileged Pod。
- 每个 Windows 实例至少可调度 6 CPU、12Gi 内存。
- Registry、Windows 下载源、应用安装源可达。

检查并打标签：

```bash
ls -l /dev/kvm /dev/net/tun
egrep -c '(vmx|svm)' /proc/cpuinfo
kubectl label node <WINDOWS_NODE_NAME> clawmanager.io/windows-runtime=true --overwrite
kubectl get nodes -l clawmanager.io/windows-runtime=true
```

当前 ClawManager 对 Windows WorkBuddy 和 Windows Codex 强制要求：

- Pro 模式
- CPU 不少于 6 核
- 容器内存不少于 12Gi
- PVC 必须正好为 80Gi

容器会预留约 2Gi 内存给 QEMU 和外围进程，其余内存分配给 Windows 客体。

### 3.4 CSI 克隆能力

黄金盘方案要求 StorageClass 支持 Kubernetes CSI Volume Clone。本文使用 Longhorn：

```bash
kubectl get storageclass
kubectl get pods -n longhorn-system
```

必须满足：

- 黄金 PVC 已经 `Bound`。
- 黄金 PVC 和目标实例 PVC 在同一个 namespace。
- 两者 StorageClass 相同。
- 两者申请容量完全相同，当前为 80Gi。
- 黄金盘中的 Windows 必须干净关机，不能在运行中直接作为母盘。

当前实现不支持跨 namespace 克隆。每个用户 namespace 都必须准备同名黄金 PVC，或者先通过 Longhorn Backup/Restore 将黄金盘恢复到目标 namespace。

### 3.5 ClawManager 版本和用户 namespace

ClawManager 必须包含并已经执行下面的数据库迁移：

```text
044_add_workbuddy_instance_type.sql
045_update_workbuddy_windows_runtime.sql
046_add_codex_and_claude_code_instance_types.sql
047_add_instance_runtime_variant.sql
```

否则系统镜像卡或创建接口可能不认识 `workbuddy`、`codex`、`claude-code` 和 WorkBuddy 的 `runtime_variant`。

先在 ClawManager 中创建或登录目标用户，让平台创建用户 namespace，再查询实际名称：

```bash
kubectl get namespace | grep clawmanager
```

namespace 通常是 `<CLAWMANAGER_SYSTEM_NAMESPACE>-user-<USER_ID>`，例如 `clawmanager-ltt-user-1`。不要根据用户名猜测 user ID，也不要在不了解平台 RBAC、标签和 NetworkPolicy 的情况下手工创建替代 namespace。

Windows 黄金盘要服务多个用户时，需要在每个真实用户 namespace 中准备同名黄金 PVC。当前 ClawManager Deployment 只配置一个黄金 PVC 名称，但会在实例所属 namespace 内查找它。

Windows 客体操作系统和商业桌面软件仍需遵守各自许可条款。黄金盘中的 Windows 应使用组织合法授权的版本和激活方式。

## 4. 构建和推送 Linux 镜像

### 4.1 WorkBuddy Linux POC

WorkBuddy Linux 构建脚本会从官方更新接口解析 Intel/x64 DMG，将其交给社区转换工具，再打包到 Webtop 镜像：

```powershell
$Registry = "10.130.14.23:5000"
$Tag = "2026.8.10"

powershell -ExecutionPolicy Bypass -File .\workbuddy\build-local.ps1 `
  -ImageName "$Registry/workbuddy-linux:$Tag"

Push-TunneledImage -Repository "workbuddy-linux" -ImageTag $Tag
```

也可以指定已经下载的 DMG：

```powershell
.\workbuddy\build-local.ps1 `
  -DmgPath "D:\Downloads\WorkBuddy.dmg" `
  -ImageName "$Registry/workbuddy-linux:$Tag"
```

本地验证：

```powershell
docker run -d --name workbuddy-linux-test `
  --shm-size=1g `
  -p 127.0.0.1:3001:3001 `
  -v workbuddy-linux-config:/config `
  "$Registry/workbuddy-linux:$Tag"
```

访问 `https://127.0.0.1:3001`。完成验证后删除测试容器，不删除数据卷：

```powershell
docker rm -f workbuddy-linux-test
```

### 4.2 Codex Linux CLI 镜像

```powershell
$CodexVersion = "latest"

docker build `
  -f codex/Dockerfile `
  --build-arg "CODEX_VERSION=$CodexVersion" `
  -t "$Registry/codex:$Tag" `
  .

Push-TunneledImage -Repository "codex" -ImageTag $Tag
```

镜像会安装 `@openai/codex`，使用 `/config/.codex` 保存配置，并在 KDE 桌面中自动打开终端。当前 ClawManager 的正式 `codex` 创建路径是 Windows Desktop；这个 Linux 镜像主要用于独立验证或后续增加 Codex runtime variant。

### 4.3 Claude Code Linux 镜像

生产环境应固定版本，不建议长期使用 `latest`：

```powershell
$ClaudeCodeVersion = "latest"

docker build `
  -f claude-code/Dockerfile `
  --build-arg "CLAUDE_CODE_VERSION=$ClaudeCodeVersion" `
  -t "$Registry/claude-code:$Tag" `
  .

Push-TunneledImage -Repository "claude-code" -ImageTag $Tag
```

镜像会安装 `@anthropic-ai/claude-code`，使用：

```text
配置目录: /config/.claude
工作目录: /config/workspace
桌面端口: 3001
```

ClawManager 会注入 `CLAWMANAGER_LLM_BASE_URL` 和实例级 token。启动脚本将其转换成 Claude Code 使用的：

```text
ANTHROPIC_BASE_URL
ANTHROPIC_AUTH_TOKEN
ANTHROPIC_MODEL
```

不要同时注入 `ANTHROPIC_API_KEY`，否则 Claude Code 会认为存在两套认证方式。

## 5. 构建和推送 Windows VM 镜像

Windows 派生镜像必须先构建通用基础镜像。

### 5.1 构建基础镜像

```powershell
$Registry = "10.130.14.23:5000"
$BaseTag = "2026.8.10-base"

docker build `
  -f windows-vm/Dockerfile `
  -t "$Registry/windows-vm:$BaseTag" `
  .

Push-TunneledImage -Repository "windows-vm" -ImageTag $BaseTag
```

### 5.2 构建 WorkBuddy Windows 镜像

默认从 WorkBuddy 官方更新接口下载当前 Windows x64 安装器，并下载 Microsoft Edge Enterprise MSI：

```powershell
$WorkBuddyTag = "2026.8.10"

docker build `
  -f windows-vm/Dockerfile.workbuddy `
  --build-arg "WINDOWS_VM_BASE_IMAGE=$Registry/windows-vm:$BaseTag" `
  -t "$Registry/windows-vm-workbuddy:$WorkBuddyTag" `
  .

Push-TunneledImage -Repository "windows-vm-workbuddy" -ImageTag $WorkBuddyTag
```

为了可重复构建，可以固定安装器 URL：

```powershell
docker build `
  -f windows-vm/Dockerfile.workbuddy `
  --build-arg "WINDOWS_VM_BASE_IMAGE=$Registry/windows-vm:$BaseTag" `
  --build-arg "WORKBUDDY_INSTALLER_URL=https://example.internal/WorkBuddySetup.exe" `
  -t "$Registry/windows-vm-workbuddy:$WorkBuddyTag" `
  .
```

镜像构建完成不代表 Windows 内已经安装 WorkBuddy。安装动作发生在“新黄金盘第一次启动 Windows”的 OEM 阶段。

### 5.3 构建 Codex Windows 镜像

先缓存 Windows 开发工具安装包：

```powershell
powershell -ExecutionPolicy Bypass `
  -File .\windows-vm\fetch-codex-devtools.ps1
```

默认版本为：

```text
Git for Windows 2.51.0
Node.js 22.18.0
Python 3.13.7
ripgrep 14.1.1
```

然后构建并推送：

```powershell
$CodexWindowsTag = "2026.8.10-phase2-r2"

docker build `
  -f windows-vm/Dockerfile.codex `
  --build-arg "WINDOWS_VM_BASE_IMAGE=$Registry/windows-vm:$BaseTag" `
  -t "$Registry/windows-vm-codex:$CodexWindowsTag" `
  .

Push-TunneledImage -Repository "windows-vm-codex" -ImageTag $CodexWindowsTag
```

该镜像包含：

- 官方 Store-signed ChatGPT/Codex x64 MSIX 和离线许可证。
- Windows 11 简体中文安装参数。
- Git、Node.js、npm、Python、ripgrep。
- 长路径和开发者模式设置。
- 登录时写入 Codex 配置并自动启动桌面应用的 Bootstrap 脚本。

验证 Registry 中的镜像：

```powershell
docker pull "$Registry/windows-vm-workbuddy:$WorkBuddyTag"
docker pull "$Registry/windows-vm-codex:$CodexWindowsTag"
docker image inspect "$Registry/windows-vm-codex:$CodexWindowsTag" `
  --format '{{json .RepoDigests}}'
```

## 6. 在服务器制作 Windows 黄金 PVC

### 6.1 命名和版本策略

黄金盘应使用不可变版本名：

```text
workbuddy-golden-v1
codex-golden-v1
codex-golden-v2
```

不要在原黄金盘上直接升级软件。正确升级流程是：

1. 构建新的不可变镜像标签。
2. 创建新的黄金 PVC。
3. 完成安装和验收。
4. 做一次临时克隆启动测试。
5. 切换 ClawManager 环境变量。
6. 保留旧黄金盘一段回滚期。

### 6.2 创建 Codex 黄金盘

仓库已经提供当前可用清单：

[windows-vm/k8s/codex-golden-v2.yaml](../windows-vm/k8s/codex-golden-v2.yaml)

复制一份环境专用文件，然后至少修改：

- `metadata.namespace`
- PVC、Pod、Service 名称和版本标签
- `storageClassName`
- `containers[].image`
- 代理地址和 `NO_PROXY`
- CPU、内存和磁盘参数

应用：

```bash
kubectl apply -f windows-vm/k8s/codex-golden-v2.yaml
kubectl get pvc,pod,svc -n clawmanager-ltt-user-1 | grep codex-golden-v2
kubectl logs -f pod/codex-golden-v2-builder -n clawmanager-ltt-user-1
```

第一次启动可能需要下载 Windows ISO、安装 Windows、执行 OEM、安装开发工具和重启。根据网络与存储性能，通常需要 20 到 60 分钟。

在可以直接使用 kubeconfig 的工作站上打开 noVNC：

```bash
kubectl port-forward -n clawmanager-ltt-user-1 svc/codex-golden-v2-builder 18006:8006
```

浏览器访问：

```text
http://127.0.0.1:18006
```

如果只能从服务器执行 `kubectl`，建立两级转发：

```bash
# 服务器上
kubectl port-forward -n clawmanager-ltt-user-1 svc/codex-golden-v2-builder 18006:8006 --address 127.0.0.1

# 工作站上
ssh -L 18006:127.0.0.1:18006 root@<K8S_SERVER>
```

### 6.3 验证 Codex 黄金盘

Windows 桌面进入稳定状态后，验证：

```powershell
Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version
Get-WinSystemLocale
Get-WinUserLanguageList
Get-AppxPackage -Name OpenAI.Codex
git --version
node --version
npm --version
python --version
rg --version
Test-Path C:\OEM\codex\installed.marker
Test-Path C:\ProgramData\ClawManager\codex-bootstrap.ps1
Get-ScheduledTask -TaskName "ClawManager Codex Bootstrap"
Get-Content C:\OEM\codex\install.log -Tail 50
Get-Content C:\OEM\codex\devtools-install.log -Tail 50
```

也可以使用仓库里的自动验证脚本：

```bash
kubectl cp windows-vm/k8s/verify-codex-golden.ps1 \
  clawmanager-ltt-user-1/codex-golden-v2-builder:/shared/verify-codex-golden.ps1
```

在 Windows PowerShell 中执行：

```powershell
powershell -ExecutionPolicy Bypass `
  -File \\host.lan\Data\verify-codex-golden.ps1
```

然后从 Pod 读取结果：

```bash
kubectl exec -n clawmanager-ltt-user-1 codex-golden-v2-builder -- \
  cat /shared/verify-result.txt
```

必须确认 Codex 能真正启动到登录或工作界面，而不只是 `Get-AppxPackage` 能找到包。

### 6.4 创建 WorkBuddy 黄金盘

WorkBuddy 使用同样的构建 Pod 结构，但关键参数不同：

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: workbuddy-golden-v1
  namespace: clawmanager-ltt-user-1
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 80Gi
  storageClassName: longhorn
---
apiVersion: v1
kind: Pod
metadata:
  name: workbuddy-golden-v1-builder
  namespace: clawmanager-ltt-user-1
  labels:
    app: workbuddy-golden-v1-builder
spec:
  restartPolicy: Never
  nodeSelector:
    clawmanager.io/windows-runtime: "true"
  terminationGracePeriodSeconds: 120
  containers:
    - name: desktop
      image: 10.130.14.23:5000/windows-vm-workbuddy:2026.8.10
      imagePullPolicy: Always
      securityContext:
        privileged: true
      env:
        - {name: VERSION, value: "10l"}
        - {name: CPU_CORES, value: "6"}
        - {name: RAM_SIZE, value: "10G"}
        - {name: DISK_SIZE, value: "64G"}
        - {name: DISK_FMT, value: "qcow2"}
        - {name: SHUTDOWN, value: "Y"}
        - {name: QEMU_TIMEOUT, value: "120"}
      ports:
        - {name: http, containerPort: 8006}
        - {name: rdp, containerPort: 3389}
      resources:
        requests: {cpu: "6", memory: 12Gi}
        limits: {cpu: "6", memory: 12Gi}
      volumeMounts:
        - {name: data, mountPath: /storage}
        - {name: shared, mountPath: /shared}
        - {name: shm, mountPath: /dev/shm}
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: workbuddy-golden-v1
    - name: shared
      emptyDir: {}
    - name: shm
      emptyDir:
        medium: Memory
        sizeLimit: 4Gi
---
apiVersion: v1
kind: Service
metadata:
  name: workbuddy-golden-v1-builder
  namespace: clawmanager-ltt-user-1
spec:
  selector:
    app: workbuddy-golden-v1-builder
  ports:
    - {name: web, port: 8006, targetPort: http}
    - {name: rdp, port: 3389, targetPort: rdp}
```

保存为 `workbuddy-golden-v1.yaml` 后执行：

```bash
kubectl apply -f workbuddy-golden-v1.yaml
kubectl port-forward -n clawmanager-ltt-user-1 svc/workbuddy-golden-v1-builder 18007:8006
```

访问 `http://127.0.0.1:18007`，确认：

- Windows 安装和重启完成。
- Edge 已安装并能打开。
- WorkBuddy 已安装并能启动。
- `C:\OEM\workbuddy-installed.marker` 存在。
- `C:\OEM\workbuddy-install.log` 没有 `ERROR`。
- `C:\Users\Docker\AppData\Local\Programs\WorkBuddy\WorkBuddy.exe` 存在。

不要把个人账号、API key 或用户文件固化到黄金盘。黄金盘只保存公共软件和系统设置。

### 6.5 干净关机并固化黄金盘

验证完成后，必须通过 QEMU monitor 请求 Windows 正常关机：

```bash
kubectl exec -n clawmanager-ltt-user-1 codex-golden-v2-builder -- \
  bash -lc "printf 'system_powerdown\n' | nc -U -q 1 /run/shm/monitor.sock"

kubectl wait -n clawmanager-ltt-user-1 \
  --for=jsonpath='{.status.phase}'=Succeeded \
  pod/codex-golden-v2-builder \
  --timeout=300s
```

WorkBuddy 将 Pod 名替换为 `workbuddy-golden-v1-builder`。

关机完成后删除构建 Pod 和 Service，保留 PVC：

```bash
kubectl delete pod codex-golden-v2-builder -n clawmanager-ltt-user-1
kubectl delete service codex-golden-v2-builder -n clawmanager-ltt-user-1
kubectl get pvc codex-golden-v2 -n clawmanager-ltt-user-1
```

不要执行下面这条命令，除非明确要销毁黄金盘：

```bash
kubectl delete pvc codex-golden-v2 -n clawmanager-ltt-user-1
```

### 6.6 黄金盘克隆烟测

Codex 可以复制并修改现有烟测清单：

[windows-vm/k8s/codex-golden-v2-smoke.yaml](../windows-vm/k8s/codex-golden-v2-smoke.yaml)

应用并等待：

```bash
kubectl apply -f windows-vm/k8s/codex-golden-v2-smoke.yaml
kubectl wait -n clawmanager-ltt-user-1 \
  --for=condition=Ready pod/codex-golden-v2-smoke \
  --timeout=600s
kubectl logs -n clawmanager-ltt-user-1 codex-golden-v2-smoke
```

通过标准：

```text
Booting Windows using QEMU
Windows started successfully
```

日志中不应再次出现下载或安装 Windows。验证完成后正常关机，再删除临时 Pod 和临时 PVC。

## 7. 配置 ClawManager

### 7.1 配置黄金 PVC 名称

将黄金 PVC 名称写入 ClawManager Deployment：

```bash
kubectl set env deployment/clawmanager-app \
  -n clawmanager-ltt-system \
  CLAWMANAGER_WORKBUDDY_GOLDEN_PVC=workbuddy-golden-v1 \
  CLAWMANAGER_CODEX_GOLDEN_PVC=codex-golden-v2

kubectl rollout status deployment/clawmanager-app \
  -n clawmanager-ltt-system \
  --timeout=300s
```

同时把这些变量写入正式部署清单，避免下一次 `kubectl apply` 回滚配置：

- `ClawManager/deployments/k8s/single-node/clawmanager.yaml`
- `ClawManager/deployments/k8s/cluster/clawmanager.yaml`

检查：

```bash
kubectl get deployment clawmanager-app -n clawmanager-ltt-system \
  -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
  | grep GOLDEN_PVC
```

### 7.2 配置系统镜像卡

进入 ClawManager：

```text
系统设置 -> Runtime/System Image Settings
```

设置并保存：

| 类型 | 模式 | 镜像示例 |
| --- | --- | --- |
| WorkBuddy | Desktop / Pro / Windows | `10.130.14.23:5000/windows-vm-workbuddy:2026.8.10` |
| Codex | Desktop / Pro / Windows | `10.130.14.23:5000/windows-vm-codex:2026.8.10-phase2-r2` |
| Claude Code | Desktop / Pro / Linux | `10.130.14.23:5000/claude-code:2026.8.10` |

镜像卡只决定新实例使用的容器镜像，不会升级已经存在的实例，也不会修改黄金盘内容。

### 7.3 WorkBuddy 预热池（可选）

Longhorn 全量克隆可能需要几分钟。WorkBuddy 已实现磁盘预热池：

```bash
kubectl set env deployment/clawmanager-app \
  -n clawmanager-ltt-system \
  CLAWMANAGER_WORKBUDDY_PREWARM_POOL_SIZE=2 \
  CLAWMANAGER_WORKBUDDY_PREWARM_IMAGE=10.130.14.23:5000/windows-vm-workbuddy:2026.8.10 \
  CLAWMANAGER_WORKBUDDY_PREWARM_INTERVAL_SECONDS=30
```

查看预热资源：

```bash
kubectl get pvc,pod -n clawmanager-ltt-user-1 \
  -l clawmanager.io/workbuddy-prewarm=true
```

预热的是完整磁盘数据，不是正在运行的 Windows VM。当前 Codex 尚未实现独立预热池，因此 Codex 冷创建通常需要 2 到 5 分钟。

## 8. 模型与凭据注入

### 8.1 通用 AI Gateway 环境变量

Linux 托管 runtime 会收到：

```text
CLAWMANAGER_LLM_BASE_URL
CLAWMANAGER_LLM_API_KEY
CLAWMANAGER_LLM_MODEL
CLAWMANAGER_LLM_PROVIDER
OPENAI_BASE_URL
OPENAI_API_KEY
OPENAI_MODEL
```

实例 token 只能由 ClawManager 创建并注入，不要写进 Dockerfile、黄金盘、Git 或文档示例。

### 8.2 Windows Codex

ClawManager 为每个 Windows Codex 实例创建独立 Secret，内容为：

```text
config.toml
auth.json
```

Secret 只读挂载到：

```text
Linux:   /shared/.clawmanager
Windows: \\host.lan\Data\.clawmanager
```

Windows 登录时，`codex-bootstrap.ps1` 原子复制到：

```text
C:\Users\Docker\.codex\config.toml
C:\Users\Docker\.codex\auth.json
```

`auth.json` 的 ACL 只允许当前用户和 SYSTEM。每次实例启动会重新安装配置，用户不应依赖手工修改长期覆盖平台配置。

当前配置使用 ClawManager Responses 兼容路由，`base_url` 必须以 `/v1` 结尾，因为 Codex 会在其后追加 `/responses`。

权限配置需要根据 Codex 版本选择一套，不能混用：

```toml
# 新权限档方案，适用于采用 permission profiles 的 Codex Desktop
default_permissions = ":danger-full-access"
approval_policy = "never"
web_search = "live"
```

或者旧方案：

```toml
sandbox_mode = "danger-full-access"
approval_policy = "never"
web_search = "live"
```

截至本文对应的代码版本，ClawManager 的 `renderWindowsCodexBootstrapFiles` 使用旧的 `sandbox_mode` 方案。如果要切换到 `default_permissions`，需要修改 ClawManager 的 `backend/internal/services/instance_service.go`、更新对应测试、重新构建并滚动发布 ClawManager；只修改 AgentsRuntime 镜像不会改变每个实例生成的 Secret。

如果会话内出现：

```text
CODEX_PERMISSION_PROFILE=:workspace
CODEX_SANDBOX_NETWORK_DISABLED=1
HTTP_PROXY=http://127.0.0.1:9
```

说明该会话仍处于工作区沙箱，`danger-full-access` 没有实际生效。修改配置后必须完全退出 Codex Desktop，再启动并新建会话验证。

### 8.3 Claude Code

Claude Code 通过 Anthropic Messages 兼容路由访问 ClawManager Gateway。镜像启动脚本使用：

```text
ANTHROPIC_BASE_URL=<CLAWMANAGER_LLM_BASE_URL>
ANTHROPIC_AUTH_TOKEN=<CLAWMANAGER_LLM_API_KEY>
ANTHROPIC_MODEL=<平台选择的模型>
```

实例内不需要执行 `claude login`。旧 OAuth 状态和 `ANTHROPIC_API_KEY` 会被启动脚本清理，避免覆盖实例级 token。

### 8.4 WorkBuddy

WorkBuddy Linux 会将平台模型写入：

```text
/config/.workbuddy/models.json
/config/.workbuddy/model.json
```

完整自定义模型数组可以通过 `CLAWMANAGER_WORKBUDDY_MODELS_JSON` 注入。

Windows WorkBuddy MVP 当前没有 Windows 客体 Agent，也不会自动写入 WorkBuddy 模型配置。需要在 WorkBuddy 应用中配置，或者后续增加类似 Codex 的 `/shared` Bootstrap。不要把生产 API key 固化进黄金盘。

## 9. 创建实例与上线验收

### 9.1 Windows WorkBuddy/Codex

在创建页选择 Pro 模式，并填写：

```text
CPU:    >= 6
Memory: >= 12Gi
Disk:   80Gi（必须完全一致）
```

ClawManager 会自动：

1. 在用户 namespace 中从黄金 PVC 克隆 `clawreef-<instance-id>-pvc`。
2. 将实例 Pod 调度到 `clawmanager.io/windows-runtime=true` 节点。
3. 将 PVC 挂载为 `/storage`。
4. 等待 RDP 3389 就绪后把实例标记为 Available。

检查：

```bash
kubectl get pod,pvc -n clawmanager-ltt-user-1 | grep <INSTANCE_ID>
kubectl describe pod -n clawmanager-ltt-user-1 <POD_NAME>
kubectl logs -n clawmanager-ltt-user-1 <POD_NAME>
```

正常日志包含：

```text
Booting Windows using QEMU
Windows started successfully
```

### 9.2 Claude Code 和 WorkBuddy Linux

创建后检查：

```bash
kubectl get pod,pvc -n clawmanager-ltt-user-1 | grep <INSTANCE_ID>
kubectl logs -n clawmanager-ltt-user-1 <POD_NAME> --all-containers
kubectl exec -n clawmanager-ltt-user-1 <POD_NAME> -- env \
  | grep -E 'CLAWMANAGER_|ANTHROPIC_|OPENAI_'
```

不要在日志或工单里粘贴 API key/token 的完整值。

验收至少包括：

- 浏览器桌面能加载。
- 终端中的智能体自动启动。
- 能看到持久化工作目录。
- 模型请求经过 ClawManager Gateway。
- Pod 重启后配置和工作区仍存在。
- ClawManager Agent 状态保持 online。

## 10. 黄金盘升级与旧盘清理

建议保留旧黄金盘至少 7 天，并满足下面条件后再删除：

1. 至少创建一个真实的新版本实例。
2. 完成桌面、模型、文件、重启和关机测试。
3. ClawManager 所有副本都已经切换到新黄金盘名。
4. 已经不再需要快速回滚，或已有 Longhorn Backup。

查看哪些实例 PVC 来源于旧黄金盘：

```bash
kubectl get pvc -n clawmanager-ltt-user-1 \
  -o custom-columns='NAME:.metadata.name,SOURCE:.spec.dataSource.name,STATUS:.status.phase'
```

CSI full-copy 克隆完成后，实例 PVC 与源盘相互独立；删除源黄金 PVC 不会自动删除已有实例盘。但删除前仍应保留回滚窗口，并检查 Longhorn snapshot/backup。

确认后删除指定旧盘：

```bash
kubectl delete pvc codex-golden-v1 -n clawmanager-ltt-user-1
```

不要批量按通配符删除 PVC，也不要为了强制删除而直接移除 Longhorn 或 cloning-protection finalizer。

## 11. 常见故障排查

### 11.1 一直停在 Starting

先看 Pod 和事件：

```bash
kubectl get pod,pvc -n clawmanager-ltt-user-1 | grep <INSTANCE_ID>
kubectl describe pod -n clawmanager-ltt-user-1 <POD_NAME>
kubectl get events -n clawmanager-ltt-user-1 --sort-by=.lastTimestamp | tail -50
```

`volume is not ready for workloads` 在 Longhorn 全量复制期间可能短暂出现。黄金盘实际数据约 25 到 30GB、两个副本时，冷创建通常需要数分钟。超过 10 分钟仍未挂载，再检查 Longhorn Volume 和 Replica：

```bash
kubectl get volumes.longhorn.io -n longhorn-system
kubectl get replicas.longhorn.io -n longhorn-system
```

### 11.2 PVC Bound，但 Pod 仍无法启动

`Bound` 只表示 Kubernetes 已分配 PV，不代表 Longhorn full-copy 已经健康。检查 Longhorn 的 `cloneStatus`、`robustness` 和副本状态。

### 11.3 Windows 被重新安装

如果实例日志出现 Windows 下载/安装，而不是直接 `Booting Windows`：

- 黄金 PVC 中没有有效 `/storage/data.qcow2`。
- 实例没有从正确黄金盘克隆。
- 黄金盘/实例 PVC 容量或 StorageClass 不一致。
- ClawManager 的 `CLAWMANAGER_*_GOLDEN_PVC` 指向了错误名称。

### 11.4 Codex 请求 404

检查 `config.toml` 中的 `base_url`。ClawManager Responses 路径最终应为：

```text
.../api/v1/gateway/llm/v1/responses
```

因此配置给 Codex 的 provider `base_url` 应停在最后的 `/v1`，不要提前写 `/responses`，也不能缺少 `/v1`。

### 11.5 Windows 共享目录和右侧文件浏览器不一致

这是两种不同存储：

- `/storage` 是 Windows 系统盘容器目录。
- `/shared` 在 Windows 中是 `\\host.lan\Data`。
- Linux Webtop 的 `/config` 不会自动等价为 Windows 的 `C:\Users\Docker`。

如果需要统一工作区，应明确设计独立共享 PVC 或 SMB/NFS 映射，不要直接把 Windows 系统盘当文件浏览器根目录。

### 11.6 浏览器提示危险或阻止站点

如果通过 IP 和自签名证书访问 ClawManager，Edge 可能把远程桌面页面误判为危险站点。生产环境应使用受信任域名和包含 SAN 的证书，不建议全局关闭 SmartScreen。

### 11.7 镜像拉取失败

```bash
kubectl describe pod -n <NAMESPACE> <POD_NAME>
crictl pull 10.130.14.23:5000/<IMAGE>:<TAG>
```

检查：

- 镜像标签是否存在。
- Kubernetes 节点能否访问 Registry。
- containerd 是否信任 HTTP/自签名 Registry。
- 私有 Registry 是否需要 `imagePullSecrets`。

## 12. 最终验收清单

交付前逐项确认：

- [ ] 三种镜像都使用不可变 tag，并已推送到 Kubernetes 节点可访问的 Registry。
- [ ] Windows 节点具备 KVM/TUN，并有正确 node label。
- [ ] Longhorn/CSI 支持同 namespace PVC Clone。
- [ ] WorkBuddy 和 Codex 黄金 PVC 均为 Bound 且 Windows 已干净关机。
- [ ] 黄金盘克隆烟测不会重新安装 Windows。
- [ ] ClawManager Deployment 中黄金盘名称正确。
- [ ] 系统镜像卡指向正确 Registry 镜像。
- [ ] Windows 实例使用 6 CPU、12Gi 内存、80Gi PVC。
- [ ] Codex 的 `config.toml`、`auth.json` 为实例级注入，没有固化到黄金盘。
- [ ] Claude Code 使用 `ANTHROPIC_AUTH_TOKEN` 访问 Gateway。
- [ ] WorkBuddy Windows 的“无自动模型注入”限制已经告知使用者。
- [ ] 新实例完成桌面、模型调用、持久化、重启和正常关机测试。
- [ ] 旧黄金盘仍保留在回滚窗口内。

## 13. 相关仓库文件

- [仓库总览](../Readme.md)
- [Windows VM 说明](../windows-vm/README.md)
- [Codex Windows Dockerfile](../windows-vm/Dockerfile.codex)
- [WorkBuddy Windows Dockerfile](../windows-vm/Dockerfile.workbuddy)
- [Codex 黄金盘清单](../windows-vm/k8s/codex-golden-v2.yaml)
- [Codex 克隆烟测清单](../windows-vm/k8s/codex-golden-v2-smoke.yaml)
- [WorkBuddy Linux 说明](../workbuddy/README.md)
- [Runtime Agent 通用接入规范](../runtime-agent-integration-guide.md)
