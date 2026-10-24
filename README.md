# zju-cli

## 常驻签到

```powershell
zju autosign discover --json                 # 只读发现，不启用、不提交
zju autosign start                           # 显式授权并保存启用状态
zju autosign start --settings autosign.example.json
zju autosign start --interval 4              # 修改间隔，保留其余已有设置
zju autosign status --json
zju autosign list --json
zju autosign answer ACTIVITY_ID --code CODE
zju autosign history --date 2026-10-23 --json
zju autosign stop                            # 持久禁用
```

首次启用前应核对雷达地点配置，示例见 [autosign.example.json](autosign.example.json)。不设置 Windows 启动项；后台以后每次启动都会恢复曾显式启用的 autosign。`status/list/history/stop` 在后台未运行时直接使用本地记录，无需学校凭据；`discover` 是只读操作，不改变启用状态。后台已在运行时使用它绑定的主账号。

默认每轮结束后等待 4 秒，不重叠、不积压；查询失败按 4、8、16、32、60 秒退避，恢复后使用配置间隔。数字码仅来自查询接口或显式 `answer`，没有枚举或批量试码。雷达依次使用配置地点、去重后的候选点及有足够距离反馈时的估计点，单活动有提交次数上限；距离不足、明确拒绝或尝试用尽时保留结束原因。默认地点沿用原脚本东一教学楼，默认候选点沿用原脚本列表。

活动状态包含 `discovered`、`processing`、`waiting_input`、`pending_verification`、`success`、`ended`。只有明确成功响应或读回确认成功才记为完成，来源 `platform_existing` 表示平台已有完成状态，`tool` 表示本工具提交后确认。响应丢失或崩溃后的 `processing/pending_verification` 只核实，不自动重提。停止后不再发起新提交，已发出的提交完成核实后退出。

状态固定存于用户私有后台运行目录的 `scheduler.sqlite3`，按账号与活动 ID 去重；按北京时间导出 `autosign-YYYY-MM-DD.jsonl`。数据库是来源，导出失败可在后续检查或后台启动时补写；日志、通知失败不会触发再次签到。完成记录不含签到码、精确位置或原始响应。Windows 默认尝试桌面通知，不可用则保留本地记录；其他系统只保留本地日志。可将 `desktop` 设为 false。钉钉默认禁用，需在 JSON 配置中提供 `dingtalk: {"webhook": "https://oapi.dingtalk.com/robot/send?access_token=...", "secret": "可选加签密钥"}`；请保护该配置文件。通知在独立有界队列内执行，超时由 `notification_timeout` 控制（最多 30 秒）。

`autosign status` 的 `running` 表示当前是否在检查或处理输入；后台离线时启用意图仍可为 true，下次后台启动恢复。该功能不占音频下载并发槽位。

`autosign history` 同时输出 `query_count`：本次后台启动以来执行的检查轮数（包括失败和手动 discover，同一轮内部认证重试不重复计数）。日期筛选仅影响历史事件；后台重启归零，离线显示 0，停止再开启 autosign 不清零。

Windows 登录自启动统一后台：`zju daemon autostart enable`。
使用 `zju daemon autostart status` 查看，`zju daemon autostart disable` 关闭。
启动项位于当前用户的注册表 Run 项，无需管理员权限；登录后通过 pythonw 隐藏启动。
可用 `--config` 指定配置文件，路径会转为绝对路径；默认使用当前目录的 `.env`（存在时）或 `~/.secrets/zju-cli/.env`。
启动项不保存账号密码，启动时读取配置并通过标准输入传给后台。若仅使用临时环境变量配置凭据，应先配置持久的 `.env` 文件。
`daemon stop` 仅停止本次后台，下一次登录仍会启动；暂停的下载仍需显式恢复。显式启用的 autosign 在后台重启后恢复。

浙江大学「学在浙大 / 智云课堂」命令行工具：查询待办、课程、作业和课表，提取回放音频。

使用 Python 标准库实现，无第三方 Python 运行时依赖。支持 JSON 输出和持久化后台音频任务，适用于终端、脚本及 Agent。

| 功能 | 命令 |
| --- | --- |
| 待办与课程 | `todos`、`courses` |
| 作业详情、提交与撤回 | `homework list / submit / retract` |
| 智云课表 | `schedule` |
| 回放音频与后台任务 | `class-audio extract / submit / jobs` |
| 统一后台 | `daemon status / stop`（`session` 为别名） |

本项目为非官方工具，暂不提供 MCP 服务。登录与平台接口实现参考了 EviterLesRoses2 的 [vibe-learning-in-zju](https://github.com/EviterLesRoses2/vibe-learning-in-zju)。

## 快速开始

### 1. 安装

需要 **Python 3.11+**，以及能够访问学校服务的网络。音频提取另需 [ffmpeg](#ffmpeg)。

克隆仓库后安装，建议使用虚拟环境：

```sh
git clone https://github.com/ZakWang1229/zju-cli.git
cd zju-cli
python -m pip install .
zju --help
```

### 2. 配置账号

在当前目录创建 `.env`，格式参考 [.env.example](.env.example)：

```dotenv
ZJU_USER=你的学号
ZJU_PASS=你的密码
```

也可以设置环境变量。PowerShell：

```powershell
$env:ZJU_USER = "你的学号"
$env:ZJU_PASS = "你的密码"
```

Bash / Zsh：

```sh
export ZJU_USER="你的学号"
export ZJU_PASS="你的密码"
```

长期使用可将凭据放在默认配置文件中，无需在每个工作目录创建 `.env`：

| 系统 | 默认凭据文件 |
| --- | --- |
| Windows | `%USERPROFILE%\.secrets\zju-cli\.env` |
| Linux / macOS | `~/.secrets/zju-cli/.env` |

配置优先级从高到低为：**环境变量 → `--config` 指定的文件（未指定时为当前目录 `.env`）→ 默认凭据文件**。

### 3. 查询

```sh
zju todos
zju courses --status ongoing
zju schedule
```

`schedule` 默认查询今天，日期使用北京时间。各命令的参数可通过 `zju <命令> --help` 查看。

## 常用操作

以下数字 ID 均为占位值，使用前请替换为查询结果中的实际 ID。

| 参数 | 从哪里获取 |
| --- | --- |
| 作业命令的 `--course-id` | `courses --json` 中的 `courses[].id` |
| `--activity-id` | `homework list --json` 中的 `homeworks[].activity_id` |
| `--sub-id` | `schedule --json` 中的 `courses[].sub_id` |
| 音频命令的 `--course-id` | `schedule --json` 中的 `courses[].course_id` |

### 课程与作业

```sh
zju courses --keyword 数学
zju homework list --course-id 12345
zju homework list --course-id 12345 --no-submission
```

作业列表默认包含自己的提交详情和成绩；`--no-submission` 跳过这些查询。部分详情读取失败时，结果会附带 `warnings`。

提交和撤回会真实修改账号数据。交互终端会要求确认；脚本或 Agent 必须显式传入 `--yes`：

```sh
zju homework submit --activity-id 23456 --file ./answer.pdf --comment "作业" --yes
zju homework retract --activity-id 23456 --yes
```

写请求不自动重试。提交超时后操作可能已经生效，请先查询作业状态，避免重复提交。

### 课表

```sh
zju schedule --date 2026-10-04 --end-date 2026-10-10 --json
```

课表单次最多查询 7 天，结果包含课次 ID、回放课程 ID、教师、教室和时间，可用于后续音频提取。

## 回放音频

### ffmpeg

安装后确认 `ffmpeg -version` 能正常执行：

| 系统 | 安装命令 |
| --- | --- |
| Windows | `winget install --id Gyan.FFmpeg --exact`，安装后重新打开终端 |
| Ubuntu / Debian | `sudo apt-get install ffmpeg` |
| macOS（Homebrew） | `brew install ffmpeg` |

可通过 `FFMPEG_BIN` 或音频命令的 `--ffmpeg-bin` 指定可执行文件路径。音频提取直接复制原始音轨，不重新编码；建议输出 `.m4a`，容器需兼容原始音频编码。

音频支持相对路径，自动创建父目录，默认拒绝覆盖。需要替换已有文件时传入 `--overwrite`；写入失败会保留原文件。

### 前台等待

```sh
zju class-audio extract --sub-id 12345 --course-id 67890 --out ./lesson.m4a
```

`extract` 创建任务，由统一后台管理的任务线程登录和提取音频（全局最多并发 2 个），前台等待最终结果。成功退出表示音频已提取并输出。

### 后台任务

`submit` 立即返回 `job_id`，后台继续提取。`extract --detach` 是等价入口，提交成功仅表示任务已创建。

```sh
zju class-audio submit --sub-id 12345 --course-id 67890 --out ./lesson.m4a --json
```

用返回的 `job_id` 查询和等待。以下为 PowerShell 示例：

```powershell
$jobId = "替换为实际 job_id"
zju class-audio jobs list --json
zju class-audio jobs status $jobId --json
zju class-audio jobs wait $jobId --wait-timeout 20 --json
zju class-audio jobs result $jobId --json
```

`status` 和 `list` 显示阶段、已用时间、实际媒体位置和错误。`list`、`status`、`wait`、`result`、`cancel` 均不要求账号凭据。

脚本和 Agent 建议使用后台提交加短等待。**退出码 `7` 仅表示本次等待超时，任务仍在运行；请继续查询原任务，避免重新提交。** Ctrl-C 也只停止前台等待。

| 参数 | 限制范围 | 默认值 |
| --- | --- | --- |
| `--wait-timeout` | `extract` / `jobs wait` 的前台等待时间 | 不限 |
| `--timeout` | 本次 ffmpeg 运行时间 | 不限 |
| `--http-timeout` | 单次登录或接口请求时间 | 30 秒 |

以上时间单位均为秒。等待期间约每 5 秒向 stderr 输出进度；等待超时不会主动唤醒 Agent。

### 取消、恢复与导出

```powershell
zju class-audio jobs cancel $jobId --json
zju class-audio jobs resume $jobId --json
zju class-audio jobs resume $jobId --config ./credentials.env --json
zju class-audio jobs result $jobId --out ./recovered.m4a --json
```

- **取消**：后台任务检查取消请求，停止 ffmpeg 并清理临时输出。登录期间需等待当前 HTTP 操作返回；临近完成时请以最终状态为准。
- **恢复**：电脑重启或后台崩溃后，使用原 ID 恢复。旧租约最多需等 60 秒过期；成功或已取消的任务不能恢复。
- **重新提取**：恢复未完成的下载时会重新登录，从头提取。已完整保存的内部音频会直接复用。
- **导出**：复用已完成的内部音频，无需重新下载。导出文件的扩展名须与原音频一致。

任务状态为 `queued`、`running`、`paused`、`succeeded`、`failed` 或 `cancelled`。运行后台任务期间，电脑需保持开机。

### 存储

| 系统 | 默认任务目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\zju-cli` |
| Linux / macOS | `$XDG_STATE_HOME/zju-cli`，未设置时为 `~/.local/state/zju-cli` |

可通过 `--data-dir` 或 `ZJU_DATA_DIR` 修改目录；提交、查询和恢复需使用同一目录。

```text
zju-cli/
  jobs.sqlite3
  jobs/<job-id>/
    audio.m4a
    worker.log
```

任务目录会保留一份完整音频，额外占用一份录音的磁盘空间。若目标文件在下载期间被其他程序创建，默认保留已有文件，结果包含 `output_error`；可用 `jobs result --out` 导出到新路径。

数据库保存任务类型、版本、参数和账号标识，不保存密码、Cookie 或回放签名 URL。后台使用启动时的主账号凭据；修改密码后停止并重新启动。旧任务记录及其结果文件继续可读。

## 脚本与 JSON 输出

```sh
zju todos --json > todos.json
zju --config ./credentials.env courses --json --quiet
```

`--json`、`--quiet`、`--config`、`--http-timeout`、`--data-dir` 可放在子命令前后。JSON 模式下 stdout 输出结果，stderr 输出日志和业务异常；业务异常使用 JSON 格式。终端输出编码为 UTF-8，请结合结果与退出码判断是否成功。

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功；后台提交命令仅表示任务已提交 |
| `2` | 参数或任务状态错误 |
| `3` | 配置或认证错误 |
| `4` | 网络 / 远端错误，或任务取消 / 暂停 |
| `5` | 本地文件、ffmpeg 或音频输出错误 |
| `6` | Worker 租约仍有效 |
| `7` | 前台等待超时，后台任务继续 |
| `130` | Ctrl-C 中断前台操作 |

### 后台复用登录

首次执行需要鉴权的命令时自动启动认证后台。后台在内存中持有 CAS Cookie；同一账号首次登录后，访问另一个平台时先使用已有 CAS 登录态获取该平台的新票据。CAS 再次返回登录表单时才提交账号密码。各平台建立的会话在后台缓存，后续 CLI 进程和音频 Worker 共用它们。

```sh
zju daemon status --json
zju daemon stop
```

`status` 不启动后台、不访问学校服务，也不需要学校凭据。每个操作系统用户共用一个后台，账号、密码、HTTP 超时和任务目录不会生成额外实例。后台绑定主账号，其他账号请求明确拒绝。

后台监听 `127.0.0.1`，用户私有运行目录存储地址与独立随机密钥，请求和响应签名校验并防止重放。凭据从启动标准输入进入内存。后台仅长期保留 CAS Cookie，每次操作使用独立站点会话；认证串行，业务 HTTP 不持有认证锁。认证失败冷却 30 秒，网络故障不触发冷却。只读认证失效最多重试一次，写入不自动重放。

`daemon stop` 停止接受业务和调度，暂停未完成下载、回收 ffmpeg，并等待已接受的短业务请求结束。下次业务自动启动后台，下载与普通周期任务保持暂停；曾显式启用的 autosign 恢复运行。切换新版前先用旧版停止旧认证后台。模块边界及迁移细节见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 开发与验证

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

测试使用标准库 `unittest`，不需要 pytest，也不访问学校账号或服务。安装 ffmpeg 后会运行本地合成音频和独立 Worker 测试；未安装时跳过需要 ffmpeg 的测试。GitHub Actions 配置了 Windows / Linux、Python 3.11 / 3.13 的安装与测试。

可选真实账号检查，仅查询待办、课程、作业详情和课表：

```sh
python -X utf8 scripts/smoke_readonly.py --date 2026-10-04 --end-date 2026-10-10
```

待办、课程、作业详情和课表已做过真实账号验证。提交 / 撤回通过模拟接口验证；音频任务通过本地 HTTP 和真实 ffmpeg 验证，尚未完整下载真实课堂回放。

<details>
<summary>代码结构</summary>

| 模块 | 职责 |
| --- | --- |
| `cli.py`、`output.py` | 参数解析、退出码、终端和 JSON 输出 |
| `clients/auth.py`、`clients/http.py` | CAS 登录、HTTP 和 Cookie |
| `session_daemon.py`、`runtime.py`、`app.py` | 用户级后台发现、签名业务 RPC、模块组装与生命周期 |
| `auth_manager.py` | 串行 CAS 认证与独立临时站点会话 |
| `job_manager.py`、`scheduler.py` | 双下载并发、任务上下文、周期调度与状态持久化 |
| `adapters/` | 课程、作业、课表和回放的 API 请求及解析 |
| `services/platform.py` | 操作内会话复用与只读认证失效重试 |
| `services/courses.py` | 学在浙大的待办、课程和作业 |
| `services/classroom.py` | 智云课表、回放链接和音频提取编排 |
| `services/audio_task.py`、`services/audio_jobs.py` | 音频任务函数、参数准备与结果导出 |
| `storage.py`、`worker.py` | SQLite 租约与旧 Worker 兼容入口 |
| `media.py`、`files.py` | ffmpeg 调用与文件安全写入 |
| `validation.py` | 共用 ID、响应列表与音频超时校验 |

服务层通过回调报告进度，不直接打印，可供其他适配器复用。

</details>

## 使用范围与隐私

仅使用自己的账号或已获授权的账号，遵守学校和平台的使用规定。课程录音、作业及成绩可能涉及版权和个人信息，下载不代表取得公开传播的许可。

凭据在本机读取，用于向学校认证服务登录。不要将真实 `.env`、任务数据库、日志、课堂材料或含个人信息的命令输出提交到公开仓库或 Issue。`.gitignore` 已排除常见凭据、任务文件及音频输出，自定义输出文件仍需自行检查。

学校接口可能变更。提交和撤回前请核对账号、作业 ID 和文件；反馈问题时请删除密码、Cookie、签名链接、学号及成绩等敏感信息。
