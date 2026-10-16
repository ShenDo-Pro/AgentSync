# AgentSync

AgentSync 将同一 Git 项目的 Codex 任务交接给 CodeBuddy。第一阶段由独立的 Daemon、Cloud 和 CodeBuddy Hook 组成；CCHV 通过只读历史接口接入，仓库目前没有包含或修改 CCHV 源码。

## 当前实现

- Daemon 只读取 `~/.codex/sessions` 和 `~/.codex/archived_sessions` 中的 JSONL 会话，提取用户消息和 Codex 最终答复，不写入 AI 工具原生文件。
- 本地 SQLite 保存原始消息记录、来源事件 ID、独立交接摘要、Git HEAD 与受跟踪文件状态，以及待上传队列。Cloud 不可用时继续采集，恢复后重试。
- 项目 ID 来自规范化的 Git `origin`；任务 ID 使用 Codex 会话 ID，分支参与交接筛选。Hook 默认选择该分支最近更新的独立任务；可以设置 `AGENTSYNC_TASK_ID` 指定较早的任务。
- CodeBuddy `SessionStart` Hook 从 Cloud 或本地 SQLite 读取交接摘要，自动注入新会话。
- Cloud 提供 `GET /v1/history?project_id=...` 和 `GET /v1/project-states?project_id=...` 供后续 CCHV 展示接入。

当前摘要从最近的用户消息与 Codex 最终答复确定性生成，不需要额外 AI API。不同厂商的原生聊天线程恢复不属于当前范围。

## 本地 Cloud

复制 `.env.example` 为 `.env`，把 `AGENTSYNC_TOKEN` 改为随机长字符串，然后执行：

```sh
docker compose up -d --build
```

默认只监听本机 `127.0.0.1:8765`。跨 SSH 机器接入时，需要给远程 Daemon 和 CodeBuddy 提供能够访问 Cloud 的地址；公网部署时应使用 HTTPS 反向代理。Cloud 数据位于 Docker volume。

没有 Docker 时，也可在 Python 3.11+ 环境启动服务端：

```sh
python -m pip install .
AGENTSYNC_TOKEN=your-token agentsync cloud
```

Windows 可运行 CI 构建的 `AgentSync.Server.exe`。它默认监听 `127.0.0.1:8765`，数据保存到用户目录的 `.agentsync/cloud.db`。启动前设置 `AGENTSYNC_TOKEN`；需要监听其他地址时设置 `AGENTSYNC_BIND`。

## Daemon 与 CodeBuddy

在运行 Codex 的设备上安装 Python 3.11+ 和 AgentSync，设置 `AGENTSYNC_CLOUD_URL` 与 `AGENTSYNC_TOKEN`，然后运行：

```sh
python -m pip install .
agentsync daemon
```

Daemon 默认每 5 秒扫描一次 Codex 会话。`AGENTSYNC_CODEX_HOME` 可覆盖 Codex 数据目录；本地数据库默认位于用户目录的 `.agentsync/local.db`。在 SSH Linux 项目中，Daemon 应运行在保存 Codex 会话的远程主机上。

在运行 CodeBuddy Hook 的同一环境中执行一次：

```sh
agentsync install-codebuddy
```

这会在用户级 `~/.codebuddy/settings.json` 添加 `SessionStart` Hook，并保留现有设置。新的 CodeBuddy 会话会加载 Hook；若 CodeBuddy 已运行，需按其 Hook 面板提示重新加载配置。Hook 使用当前 Git 项目和分支查询最近的 Codex 任务。要继续较早的任务，可设置 `AGENTSYNC_TASK_ID`。

Windows 客户端为 CI 构建的 `AgentSync.Client.exe`。先执行 `AgentSync.Client.exe install-codebuddy`，再运行 `scripts/install-windows-client.ps1 -ClientExe <完整路径>`，即可将 Daemon 注册为当前用户登录时自动启动的计划任务。Cloud 地址和令牌需预先配置为该用户的环境变量。

SSH Linux 可将 `systemd/agentsync-daemon.service` 复制到 `~/.config/systemd/user/`，把 `~/.config/agentsync/daemon.env` 设置为仅本人可读并写入 Cloud 地址及令牌，然后运行 `systemctl --user enable --now agentsync-daemon`。如需退出登录后继续运行，系统管理员还需为该用户启用 linger。

## 查询和验证

```sh
agentsync project /path/to/repo
agentsync scan
agentsync sync
agentsync handoff /path/to/repo
agentsync history /path/to/repo
```

`scan` 与 `sync` 适合手动检查；正常使用时 Daemon 自动执行。`handoff` 返回 `ready`、`missing` 或时间戳相同时的 `ambiguous`。`history` 输出本地原始对话消息及来源信息。Cloud 的读写接口均需要 `Authorization: Bearer <AGENTSYNC_TOKEN>`，只有 `/health` 不需要认证。

GitHub Actions 在 Windows 和 Linux 上运行端到端测试，在 Windows 上生成包含 `AgentSync.Client.exe` 和 `AgentSync.Server.exe` 的 `AgentSync-Windows.zip`，并构建 Cloud Docker 镜像。CI 产物需要在首次推送到 GitHub 后生成。

## 边界

项目 ID 在配置相同 Git `origin` 的设备之间一致；没有 `origin` 的仓库使用本机路径 ID。当前任务隔离以 Git 分支和 Codex 会话 ID 为准，多个候选任务不会合并，默认只接力最近更新的任务。交接摘要只取最新一轮消息，完整原始消息保存在独立事件表中。

当前仓库不分发 CCHV 或其他上游代码。将来接入 CCHV 时需保留对应上游许可证及署名。
