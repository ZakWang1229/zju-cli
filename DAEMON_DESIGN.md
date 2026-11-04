# Daemon 设计草案

本文件是本轮重构的设计依据。接口与验收要求见 [MODULE_SPEC.md](MODULE_SPEC.md)，实际验证记录见 [VALIDATION.md](VALIDATION.md)。

## 结构

一个常驻 daemon 进程，模块作为实例挂在 daemon.* 下。模块初始化时注册命令绑定方法，内部直接调用其他模块的公开方法。

```text
CLI → 本机 RPC → daemon 命令注册表 → 模块绑定方法
                                      └─ self.daemon.zjuam 获取认证
```

| 实例 | 职责 |
|---|---|
| zjuam | CAS 登录与平台会话 |
| webvpn | WebVPN 登录与代理访问 |
| courses | 课程、作业、资料及学校签到接口 |
| classroom | 智云课表、回放音频 |
| autosign | 自动签到策略与状态 |
| cc98 | 论坛功能和 Token |
| tasks | 一次任务的执行与状态 |
| schedules | 周期性创建任务 |

## 模块注册

```python
class CoursesModule:
    def __init__(self, daemon, config):
        self.daemon = daemon
        self.config = config
        daemon.register("courses.list", self.list_courses)

    def list_courses(self, *, keyword=""):
        session = self.daemon.zjuam.courses()
        return fetch_courses(session, keyword)

    def close(self): ...
```

Daemon 先创建 tasks/schedules 和认证模块，再创建业务模块。全部初始化后接收请求，退出时等待任务结束并逆序 close()。

## 调度

所有业务命令进入 tasks 的统一队列。短请求和长任务使用同一执行机制，CLI 决定等待还是展示任务 ID。tasks 提供 submit/get/list/cancel/resume；CLI 通过 get 轮询等待。

schedules 保存命令、参数和间隔，到期调用 tasks.submit。每轮完成后等待指定间隔，同一计划单轮执行。启用状态和最近业务结果分别保存。

命令通过 daemon.task 获取进度、取消、恢复点和产物路径。取消采用协作检查，外部工具由任务登记并回收。

## 认证与资源

zjuam 保护共享 CAS 登录态，完成平台登录后返回独立会话。业务模块复用自己的会话，失效时重新获取。webvpn 独立提供代理访问，CC98 自身维护论坛 Token。

各模块使用 Python logging，入口统一配置。默认 INFO，文件按天保存为 logs/daemon-YYYY-MM-DD.log，保留历史；服务器托管可选择 stderr，由 systemd 等环境收集。前台入口持续运行，由服务管理器跟踪生命周期。Linux 使用 XDG 目录，支持信号退出。模块负责自身共享资源的线程安全和 close() 清理；配置、状态和恢复数据归属明确。

## 迁移

先实现命令注册和 tasks/schedules，再迁移 zjuam/webvpn、courses/classroom、autosign/cc98，最后统一 CLI 映射和数据迁移。保留现有命令，按业务归并实现文件。
