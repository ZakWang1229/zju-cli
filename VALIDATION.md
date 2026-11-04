# 重构验证记录

## 旧音频任务兼容层删除（2026-11-05）

- 删除 `jobs.sqlite3` 任务导入、旧 Worker 租约取消、旧任务视图、`LegacyAudioResults`、`JobStore`、`read_legacy_audio` 和 `wait_audio_job`。
- `class-audio jobs` 的列表、状态、等待、恢复、取消和导出只读取或操作 tasks.sqlite3；`--data-dir` 仅指定新任务产物根目录。

## 命令注册表并入 Daemon（2026-11-04）

- `CommandRegistry` 保留为独立类，Daemon 不继承它，而是通过 `command_registry` 属性组合；Daemon 不再提供重复的注册/查询转发方法。模块直接调用 `daemon.command_registry.register()` 和 `command()`，RPC `command` action 授权后直接调用 `command_registry.dispatch()`。
- Daemon 不再提供 `submit()`、`submit_and_wait()`、`dispatch()` 或 `autosign_operation()`。任务账号/停止状态校验和入队归入 `TasksModule.submit_request()`；不再保留任何 submit-and-wait 方法。旧 `business`、`autosign` action 已移除，签到写操作由 CLI 统一 submit 并轮询。
- `Daemon.begin_stop()` 只关闭全局接收、调度器和任务队列，不再直接调用 `autosign.begin_stop()`；签到模块自身状态由 `AutosignModule.close()` 负责。
- `SchedulesModule` 不再识别 `autosign` 字符串，也不再保存无意义的 `internal` 字段。启用/停用状态由 schedule 行单独保管，中断恢复仅使用通用 `resume_on_restart`；`autosign` 的 `control` 记录不再保存独立 `enabled` 状态。
- 测试通过 `CommandRegistry` 或最小化测试运行时验证注册元数据、冻结、内部命令隐藏、注册表分发、任务计划和模块边界；相关定向回归 37 项通过（1 项 POSIX 权限按 Windows 环境跳过）。完整回归结果在本节后续更新。

## 日志模块命名（2026-11-04）

- 将 `backend/logging_setup.py` 最终更名为 `backend/logger.py`，测试同步改名为 `test_logger.py`；行为不变，模块名直接表示日志职责。定向回归 11 项、完整回归 140 项均通过，2 项因 Windows 环境跳过。

## RPC action 分发收敛到 Daemon（2026-11-04）

- 将 `stop`、`submit`、`command`、`status`、`command.list` 分支从 `backend/server.py` 移到 `Daemon.handle_rpc()`；旧 `business` 和 `autosign` 分支后续已删除。server 只保留验签、JSON、响应编码和监听生命周期。
- 新增 action 路由、旧名称映射和关闭标志测试；真实 daemon RPC、自动签到及启动停止定向回归 25 项通过（1 项 POSIX 信号按 Windows 环境跳过）。完整回归 140 项，138 项通过、2 项因 Windows 环境跳过。

## 媒体处理与 NextRun 归位（2026-11-04）

- `AUDIO_FORMATS` 与 ffmpeg 音频提取并入 `backend/modules/classroom.py`；跨平台任务子进程终止并入 `backend/modules/tasks.py`，删除独立 `backend/media.py`。
- 自动签到轮次的 `NextRun` 直接定义在 `backend/modules/autosign.py`，删除 `backend/scheduling.py`。
- 媒体、任务、签到和包边界定向回归 52 项通过；后续 RPC action 调整另行记录回归结果。源码编译和 `git diff --check` 通过。

## 学校协议适配器并入模块（2026-11-04）

- 课程、作业、资料和签到平台适配器并入 `backend/modules/courses.py`；课表与回放适配器并入 `backend/modules/classroom.py`。签到策略继续位于 `autosign.py`。
- 删除独立 `backend/adapters/`，测试改用模块公开位置；包边界测试禁止该目录重新出现。
- 协议、课程、签到、教室和模块边界定向回归 60 项通过；完整回归 139 项，137 项通过、2 项因 Windows 环境跳过。源码编译与 `git diff --check` 通过。

## CAS 与 HTTP 基础层归位（2026-11-04）

- CAS 表单解析、密码加密、登录和平台会话选择并入 `backend/modules/zjuam.py`；通用 HTTP、Cookie、JSON 与认证下载逻辑移至 `shared/http.py`。
- 课程、作业、签到适配器和认证测试全部改用新路径；删除 `backend/clients/`。
- 认证、HTTP、模块边界定向回归 23 项通过；完整回归 139 项，137 项通过、2 项因 Windows 环境跳过。源码编译和 `git diff --check` 通过。

## Tasks 上下文与过时门面清理（2026-11-04）

- 将 `ContextVar` 放入 `backend/modules/tasks.py`，daemon、课程/教室模块及测试直接从统一任务模块引用；删除独立 `backend/task_context.py`。
- 删除仅剩转发用途、已无调用的 `backend/legacy/platform.py`。原服务门面测试改为直接覆盖 `CoursesService` 与 `ClassroomService` 的业务逻辑。
- 前后端、任务、课程和模块服务定向回归 58 项通过。

## 服务实现归入业务模块（2026-11-04）

- 将课程和资料实现并入 `backend/modules/courses.py`，课表和音频实现并入 `backend/modules/classroom.py`，签到及通知实现并入 `backend/modules/autosign.py`；删除 `backend/services/` 源文件目录。模块现在拥有业务入口和实际实现。
- 更新业务测试到新的模块位置，定向回归 62 项通过。

## CC98 实现归入模块（2026-11-04）

- 将 OAuth/API 客户端、Token 刷新、论坛查询与解析、UBB 转换、附件下载并入 `backend/modules/cc98.py`；更新测试和文档引用，移除独立 `backend/cc98/` 目录及无调用的旧展示模块。
- 当时的 `backend/adapters/` 六类文件均在调用链中；后续迁移已将这些协议实现并入对应业务模块。
- CC98、模块边界及真实 daemon RPC 定向回归通过；完整回归 139 项，137 项通过、2 项因 Windows 环境跳过。源码编译及 `git diff --check` 通过。

## 旧调度器清理（2026-11-04）

- 删除仅被旧测试使用的 `backend/legacy/scheduler.py`、`backend/legacy/job_manager.py` 及对应旧实现测试；保留旧数据库记录的读取兼容。
- 签到测试改用实际的 `TasksModule`、`SchedulesModule` 和签到调度适配器，验证退避与恢复、重启意图和未确认写入；新计划测试覆盖运行轮次不重叠与版本不匹配拒绝启用。
- 全量回归 142 项，140 项通过、2 项因 Windows 环境跳过；源码编译和 `git diff --check` 通过。干净源码构建 wheel 并逐文件核对 63 个 Python 源文件，确认不含旧调度器和旧任务管理器。

## Worker 清理与前后端分目录（2026-11-04）

- 删除独立 `worker.py`、旧音频提交/启动/恢复代码，以及旧的 `app.py`、`client.py` 转发文件。音频执行统一使用 daemon 的 tasks/classroom 模块；旧任务数据、租约取消标记和结果导出兼容保留。
- 源码分为 `frontend/`、`backend/`、`shared/`：RPC 客户端与服务端拆开，共用配置、协议和本地记录读写独立存放。签到轮次的 `NextRun` 后续已并入 `autosign`，生产 daemon 不再导入旧执行器；当时的 `backend/legacy/` 也已在后续迁移中删除。
- 保留根目录 `autostart.py` 的小型启动转发，兼容已有 Windows 登录启动项；没有修改注册表。已刷新本仓库的 editable 安装入口，`zju --version` 与 `python -m zju_cli --version` 均可运行。
- 测试迁移到新路径，补充前后端导入隔离、音频取消、冲突后导出，以及真实 daemon + 本地 HTTP + ffmpeg 在前端超时后继续完成的集成验证。删除仅验证旧 Worker 实现的测试；完整回归 148 项，146 项通过、2 项因 Windows 环境跳过（POSIX 信号和权限）。未向学校或论坛提交真实写入。
- 旧目录仅剩的字节码缓存与旧 build 缓存已移动至临时备份，避免源码树显示旧实现或污染打包。
- `git diff --check`、源码编译、CLI 和只读检查脚本的帮助入口通过。重新构建 `dist/zju_cli-0.1.0-py3-none-any.whl`，核对其中 68 个 Python 文件内容与当前 src 完全一致，确认已删除执行器没有残留、许可证齐全。在临时安装目录验证 CLI 导入和真实 daemon 启动、注册表查询、停止通过；未变更正在使用的后台实例。

## Daemon 草案实现（2026-11-04）

用户确认回退全部未提交改动后，工作区恢复至干净状态，再以 DAEMON_DESIGN.md / MODULE_SPEC.md 的模块边界重建。原有平台协议、签到策略和音频兼容代码作为业务算法复用；新 Daemon 不通过旧 ZjuService/service_factory 组装或派发业务。

- 新入口 daemon.py 与八个 modules：tasks、schedules、zjuam、webvpn、courses、classroom、autosign、cc98。模块构造注册绑定方法，启动后冻结注册表；控制接口与业务执行分开，初始化失败逆序清理。
- tasks 统一处理普通请求、下载和计划轮次：4 个执行线程、16 个等待任务；内存查询保留 30 分钟，持久任务重启为 interrupted；恢复保持 ID、增加 attempt、校验命令版本与恢复能力。取消排队任务立即释放槽位，运行任务退出后释放。transient_fields 不进入 SQLite。
- schedules 每轮通过 tasks.submit 创建持久任务，不另建业务线程池；完成后等待间隔，错过轮次合并；业务错误继续，内部异常停用。autosign 通过同一队列和计划执行，保留旧意图/完成记录及提交核实算法。
- 资料与 CC98：模拟接口验证附件去重、增量同步、冲突保留、单附件失败后继续、账号绑定、论坛 OAuth 刷新/401、WebVPN 一次只读回退、下载大小及安全文件名。保留两份上游 MIT 许可证。
- 真实本机子进程验证签名 RPC、跨 CLI 进程共享、提交后轮询 task.get、计划/任务重启持久化、新音频 RPC 和自定义产物目录；真实 ffmpeg 与登记的 Python 子进程覆盖停止及取消回收。
- 日志覆盖上海日期日切、时区、追加、stderr、失败后重试与关闭。提供 XDG 路径、POSIX 权限/信号测试及 systemd 用户服务样例。

最终源码完整回归 `python -m unittest discover -s tests`：150 项，148 项通过、2 项因 Windows 环境跳过（POSIX SIGTERM 与 POSIX 文件权限），68.725 秒。测试使用假账号、临时目录与模拟平台，未执行学校或论坛真实写入。Linux 专项由既有 Linux CI 运行，本次 Windows 会话没有执行这些专项。

`git diff --check` 与源码编译检查通过。清理旧 build 目录后构建 wheel；逐文件核对包内 Python 源码清单与 src 一致，许可证齐全。将 wheel 无依赖下载地安装到临时虚拟环境，确认从安装目录导入，CLI 与 daemon 启动/查询/停止通过，公开命令 50 个。产物：dist/zju_cli-0.1.0-py3-none-any.whl。包版本仍为 0.1.0，本次没有提交或发布。

音频迁移收尾：`class-audio` 的提交和等待经统一 task API；旧 `jobs.sqlite3` 按需导入任务库，保留原 ID 与音频产物目录。有效旧租约继续由旧记录接收取消标记，暂停的旧任务可导入后取消。空目录的只读列表不会创建旧数据库。删除无调用的 `services/audio_task.py`；`--data-dir` 帮助文本明确为产物目录。定向音频、任务及 daemon RPC 测试通过；完整回归 157 项，155 项通过、2 项 Windows 专属测试跳过。源码编译及 `git diff --check` 通过。重新构建并更新 `dist/zju_cli-0.1.0-py3-none-any.whl`，确认 wheel 不再包含已删除模块。

## autosign 接入验收（2026-10-23）

- 完整回归 `python -m unittest discover -s tests`：119 项通过，51.751 秒。`git diff --check` 通过。
- 模拟签到覆盖无活动、平台已有完成状态、过期/结束、未知类型、数字码存在/缺失/错误、雷达配置点/候选点/足够及不足距离/有限次数用尽。
- 验证实际解析 JSON，HTTP 200 携带错误字段不能当作成功；数字签到没有枚举请求。
- 受控时钟验证 4、8、16、32、60 秒退避、成功恢复正常间隔及不重叠；既有 Auth Manager 测试覆盖认证失败冷却与网络失败区分。
- 丢失响应先读回确认；无法确认以及重启后的 processing 状态只核实、不重提。测试覆盖停止时的在途写入核实及后台关闭后排队 start 不能重新开放提交。
- 隔离运行目录的真实后台进程测试验证签名 RPC、账号拒绝、显式开启后重启恢复和显式停止后保持禁用。
- 完成事件与来源写入 SQLite，JSONL 未确认导出可按事件 ID 补写去重；通知失败及日志导出失败不改变签到结果。桌面不可用和非 Windows 回退均使用模拟发送器验证，未向真实钉钉发送通知。
- 真实环境仅执行 `autosign discover`：认证和列表 JSON 解析成功，0 个签到活动，enabled=false。未执行真实 autosign start、数字或雷达提交；真实签到成功率和桌面通知实际呈现未在本次写入验收中验证。
- 没有新增或修改 Windows 启动项；沿用统一后台现有生命周期。

后续真实下载测试：修复 RPC 字符串目录传入 JobStore 的路径类型兼容；后台集成测试现使用字符串路径。真实进程测试改用独立临时运行目录，避免停止用户正在使用的后台。修复后完整回归 90 项通过（30.928 秒）。2026-10-22 课表查询成功解析 4 条回放，之前记录的智云格式错误本轮未复现。

数学分析Ⅰ 2026-10-22 第7–8节真实下载成功，任务 `cbd7e7b66c844694993c1cf974a56807`。经历一次停止暂停后以原 ID 恢复完成；ffprobe 确认 AAC 音频、5569.6085 秒、51,864,855 字节。下载期间普通待办查询成功返回 14 项。仅完成下载测试，未提交 ASR。

2026-10-23，有限真实只读检查：

- 学在浙大认证成功；课程业务解析成功，返回 12 门课程。
- 智云认证成功；课表业务解析失败，返回结构缺少有效 `list` 列表。此项作为既有平台格式问题单独保留，不以认证成功替代业务验收。
- 未提交或撤回作业，未接入签到服务。
- 通过真实后台 RPC 查询课程，15 秒和 20 秒 HTTP 超时均返回 12 门课程，并共用同一发现端点；错误账号请求明确拒绝，后台正常响应停止命令。

## 验收证据

| 行为 | 当前证据 |
|---|---|
| 多进程共享、停止与重启 | `test_session_daemon.py` 的真实后台进程测试 |
| 请求防重放、未认证拒绝、写入丢响应不重放 | 后台进程测试及业务服务测试 |
| 临时 CookieJar、父域路径筛选、失败冷却 | `test_auth_manager.py` |
| 跨目录双并发、停止暂停、旧租约保护与过期恢复、取消释放 | `test_job_manager.py` |
| 后台创建、音频结果导出、完整内部音频复用 | `test_app.py` |
| 慢业务不阻塞状态、账号拒绝、停止拒绝新业务 | `test_app.py` |
| 周期合并、不重叠、退避、暂停、重启暂停、写入失败不重试 | 可控时钟 `test_scheduler.py` |
| ffmpeg 超时、取消和子进程回收 | `test_media.py`；旧 Worker 保留本地 HTTP 与真实 ffmpeg 集成测试 |

统一后台停止时的真实 ffmpeg 回收集成测试已通过，下载暂停并释放租约；独立临时会话间仅首次提交密码的组合测试已通过。

完整回归 `python -m unittest discover -s tests`：88 项测试全部通过，耗时 31.138 秒（Windows 本地环境）。`git diff --check` 通过。默认下载提交已验证通过统一后台启动；旧 Worker 测试显式使用兼容入口。智云课表格式问题单独保留。

## 0.2.0 发布验收（2026-11-05）

- 完整回归 `python -m unittest discover -s tests`：136 项，134 项通过、2 项因 Windows 环境跳过，耗时 73.311 秒。
- `CommandRegistry` 已从 `backend/daemon.py` 拆至独立的 `backend/command_registry.py`；daemon 只组合实例，不再定义或转出注册表类型。
- `zju cc98 hot --range today --size 10 --json` 真实只读查询成功，返回 10 条今日热门主题及下一页游标。
- `zju autosign status` 真实 RPC 查询成功；验证 `autosign.status/list/history/stop` 可由 daemon 级调用执行，无需重复提供配置账号。
- 包版本在 `pyproject.toml` 与 `zju_cli.__version__` 中统一为 `0.2.0`。
- `python -m pip wheel . --no-deps --wheel-dir dist` 生成了 `dist/zju_cli-0.2.0-py3-none-any.whl`；wheel 元数据版本为 0.2.0，包含独立注册表模块且不包含旧 `legacy`、`services`、`adapters` 目录。
