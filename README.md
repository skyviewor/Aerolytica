# Aerolytica

Aerolytica（命令行工具名：`aero`）是面向气象与地球科学研究的 AI Agent IDE。它将数据发现与下载、文件检查、科研绘图、文献整理、计划管理和报告交付整合在同一个对话式工作流中。

## 能力概览

- **数据发现与获取**：支持检索数据集、变量和站点；下载 ERA5/ERA5-Land、GFS、GEFS、IFS/AIFS、CAMS，以及多种开放观测和卫星数据。
- **开放数据源**：内置 NOAA ISD、GHCN-Daily、NCEP Reanalysis、HRRR、MRMS、CHIRPS、MERRA-2、JRA-55、JRA-3Q、GOES 和 Himawari 等提供方。
- **数据检查与处理**：检查 NetCDF、GRIB2、CSV/ISD 和 PDF；支持 NetCDF 时空子集、下载记录查询与失败重试。
- **科研工作流**：生成并保存研究计划，管理项目指令、文献与 PDF，调用科学绘图技能，并可由子 Agent 执行后台任务。
- **交互体验**：提供中文优先的 Textual TUI 与纯文本模式，支持流式回复、图像理解、会话持久化、上下文压缩与工具授权确认。

## 安装

推荐使用一键安装脚本。无需预装 Python、Conda 或 uv；脚本准备私有 Python，
把客户端安装到 `~/.aero/client/versions/<版本>/`，通过稳定入口启动。
不会修改系统 Python、Conda base 或用户已有环境。

```bash
curl -LsSf https://aero.skyviewor.com/download/install.sh | sh
```

安装完成后运行 `aero setup` 配置账号/模型，并准备 `~/.aero/runtime/` 中的
私有科学计算运行时，再用 `aero doctor` 检查。项目初始化仍由 `aero init` 单独完成。
需要提前准备完整工具集时运行 `aero setup --full`。

已有 Python 3.12+ 环境的用户也可运行 `pip install aerolytica`（正式发布后）。
这类安装使用自己的包管理器升级；官网托管安装支持 `aero update`、`/update`
及 `aero update --rollback`。详见 [安装与升级](docs/installation.md)。

源码开发安装：

```bash
git clone https://github.com/skyviewor/Aerolytica.git
cd Aerolytica
uv sync --extra dev
uv run pytest
```

## 快速开始

在研究项目目录中初始化工作区：

```bash
aero init
```

该命令会创建 `aero.yaml` 及下列目录：

```text
data/       # 下载与分析数据
figures/    # 图件
scripts/tmp/# 临时脚本
plans/      # 会话计划
literature/ # 文献与 PDF
```

启动交互式对话：

```bash
aero chat
```

首次启动时，程序会引导配置模型服务商与 API Key；密钥保存于 `~/.aero/secrets.yaml`，不会写入项目的 `aero.yaml`。内置 DeepSeek、阿里云百炼、Kimi 与 OpenAI 的 OpenAI-compatible 配置，也支持在 `aero.yaml` 中自定义兼容服务。

如果更偏好浏览器工作台，可在项目目录运行 `aero serve`。它会打开一个本机网页界面，提供会话侧栏、流式 Agent 对话、工具进度、确认卡片、项目文件浏览与产物预览。首次启动若尚未配置模型 API Key，网页会自动弹出配置引导；密钥只保存到本机凭据存储。服务只绑定 `127.0.0.1`，不会替换或关闭 TUI；最终用户运行已安装的 `aero serve` 不需要 Node.js。

可以直接提出研究任务，例如：

```text
下载 2024 年 7 月华北区域的 ERA5 2 米气温，并绘制月平均分布图。
查找北京站 2023 年 7 月的逐小时观测，检查缺测情况。
检索近五年关于东亚夏季风与极端降水的文献，保存可获取的 PDF。
```

## 命令

| 命令 | 说明 |
|---|---|
| `aero init` | 初始化当前目录的项目配置与工作目录 |
| `aero setup` | 配置账号/模型，安装或修复私有科学运行时 |
| `aero setup --runtime-only` | 仅准备运行时，不修改账号或项目 |
| `aero setup --full` | 预装完整科学计算工具集 |
| `aero doctor` | 检查私有运行时及 Python 版本 |
| `aero update --check` | 检查稳定版更新 |
| `aero update` | 经确认后安全更新官网托管安装 |
| `aero update --rollback` | 恢复上一版客户端 |
| `aero runtime clean` | 删除私有运行时，不影响项目文件和用户 Conda |
| `aero chat` | 启动 Textual TUI 对话（默认启用鼠标） |
| `aero chat --continue` | 续接当前目录最近保存的 TUI 会话（短参数 `-c`） |
| `aero serve` | 启动本地现代 Web Agent 工作台（默认只监听 localhost:8765） |
| `aero serve --port 8765 --no-open` | 使用指定端口启动 Web 工作台且不自动打开浏览器 |
| `aero chat --no-mouse` | 禁用 TUI 鼠标模式，使用终端原生选择与复制 |
| `aero version` | 显示版本号 |
| `aero help` | 显示命令帮助 |

在对话中可使用以下常用指令：

| 指令 | 说明 |
|---|---|
| `/copy` | 复制最后一条 Aero 回复 |
| `/model <name>` | 查看或切换当前模型 |
| `/provider <name>` | 切换模型服务商 |
| `/variants low\|medium\|high\|max\|auto` | 设置推理强度 |
| `/theme dark\|light` | 切换 TUI 主题 |
| `/language zh\|en` | 切换回复语言 |
| `/set max_tool_rounds N` | 设置当前会话的最大工具调用轮次（默认 999） |
| `/revoke [tool]` | 查看或撤销某工具的“始终允许”授权 |
| `/clear`、`/quit` | 清除当前上下文、退出对话 |

TUI 中使用 `Shift+Enter` 输入多行；若终端未传递此组合键，可使用 `Ctrl+J` 换行。可用方向键或 `PageUp` / `PageDown` 滚动聊天记录。

## 数据凭据

部分服务需要在对话中按提示配置凭据：

- Copernicus Climate Data Store：ERA5 与 ERA5-Land
- Copernicus Atmosphere Data Store：CAMS
- NASA Earthdata：MERRA-2

凭据同样保存在 `~/.aero/secrets.yaml`。无需凭据的开放数据源可以直接由 Agent 查询和下载；实际可用时间范围与产品参数会在请求时确认。

## 开发

运行测试：

```bash
python -m pytest tests/ -v
```

运行静态检查：

```bash
ruff check src tests
```

主要代码位于 `src/aero/`：

```text
src/aero/
├── agent/       # Agent 循环、LLM/视觉客户端、会话与子 Agent
├── adapters/    # ERA5、GFS、GEFS、IFS 等数据适配器
├── cli/         # aero 命令与 Textual 界面
├── core/        # 配置、日志、模型服务商与通用类型
├── datasets/    # 数据集目录、数据模型与提供方实现
├── toolbox/     # Agent 可调用的领域工具
├── skills/      # 内置科学绘图、地图与运行环境技能
└── data/        # 参数表、可用性与文献辅助数据
```

## 许可证

[Apache License 2.0](LICENSE)
