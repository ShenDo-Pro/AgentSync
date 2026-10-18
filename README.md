# AgentSync

AgentSync 将同一 Git 项目的 Codex 任务和历史对话交接给 CodeBuddy。当前可在 Windows 本机配合 Codex 与 CodeBuddyCN 使用，无需部署 Cloud，也无需额外 AI API。

## 先选择使用方式

| 需求 | 使用方式 | 在 CodeBuddyCN 中的效果 |
| --- | --- | --- |
| 让新会话知道 Codex 最近在做什么 | 一次配置后，后台采集 + SessionStart Hook | 新会话获得最近任务摘要，不新增历史聊天 |
| 在聊天列表里看到 Codex 的对话，并继续聊天 | 退出 CodeBuddyCN 后运行历史导入命令 | 新增 `[Codex]` 历史会话，包含用户文字、助手答复和可见进度消息 |

**后台采集不等于实时复制历史。** 原生历史目前按快照导入；Codex 后续有新消息，需要再次导入。当前为 Codex → CodeBuddyCN 单向接力，不会把 CodeBuddyCN 的新消息写回 Codex。

## 当前实现

- Daemon 只读取 `~/.codex/sessions` 和 `~/.codex/archived_sessions` 中的 JSONL 会话，提取用户消息和 Codex 最终答复，不写入 AI 工具原生文件。
- 本地 SQLite 保存原始消息记录、来源事件 ID、独立交接摘要、Git HEAD 与受跟踪文件状态，以及待上传队列。Cloud 不可用时继续采集，恢复后重试。
- 项目 ID 来自规范化的 Git `origin`；任务 ID 使用 Codex 会话 ID，分支参与交接筛选。Hook 默认选择该分支最近更新的独立任务；可以设置 `AGENTSYNC_TASK_ID` 指定较早的任务。
- CodeBuddy `SessionStart` Hook 从 Cloud 或本地 SQLite 读取交接摘要，自动注入新会话。
- Cloud 提供 `GET /v1/history?project_id=...` 和 `GET /v1/project-states?project_id=...` 供后续 CCHV 展示接入。

当前摘要从最近的用户消息与 Codex 最终答复确定性生成，不需要额外 AI API。
另提供 CodeBuddyCN 3.10 原生历史快照导入，可在聊天列表中查看 Codex 的可见文字消息。

## Windows 一键本地配置

准备好 Git、Python 3.11+、Codex 和 CodeBuddyCN。先在 Codex 中使用一次 Git 项目，并在 CodeBuddyCN 打开同一个项目，让两边创建本地数据目录。

在仓库目录打开 PowerShell，运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-local.ps1
```

自动寻找 Codex 配套 Python，也可通过 `-PythonExe <python.exe完整路径>` 指定 Python 3.11+。
脚本将程序复制到 `~/.agentsync/app`，备份并更新 CodeBuddyCN 用户 Hook，首次扫描 Codex 会话，
启动隐藏的后台采集进程，并通过当前用户 Startup 中的 `AgentSync.vbs` 设置登录自启。
本模式只使用 `~/.agentsync/local.db`，不访问 Cloud，不需要令牌。日志位于 `~/.agentsync/daemon.log`。
运行依赖所选 Python 路径持续可用；重新配置可刷新程序副本和 Hook。

指定自己的 Python 安装时：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-local.ps1 -PythonExe "C:\Python312\python.exe"
```

自动配置不会向 PATH 安装 `agentsync` 命令。以下本地操作使用安装时的 Python 和已部署的启动器。
如果使用的是脚本自动找到的 Codex 配套 Python，在每个新的 PowerShell 窗口先设置：

```powershell
$AgentSyncPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$AgentSyncLauncher = Join-Path $env:USERPROFILE '.agentsync\agentsync-local.py'
```

若安装时指定了 `-PythonExe`，将 `$AgentSyncPython` 改为该 Python 的完整路径。

### 日常摘要接力

配置后，在 CodeBuddyCN 打开相同 Git 项目和分支，新建会话，询问“上一条 Codex 任务是什么？”。
如已有窗口未加载 Hook，请重新加载窗口后再新建会话。采集间隔默认 5 秒。
后台 Hook 提供 Codex 到 CodeBuddyCN 的单向任务摘要接力。若需要在聊天列表中看到原始对话，使用下面的历史导入命令。

## 导入历史对话到 CodeBuddyCN

1. 找到要导入的 Codex 会话文件。下面会列出最近 20 个会话的项目目录、会话 ID 和文件路径，按 `Workspace` 选择目标项目；不要直接假定最新文件就是目标会话。

```powershell
Get-ChildItem "$env:USERPROFILE\.codex\sessions" -Filter 'rollout-*.jsonl' -Recurse |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 20 |
    ForEach-Object {
        $session = (Get-Content -LiteralPath $_.FullName -Encoding UTF8 -TotalCount 1 | ConvertFrom-Json).payload
        [PSCustomObject]@{ Workspace = $session.cwd; TaskId = $session.id; File = $_.FullName }
    } | Format-List
```

已归档会话可在 `~/.codex/archived_sessions` 中按同样方法查找。

2. 正常退出所有 CodeBuddyCN 窗口，保存需要保留的编辑内容。
3. 在已设置上述两个变量的 PowerShell 窗口中执行，将示例路径换成第一步找到的文件：

```powershell
& $AgentSyncPython -X utf8 $AgentSyncLauncher import-codebuddy "C:\完整路径\rollout-会话.jsonl" --name "项目开发记录" --activate
```

4. 命令返回 `imported` 后，重新打开 CodeBuddyCN 的同一项目，点击聊天面板右上角的时钟图标“历史对话记录”，选择 `[Codex] 项目开发记录`。打开后可查看历史并继续聊天。

命令输出包含消息数量、会话 ID 和备份位置。相同快照返回 `unchanged`，不会重复创建。
若采用 `python -m pip install .` 安装，也可直接使用 `agentsync import-codebuddy ...`。
导入程序自动寻找该项目的现有 CodeBuddyCN 历史目录；首次使用请先在 CodeBuddyCN 打开项目一次。
多个账号匹配时需显式指定 `--history-dir`。`--activate` 更新原生历史的当前会话指针；
CodeBuddyCN 若恢复了上次的标签页，仍需点击聊天面板右上角“历史对话记录”，选择 `[Codex]` 会话打开。

导入前完整备份项目历史到 `~/.agentsync/backups/`，然后新增 `[Codex]` 会话。
复制用户文字、助手答复和可见进度消息，保留顺序、角色和时间；不复制工具执行状态、系统指令和环境注入消息。
这是独立的历史快照：重复导入相同内容不会重复创建；源对话有新增消息时会创建新快照，保留在 CodeBuddyCN 中继续聊的旧会话。
为避免覆盖客户端缓存，CodeBuddyCN 运行时会拒绝导入；后台采集不会持续写入原生历史。
目前根据本地 CodeBuddyCN 3.10.0 的格式实现，其他版本尚未验证。

## 本地运行检查与维护

使用前面的 `$AgentSyncPython` 和 `$AgentSyncLauncher` 变量：

```powershell
# 立即采集一次；正常情况下后台每 5 秒执行
& $AgentSyncPython -X utf8 $AgentSyncLauncher scan

# 查看当前项目的交接摘要，返回 ready / missing / ambiguous
& $AgentSyncPython -X utf8 $AgentSyncLauncher handoff .

# 查看本地数据库保存的对话消息
& $AgentSyncPython -X utf8 $AgentSyncLauncher history . --limit 20

# 查看后台日志
Get-Content "$env:USERPROFILE\.agentsync\daemon.log" -Tail 30
```

`handoff .` 和 `history .` 中的 `.` 是 PowerShell 当前目录，需要先进入目标 Git 项目。
后台程序没有独立窗口，正常运行时不会反复弹出黑色控制台。

### 停止、启动和更新

临时停止时，通过 PID 文件和命令行共同确认进程身份后结束采集：

```powershell
$AgentSyncPidFile = Join-Path $env:USERPROFILE '.agentsync\daemon.pid'
if (Test-Path -LiteralPath $AgentSyncPidFile) {
    $AgentSyncProcessId = [int](Get-Content -LiteralPath $AgentSyncPidFile)
    $AgentSyncProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $AgentSyncProcessId"
    if ($AgentSyncProcess.CommandLine -like '*agentsync-local.py*daemon*') {
        Stop-Process -Id $AgentSyncProcessId
    }
}
```

重新启动已安装的后台程序：

```powershell
$AgentSyncStartup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\AgentSync.vbs'
Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $AgentSyncStartup + '"') -WindowStyle Hidden
```

更新仓库代码后，先按上面的步骤停止旧后台进程，再重新运行 `scripts/setup-local.ps1`。
仅重复安装不会替换正在运行的 Python 进程；旧进程退出后才会加载新代码。

如需取消登录自启，可在 Windows“运行”中输入 `shell:startup`，删除其中的 `AgentSync.vbs`。
这不会删除数据库或历史备份。若还需停用摘要注入，在 CodeBuddyCN Hook 配置中移除 AgentSync 的 SessionStart 命令。

### 数据位置与常见情况

| 位置或现象 | 说明 |
| --- | --- |
| `~/.agentsync/local.db` | 本地采集数据库 |
| `~/.agentsync/daemon.log` | 后台日志 |
| `~/.agentsync/backups/` | 导入前的 CodeBuddyCN 项目历史完整备份 |
| `~/.codebuddy/settings.agentsync-backup-*.json` | 自动配置前的 Hook 设置备份 |
| 新会话没有原来的聊天气泡 | Hook 只注入摘要；要查看聊天气泡，使用历史导入 |
| 导入提示 CodeBuddyCN 正在运行 | 正常退出所有 CodeBuddyCN 窗口后重试 |
| 导入后仍显示 New Chat | 点击右上角历史图标，选择 `[Codex]` 会话；客户端可能恢复旧标签页 |
| `handoff` 返回 `missing` | 检查是否为同一 Git 项目和分支，并执行一次 `scan` |
| 源码更新后效果未变 | 先停止旧后台进程，再运行自动配置脚本 |

## 可选：部署 Cloud

仅本机使用可跳过此节。Cloud 用于跨设备上传和查询采集数据；它不会自动在其他设备写入 CodeBuddyCN 原生历史。

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
