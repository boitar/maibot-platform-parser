# 万能链接解析器（MaiBot 移植版）

自动解析 QQ 群里的多平台链接，并把图文或视频发送回群聊。

## Custom 分支与本地调试

本 fork 的维护分支为 `custom`，功能迭代从 `custom` 开始。`origin` 指向维护 fork，`upstream` 指向 `tidain/maibot-platform-parser`。上游更新保留为 merge 提交：

```sh
git switch custom
git fetch upstream main
git merge --no-ff upstream/main
```

有未提交改动时先检查和保存自己的改动，再处理冲突；不要把 `custom` 重置到上游，也不要 rebase 已有定制历史。

本地调试使用 Python 3.11+ 同步当前工作区源码（包含未提交修复）到已经安装的同 ID 插件：

```sh
python3 scripts/dev_sync.py --maibot-dir ../Maibot
python3 scripts/dev_sync.py --maibot-dir ../Maibot --apply
```

第一条只预览，第二条应用。同步会保留 `config.toml`、配置备份和用户文件，旧安装完整保存在 MaiBot 的 `data/local_plugin_backups/` 下。`.local-development.json` 记录本地来源、提交和文件哈希；运行中的内容来自最近一次同步，改完源码后需再次执行同步命令。调试期间通过该命令更新，市场更新仍会替换安装目录。

MaiBot 检测源码变更后会重启插件运行进程，QQ 连接会短暂重连。同步输出只确认文件替换完成，加载结果需查看管理端状态和日志。图片和视频的单发及合并转发使用 Base64，避免 QQ 无法读取机器人缓存目录；发送结果同时校验 OneBot `status` 和 `retcode`，日志记录返回的消息 ID 供实收核对。

回归测试使用 `aiohttp`、Pillow 和 MaiBot SDK，可直接使用已有 MaiBot 的 Python 环境运行：

```sh
../Maibot/.venv/bin/python -m unittest discover -s tests -v
```

### X 作者、原文、翻译与图片预处理

- QQ 分享卡片优先使用完整跳转地址，支持原始 JSON 卡片、适配器文字及引用卡片。标题/摘要里带省略号的截断链接不会被当作短推文 ID；只有截断链接时会提示补发完整链接，避免串到其他推文。
- 优先通过 FxTwitter 获取作者昵称、`@账号`、完整原文和媒体，兼容新旧返回结构。纯文字推文也可发送。详情不可用时使用 xdown 获取媒体，并明确标注作者/正文未获取。
- 文字按“作者名(@账号) → 正文 → 中文译文”换行展示，不添加栏目标题和来源链接页脚。长文按顺序分段，X 原文不受通用 `max_text_chars = 700` 摘要限制；中文原文无需翻译时不追加重复提示。
- X 视频推文始终优先使用合并转发，文字、图片及所有视频保持原顺序，普通多图转发开关不拆出视频。SDK 节点按 MaiBot 的格式构造，媒体一次编码后供 SDK 和 HTTP 回退复用；视频转发等待时间为 300 秒。
- 合并转发失败后按原顺序逐条发送全部文字和媒体；单发视频也使用 Base64。SDK 发送含视频的转发时不把大型 Base64 副本写入聊天历史。
- `[twitter_translation]` 默认开启，通过 MaiBot 的 `utils` 模型任务翻译为简体中文，独立调用且不携带聊天历史。中文原文无需翻译；空正文、模型超时和失败都有明确标记，原文及可用图片继续发送。
- 成功译文按推文 ID、正文内容、目标语言和模型任务缓存，默认保留 24 小时、最多 256 条；插件或模型配置更新时清空缓存。失败结果不缓存。
- `[media_randomizer]` 默认开启，仅处理 X 静态图片。六种策略的相对权重为重编码 30、加边框 20、裁边 15、扩展画布 15、边缘噪声 10、像素微调 10；每张图随机选一种可用策略，权重 0 表示禁用。
- 默认边框 1–3 px、单边裁剪 1–2 px、画布宽高扩展 1–6 px；噪声作用于外圈 2 px，修改像素数不超过总像素的 0.1%。这些几何策略允许少量尺寸变化，有损重编码可能产生轻微画质变化。
- JPEG、PNG、静态 WebP 按实际内容识别并保持编码格式，保留透明通道和无损 WebP 编码；GIF、动画 WebP、APNG 原样发送。预处理参考 `astrbot_plugin_link_resolver_x` 的行为，以独立模块接入 MaiBot。
- 原缓存保持只读；同一次发送的相同图片按内容哈希只处理一次，SDK/HTTP/逐条回退复用处理后的副本，新一次发送重新随机选择。处理失败使用原图。
- 副本存放在 MaiBot 为本插件分配的数据目录下 `temp/media_randomizer/`，默认发送结束即清理；启动和每 60 分钟清理超过 24 小时的残留，跳过活跃事务。配置字段及默认值见 `config.toml.example`。

## 迁移来源

本插件是基于原 MaiBot 移植版的改进版本，解析核心迁移自 AstrBot 插件：

- **解析核心来源**：`Zhalslar/astrbot_plugin_parser`
  - 原仓库：https://github.com/Zhalslar/astrbot_plugin_parser
  - 原许可：MIT License
- **原移植版来源**：`Color2333/maibot-multi-platform-parser`
  - 原仓库：https://github.com/Color2333/maibot-multi-platform-parser

本版本在原移植版基础上扩展了全部20个平台的解析支持，并添加了管理员命令、B站扫码登录、群聊@响应、引用消息解析等功能。

## 功能

支持解析以下平台的链接：

- 哔哩哔哩（视频、动态、专栏、直播）
- 抖音（短链、视频、图文）
- 小红书（短链、图文、视频）
- 小黑盒（帖子、游戏分享）
- A站（视频）
- Instagram（帖子）
- iwara（视频）
- 快手（视频）
- 网易云音乐（歌曲、歌单）
- NGA（帖子）
- 微信视频号
- TikTok（视频）
- Twitter/X（推文）
- 微博（微博、视频）
- YouTube（视频）
- 知乎（回答、文章）
- Pixiv（插画、漫画、小说、动图，含R18/R18G内容，支持图片混淆加密）
- CPP 无差别同人站（展品图集、后花园图文）
- QQ空间（公开分享图文/视频，通过 SnowLuma 获取登录态，默认关闭）
- Metube（通过你自建的 Metube 服务解析下载，默认关闭）

### 群聊@响应与引用消息解析

- **群聊仅被@才响应**：群聊消息只有 @ 机器人时才会触发解析，私聊不受影响。由 `require_at_in_group` 控制，默认开启。
- **引用消息解析**：开启 `enable_reply_parse` 后，可以引用（回复）一条含链接的消息并 @ 机器人，插件会通过 OneBot `get_msg` 接口拉取被引用消息原文并解析其中的链接。默认关闭。

### Pixiv 图片混淆功能

- 开启后，仅对 R18/R18G 作品的图片进行像素混淆加密处理，全年龄作品不受影响。用于降低发送时被系统风控拦截或封禁的风险（如果被举报可能会导致封号）。
- 群聊默认开启，私聊默认关闭，可分别通过 `pixiv_encrypt_image_group` 和 `pixiv_encrypt_image_private` 配置。

> ⚠️ **风险警告**：QQ 群聊环境下，发送 R18/R18G 图片极易触发系统风控（包括 AI 自动举报机制），**即使未被人工举报也可能导致群聊被封禁、机器人账号被处罚**。群聊场景下**必须保持混淆开启**，切勿抱有"不开也没事"的侥幸心理。私聊环境风险相对较低，但同样存在被举报后封禁的可能，请自行评估。

> 以上为基于实际使用经验的提示：作者曾因忘记开启群聊混淆，导致群聊被封禁 7 天、个人账号被封禁 1 天。

**注意**：混淆后的图片无法直接查看原始内容，需要使用解密工具还原：

- 🔗 **解密工具**：[点击跳转解密页面](https://nj-1307802825.cos-website.ap-nanjing.myqcloud.com/hunxiao//)

> 将加密图片上传到解密工具即可还原为原始图片。

其他特性：
- 文本/图片/合并转发优先通过 MaiBot SDK（`ctx.send.*`）发送，享受 SDK 的权限校验与频率控制
- SDK 调用失败时自动回退到 OneBot HTTP API，确保消息发送可靠性
- 视频和文件因 SDK 暂不支持，通过 OneBot HTTP API 直接发送（需配置 `[api]` 中的 host/port/token）
- 群白名单、去重冷却、媒体大小限制
- 支持代理配置
- 支持 Cookie 配置以解锁更多内容

### 消息发送机制与回退策略

插件采用「SDK 优先，HTTP 兜底」的双重保险策略：

| 消息类型 | 主通道（SDK） | 回退通道（OneBot HTTP） | 说明 |
|---------|-------------|----------------------|------|
| 文本消息 | `ctx.send.text()` | `send_group_msg`/`send_private_msg` | SDK 异常或返回 False 时自动回退 |
| 图片消息 | `ctx.send.image()` | `send_group_msg`/`send_private_msg` | SDK 异常或返回 False 时自动回退 |
| 合并转发 | `ctx.send.forward()` | `send_group_forward_msg`/`send_private_forward_msg` | SDK 异常或返回 False 时自动回退 |
| 视频消息 | —（SDK 不支持） | `send_group_msg`/`send_private_msg` | 仅 OneBot HTTP |
| 文件上传 | —（SDK 不支持） | `upload_group_file`/`upload_private_file` | 仅 OneBot HTTP |

> **注意**：SDK 发送失败时会在日志中输出 `SDK send.* 异常/返回 False，回退到 OneBot HTTP` 警告，便于排查问题。命令处理器（开启/关闭解析、B站登录）仅使用 SDK，失败时记录日志但不回退（命令场景无 message context 可用于 HTTP 回退）。

X 视频作为合并转发节点时可经 SDK 发送；上表的“视频消息”指独立视频消息，仍使用 OneBot HTTP。

> **关于 manifest 能力声明**：插件在 `_manifest.json` 中声明了 `send.text`、`send.image`、`send.forward`、`llm.generate` 四个 SDK 能力，覆盖文本、图片、合并转发与 X 译文生成。视频消息和文件上传因 SDK 暂不支持对应接口，直接通过 OneBot HTTP API 发送，**不受 manifest 能力声明约束**。这意味着插件实际具备的视频/文件发送能力超出了 manifest 声明范围，这是当前 SDK 能力限制下的折中方案。

## 命令

| 命令 | 说明 | 权限 |
|------|------|------|
| `开启解析` | 开启当前会话的链接解析 | 管理员 |
| `关闭解析` | 关闭当前会话的链接解析 | 管理员 |
| `登录B站` / `blogin` / `登录b站` | 扫码登录B站获取Cookie | 管理员 |

## 配置

主要配置位于 `config.toml`（参考 `config.toml.example`）：

### 插件配置

```toml
[plugin]
enabled = true              # 是否启用插件
admin_qqs = []              # 管理员QQ号列表，只有管理员可以使用命令
```

### 解析器配置

```toml
[parser]
enable_bilibili = true      # 启用B站解析
enable_douyin = true        # 启用抖音解析
enable_xhs = true           # 启用小红书解析
enable_xiaoheihe = true     # 启用小黑盒解析
enable_acfun = true         # 启用A站解析
enable_instagram = true     # 启用Instagram解析
enable_iwara = false        # 启用iwara解析（成人内容平台，默认关闭）
enable_kuaishou = true      # 启用快手解析
enable_ncm = true           # 启用网易云音乐解析
enable_nga = true           # 启用NGA解析
enable_shipinhao = true     # 启用微信视频号解析（需在[more]中开启 shipinhao_confirm_thirdparty 才会实际生效）
enable_tiktok = true        # 启用TikTok解析
enable_twitter = true       # 启用Twitter/X解析（需在[more]中开启 twitter_confirm_thirdparty 才会实际生效）
enable_weibo = true         # 启用微博解析
enable_youtube = true       # 启用YouTube解析
enable_zhihu = true         # 启用知乎解析
enable_pixiv = false        # 启用Pixiv解析（需配置Cookie，含R18内容，默认关闭）
enable_allcpp = true        # 启用CPP无差别同人站解析
enable_metube = false       # 启用Metube解析（需开启 metube_confirm，默认关闭）
enable_qzone = false        # 启用QQ空间解析（需开启 qzone_confirm_thirdparty，默认关闭）
group_whitelist = []        # 允许自动解析的群号，空列表表示所有群
block_ai_reply = true       # 命中链接后是否阻止麦麦继续普通聊天
debounce_seconds = 120      # 同一会话同一链接去重时间（秒）
max_images = 9              # 单条链接最多发送图片数
max_text_chars = 700        # 摘要正文最大字符数
send_images = true          # 是否发送图片
send_video = true           # 是否发送视频
use_forward_for_multi = true  # 群聊多图/图文是否使用合并转发
pixiv_use_forward = true      # Pixiv图片合并转发发送（关闭则合成为PDF）
source_max_size_mb = 80     # 单个媒体最大下载大小（MB），范围：1-300
source_max_minutes = 8      # 视频最大时长（分钟），范围：1-60
require_at_in_group = true  # 群聊必须@机器人才触发解析
enable_reply_parse = false  # 开启引用消息中的链接解析
metube_url = "http://127.0.0.1:8081"  # 自建 Metube 服务地址
```

### 更多设置

```toml
[more]
pixiv_encrypt_image_group = true      # 群聊Pixiv图片混淆（仅R18/R18G作品，默认开启）
pixiv_encrypt_image_private = false   # 私聊Pixiv图片混淆（仅R18/R18G作品，默认关闭）
twitter_confirm_thirdparty = false    # Twitter第三方服务确认（推文 ID 会发送到 FxTwitter，媒体兜底时链接会转发到 xdown.app，开启表示知悉并同意）
shipinhao_confirm_thirdparty = false  # 微信视频号第三方服务确认（分享链接会转发到腾讯元宝，开启表示知悉并同意）
qzone_confirm_thirdparty = false      # QQ空间SnowLuma确认（通过SnowLuma获取登录态凭证，开启表示知悉并同意）
metube_confirm = false                # Metube确认（链接提交到自建Metube服务，开启表示知悉并同意）
```

### 网络配置

```toml
[network]
proxy = ""                  # 解析/下载代理，例如 http://127.0.0.1:7890，留空则不使用
proxy_bilibili = false      # B站使用代理
proxy_douyin = false        # 抖音使用代理
proxy_xhs = false           # 小红书使用代理
proxy_xiaoheihe = false     # 小黑盒使用代理
proxy_acfun = false         # A站使用代理
proxy_instagram = true      # Instagram使用代理（默认开启）
proxy_iwara = true          # iwara使用代理（默认开启）
proxy_kuaishou = false      # 快手使用代理
proxy_ncm = false           # 网易云音乐使用代理
proxy_nga = false           # NGA使用代理
proxy_shipinhao = false     # 微信视频号使用代理
proxy_tiktok = true         # TikTok使用代理（默认开启）
proxy_twitter = true        # Twitter/X使用代理（默认开启）
proxy_weibo = false         # 微博使用代理
proxy_youtube = true        # YouTube使用代理（默认开启）
proxy_zhihu = false         # 知乎使用代理
proxy_pixiv = true          # Pixiv使用代理（默认开启）
proxy_allcpp = false        # CPP无差别使用代理
proxy_metube = false        # Metube使用代理（本地服务通常无需开启）
proxy_qzone = false         # QQ空间使用代理
common_timeout = 30         # 普通请求超时秒数
download_timeout = 120      # 下载超时秒数
download_retry_times = 1    # 下载重试次数
```

### Cookie 配置

```toml
[cookies]
bilibili = ""               # B站 Cookie（可选）
douyin = ""                 # 抖音 Cookie（可选）
xhs = ""                    # 小红书 Cookie（可选）
# ... 其他平台 Cookie
pixiv = ""                  # Pixiv Cookie（必填，否则无法解析）
allcpp = ""                 # CPP无差别 Cookie（可选，配置后可解析试阅图集）
qzone = ""                  # QQ空间 Cookie（SnowLuma不可用时回退，可选）
```

### API 配置

```toml
[api]
host = "127.0.0.1"          # OneBot HTTP API 主机
port = 3000                 # OneBot HTTP API 端口
token = ""                  # OneBot HTTP API Token
bot_uin = ""                # 发送合并转发节点时使用的 bot QQ
```

## 依赖

依赖已在 `_manifest.json` 中声明，MaiBot 会自动安装。主要依赖包括：

| 依赖 | 用途 |
|------|------|
| `aiofiles` | 异步文件读写 |
| `msgspec` | 高性能 JSON 解析 |
| `yt-dlp` | YouTube/TikTok 等视频下载 |
| `bilibili-api-python` | B站 API 调用 |
| `curl_cffi` | 小黑盒/iwara/知乎请求（绕过反爬） |
| `tqdm` | 下载进度条 |
| `beautifulsoup4` | 微博/NGA/Twitter/知乎 HTML 解析 |
| `apscheduler` | 缓存清理定时任务 |
| `apilmoji` | 卡片渲染中的 emoji 处理 |
| `pillow` | 图片处理/卡片渲染 |
| `httpx` | Pixiv API 请求 |
| `json5` | QQ空间 CGI JSON5 响应解析 |
| `gallery-dl` | Instagram 图文解析 |

## 外部工具与第三方服务

本插件在解析过程中会调用以下外部工具或第三方服务，请知悉：

### gallery-dl（Instagram 解析）

- Instagram 解析器通过 `subprocess` 调用 `gallery-dl`（`python -m gallery_dl`）获取图片链接。
- **风险边界**：URL 来自用户消息，经正则匹配后作为子进程参数传入。由于使用 `create_subprocess_exec`（非 shell 执行），且 URL 已经过正则白名单过滤，命令注入风险较低。但仍建议仅在可信会话中启用 Instagram 解析。
- gallery-dl 已在依赖中声明会自动安装，无需手动配置。

### ffmpeg（视频处理）

- B站、抖音等平台下载的视频需要通过 `ffmpeg` 合并/转码音视频流（`core/utils.py`）。
- ffmpeg 调用使用固定命令模板，参数均为本地文件路径，不接受用户输入，风险较低。
- **请确保系统已安装 ffmpeg 并可在 PATH 中找到**，否则视频解析功能将不可用。

### FxTwitter / xdown.app（Twitter/X 解析）

- Twitter/X 解析器（`core/parsers/twitter.py`）优先将推文 ID 发送至第三方服务 FxTwitter（`https://api.fxtwitter.com/2/status/{id}`），获取作者、正文及媒体。详情获取失败时，原链接才会发送至 xdown（`https://xdown.app/api/ajaxSearch`）获取媒体。
- 默认翻译会将原文发送给 MaiBot `utils` 任务配置的模型服务；可在 `[twitter_translation]` 中关闭或修改任务。
- 这意味着推文链接会被发送到第三方服务器，请评估是否可接受此外部数据流转。
- **二次确认机制**：即使 `enable_twitter = true`，还必须额外开启 `twitter_confirm_thirdparty = true` 才会实际解析 Twitter 链接。这是强制性的知情同意机制，确保用户明确知悉推文链接会被转发到第三方服务。关闭时会在日志中输出警告。

### 腾讯元宝（微信视频号解析）

- **重要提示**：微信视频号解析器（`core/parsers/shipinhao.py`）会将用户发送的视频号分享短链 POST 到腾讯元宝的解析接口（`https://yuanbao.tencent.com/api/weixin/get_parse_result`）换取可播放链接。
- 这意味着视频号分享链接会被发送到腾讯元宝服务器，请评估是否可接受此外部数据流转。该接口需要配置元宝登录 Cookie（在 `[cookies]` 中设置 `shipinhao`）。
- **二次确认机制**：即使 `enable_shipinhao = true`，还必须额外开启 `shipinhao_confirm_thirdparty = true` 才会实际解析视频号链接。这是强制性的知情同意机制，确保用户明确知悉分享链接会被转发到腾讯元宝。关闭时会在日志中输出警告。

### SnowLuma（QQ空间解析）

- **重要提示**：QQ空间解析器（`core/parsers/qzone.py`）默认通过 SnowLuma 的 OneBot HTTP `get_credentials` 动作动态获取 `qzone.qq.com` 登录态凭证。SnowLuma 不可用时会自动回退到手动配置的 QQ空间 Cookie。
- 本移植版默认复用 `[api]` 中的 `host`/`port`/`token` 作为 SnowLuma 的连接地址与访问令牌，请确保该地址指向可用的 SnowLuma 服务。插件只调用 SnowLuma 的公开 OneBot HTTP action，不读取其私有文件或进程内存。
- **二次确认机制**：即使 `enable_qzone = true`，还必须额外开启 `qzone_confirm_thirdparty = true` 才会实际解析QQ空间链接。关闭时会在日志中输出警告。

### Metube（自建下载服务解析）

- Metube 解析器（`core/parsers/metube.py`）会把待解析链接提交到你自建的 Metube 服务（地址由 `metube_url` 指定，默认 `http://127.0.0.1:8081`）进行下载与转码，再取回成品文件。
- Metube 是你自行部署的本地服务，不属于第三方云服务；但仍需开启确认开关。
- **二次确认机制**：即使 `enable_metube = true`，还必须额外开启 `metube_confirm = true` 才会实际解析。关闭时会在日志中输出警告。

## 数据存储与凭据安全

- **Cookie 明文存储**：各平台 Cookie 和 B站登录凭据（含 `SESSDATA` 等敏感字段）会以明文 JSON 形式写入插件数据目录 `data/cookies/`（如 `data/cookies/bilibili_credential.json`）。
- **权限隔离建议**：请确保运行 MaiBot 的服务器/容器对该数据目录设置了合理的文件系统权限，避免其他用户或进程读取。生产环境建议以独立低权限用户运行，并对 `data/cookies/` 目录设置 `600`/`700` 权限。
- 这些凭据是解析器类插件功能所需（用于访问需要登录的平台内容），无法避免落盘，请妥善管理。

## ⚠️ 免责声明
- 本插件仅供技术学习与交流，不鼓励用于任何违规用途。
- 本插件支持的 iwara、Pixiv 等平台可能包含成人/R18内容。发送此类内容可能违反 QQ/Tencent 服务条款，**存在导致机器人账号被封禁的风险**。使用者需自行承担一切风险和后果，插件开发者不对因使用本插件而造成的任何损失负责。请谨慎使用，并在合规范围内使用本插件。

## 许可与署名

本移植版遵循 MIT License。第三方来源与许可见 `THIRD_PARTY_NOTICES.md`。

## 致谢

- 感谢 `Zhalslar` 提供的解析核心（`astrbot_plugin_parser`）
- 感谢 `Color2333` 提供的 MaiBot 移植版基础框架
- 感谢所有为项目做出贡献的开发者！
