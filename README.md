# meteora
Meteora 是一个面向气象与地球科学研究的 AI Agent IDE，帮助研究者从数据获取、算法计算、科研绘图、文献整理到 LaTeX 论文生成，完成端到端的科研工作流。

## 安装

```bash
git clone <repo-url> && cd meteora
pip install -e ".[dev]"
```

## 使用

```bash
meteora init
meteora chat
```

## 运行测试

```bash
pytest tests/ -v
```

## 命令

| 命令 | 说明 |
|---|---|
| `meteora init` | 初始化当前目录，并引导准备 Miniconda 与 `meteora-agent` 环境 |
| `meteora chat` | 启动 Textual TUI 对话 |
| `meteora chat --simple` | 启动纯文本对话 |
| `meteora chat --mouse` | 启用 Textual 鼠标滚轮与拖选自动复制 |
| `↑/↓` 或 `PageUp/PageDown` | 在 TUI 中滚动聊天区域 |
| `/copy` 或 `Ctrl+Y` | 对话中复制最后一条 Meteora 回复到剪贴板 |
| `/set max_tool_rounds N` | 对话中设置当前会话最大工具调用轮次（默认 20，范围 1-100） |
| `meteora chat --debug-input` | 诊断 Textual 是否收到中文输入事件 |
| `meteora version` | 显示版本号 |

> **提示**：日常使用 `meteora chat` 即可进入 TUI 模式并直接输入中文。多行输入使用 `Shift+Enter`；如果你的终端没有把这个组合键传给应用，可用 `Ctrl+J` 作为换行。

> **复制文本**：在启用鼠标的 TUI 中，拖选聊天区文字后会自动复制到剪贴板。使用 `--no-mouse` 时由终端处理原生选择和复制。只想复制最后一条回复时，用 `/copy` 或 `Ctrl+Y` 更快。

## 项目结构

```
meteora/
├── src/meteora/
│   ├── cli/main.py              # meteora 命令入口
│   ├── core/                    # 配置、类型定义
│   ├── agent/                   # Agent 循环、LLM 客户端、运行时
│   ├── toolbox/                 # 工具注册表、内置工具
│   └── adapters/era5_cds.py     # ERA5 CDS 适配器
├── tests/                       # 测试
└── issues/                      # 设计文档
```
