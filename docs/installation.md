# 安装与运行时

## 推荐安装

```bash
curl -LsSf https://aero.skyviewor.com/download/install.sh | sh
```

安装过程分为两层：

1. 脚本下载带校验值的私有 uv 和 Python 3.12，再安装签名清单指定的
   `aerolytica` wheel 与锁定依赖，无需用户预装 Python、Conda、mamba 或 uv。
2. 安装成功后提示用户手动运行 `aero setup`，将 Micromamba 和科学计算环境安装到
   `~/.aero/runtime/`。此步骤不登录、不配置模型，也不初始化研究项目。

Aero 不使用系统 Python，不向 Conda base 安装包，也不会复用或修改用户已有的
Miniconda、Anaconda、Miniforge 环境。

测试发行使用独立下载前缀的安装脚本，自动安装为 `aero-testing`，后续运行
`aero-testing setup` / `aero-testing chat`。默认状态与科研环境在 `~/.aero-testing/`，
不覆盖正式版的 `aero` 入口、账号凭据或运行时。后端地址内置于该环境的发行包；
发布配置见 [客户端发布](release.md)。

首次安装的信任入口是通过 HTTPS 获取的官方脚本和引导文件；新环境启用前，
仍需用 wheel 内嵌的 Ed25519 公钥验证清单。后续更新先验证签名再执行安装。
Python/uv 是按平台选择的私有引导工具；客户端本身是统一的 `py3-none-any` wheel。
支持 glibc Linux x86_64/ARM64 和 macOS Intel/Apple Silicon，不支持 Alpine/musl。

默认 CLI 入口是 `~/.local/bin/aero`；脚本在 `.zshrc` 或 `.bashrc` 追加 PATH，
并显示当前终端应执行的 export。不会覆盖其他软件同名的 `aero` 入口。
旧版 `uv tool` 安装若占用入口，先确认其为旧 Aerolytica，再由用户迁移/移除旧入口。

## 已有 Python 环境 / PyPI

正式发布后，可在 Python 3.12+ 环境运行：

```bash
python -m pip install aerolytica
aero setup
```

PyPI 项目名为 `aerolytica`，命令和导入名仍是 `aero`。PyPI 安装使用用户当前环境；
`aero setup` 仍下载 Aero 专用 Micromamba，不调用用户 Conda/mamba 创建环境，
也不把科学计算工具装进用户 venv/base。账号配置和科学运行时与 CLI 升级分开保存。
项目声明 Python >=3.12；依赖中的原生 wheel 是否可用仍取决于具体 Python/平台。

## 项目初始化

进入研究项目目录后执行：

```bash
aero init
aero chat
```

`aero init` 只在当前目录创建项目配置和工作目录，不安装软件，也不会继承父目录的
Aero 项目配置。

## 运行时模式

```bash
aero setup          # 只准备私有 Python 3.12 科研运行环境，无需登录
aero setup --yes    # 自动化准备环境，不弹出登录或模型配置向导
aero setup --runtime-only --yes  # 兼容旧命令，行为与 setup --yes 相同
aero setup --full   # 预装 CDO、NCO、ecCodes、GDAL、绘图等完整工具集
aero doctor         # 检查 Micromamba、环境 Python 和版本
aero runtime clean  # 删除 ~/.aero/runtime，项目数据不受影响
```

可通过 `AERO_RUNTIME_ROOT` 自定义运行时根目录。清理后再次执行 `aero setup` 即可重建。

官方账号登录由 `aero login` 或首次进入 `aero chat` 时的网页授权流程负责。
`setup` 不读取或修改账号、模型设置；已有登录保留，未登录也可以完成环境准备。
测试发行对应使用 `aero-testing setup` / `aero-testing login` / `aero-testing chat`。

登录提供两种方式，每次由用户选择（环境检测只影响推荐项）：

- **浏览器授权**：打开官网后直接点击“批准”，客户端和网页均不展示验证码。
- **验证码授权**：终端显示网址与验证码，可在其他设备打开网页并输入验证码，适合 SSH。

浏览器打开失败时会切换为验证码授权，复用同一个请求；也可主动切换。
自动化调用可用 `aero login --browser` 或 `aero login --no-browser` 明确选择。
登录框支持 Tab / Shift+Tab、左右键切换按钮，Enter 执行当前按钮，Esc 取消。

## 自动化安装

```bash
./install.sh  # 始终只安装 CLI，安装后手动 aero setup
AERO_DOWNLOAD_BASE=https://mirror.example.com/download ./install.sh
```

中国大陆网络会沿用项目的 PyPI 与 Conda 镜像检测，也可用
`AERO_NETWORK_REGION=mainland_china` 显式指定。

客户端锁定依赖在大陆默认依次尝试清华镜像、阿里云镜像、官方 PyPI；其他地区
先使用官方 PyPI，再尝试阿里云。连接失败、镜像拒绝访问或缺包时有界切换，每次
仍强制 SHA256 校验、仅安装二进制 wheel，不关闭 TLS 校验。校验失败、磁盘或
权限错误直接停止。安装失败会显示步骤和脱敏后的原因，不输出凭据或私人路径。
显式设置 `UV_DEFAULT_INDEX` 等依赖源环境变量时尊重该配置，不自动改投其他源；
官方私有安装不读取当前研究目录的 uv 配置，以免项目配置意外影响客户端安装。

## 更新与回滚

```bash
aero update --check
aero update                  # 显式确认后执行
aero update --yes --wait 300  # 显式授权，最多等后台任务空闲 5 分钟
aero update --rollback
```

正常 chat 和常驻 Agent 对话支持 `/update`。自动检查在后台进行、最多每 24 小时一次，
网络失败也会缓存；空闲时用现有审批框提供“更新 / 稍后 / 跳过此版本”，不会在
生成回复、审批、用户输入或子任务执行中弹窗。源码开发安装不自动检查或改动环境。

官网安装使用 `~/.aero/client/versions/<版本>/`：新版本在最终路径安装、验签与冒烟，
随后原子切换 `current`；`previous` 保留上一版本。不移动 venv，不修改旧环境。
本机后台 Agent / daemon 先暂停接新任务，已有任务（包括等待审批/输入）完成后
再正常退出、按原工作目录重启。新进程先暂停，全部启动成功后才恢复接单。
启动失败自动回滚；恢复本身失败会保留私有更新日志，须运行 `aero update --rollback`。
不强杀任务、不根据过期 PID 发信号，不删除用户项目、密钥、对话或科学运行时。

其他终端的前台 Aero 必须先退出；无法识别安全握手的旧版 Agent / daemon 必须手动
停止。任务未在等待期限内结束时，更新撤回并恢复接单，之后可重试。
非托管的 pip/pipx/普通 uv 安装只检查和提示对应的升级命令，不覆盖自有环境。

## 发行维护

正式发行、OSS 映射、签名公钥轮换、GitHub Secrets 和 TestPyPI 演练见
[发布流程](release.md)。旧构建入口 `scripts/release/build-download-packages.sh`
转发到签名构建器，只生成产物，不执行上传。
