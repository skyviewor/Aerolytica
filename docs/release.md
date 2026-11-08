# 客户端发布

当前仅发布到 OSS，暂不向 PyPI 或 TestPyPI 发布 Aerolytica 包。
版本唯一来源是 `pyproject.toml` 的 `project.version`，发行包名为 `aerolytica`。
命令、import 包仍为 `aero`，TUI/CLI 从已安装 metadata 读取版本；源码开发回退读取
pyproject。发布不会自动篡改版本号：先修改版本，再建完全一致的 `v<版本>` tag。

## 流程和闸门

`.github/workflows/ci.yml` 在 main/PR 执行四平台 Python 3.12 测试、Ruff、WebUI
检查/构建、wheel/sdist 校验，无发行密钥。生产 `.github/workflows/release.yml`
正式 Release published 自动选择 `production`，不发布 prerelease；也可手动选择
`testing` 或 `production` 和已有 tag。两个环境分别串行，不会取消前一发行。
所有 job 显式绑定所选 GitHub Environment，使用该环境的 Variables/Secrets。

`testing` 分支的 push 自动发布测试环境，直接构建该次 push 的 commit SHA，
从 pyproject 读取版本（无需先创建 Git tag），不要求代码已合入 main。
后续上传和推进步骤固定使用同一 SHA，避免运行期间分支移动改变发布代码。
生产发行仍要求 tag 版本匹配且源提交已进入 main；测试 push 不能选择生产环境。
这样安装构建可以先在 testing 验证，main 保持已充分测试的代码。

正式 Release → checkout tag → 验证版本与 main 祖先关系 → 测试/WebUI →
构建一次签名 wheel/sdist/清单 → 上传不可变 OSS 文件并检查公网映射 →
四平台从 OSS 脚本安装、setup/doctor/冒烟 →
最后更新 OSS `install.sh` 和 `stable.json`，无需等待 PyPI 组织审批。

```mermaid
flowchart TB
    A["正式 GitHub Release：v版本"] --> B["版本校验、测试、Ruff、WebUI 构建"]
    B --> C["构建一次：wheel / sdist、依赖锁、签名清单"]
    C --> D["OSS 版本目录：不可变文件、公网校验"]
    D --> E["Linux / macOS × x86_64 / ARM64 安装冒烟"]
    E --> F["更新 OSS 稳定入口：install.sh、stable.json"]
    F --> G["新用户：官网一行安装"]
    G --> H["私有 Python + OSS 客户端 wheel"]
    H --> I["aero setup → aero init → aero chat"]
    F --> J["已有客户端：检查版本、验签"]
    J --> K["用户批准、等待空闲、升级；失败回滚"]
```

上图仅展示 OSS 发布、安装与升级链路，不展开第三方依赖下载来源。

正式版新产物使用 `releases/<version>/builds/<完整提交SHA>/` 不可变路径；
版本选择仍通过无缓存的 `releases/<version>/latest.json`，兼容旧的平铺发行目录。
这可以避免尚未上线的失败构建重试时命中旧 CDN 缓存。已经推进默认入口的正式版本
仍禁止被不同清单替换，不因新构建路径而放宽同版本覆盖限制。

### 安装时自动选择依赖源

安装器先检查依赖源可用性，再安装依赖。在线预检使用 `uv pip sync --dry-run --refresh`
刷新源元数据，避免把旧缓存误判为在线可用，并检查发行版
实际锁定的依赖、平台标记和二进制包，不只是测试源首页能否打开；预检不安装软件包。
每个候选源的预检最多 20 秒，首次通过即开始安装，不等待其他源全部检查完成。

用户设置了 `UV_DEFAULT_INDEX`、`UV_INDEX_URL`、`UV_INDEX`、`UV_EXTRA_INDEX_URL`
或 `UV_FIND_LINKS` 时，优先检查并使用这套配置；不可用时自动改用内置备用源，
不要求用户先取消环境变量。中国大陆默认依次检查阿里云、腾讯云、华为云、清华和
官方 PyPI；其他地区默认先检查官方 PyPI，再按上述顺序检查其余镜像。

切换只影响安装器子进程，不修改 shell 配置或用户环境变量。备用尝试移除原有的
索引、额外索引、find-links、索引凭据和不安全主机配置，不把私有源凭据带给公共镜像；
不会上传用户项目或私有包，只下载官方发行清单的锁定依赖。显式设置 `UV_OFFLINE`
或 `UV_NO_INDEX` 为真仍表示禁止在线索引，不会擅自改为公共在线源。

预检跳过不可用源时不展示子进程报错、源地址或凭据。选定后若网络中断，继续有界切换；
全部候选均不可用才报告最终错误。SHA256 不匹配、磁盘不足和权限错误直接中止，
不以换源掩盖。所有候选保持版本锁定、哈希检查、仅二进制依赖及 TLS 校验。
同一逻辑用于首次安装和托管更新准备；安装失败不覆盖正在使用的旧版本。

### 安装目录与 shell 初始化

macOS/Linux 的交互式本地终端与 SSH TTY 均从 `/dev/tty` 读取安装确认，
因此 `curl … | sh` 的脚本标准输入不会被误当成用户回答。默认主目录为
`~/.aero`（测试环境 `~/.aero-testing`），固定命令入口在主目录的 `bin/`，
不会将具体版本的 Python 环境目录加入 PATH。用户可以回车接受默认值，
也可以自定义目录；旧安装和旧 `.local/bin` 入口不自动删除或迁移。

安装后询问配置哪个 shell，优先推荐识别出的父终端 shell，无法识别时参考
`SHELL`，最终以用户选择为准。Bash 同时覆盖 `.bashrc` 和已生效的登录配置文件，
不新建 `.bash_profile` 遮挡已有 `.profile`；Zsh 尊重 `ZDOTDIR`，Fish 尊重
`XDG_CONFIG_HOME` 并使用 Fish 语法。未知 shell 不当作 Bash 自动写入。
写入前列出具体配置文件并征求同意，修改使用独立的生产/测试标记块，可重复运行；
保留原内容并生成备份，符号链接保留、修改其已确认的目标，只读文件不覆盖。
不执行或自动 source 用户 rc 文件，也不启动嵌套 shell。

子进程不能修改当前父 shell 的环境：配置完成后提示准确的 `source` 命令，
或重新打开同类终端即可；拒绝配置则提示临时 PATH 和完整命令路径。
无 TTY、CI 或 `--yes` 模式默认不修改 rc 文件。

无人值守可显式指定路径与配置授权，例如：

```sh
curl -LsSf https://aero.skyviewor.com/download/install.sh | sh -s -- \
  --yes --prefix /absolute/path/aero --shell bash --shell-init
```

`--bin-dir` 可单独指定命令目录，`--rc-file` 可指定配置文件；
`--no-shell-init` 禁止写入任何 shell 配置。`--yes` 本身不等于同意修改 rc。

测试失败、格式检查失败、依赖缺 wheel 或 OSS 失败均阻止稳定入口推进，
没有忽略错误的生产分支。现有仓库测试/格式问题需先处理，不能用跳过检查上线。
一般开发 wheel 构建不需要签名密钥；正式发行构建在临时源码副本嵌入公钥。
私钥只在构建步骤环境中使用，不写入源码、wheel、sdist 或日志。

## GitHub / OSS 配置

当前生产 workflow 不使用 PyPI Environment、Trusted Publisher 或 OIDC 发布权限。

先在仓库 Settings → Environments 创建 `production`、`testing`。下面配置分别填入
两个环境，各自配置独立 bucket、后端地址与密钥。生产环境可启用部署审批。
环境名由 workflow 决定，无需另外配置 `AERO_ENVIRONMENT` Variable。

GitHub Environment secrets：

| Secret | 用途 |
| --- | --- |
| `OSS_ACCESS_KEY_ID` | 仅 OSS 上传步骤使用 |
| `OSS_ACCESS_KEY_SECRET` | 仅 OSS 上传步骤使用 |
| `RELEASE_SIGNING_KEY` | base64 编码的 Ed25519 32-byte seed，仅签名构建步骤使用 |

GitHub Environment variables：

下面非敏感配置也可以全部保存为同名 Environment Secrets；workflow 优先读取
Secrets，未配置时才读取 Variables。私钥与 OSS 访问凭据必须保持在 Secrets 中。

| Variable | 配置 |
| --- | --- |
| `OSS_BUCKET` | 已存在的发布 bucket 名称 |
| `OSS_ENDPOINT` | bucket 区域 HTTPS endpoint |
| `OSS_PREFIX` | 必填；两个独立 bucket 均填 `download`；不允许 bucket 根目录 |
| `AERO_DOWNLOAD_BASE` | 必填；对应环境 bucket 的公网 HTTPS 下载地址，路径为 `/download` |
| `AERO_OFFICIAL_API_URL` | 必填；该环境的平台 API HTTPS 地址（生产 `https://api.aerolytica.skyviewor.com`） |
| `AERO_OFFICIAL_LLM_URL` | 必填；该环境的 LLM 网关 HTTPS 地址，含 `/v1`（生产 `https://llm.aerolytica.skyviewor.host/v1`） |
| `RELEASE_PUBLIC_KEYS` | 可选，key-id → base64 公钥的 JSON；用于提前分发新公钥 |

密钥必须由维护者在受控环境生成并直接保存为 Secret，不要提交或粘贴进 issue。
OSS RAM 权限只需这个发布前缀的对象读取/写入；不需要删除对象或 bucket 管理权限。
公网需要匿名读取；对象必须经 HTTPS 提供，CDN/反向代理应尊重稳定对象 no-cache。
不要为排错打印环境变量、Secret 或 OSS SDK 的签名请求异常。

### 后端地址、用户状态与升级隔离

CI 将环境名、API/LLM 地址和下载入口写入 wheel 的 `release_environment.json`，
并纳入签名发行清单。安装脚本也内置所选环境和下载入口，用户无需手动 export。
测试地址未配置时构建失败，绝不使用生产地址替代。地址不包含凭据；客户端需要
连接的地址必然可以从安装包读取，不能将域名当作秘密或鉴权措施。

生产入口仍为 `aero`，默认用户目录 `~/.aero`；测试入口为 `aero-testing`，默认目录
`~/.aero-testing`。登录凭据、Agent 注册与锁、用户会话、科研运行时及升级记录隔离。
两个环境可以同时安装；测试版升级不会改写生产版入口，也拒绝安装生产清单。
系统后台服务名称和持久化环境同样分开。已有生产目录不搬迁、不修改账号数据。
工作区内用户主动选择的项目文件仍共享，不复制或清理科研数据。

运行时显式 `AERO_OFFICIAL_API_URL` / `AERO_OFFICIAL_LLM_URL` 覆盖仍可用于本地联调。
不要用 `AERO_ENVIRONMENT` 把已构建的测试安装包切成生产版，这种不一致会被拒绝。
源码/普通 pip 构建默认生产；源码测试模式须显式设置两个后端地址，检查更新还需设置
测试 `AERO_DOWNLOAD_BASE`。升级读取新安装包的默认地址，旧默认值不会继承为覆盖值。

正式 Release 自动走生产；测试在 Actions 中手动运行 Publish Aerolytica to OSS，
选择 `testing` 和匹配版本的已有 tag。同一版本可在两个独立 bucket 各构建一次；
每个 bucket 的 `download/` 内不可覆盖同版本产物，不添加 `testing/` 子目录。
下载域名须映射到对应环境 bucket。仍不向 PyPI 发布。

测试分支按提交 SHA 保存修订：`releases/<版本>/revisions/<完整提交 SHA>/`。
这允许测试环境在不改版本号时重新发布：新修订仍走签名、上传、公网哈希校验及四平台
安装测试，最后才替换测试环境的 `stable.json` 和默认安装脚本。旧修订及旧版平铺目录
保持不可变，失败时默认入口不变。生产环境仍使用原布局并要求递增版本。

同版本测试修订不会触发普通自动更新。新客户端可显式执行 `aero-testing update --reinstall`；
已安装旧版 0.1.2（尚不认识该参数）的用户可重跑官网安装脚本：

```sh
curl -fLsS https://aerolytica.skyviewor.team/download/install.sh | AERO_REINSTALL=1 sh
```

安装器只为同版本、同测试环境的修订启用此入口，先在独立目录验证，再由正常升级器
检查空闲、切换和恢复后台进程。不会覆盖正在运行的环境，也不会强制杀进程。
仍打开的其他 TUI 或不支持交接的旧服务必须先关闭。账号、项目和科研环境不清理。

首次验证可直接从 develop 创建 testing 分支并推送，自动跑完整流程。
这不需要把 workflow 或客户端代码合入 main，也不修改仓库默认分支。
GitHub 的网页手动运行按钮依赖默认分支存在 workflow；尚未合入 main 时，
使用 testing push 触发，失败后可在该次运行页面使用 Re-run failed jobs。
同一测试版本成功上传后仍不可覆盖；后续不同构建应先递增版本号。

### 本地生成签名密钥与备份

在已安装开发依赖的本地交互式终端运行：

```bash
uv run --locked python scripts/release/generate_keys.py --environment production
```

测试密钥使用 `--environment testing` 单独生成。参数仅标注环境，不创建或配置
GitHub Environment。脚本显示私钥、公钥、密钥标识、生成时间，以及符合发行格式的
`RELEASE_PUBLIC_KEYS` JSON；不联网上传、不写文件、不修改已有配置。
私钥填入 GitHub Secret `RELEASE_SIGNING_KEY`，公钥 JSON 可填入 Variable
`RELEASE_PUBLIC_KEYS`（首次可不填，构建器会自动嵌入当前公钥）。

执行前关闭录屏/共享屏幕；将完整输出保存到 1Password 等密码管理器，生产/测试
分别标注，保存后清理终端记录。私钥不能仅依赖 GitHub Secret 作为唯一备份。
脚本拒绝管道和重定向输出，避免私钥意外落入日志或仓库文件；不要在 CI 中运行。
每次执行都会生成新密钥，不能当作读取旧密钥的命令。已发布后必须按轮换流程
分发新公钥再更换私钥，并合并保留所需历史公钥条目。

## OSS 布局

```text
download/
  install.sh                         # 成功发布后才推进
  stable.json                        # 签名清单，最后一步推进
  releases/<版本>/
    latest.json                      # 此版本最近验收修订的签名清单；promote 后更新，不设 immutable
    aerolytica-<版本>-py3-none-any.whl
    requirements.txt                 # uv.lock 导出的平台标记 + SHA256 锁
    bootstrap.py
    install.sh
    release.json
    aero-<平台>.tar.gz               # 同一 wheel 的兼容归档
    *.sha256
  bootstrap/uv/<固定版本>/<target>.tar.gz
  bootstrap/python/<固定构建>/<平台>.tar.gz
```

`--version` 优先读取版本自己的 `latest.json`；当前 stable 恰好是该版本时直接使用 stable，
兼容旧发行时回退到原有的平铺 `release.json`。只有 404/410 才尝试旧布局，网络/服务错误
不改投其他版本。`--revision` 直接读取不可变的修订清单，不跟随任何版本指针。
版本指针只在安装和公网验收后的 promote 阶段更新；发布旧版本可记录其指针，但不能
将全局 stable 降级。旧发行文件、修订与签名不修改，也不将选择的版本永久写成更新源。
首次使用新发布器推进 stable 到新版本时，会为上一版缺失的版本指针保留原 stable
签名清单，因此既有测试修订也能在后续发布后通过 `--version` 选回，不必重打旧包。

归档的固定平台名不表示客户端有四种二进制 wheel：客户端是一个跨平台 wheel；
私有 Python/uv 和依赖中的原生 wheel 按平台选择。引导工具版本由
`scripts/release/toolchain.json` 与 install.sh 同步固定，构建器会校验一致性。
新 Python 解释器用独立目录，不向活动解释器重复解压。依赖下载必须匹配锁中的 hash。

`dist/release/pypi/` 仍是本地 wheel/sdist 构建目录名，不表示执行网络发布。
官方安装与升级从 OSS 取得 Aerolytica wheel；第三方 Python 依赖仍从 PyPI 或配置的
镜像下载，科研环境依赖仍从 conda-forge 或镜像下载。暂停的是 Aerolytica 包发布，
不是禁用所有依赖仓库。

## 重试、回滚与公钥轮换

构建结果保留为 GitHub Artifact 90 天。部分失败时用“Re-run failed jobs”，
复用同一 wheel/sdist，勿修改 Release 描述或公钥配置后重新构建已发布版本。
已存在的 OSS 不可变对象必须与本次 hash 完全相同，否则拒绝覆盖。
新的全量构建也使用 SOURCE_DATE_EPOCH 和无路径头的依赖导出，避免时钟/临时路径漂移。
旧版本 Release 重新跑不会把 stable 降级；客户端也不自动降级。

需要撤销发行时，先修复并发布更高版本；用户可显式 `aero update --rollback`。
不要覆盖同版本产物或删除用户正在使用的历史 OSS 产物。

公钥轮换分两次发行：先把新公钥放入 RELEASE_PUBLIC_KEYS，仍用旧私钥签名；
待客户端获得新公钥后，再切换 RELEASE_SIGNING_KEY。历史公钥不能直接移除，
否则未更新的用户无法验证下一版。首次安装依赖官方 HTTPS 信任入口。

## 暂停 PyPI / TestPyPI

正式发布已移除 PyPI job，OSS 推进直接依赖四平台安装冒烟。
未来恢复 PyPI 发布时，需重新加入发布 job 与依赖闸门，并为 `aerolytica` 配置
Trusted Publisher：owner/repository 为 `skyviewor` / `Aerolytica`，workflow 为
`release.yml`，GitHub Environment 为 `pypi`。首次发布需确认项目名所有权，
可用 pending publisher；不需要 PyPI API token。

`.github/workflows/testpypi.yml` 保留为手动演练，但默认暂停：仅在 repository variable
`ENABLE_TESTPYPI_PUBLISH` 明确设置为字符串 `true` 后才执行，不写 OSS、不推进 stable。
TestPyPI 配置相同项目的 pending/trusted publisher，workflow 为 `testpypi.yml`，
environment 为 `testpypi`。选择已有且与 pyproject 完全匹配的 tag。
TestPyPI 不含生产签名公钥，不用于验证生产自动更新。

## 上线验收

首次生产发布前确认：所有测试/格式检查通过；四平台 shell 安装通过；
`setup` 不启动账号向导或初始化项目；`setup --yes`、兼容参数 `--runtime-only` 和 doctor 成功；
空闲更新/忙碌撤回/后台 cwd 恢复/失败回滚验证通过；官网指向公开且有正确缓存策略的
bucket 前缀；OSS 上传密钥与发行签名密钥已配置。
本地验签/切换/真实构建测试不替代 Linux/macOS 四平台的真实安装验收。
