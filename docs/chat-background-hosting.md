# 将当前聊天转为后台常驻

在普通 `aero chat` 的聊天输入框输入（不是另一个终端）：

```text
/agent background my_agent
```

也可使用等价快捷别名 `/host my_agent`。两者都保留当前对话，执行同一托管流程。

- `my_agent` 是本机注册名称，以小写字母开头，可包含数字，用下划线连接，且不能重复。
  例如 `mytest2`、`ocean_data2` 都合法。
- 须先登录官方账号；当前任务和子任务必须先完成或取消。
- 不要求绑定云项目。转为常驻需要可用的常驻智能体名额。
- 转换保留原云端对话 ID、本地会话、历史、网页显示名称和模型/模式。
- 后台取得云端运行授权后 TUI 退出；关闭终端不终止后台进程。
- 空会话会发送预设的报到提示；已有用户对话不再额外报到。
- 原前台令牌撤销，避免前台和后台同时执行。
- 若云端拒绝转换，保留前台聊天；若转换成功但后台启动失败，退出前台并显示恢复命令。

管理后台进程：

```bash
aero agent list
aero agent stop my_agent
aero agent run my_agent --background
```

恢复命令须在原工作目录执行。已有云项目绑定保持不变。
`aero chat --service` 是本机服务附加界面，不支持上述普通 TUI 的托管命令。

## 在本地打开常驻 Agent 原对话

```bash
aero chat --agent my_agent
```

须登录注册该 Agent 的官方账号，可以使用本机注册名称或 Agent ID。
此命令只打开对话界面，不注册新 Agent、不启动第二个执行器。
读取原云端历史，输入消息后由原常驻进程执行，网页和 TUI 显示同一对话。
离线 Agent 不会自动启动；可先在原工作目录执行 `aero agent run my_agent --background`。
输入 `/quit` 或按 Ctrl+Q 关闭界面，后台 Agent 继续运行。

该入口复用普通聊天的主题、消息布局、Markdown 渲染和多行输入框。
Enter 发送，Shift+Enter 换行；Tab 切换执行/问答/规划模式，Ctrl+Y 复制最后回复。
此处只发送远程任务，不运行普通聊天的本地执行器；支持的命令以下方列表为准。
输入 `/` 弹出与普通聊天相同的命令菜单，上下键选择、Enter 确认、Esc 关闭。
`/model`、`/mode`、`/theme` 可直接打开选择界面；`/copy` 复制回复，`/help` 查看支持范围。

界面使用实时事件接收更新，断线退避重连，不定时轮询整个历史。
发送失败不自动重发，先检查对话是否已有该消息再决定重试。
支持 `/approve [审批ID]`、`/deny [审批ID]`、`/cancel`、
`/model 模型ID` 和 `/mode execute|qa|plan`。
凭据或额外输入请求目前提示在网页的同一 Agent 对话中处理。
不能与 `--service`、`--attach`、`--continue` 混用。
