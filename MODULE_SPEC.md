# 模块功能与接口 Spec

本文件保留草案的目标接口与验收范围；实现与验证进度单独记录，不以修改要求代替实现。

## 1. 模块与公共约定

模块通过构造参数获得 daemon 和配置，初始化时注册绑定方法。模块间通过 daemon.* 的公开方法直接调用；close() 释放资源，可重复调用。

| 实例 | 功能 |
|---|---|
| daemon.zjuam | CAS 登录态与平台认证会话 |
| daemon.webvpn | WebVPN 登录与代理访问 |
| daemon.courses | 课程、待办、作业、资料及签到接口 |
| daemon.classroom | 智云课表、回放和音频 |
| daemon.autosign | 自动签到、结果核实、历史与通知 |
| daemon.cc98 | 论坛查询、附件和 CC98 认证 |
| daemon.tasks | 任务执行、状态、取消和恢复 |
| daemon.schedules | 周期计划与任务触发 |

外部命令接受关键字参数，返回 JSON 数据；内部方法可返回会话等 Python 对象。各模块负责校验、业务错误、线程安全和资源清理；日志使用 logging.getLogger(__name__)。

## 2. Daemon

```python
register(name, handler, *, mode="request", durable=False,
         resumable=False, version=1, serial_group=None,
         internal_only=False, transient_fields=())
```

name 唯一；执行 handler(**parameters)。mode 决定默认等待或返回任务 ID；durable 决定持久化；resumable 与 version 限制恢复；serial_group 限制同组并发；internal_only 阻止外部提交；transient_fields 只保存在内存。

控制接口：daemon.status、daemon.stop、command.list。CLI 提供 start、restart、run 和 autostart。启动顺序：tasks/schedules → zjuam/webvpn → 业务模块 → 恢复记录 → 接收请求。关闭顺序：停止接收和调度 → 通知任务退出及回收外部工具 → 等待退出 → 逆序关闭模块。

## 3. Tasks

| 接口 | 功能 |
|---|---|
| submit(command, parameters) → task_id | 普通请求或长任务进入统一队列 |
| get(task_id) → dict | 状态、进度、结果和错误 |
| list(command=None, state=None, limit=20) | 筛选任务，最多 100 条 |
| cancel(task_id) | 取消排队任务或通知运行任务协作退出 |
| resume(task_id) → task_id | 从 interrupted 恢复，保持 ID，增加 attempt |
| close() | 停止接收并等待退出 |

RPC 对应 task.submit/get/list/cancel/resume；CLI 轮询 task.get。状态：queued → running → succeeded / failed / cancelled / interrupted。默认四个执行线程、十六个等待任务，实际退出后释放容量。持久任务重启保留；普通查询终态内存保留三十分钟。恢复须同时声明 durable、resumable 且版本匹配。

daemon.task 暴露 id、attempt、recovery、deadline、check_cancelled()、report(stage, progress=None)、checkpoint(data)、artifact_path(name)、register_process(process)。取消采用协作检查，网络必须使用有限超时。

## 4. Schedules

| 接口 | 功能 |
|---|---|
| create(command, parameters, interval, enabled=False) → schedule_id | 固定间隔计划 |
| get(schedule_id) / list() | 配置、启用意图、当前轮次、下次执行时间 |
| enable(schedule_id) / disable(schedule_id) | 控制后续轮次 |
| delete(schedule_id) | 仅停用且当前轮结束时删除 |
| set_next_delay(schedule_id, seconds) | 内部调整下一轮等待 |
| close() | 停止调度并保存启用意图 |

每轮通过 tasks.submit 创建持久任务，完成后再等待 interval。同一计划不重叠，错过轮次合并。业务失败继续、内部异常暂停；队列满等待容量。重启恢复启用计划；存在中断轮次时先核实恢复，或要求显式 enable。取消当前轮使用 tasks.cancel。RPC 对应 schedule.create/get/list/enable/disable/delete。

## 5. ZJUAM

courses(timeout=30) 与 cmc(timeout=30) 返回独立 HttpSession，cmc 附带 user_id；status() 返回认证及冷却状态；close() 清理资源。外部命令 zjuam.status。CAS 握手串行，认证失败冷却三十秒，业务模块缓存并刷新自己的会话。

## 6. WebVPN

open_client(mode="auto", timeout=30) 返回支持 request(method, url, **kwargs)、route、close() 的客户端。status() 只报告本地登录与路由。direct 直连、vpn 代理、auto 在只读网络失败时至多切换一次；两种路由隔离 Cookie。验证码返回明确认证错误。模块 close() 清理客户端与缓存，外部命令 webvpn.status。

## 7. Courses

| 命令 | 参数 |
|---|---|
| courses.list | keyword、statuses |
| courses.todos | 无 |
| courses.homework.list | course_id、include_submission |
| courses.homework.submit | activity_id、file_path、comment |
| courses.homework.retract | activity_id |
| courses.materials.list | course_id |
| courses.materials.download | course_id、out、file_ids、overwrite |
| courses.materials.sync | course_id、out |

查询为 request；作业写入为 durable request，上传、提交及撤回后核实结果，写入丢响应不重放。资料下载为 durable job；清单支持重新提交后的增量继续。资料使用 course_<ID> 目录、附件 ID 文件名和 SHA-256 清单，账号与课程绑定、原子导出、同目录互斥。同步保留修改文件及平台删除的附件；单附件失败后继续汇总。

内部提供 attendance_list(*, session=None)、attendance_details(activity_id, *, session)、attendance_submit_number(activity_id, code, *, session)、attendance_submit_radar(activity_id, point, *, session)。courses 负责平台调用与解析，autosign 负责策略，同轮可共用 session。

## 8. Classroom

classroom.schedule(start_date, end_date)：北京时间课表，最多七天。classroom.audio.download(sub_id, course_id, out, overwrite, ffmpeg_bin, timeout)：durable/resumable job。classroom.audio.export(source_task_id, out, overwrite)：durable request。

内部 playback_url(sub_id, course_id, session) 获取媒体地址；认证来自 zjuam.cmc。ffmpeg 报告进度、响应取消并登记外部进程。先保存内部产物再原子导出，冲突保留已有文件与完整产物。恢复校验完整产物；完整则继续导出，不完整重新提取。

## 9. Autosign

autosign.start(settings, interval)、stop、status、list、discover、answer(activity_id, code)、history(date)、check。check 为内部 durable/resumable 周期轮次；answer 的 code 为 transient_fields。

通过 courses 访问签到接口，维护策略、去重、有限次数、恢复核实、历史、通知。数字码缺失等待显式输入。默认四秒，失败等待 4/8/16/32/60 秒，成功恢复间隔；通过 schedules.set_next_delay 调整下轮。空列表视为成功。

恢复先核实未确认写入，不自动重提。完成状态与事件事务写入，JSONL 和通知独立补发；stop 在提交边界停止新写入；关闭保留启用意图。敏感签到码、位置和原始响应不进入诊断日志或完成事件。

## 10. CC98

cc98.search(keyword, offset, size)、posts(id, offset, size)、hot(range, offset, size)、new(offset, size)、boards()、board(id, best, offset, size)、user(id 或 name, offset, size)、image/download(url, directory, filename)、status(webvpn_mode)。

各命令支持 auto/direct/vpn；size 为 1–20，posts 默认 10，其余默认 20，返回 nextFrom。下载为 durable job，其余为 request。独立 Access/Refresh Token，优先刷新，通过 daemon.webvpn 获取路由客户端；按路由缓存认证、锁保护共享状态，搜索至少间隔一秒。保留 UBB 解析、部分结果 warnings、安全文件名、同名编号；图片上限 3.75 MiB，附件上限 100 MiB。close 清理 Token 和客户端。

## 11. 日志、Linux 与服务器

默认日志路径：Windows %LOCALAPPDATA%/zju-cli/logs/；Linux/macOS $XDG_STATE_HOME/zju-cli/logs/，缺省 ~/.local/state/zju-cli/logs/。日志和状态独立于临时运行目录，单一 daemon 日志由各模块 logger 名区分。

统一配置 Python logging，UTF-8 纯文本，每条含北京时间与 +08:00、级别、模块名、消息。默认 INFO，支持 DEBUG/INFO/WARNING/ERROR；按相同日志时区日切 daemon-YYYY-MM-DD.log、重启追加、历史保留。每条同步 flush，关闭统一清理。支持 log_level、log_output=file/stderr、log_dir 和 UTC 时区；业务日期保持北京时间。任务开始、完成、失败记录 ID 与命令，不记录凭据、Cookie、Token、签名链接及敏感响应。写文件失败不影响业务，安全 stderr 提示且后续可重试。

Linux：配置 $XDG_CONFIG_HOME/zju-cli/（~/.config/zju-cli/）；状态 $XDG_STATE_HOME/zju-cli/（~/.local/state/zju-cli/）；运行信息 $XDG_RUNTIME_DIR/zju-cli/，缺省状态目录下 run/。只接受绝对 XDG 路径，目录 0700、凭据/数据库/日志/密钥 0600。保留原配置兼容，显式 --config 优先。

daemon run 是持续运行的前台进程；SIGINT/SIGTERM 进入统一关闭。Linux 子进程独立进程组并回收，Windows 使用对应机制。强停后持久任务 interrupted。systemd 用户服务示例使用 Type=simple、绝对 ExecStart、stderr journal；CLI 与服务使用相同非特权用户和目录。CI 覆盖 XDG、权限、前台信号、子进程组与无桌面运行。

## 12. CLI、数据和验收

CLI 负责解析、绝对路径、配置、真实写入确认、展示和退出码。request 默认轮询 task.get，job 默认返回 ID；既有音频 extract 默认等待。超时或 Ctrl+C 只停止前台等待，任务继续。保留旧 CLI；schedule 为课表，对应 classroom.schedule，schedules 为周期计划。

任务/计划由 daemon 持久化，业务记录归模块。数据升级校验版本并保留备份；认证材料仅内存保存。验收覆盖命令注册、生命周期、任务并发/取消/恢复、CLI 轮询、计划持久化、CAS 失效、WebVPN 路由、CC98 刷新、音频恢复、资料同步、作业核实、签到去重、日志与敏感信息。自动测试使用模拟平台，不向真实服务提交写入。
