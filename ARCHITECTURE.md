# Daemon 模块架构

设计依据是 [DAEMON_DESIGN.md](DAEMON_DESIGN.md) 与 [MODULE_SPEC.md](MODULE_SPEC.md)。CLI 前端入口为 `src/zju_cli/frontend/cli.py`，daemon 后端入口为 `src/zju_cli/backend/daemon.py` 中的 `Daemon`。旧 `app.py`、`client.py` 和独立 `worker.py` 已删除。

`frontend/` 放参数、展示、RPC 客户端和后台启动配置；`backend/` 放 RPC 服务、模块和业务实现；`shared/` 放配置、错误、协议、本机路径和供双方使用的数据读写。RPC 客户端与服务端已拆开，导入 CLI 不加载后端，导入 daemon 不加载前端或旧执行器。`daemon run` 在用户显式选择前台服务时加载后端入口。根目录 `autostart.py` 仅保留已有 Windows 登录启动项所需的转发入口。

```text
CLI 参数、确认、展示
  → DaemonClient 提交命令并轮询 task.get
  → 带 HMAC、时间戳、nonce 的本机 RPC
  → Daemon 命令注册表
  → tasks 统一队列
  → 模块绑定方法
```

`backend/server.py` 只负责验签、JSON 编解码、响应签名和进程生命周期。RPC 消息的 `stop`、`submit`、`command`、`status` 和 `command.list` 由 `Daemon.handle_rpc()` 处理；server 根据返回的关闭标志停止监听。

## 模块布局

模块实现统一放在 `src/zju_cli/backend/modules/`，以 `(daemon, config)` 构造并注册绑定方法。模块通过 `daemon.*` 调用其他模块的公开方法。以下路径均相对于 `backend/`。

| 文件 | 实例与职责 |
|---|---|
| command_registry.py | CommandRegistry：命令元数据、注册冻结、可见性与统一 dispatch |
| modules/tasks.py | daemon.tasks：统一队列、任务状态、持久化、上下文、取消与恢复 |
| modules/schedules.py | daemon.schedules：固定间隔计划，每轮通过 tasks.submit 执行 |
| modules/zjuam.py | daemon.zjuam：CAS 登录、认证冷却、独立平台会话 |
| modules/webvpn.py | daemon.webvpn：独立路由客户端、代理登录与只读回退 |
| modules/courses.py | daemon.courses：课程、作业、资料与内部签到接口 |
| modules/classroom.py | daemon.classroom：课表、音频下载与产物导出 |
| modules/autosign.py | daemon.autosign：签到策略、历史、通知与唯一周期计划 |
| modules/cc98.py | daemon.cc98：论坛账号、分路由 Token、查询、解析与附件任务 |

学校平台协议、响应解析、业务流程和算法都放在对应模块：课程/作业/资料/签到接口在 `modules/courses.py`，课表/回放/ffmpeg 在 `modules/classroom.py`，任务子进程回收在 `modules/tasks.py`，签到策略、通知及轮次等待类型 `NextRun` 在 `modules/autosign.py`，论坛与附件在 `modules/cc98.py`。CAS 登录在 `modules/zjuam.py`，通用 HTTP/Cookie 会话在 `shared/http.py`。这些模块也注册各自的 daemon 命令。旧 `JobManager`、`PeriodicScheduler`、后端 `ZjuService` 门面及独立 `services/`、`adapters/`、`cc98/`、`clients/`、`media.py`、`scheduling.py` 均已移除；签到和周期任务统一通过 `modules/tasks.py`、`modules/schedules.py` 执行。

## 命令与任务

`CommandRegistry` 位于独立的 `command_registry.py`；`Daemon` 仅通过 `command_registry` 属性组合它，不再定义或转出注册表类型。各模块直接调用 `daemon.command_registry.register()`，任务和计划模块直接调用 `command()`，不再经过 Daemon 转发方法。`CommandRegistry.dispatch()` 是唯一的命令调用入口；`Daemon.handle_rpc()` 的 `command` action 在账号校验后直接调用它。RPC `submit` 调用 `TasksModule.submit_request()` 并立即返回任务 ID，写操作由 CLI 轮询 `task.get`。旧 `business` 和 `autosign` action 已移除；签到 CLI 对只读命令使用 `command`，写命令使用 `submit`。公开命令可由 `zju command list` 查询；内部命令不能从 RPC 绕过校验提交。

普通请求、下载、资料同步、论坛下载、自动签到轮次共用四线程执行器，最多同时容纳四个运行任务和十六个等待任务。控制查询直接访问 task/schedule 模块，不占用正在执行业务的线程，因此队列满时仍能查询或取消任务。同 serial_group 的任务串行执行。

状态为 queued、running、succeeded、failed、cancelled、interrupted。普通查询只保存在内存，终态三十分钟后按需清理；持久命令保存到 tasks.sqlite3。数据库以账号隔离，启动时将遗留 queued/running 改为 interrupted。只有声明 durable/resumable 且命令版本匹配的中断任务可恢复，保留 ID 并增加 attempt。签到码等 transient_fields 仅驻留内存，执行结束清理。

`daemon.task` 是线程上下文，提供取消/截止时间检查、进度、恢复点、产物路径和外部进程登记。任务槽位在实际退出后释放；排队任务可立即取消。ffmpeg 使用独立进程组，取消或关闭时回收。

## 周期计划与 autosign

任务历史按需从 SQLite 读取：启动通过账号和状态索引将遗留 queued/running 任务标为 interrupted，不全量加载历史；get 按 ID 加载并缓存，list 在数据库中筛选、排序和限制条数，再合并本次运行的内存任务。JSON 表达式索引首次升级时建立，后续启动复用。

计划与每轮任务分别持久化。调度器仅检查时间和 task 状态，不另起业务执行池；每轮完成后再等待 interval，错过轮次合并，队列满则等待容量。普通业务失败继续计划，未预期异常停用计划；存在中断轮次的普通计划要求显式 enable。autosign 的中断轮次恢复后先核实已记录的提交意图。

autosign 通过 courses 的平台接口执行策略，使用唯一内置计划。AttendanceService 保留已验证的去重、有限尝试、提交核实与通知算法；它的调度钩子由模块转接到 daemon.schedules，执行仍全部经过 daemon.tasks。stop 停用后续轮次并通知当前轮停止，关闭后台则保留启用意图。签到完成状态和事件在同一事务写入，JSONL/通知失败不改变签到结果。

AutosignModule 独立持有学在浙大的 HTTP 会话，跨检查轮次及手动操作复用 Cookie；首次使用或会话过期后才向 AuthManager 申请新会话。过期会话和模块关闭时释放连接；写请求不会因会话过期自动重放，仍沿用提交核实流程。

## 认证与资源

CAS 串行握手，仅集中保存 CAS Cookie；courses/classroom 以线程隔离的会话缓存执行业务，失效读最多重建一次。作业写入丢响应不重放。WebVPN 的直连和代理会话隔离，CC98 按路由保存 Access/Refresh Token，认证材料只保存在内存。

后台接收密码使用 stdin，启动命令和环境中不携带学校或论坛密码。本机 RPC 有请求与响应签名、时间窗口和 nonce 防重放；业务账号必须匹配后台绑定账号。

关闭先停止接收和调度，通知任务停止并回收进程，等待已接收工作退出，再按逆序关闭模块。初始化失败同样关闭已经建立的模块，并保留原始失败。

## 状态、日志与路径

新任务与计划保存在用户状态目录的 tasks.sqlite3；签到业务记录保存在同目录 scheduler.sqlite3。后台只读取当前状态目录，不扫描或复制旧运行目录中的数据。未知任务数据库版本会拒绝加载。

音频任务只使用 tasks.sqlite3 和统一 task API。旧 jobs.sqlite3、Worker 租约、旧任务导入及结果兼容层均已删除；`--data-dir` 只指定新任务的产物根目录。

日志由统一入口配置，默认 INFO、UTF-8，按上海日期追加 daemon-YYYY-MM-DD.log；支持 UTC 与 stderr。日志失败不会使业务失败，后续写入可重试。Linux 遵循绝对 XDG 目录、0700 目录与 0600 私有文件。`daemon run` 在前台持续运行并处理 SIGINT/SIGTERM；`deploy/systemd/zju-cli.service` 提供用户服务样例。

自动测试使用模拟平台、临时状态目录、本地 HTTP 服务和真实 ffmpeg，不向学校或论坛提交真实写入。实际通过数量与当前平台限制见 VALIDATION.md。
