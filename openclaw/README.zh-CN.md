# ClawManager OpenClaw Image

<p align="center">
  <img src="docs//assets/openclaw_logo.jpg" alt="ClawManager" width="100%" />
</p>

<p align="center">
  <strong>Languages:</strong>
  <a href="./README.md">English</a> |
  <a href="./README.zh-CN.md">中文</a>
</p>

本指南帮助您为 **ClawManager** 控制面构建 **OpenClaw** 基础镜像，支持 **自动化配置注入**（API Key、Base URL 等）与多租户场景下的 **持久化目录布局**。

镜像现在也内置了 **OpenClaw Agent** 服务。它会在容器内自动启动，负责向 ClawManager 注册、发送心跳、轮询命令、托管 OpenClaw 进程，并提供基于 Gin 的本地健康检查接口。

**推荐优先使用 `Dockerfile.openclaw` 一键构建。** 若需在 WebTop 桌面内手动安装组件后再执行 `docker commit`，请见下文 **进阶：基础流程（手动 / docker commit）**。


## 项目概览

在 ClawManager 批量管理场景下，逐台手动配置容器不可扩展。本项目提供：

* **预装运行环境**：Node.js 与最新 OpenClaw CLI，开箱即用。
* **Agent 驱动启动**：容器启动时，`openclaw-agent`（Go 实现）以 root 身份运行，从 `/defaults/.openclaw` 同步到 `/config/.openclaw`、规范化 channels、修正归属权，然后降权到 `abc` 再派生 OpenClaw 进程。
* **动态注入**：用环境变量更新 `openclaw.json`，无需在桌面会话里手改文件。

---

## 快速开始（推荐）

镜像基于 `lscr.io/linuxserver/webtop:ubuntu-xfce`：Dockerfile 会安装 Node.js 与全局 OpenClaw，写入 `/defaults/.openclaw`，并将 `openclaw-agent` 二进制安装为 s6 服务。每次容器启动时，agent 会完成所有 bootstrap 工作（defaults 同步、extensions 目录、XFCE autostart、配置规范化、channels 合并），然后降权到 `abc` 再派生 `openclaw gateway run`。运行时配置位于 **`/config/.openclaw`**，而非 `~/.openclaw`。启动后可在容器日志中查找 `openclaw-agent` 相关输出。

### 构建

**Bash**

请在仓库根目录执行，这样 Docker 才能包含公共的 `clawmanager-agent/` 组件。

```
docker build -f openclaw/Dockerfile.openclaw -t openclaw:local .
```

### 运行

> **共享内存**：运行 WebTop 时必须传入 `--shm-size="1gb"`（至少 1GB），否则浏览器/桌面栈可能崩溃或行为异常。

**Bash**

```
docker run -d \
  --name=webtop-openclaw \
  --shm-size="1gb" \
  --restart unless-stopped \
  -e PUID=1000 \
  -e PGID=1000 \
  -e TZ=Asia/Shanghai \
  -e CLAWMANAGER_LLM_BASE_URL=https://your-gateway/v1 \
  -e CLAWMANAGER_LLM_API_KEY=your-sk-key \
  -e CLAWMANAGER_LLM_MODEL=gpt-4o \
  -p 3000:3000 \
  -p 3001:3001 \
  openclaw:local
```

按需调整端口与占位符。

---

## 环境变量

在 ClawManager 或 `docker run` 中设置以下变量，以写入 `openclaw.json`：

| 变量                         | 配置路径                                 | 说明                                                                      |
| ---------------------------- | ---------------------------------------- | ------------------------------------------------------------------------- |
| `CLAWMANAGER_LLM_BASE_URL` | `models.providers.auto.baseUrl`        | 网关或上游 Base URL                                                       |
| `CLAWMANAGER_LLM_API_KEY`  | `apiKey`                               | 模型 API 密钥                                                             |
| `CLAWMANAGER_LLM_MODEL`    | `primary` / `agents.defaults.models` | 模型 ID 替换；支持单值或 JSON 数组；由 agent 的配置规范化器统一处理 |
| `CLAWMANAGER_OPENCLAW_CHANNELS_JSON` | `channels`（合并）                     | JSON 对象，可含一个或多个通道键（`feishu`、`slack` 等）；与已有 `channels` 做浅合并；解析失败时容器初始化失败 |
| `OPENCLAW_AGENT_INSTANCE_ID` | agent 启动参数                           | 必填。实例唯一 ID，用于 `/api/v1/agent/register` |
| `OPENCLAW_AGENT_BOOTSTRAP_TOKEN` | agent 启动参数                      | 必填。agent 注册使用的 bootstrap token |
| `OPENCLAW_AGENT_CONTROL_PLANE_BASE_URL` | agent 启动参数             | 必填。ClawManager 基础地址 |
| `OPENCLAW_AGENT_INITIAL_CONFIG_REVISION_ID` | agent 启动参数      | 可选。初始配置 revision id |
| `OPENCLAW_AGENT_OPENCLAW_COMMAND` | 进程托管参数                  | 可选。默认是 `openclaw gateway run` |
| `OPENCLAW_AGENT_OPENCLAW_DOCTOR_COMMAND` | 启动修复参数          | 可选。默认是 `openclaw doctor --fix` |
| `OPENCLAW_AGENT_OPENCLAW_DOCTOR_POLICY` | 启动修复策略          | 可选。`auto`（默认）、`always` 或 `never`；`auto` 不会在首次拉起前执行 doctor，只会在 gateway 拉起失败、启动阶段退出或健康检查未就绪时自动修复 |
| `OPENCLAW_AGENT_OPENCLAW_STARTUP_HEALTH_TIMEOUT` | 进程托管参数 | 可选。默认 `90s`；gateway 健康检查未就绪时，等待多久后触发自动 doctor |
| `OPENCLAW_AGENT_STARTUP_NOTIFICATION_MESSAGE` | 桌面通知文案      | 可选。默认是 `正在启动龙虾` |
| `OPENCLAW_AGENT_STARTUP_REPAIR_NOTIFICATION_MESSAGE` | 桌面通知文案 | 可选。默认是 `正在修复启动环境，可能需要 1-2 分钟` |

agent 默认配置位于 `/etc/openclaw-agent/config.yaml`，初次启动时由 `/defaults/openclaw-agent/config.yaml` 填充；本地健康与调试接口默认监听 `:18080`。

---

## GitHub Actions 与 GHCR（可选）

工作流 [`.github/workflows/docker-ghcr.yml`](.github/workflows/docker-ghcr.yml) 会在推送到默认分支（`main` / `master`）或推送 `v*` 标签时构建 `Dockerfile.openclaw`，并推送到 **GitHub Container Registry**，发布时无需在本地执行 `docker build`。

**简要步骤**

1. 将仓库推送到 GitHub，在 **Actions** 中确认 **Build and push to GHCR** 成功。
2. 在 **Packages** 中查找镜像，地址一般为 `ghcr.io/<user>/<repo>`。
3. 私有包需先执行 `docker login ghcr.io`；若希望匿名 `docker pull`，请将 Package 设为 **Public**。

**Bash**

```
docker pull ghcr.io/<github_user>/<repo>:latest

docker run -d \
  --name=webtop-openclaw \
  --shm-size="1gb" \
  --restart unless-stopped \
  -e PUID=1000 -e PGID=1000 -e TZ=Asia/Shanghai \
  -e CLAWMANAGER_LLM_BASE_URL=https://your-gateway/v1 \
  -e CLAWMANAGER_LLM_API_KEY=your-sk-key \
  -e CLAWMANAGER_LLM_MODEL=gpt-4o \
  -e CLAWMANAGER_OPENCLAW_CHANNELS_JSON='{"feishu":{"enabled":true,"accounts":{"main":{"appId":"cli_xxx","appSecret":"your-secret"}}}}' \
  -p 3000:3000 -p 3001:3001 \
  ghcr.io/<github_user>/<repo>:latest
```

推送如 `v1.0.0` 等标签时，工作流还会按 metadata 规则发布语义化版本标签。

---

## 进阶：基础流程（`docker commit`）

仅在需要在保存镜像前于 WebTop 内安装额外工具时使用；与 `Dockerfile.openclaw` 二选一。

### 安装软件

在浏览器中打开 `https://<IP>:3001`，在终端中执行：

**Bash**

```
curl -fsSL https://deb.nodesource.com/setup_current.x | sudo -E bash -
sudo apt-get install -y nodejs

npm config set registry https://registry.npmmirror.com
sudo npm install -g openclaw@latest
```

### 初始化脚本与清理

1. **写入默认模板**：`cp -rp /config/.openclaw /defaults/`。
2. **安装 agent**：将编译好的 `openclaw-agent` 二进制放到 `/usr/local/bin/`，并把 `scripts/openclaw-agent-run` / `openclaw-agent-finish` 放到 `/etc/services.d/openclaw-agent/`。agent 会在每次启动时完成 `/defaults` → `/config` 同步与环境变量写入。
3. **保存镜像前清理**：`rm -rf /config/.openclaw`。若跳过此步，新容器可能无法按预期执行首次启动初始化。

### 保存镜像

**Bash**

```
docker commit webtop-running openclaw:v1.0
```

---

## 说明

* **权限**：agent 会在降权前对 `/config/.openclaw` 与 `/config/.config/autostart` 执行 `chown -R abc:abc`，保证默认用户可读写持久化配置。
* **Docker Compose**：将 `image` 指向您构建的标签，或在仓库根目录使用 `build`，设置 `context: .` 和 `dockerfile: openclaw/Dockerfile.openclaw`。不要仅依赖官方 `webtop` 镜像，其中不包含本仓库的模板与 agent 服务。
* **单独使用 WebTop**：若不使用 ClawManager 批量能力，可跳过进阶流程中与 ClawManager 相关的步骤。

---

## 链接

* [ClawManager - 管理AI代理最友好的方式](https://github.com/Yuan-lab-LLM/ClawManager)
