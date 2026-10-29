# 官方云与本机常驻服务

## 服务生命周期

公开测试版使用 `aero chat --service` 连接工作区专属的本机服务。Web 工作台目前仅供开发预览，
需显式设置 `AERO_ENABLE_WEB_PREVIEW=1` 后运行 `aero serve`。本机服务只监听
`127.0.0.1`；关闭 TUI 不会停止后台任务。可用以下命令管理：

```sh
aero service start
aero service status
aero service logs
aero service stop
aero service enable       # 写入用户级开机启动配置，不立即调用系统激活命令
aero service disable
```

需要在终端继续使用对话界面时运行 `aero chat --service`（`--attach` 为同义参数）。
该 TUI 通过本机服务的会话接口工作：会话、运行中的任务、流式事件和确认状态都由
服务持有，因此关闭 TUI 不会结束任务，重新打开时会续接同一会话。

macOS 使用用户级 LaunchAgent，Linux 使用用户级 systemd 配置；`enable` 会在用户
明确执行后写入并激活对应服务，`disable` 会停用并移除它。没有用户级 systemd
时可直接使用 `aero service start` 的独立后台进程。服务状态文件和日志位于
`~/.aero/daemons/`，权限为用户私有。

## 登录、额度和同步

网页中的“官方云”面板将账号凭据交给本机服务处理，浏览器只得到脱敏状态。官方
模型请求统一发送到 Relay，并为每个实际模型请求生成独立的
`Idempotency-Key`；网络结果不确定时客户端查询请求状态，不盲目重发。

绑定同步前，先在面板中登录并选择云端项目。默认白名单为 `papers`、
`literature`、`scripts`、`plans`，可增加其他相对路径；凭证、私钥、缓存、
`.aero` 内部状态和符号链接越界路径始终排除。同步状态保存在工作区
`.aero/cloud-sync/`，文件内容通过 Relay 返回的 OSS 预签名 URL 直传，不经过
Relay 服务端。

## 智能体托管

一行注册并运行新智能体（已在本地注册同名智能体时直接运行，不重复注册）：

```sh
aero agent start report_recovery_test
```

此命令前台常驻，方便查看报到和网络恢复提示；按 Ctrl+C 停止。
注册失败时不继续运行，不把登录、配额或网络错误当作需要新建另一智能体。

直接后台运行，关闭终端不退出：

```sh
aero agent start report_recovery_test --background
# 已注册时也可用：
aero agent run report_recovery_test --background
```

命令返回后台 PID、私有日志路径及停止命令。后台进程启动不等于云端已连接，
在线状态通过 `aero agent list` 确认，报到及失败信息写入日志。
同一智能体受本机进程锁保护，不能重复启动。此参数不安装开机启动，电脑重启后需再次启动。

停止本机后台智能体，保留注册、凭据和历史（无需联网）：

```sh
aero agent stop report_recovery_test
```

该命令通过私有本机控制通道停止进程，不根据旧 PID 盲目发送信号。
前台或旧版后台进程没有控制通道时会明确提示在原终端停止，不误报已停止。
停止后可再次使用 `aero agent start report_recovery_test --background`。

删除指定智能体：

```sh
aero agent delete report_recovery_test
```

执行前展示 Agent ID 和删除范围，必须输入完整注册名称确认。确认后先停止本机后台进程，
再通过现有云端删除接口删除该智能体及云端对话、上下文和相关附件/审计文件，
成功后才移除该智能体的本机注册凭据。云端删除失败保留凭据，便于重试。
本地工作区、会话文件、日志和其他智能体不删除。云端删除无法撤销。

运行 `aero agent list` 列出本机保存的全部注册记录，包括名称、描述、智能体 ID、
后端确认的在线/执行中/离线状态及最后活动时间。不混入仅存在于云端的其他智能体。
断网或登录失效时仍列出本机记录，但状态显示“状态未知”并提示原因，退出码为 1；
不把未知当离线，不显示令牌，也不自动删除云端不可见的本机记录。

在面板或命令行注册智能体后，可直接使用 `aero agent run 智能体名称` 启动常驻报到和对话，
不要求先绑定云端项目。项目文件同步需要另行绑定；已有项目绑定仍须一致，不能通过省略绑定绕过。
`aero agent run` 使用同一个本机会话建立带设备租约的心跳连接；关闭 Web 界面不影响托管，租约
丢失会停止领取新任务并将无法确认的操作标为中断。多个智能体共享项目时由 Relay
项目租约串行化执行，不自动合并不同智能体的聊天记忆。
