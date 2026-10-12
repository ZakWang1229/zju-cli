# zju-cli

浙江大学「学在浙大 / 智云课堂」命令行工具：查询待办、课程、作业和课表，提取回放音频。

使用 Python 标准库实现，无第三方 Python 运行时依赖。支持 JSON 输出和持久化后台音频任务，适用于终端、脚本及 Agent。

| 功能 | 命令 |
| --- | --- |
| 待办与课程 | `todos`、`courses` |
| 作业详情、提交与撤回 | `homework list / submit / retract` |
| 智云课表 | `schedule` |
| 回放音频与后台任务 | `class-audio extract / submit / jobs` |

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

`extract` 创建任务，由独立 Worker 登录和提取音频，前台等待最终结果。成功退出表示音频已提取并输出。

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

- **取消**：Worker 检查取消请求，停止 ffmpeg 并清理临时输出。登录期间需等待当前 HTTP 操作返回；临近完成时请以最终状态为准。
- **恢复**：电脑重启或 Worker 崩溃后，使用原 ID 恢复。旧租约最多需等 60 秒过期；成功或已取消的任务不能恢复。
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

数据库保存任务参数、凭据文件路径和账号标识，不保存密码、Cookie 或回放签名 URL。Worker 使用提交时的配置路径，环境变量仍优先；如果凭据只通过环境变量提供，重启后恢复前需重新设置。

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

会话仅在当前进程内缓存，每次新的 CLI 进程会重新登录。只读请求遇到会话过期时会重登并重试一次。

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
| `services/platform.py` | 统一调用入口、会话缓存与只读请求重登 |
| `services/courses.py` | 学在浙大的待办、课程和作业 |
| `services/classroom.py` | 智云课表、回放链接和音频提取编排 |
| `services/audio_jobs.py`、`storage.py`、`worker.py` | 后台任务、SQLite 租约、恢复与结果导出 |
| `media.py`、`files.py` | ffmpeg 调用与文件安全写入 |
| `validation.py` | 共用 ID、响应列表与音频超时校验 |

服务层通过回调报告进度，不直接打印，可供其他适配器复用。

</details>

## 使用范围与隐私

仅使用自己的账号或已获授权的账号，遵守学校和平台的使用规定。课程录音、作业及成绩可能涉及版权和个人信息，下载不代表取得公开传播的许可。

凭据在本机读取，用于向学校认证服务登录。不要将真实 `.env`、任务数据库、日志、课堂材料或含个人信息的命令输出提交到公开仓库或 Issue。`.gitignore` 已排除常见凭据、任务文件及音频输出，自定义输出文件仍需自行检查。

学校接口可能变更。提交和撤回前请核对账号、作业 ID 和文件；反馈问题时请删除密码、Cookie、签名链接、学号及成绩等敏感信息。
