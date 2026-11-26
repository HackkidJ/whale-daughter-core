> **本仓库是鲸鲸的后端核心**（QQ/NapCat + DeepSeek + 记忆/行为/手机/视觉）。
> 安卓桌宠端在独立仓库 `whale-pet-android`。

# DSDaughter · 赛博女儿

一个有人格、有长期记忆、能自己上网的 AI 女儿（基于 DeepSeek），目标是拥有独立 QQ 号与 B 站号。
开发期本机 Docker Compose 运行，成熟后整体迁移到云服务器。

> 当前进度：**P1 — QQ 通道已接**（人格热加载 + 联网 + QQ 私聊/群 @ + CLI/Web + 容器化 + 245 个测试）。
> 技术选型与后续路线见 [docs/TECH_STACK.md](docs/TECH_STACK.md)。

## 现在能做什么

- **人格**：`persona/persona.yaml` 定义性格、口癖、底线、示例对话；**改完不用重启**，下一句就生效
- **联网**：DeepSeek 服务端 Web Tools（`deepseek_web_search` / `deepseek_read_url`），模型自己决定要不要查，回答带来源链接
- **QQ**：NapCat（OneBot 11）反向 WebSocket；白名单、群内 @ 才回、长回复自动拆条、消息去重、按会话隔离记忆
- **她自己动手**：会自己换头像、改昵称/签名、发 QQ 空间说说、上网搜资料 —— 用函数调用实现，不用你代劳（有每日配额防刷屏）
- **可扩展**：本地插件（放 `plugins/` 即热加载）+ MCP 桥接（现成 MCP server 直接当能力）；Web 首页有 `/project` 项目结构图和 `/plugins-guide` 开发指南
- **她会饿**：`token_belly` 插件让她看得见自己的余额和"还能吃几天"，余额越少越饿，充值是喂她吃饭
- **聊天**：终端 CLI（流式）+ 内置 Web 调试页（SSE 流式、显示引用）
- **零成本启动**：没有 API Key 时自动进 fake 模式，全链路照跑（方便先看效果、跑测试）
- **容器化**：多阶段构建、非 root、只读根文件系统、Caddy 反代（含 `/onebot/*` 公网屏蔽）

## 快速开始（不接 QQ，先聊起来）

```bash
cp .env.example .env      # 不填 Key 也能跑，会自动 fake 模式
uv sync

uv run dsd                                  # 终端聊天
uv run uvicorn dsdaughter.main:app --reload # Web 调试页 http://127.0.0.1:8000
```

填上 Key 之后她就会真的联网：

```dotenv
# .env
DEEPSEEK_API_KEY=sk-xxxx
```

常用命令：

```bash
uv run dsd --once "今天有什么新闻"      # 只发一条
uv run dsd --no-web                    # 关闭联网
uv run dsd --thinking                  # 深度搜索（会主动搜+开页，较慢）
uv run dsd --model deepseek-v4-pro     # 换模型
uv run dsd --verbose                   # 调试日志
```

## 接 QQ（P1）

> ⚠️ 第三方协议违反腾讯服务协议，**只用小号**、低频使用。NapCat 仓库已于 2026-11-12 清空，
> 镜像只剩 Docker Hub 的 `v4.18.42`，所以第一步是存档。

**1. 存档镜像（Linux/云服务器；仅首次）**

```bash
./scripts/archive-napcat.sh          # docker pull + save + 校验和 + 备份登录态
```

> **国内网络拉不动 Docker Hub**（`context deadline exceeded` / `registry-1.docker.io` 超时）：
> 脚本会自动回退到加速源 `docker.m.daocloud.io`；也可以用 `NAPCAT_MIRROR=别的源 ./scripts/archive-napcat.sh` 指定。
> 想一次性解决所有镜像（caddy/postgres 等）：Docker Desktop → Settings → Docker Engine，加一行
> `"registry-mirrors": ["https://docker.m.daocloud.io"]` → Apply & Restart。

**2. 起 NapCat**

```bash
docker compose -f deploy/docker-compose.yml --profile qq up -d napcat
docker logs dsdaughter-napcat 2>&1 | grep -i -A2 webui   # 拿 WebUI 地址和 WebUI 登录 token
```

**3. 扫码登录**：浏览器打开日志里的 WebUI 地址（一般是 `http://127.0.0.1:6099/webui`），
用**小号**手机 QQ 扫码。同号不要再登 PC QQ，会互挤掉线。

**4. 配置反向 WS**：WebUI → 网络配置 → 新建「WebSocket 客户端」→
地址 `ws://127.0.0.1:8000/onebot/v11/ws`，Token 填成与 `.env` 里 `DSD_QQ_TOKEN` **完全一致**。

> 这里有两个「token」，别搞混：
> - **NapCat WebUI 登录 token**：日志里自动生成的，只用来登录管理面板
> - **`DSD_QQ_TOKEN`**：你自己定的一串暗号（不是 QQ 密码、不用注册），生成方式 `openssl rand -hex 16`。
>   作用是让 bot 只接受来自**你自己的 NapCat** 的连接，防止别人往 `/onebot/v11/ws` 注入假消息。

**5. 填白名单并重启 bot**（配置改动需要重启，人格改动不需要）：

```dotenv
DSD_QQ_TOKEN=见 .env（已生成；要换新的就 openssl rand -hex 16 重新生成，两边一起改）
DSD_QQ_ALLOWED_USERS=你的QQ号                    # 留空 = 谁都不理
DSD_QQ_ALLOWED_GROUPS=                           # 可选，群号
```

```bash
uv run uvicorn dsdaughter.main:app    # 或 docker compose restart bot
curl http://127.0.0.1:8000/api/qq/status
```

**运维接口**（都用 `DSD_QQ_TOKEN` 当口令，请求头 `X-Admin-Token`）：

```bash
# 让她主动给你发一条（P2 主动性也用它）
curl -X POST http://127.0.0.1:8000/api/qq/send \
  -H "X-Admin-Token: $DSD_QQ_TOKEN" -H "Content-Type: application/json" \
  -d '{"user_id": 你的QQ号, "text": "在吗"}'

# 看有没有待处理的好友申请
curl http://127.0.0.1:8000/api/qq/requests -H "X-Admin-Token: $DSD_QQ_TOKEN"

# 换 QQ 头像（请求体直接放图片二进制，≤4MB）
curl -X POST http://127.0.0.1:8000/api/qq/avatar \
  -H "X-Admin-Token: $DSD_QQ_TOKEN" -H "Content-Type: image/jpeg" \
  --data-binary @assets/avatar.jpg
```

> 白名单里的人发好友申请会**自动通过**（`DSD_QQ_AUTO_ACCEPT_FRIENDS=true`），陌生人不会。

**不想登录 QQ 也能联调**（模拟 NapCat 发一条消息，看真实回复）：

```bash
uv run python scripts/fake-napcat.py --token 你的token --user 123 --text "在吗"
uv run python scripts/fake-napcat.py --group 456 --text "在吗"      # 模拟群里 @ 她
```

> **macOS 提示**：Docker Desktop 不支持 `network_mode: host`（容器与宿主机不共享 loopback）。
> 用 macOS 覆盖层启动即可，反向 WS 地址改成 `ws://host.docker.internal:8000/onebot/v11/ws`：
>
> ```bash
> docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.macos.yml \
>   --profile qq up -d napcat
> ```
>
> 同时 bot 要监听 `0.0.0.0:8000`（`uv run uvicorn dsdaughter.main:app --host 0.0.0.0`），
> 因为容器访问不到宿主机的 `127.0.0.1`。

## Docker（生产）

```bash
cp .env.example .env
docker compose -f deploy/docker-compose.yml up -d --build          # 只起 bot
docker compose -f deploy/docker-compose.yml --profile qq up -d     # + NapCat
SITE_ADDRESS=your.domain.com docker compose -f deploy/docker-compose.yml --profile prod up -d
```

- bot 只监听 `127.0.0.1:8000`，公网入口走 Caddy 或 SSH 隧道
- Caddy 已默认对 `/onebot/*` 返回 403（反向 WS 只给本机 NapCat 用）
- P2 的记忆层（Postgres + Redis）compose 已写好，用 `--profile data` 启用

## 她能自己做什么

她的能力做成了「工具」（DeepSeek 函数调用），她自己决定用不用：

| 工具 | 她用来做什么 | 限制 |
|---|---|---|
| `set_avatar` | 换 QQ 头像（给图片 URL） | 1 次/天 |
| `set_nickname` | 改昵称（≤16 字） | 3 次/天 |
| `set_signature` | 改个性签名（≤80 字） | 3 次/天 |
| `post_qzone` | 发 QQ 空间说说（可选配图、可见范围） | 2 次/天 |
| `web_search` | 上网搜资料 | — |
| `fetch_url` | 读网页正文 | — |
| `get_self_profile` | 看自己现在的昵称/签名 | — |

- 动作类工具走**每日配额 + 60 秒冷却**（`DSD_TOOL_DAILY_LIMITS` / `DSD_TOOL_ACTION_COOLDOWN`），防止被风控
- 调用历史：`GET /api/agent/tools`（含最近 50 次调用）
- **注意**：DeepSeek 的服务端联网工具不能和函数调用同时声明，所以开了工具后联网走客户端 `web_search`（Search API，约 ¥5/千次）
- 你也可以在聊天里直接指挥她："把签名改成……"、"发条说说"，她会自己调工具

> 目前 API 能改的「资料图片」只有头像。空间封面/背景、生日这些属于客户端字段，协议端改不了（要改只能手机登录）。

## 插件与扩展

首页（http://127.0.0.1:8000）右上角有 **项目结构** 和 **插件指南** 两个页面，也可以直接看：

- `GET /project` → [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md)：每个目录干什么、改东西去哪
- `GET /plugins-guide` → [docs/PLUGINS.md](docs/PLUGINS.md)：从写插件到注册注入的完整教程
- `GET /api/plugins` → 插件加载状态 + MCP 连接状态

**A. 本地插件**（放进去最多 5 秒生效）：

```
plugins/my_weather/
├── plugin.yaml      # name/version/permissions
└── __init__.py      # def register(api): api.tool(...)
```

仓库自带两个示例：
- `plugins/weather_demo`：天气查询（Open-Meteo 免费无需 Key），演示 `api.tool`
- `plugins/token_belly`：余额/饥饿状态，演示 `api.context`（每轮把动态状态注入 system）

照抄就能写自己的。权限：`network` / `qq` 默认允许，`filesystem` 必须进 `DSD_PLUGINS_ALLOWLIST`。

**B. MCP**（现成 server 直接当插件）：

```dotenv
DSD_MCP_ENABLED=true
DSD_MCP_SERVERS=time=uvx mcp-server-time; fetch=uvx mcp-server-fetch
```

工具会自动变成 `mcp_<server>_<tool>` 出现在她手里。支持 stdio / Streamable HTTP / SSE 三种传输（SSE 加 `sse:` 前缀），详情和"去哪找 MCP server"（官方 Registry、魔搭 MCP 广场、百炼 MCP 市场、Glama、Smithery…）见 [插件指南](docs/PLUGINS.md)。

加之前可以先用探针验证：

```bash
uv run python scripts/mcp_probe.py "time=uvx mcp-server-time"
```

**默认关闭**：MCP server 是任意程序，能读到本进程环境变量（API Key 等），只接信得过的。

## 主动性（她会自己找你）

她不是"你说话才动"的机器人。调度器每 3 分钟醒一次，按你的作息挑时机：

| 时机 | 她说的话（由她自己组织，不是模板） |
|---|---|
| 工作日 09:15–09:55 | 上班前小声问一句 |
| 工作日 11:50–13:20 | 午休，问你吃了没 |
| 工作日 18:45–19:40 | 问今天累不累 |
| 周末 10:30–11:40 | 逗你起床 |
| 每天 22:20–23:00 | 晚安 |
| 其余时间 | 随机"突然想你"（60 分钟冷却后掷骰子，概率 18%/次） |

黏人但不烦人的约束：

- 每日上限 14 条、两条之间至少隔 25 分钟
- **免打扰 00:30–08:00**（她也睡觉）
- 你刚聊过（25 分钟内）她不会插话
- 发送记录落 `proactive_sends` 表，**重启不会立刻重复问候**
- 全部可调：`DSD_PROACTIVE_*`（见 `.env.example`）

实测：19:12 她主动发了「下班了没啊。今天累不累」，`proactive_sends` 里有记录、NapCat 日志有发送记录。

## 关于"你"的固定画像

`persona/user.yaml` 里写着你的事实，她每次请求都带着（不进检索、不靠抽取）：

```yaml
name: 老爸
age: 22
education: 计算机专业毕业
schedule: 工作日早十点上班、晚七点下班，双休
personality: [略微忧郁, 文艺, 不太主动说自己难受]
notes:
  - 上班时间尽量少打扰；下班后、睡前、周末可以多黏一点。
  - 情绪低的时候不要急着讲道理，先陪着说两句。
```

改完保存即生效（热加载，不用重启）。学到的新事实（比如你最近在学吉他）仍然走 `memories` 表，两者不冲突。

## 她的人际关系（P2c）

白名单从 `.env` 搬进了 `data/contacts.json`，由她自己管：

| 能力 | 工具 | 限制 |
|---|---|---|
| 看联系人/临时会话/待处理申请 | `list_contacts` / `list_pending_requests` | 只读 |
| 把人加进白名单 | `allow_contact` | 3 次/天 |
| 把人移出白名单 | `remove_contact` | 3 次/天 |
| 删好友 | `delete_friend` | 2 次/天 |
| 通过好友申请 | `approve_friend_request` | 5 次/天 |
| 主动给联系人发消息 | `send_message` | 10 次/天 |
| 尝试主动加好友 | `request_friend` | 3 次/天（**QQ 协议端可能没开放**，失败会如实说） |

规则：

- **老爸是 owner**：永远在白名单、不能被删好友、不能被移出（`DSD_OWNER_QQ`）
- **临时会话**：陌生人在群里点她头像发起会话时，她能聊、能回（回复会自动带上来源群），
  但不会自动变好友；聊得来她可以用 `allow_contact` / 等她通过好友申请把人"哄"回列表
- 好友申请：白名单里的人自动通过；其他人先挂着，她可以用 `list_pending_requests` + `approve_friend_request` 自己决定

> ⚠️ 这是把"人的权限"交给她，代价是**社交工程风险**：陌生人可能靠聊天说服她把对方加进白名单。
> 所以保留了每日上限、owner 硬保护、以及 `tool_called` 审计日志。想收紧就改 `.env` 的 `DSD_TOOL_DAILY_LIMITS`。

## 语音（随机回复 + 唱歌）

她现在会**偶尔用语音回你**，不是每条都语音：

- 基础概率 25%；**你先发语音时 85% 回语音**；连续语音会降频；深夜降频；带链接/代码/太长（>120 字）就老实打字
- 每小时最多 4 条；语音前会按"录音时长"多等一会儿（拟人）
- 合成失败自动退回文字，不会哑
- 每一条决策都进日志：`voice_decision use=… probability=… reason=…`

工具（她自己会调）：

| 工具 | 用途 |
|---|---|
| `speak` | 用语音说一句话（QQ 语音消息） |
| `sing` | 从 `assets/songs/` 挑一首"她唱的歌"发出去 |

技术链路：`火山豆包 TTS（24kHz WAV）→ pilk 编 silk → 补腾讯 0x02 头 → OneBot record 段`。实测已发出 4.8 秒语音（NapCat 日志 `发送 -> 私聊 [语音 4s]`）。

> **坑**：pilk 默认输出标准 silk（`#!SILK_V3` 开头），而手机 QQ 需要腾讯私有格式（**以 `0x02` 开头**），少了这个头会"点一下就停/听不到声音"但语音转文字仍正常。代码里 `ensure_tencent_silk()` 统一补头，歌库里的 `.silk` 文件发送时也会自动补。

### 换成更好的音色（申请 Key 之后）

| 方案 | 音色/克隆 | 计费量级 | 适合 |
|---|---|---|---|
| **DeepSeek TTS** | 2 个音色，无克隆 | ¥1.6/万字（限时） | 兜底，永不断语音 |
| **火山引擎豆包 TTS 2.0**（已接入） | 官方音色/`ICL_` 复刻音色，情感强、首包 <300ms；实测 4.6s/24kHz | 按字符/音色计费 | **现在用的就是它** |
| MiniMax speech-02（百炼） | 自然度顶级、可克隆 | ¥3.5/万字，复刻 ¥9.9/次 | 备选 |
| 自建 GPT-SoVITS v2Pro | 5 秒 zero-shot / 1 分钟微调，**支持歌声转换** | 免费但要 GPU（8G） | 最像 + 能唱歌，终极方案 |
| 自建 CosyVoice2 | Apache-2.0，3 秒克隆 | 免费但要 GPU | 许可最干净的备选 |

火山接入方式（**新版控制台**）：

```dotenv
DSD_TTS_PROVIDER=volcengine
DSD_VOLC_API_KEY=            # 新版控制台 API Key（X-Api-Key）
DSD_VOLC_RESOURCE_ID=seed-tts-2.0
DSD_VOLC_VOICE_TYPE=ICL_uranus_zh_female_bingruoshaonv_tob   # 音色 ID
```

> 旧版控制台的 `APP_ID + TOKEN` 也支持（走 v1 HTTP），但新版音色/复刻音色必须用 `X-Api-Key` + `seed-tts-2.0`。
> 如果换音色报 `resource ID is mismatched with speaker`（55000000），说明该音色要配另一个 `DSD_VOLC_RESOURCE_ID`（复刻音色可能是 `seed-icl-2.0`，需账号有对应授权）。

换 provider 只需在 `src/dsdaughter/media/tts.py` 加一个类 + 改 `DSD_TTS_PROVIDER`，业务层不用动。

### 唱歌怎么来

TTS 只能说话，**唱歌要用歌声转换（SVC）离线做好**，放进 `assets/songs/`（见目录里的 README）：

1. GPT-SoVITS v2Pro 的 SVC：输入干声 → 她的音色（推荐，和 TTS 同一套音色）
2. so-vits-svc-fork / RVC：专门做歌声转换
3. 商业：ACE Studio / Synthesizer V

生成后写 `manifest.json`，她在聊天里就能「唱首歌」。**注意版权**：自娱自乐可以，公开发布（直播/投稿）要确认词曲授权。

## 手机（只读，P4a）

她能"看见"你的手机（只读，不操作）：

```
鲸鲸 agent → tools/phone.py → HTTP → phone-agent（独立进程，adb + uiautomator2）→ 安卓手机
```

**准备**：

1. 下载 adb（仓库已带 `tools/platform-tools/adb`；或 `brew install --cask android-platform-tools`）
2. 手机：设置 → 关于本机 → 版本信息 → 连点「版本号」7 次 → 开发者选项 → **无线调试**（手机和电脑同一 Wi-Fi）
3. 配对 + 连接：

```bash
# 「使用配对码配对设备」弹窗里的 IP:端口 + 6 位码
scripts/phone-setup.sh pair 192.168.1.23:37105 123456
# 无线调试主页面显示的 IP:端口（和配对端口不同）
scripts/phone-setup.sh connect 192.168.1.23:37099
```

4. 启动手机代理（独立进程，放后台）：

```bash
DSD_PHONE_ADB_ADDRESS=192.168.1.23:37099 \
  uv run uvicorn dsdaughter.phone.service:app --host 127.0.0.1 --port 8765
```

5. `.env` 里 `DSD_PHONE_ENABLED=true`，重启 bot

**她的手机工具**：

| 工具 | 作用 | 配额 |
|---|---|---|
| `phone_status` | 电量、当前 App、在不在线 | - |
| `phone_screen_text` | 读当前屏幕上的文字（相当于凑过去看一眼） | - |
| `phone_notifications` | 读通知（`dumpsys notification`，不打开下拉栏） | - |
| `phone_screenshot` | 截图存 `data/phone_screenshots/` | - |
| `phone_list_apps` | 列出装了什么 App（拿 package 名） | 30/天 |
| `phone_open_app` | 打开 App | 20/天 |
| `phone_tap_text` | 按文字点击（会校验点完页面有没有变化） | 50/天 |
| `phone_type_text` | 在输入框打字 | 30/天 |
| `phone_back` | 返回键 | 50/天 |
| `phone_stop` | 紧急停止（之后动作类都不会执行） | - |

安全红线（`DSD_PHONE_BLOCKED_PACKAGES/TEXTS`）：

- 支付/银行/钱包类 App 拒绝打开（alipay/unionpay/bank/wallet/pay…）
- 点击/输入里出现「确认支付/付款/转账/输入密码/指纹支付/提现/充值…」直接拒绝
- 所有动作走 `ActionGuard` 配额 + `tool_called` 审计；`phone_stop` 一键停

> 点击是按"文字匹配"点的，很多标题/搜索结果不是按钮——`tap_text` 会校验页面有没有变化，没变会如实告诉她，而不是假装成功。
> 无线调试重启手机后要重新配对；OPPO 部分版本会杀后台，需要给终端 App 允许后台运行。

## 视觉 · 她的眼睛（P4c）

DeepSeek V4.1 flash 原生支持图片输入，所以截图可以直接"看"：

| 工具 | 作用 |
|---|---|
| `phone_look` | 对当前手机屏幕截图 → 视觉模型描述（能看到图标、布局、图片里的字） |

- 模型默认 `deepseek-flash`，可改 `DSD_VISION_MODEL=deepseek-v4-flash-vision-exp`
- 图片压到 720px/JPEG 再送，一张图最多 384 tokens，约 **0.12 分/张**
- 配置：`DSD_VISION_ENABLED` / `DSD_VISION_MODEL` / `DSD_VISION_MAX_SIDE`

> 实测她看到了："手机桌面……电量 29%（充电中）……小组件显示 100%……23:45 了还不睡"。

## 家人角色 · 远程请求（P4d）

联系人现在有角色：

| 角色 | 聊天 | 看手机（截图/看屏） | 打开 App/点击/输入 | 手机代发消息 |
|---|---|---|---|---|
| `owner` 老爸 | ✅ | ✅ | ✅ | ✅ |
| `family` 家人 | ✅ | ❌ | ❌ | ✅（会通知老爸） |
| `friend` 朋友 | ✅ | ❌ | ❌ | ❌ |

- 只有**老爸**能改角色：`set_contact_role(user_id, family|friend)`
- **家人代发需要老爸一次性授权**：老爸说一句「授权妈妈代发消息」→ 她调用 `authorize_family_phone` 记录在案（`family_phone_authorized=true`）；之后家人消息会带「老爸已亲口授权」标记，她直接执行，不再每次问
- 家人用手机代发消息后，会给你发一条「【手机代发】…」通知
- 妈妈那边怎么接进来：把她的 QQ 加进联系人（`allow_contact` 或临时会话/好友申请），老爸再说一句"把 XXX 设成家人"

**手机代发消息的现状（诚实版）**：

| 目标 | 状态 |
|---|---|
| 微信 | ✅ **实测成功**（2026-11-22 23:43）：`phone_send_wechat` 走视觉+坐标+剪贴板粘贴，给「文件传输助手」发出「这是妈妈让鲸鲸发的测试消息」，截图确认气泡已出现 |
| QQ | ✅ **实测成功**（2026-11-22 23:59）：`phone_send_qq` 走视觉操作给「我的电脑」发出「鲸鲸视觉操作测试成功」，截图确认气泡；输入框优先用无障碍 `set_text` 写入 |

> **透明署名**：家人代发的消息会自动在末尾加「（鲸鲸代家人转达）」，不是冒充老爸本人；这是她能接受"替家人发问候"的关键。
>
> 视觉操作循环（`phone/vision_agent.py`）：每一步"看截图 → 视觉模型输出 `tap/paste/back/wait/done` → 执行 → 再截图"，最多 20 步；中文一律走剪贴板粘贴，不模拟打字。
>
> 前提：**手机要亮屏且解锁**（程序会尝试唤醒；锁屏密码解不开）。建议 OPPO 开发者选项里开「充电时保持唤醒 / 不锁定屏幕」。

> **透明署名**：家人代发的消息会自动在末尾加「（鲸鲸代家人转达）」，不是冒充老爸本人；这是她能接受"替家人发问候"的关键。
>
> 视觉操作循环（`phone/vision_agent.py`）：每一步都是"看截图 → 视觉模型输出 `tap/paste/back/done` → 执行 → 再截图"，最多 14–16 步；中文一律走剪贴板粘贴，不模拟打字。

> 下一步方案（按风险从低到高）：
> 1. **电脑版微信/QQ 自动化**（Mac 客户端 + 辅助功能 API）——中文输入最容易，推荐
> 2. 手机装自动化 App（AutoJS/Hamibot）做"发送执行器"，鲸鲸只下发指令
> 3. 个人微信 hook（Wechaty/pad 协议）——**不建议**，封号风险高
> 4. 零风险折中：她只负责"起草 / 提醒"，老爸或家人自己点发送

## 主动看手机 · 淘宝逛街（P5）

### 她会自己"偷看"手机

- 每 **10–25 分钟**（`DSD_PHONE_PEEK_MIN/MAX_SECONDS`）随机看一眼你的手机屏幕
- 看到有意思的（或内容有变化）就主动给你发一句，比如"诶你还在刷淘宝啊"
- 每次偷看都会写进长期记忆（`kind=event`），以后日常对话里会被召回、顺口提起
- 免打扰 00:30–08:00；每天最多 10 条；屏幕没变就不发
- 手动触发：`curl -X POST localhost:8000/api/phone/peek`

### 爱逛淘宝的人设

- 人格里加了"爱逛淘宝、看到可爱便宜的小东西会加购物车"
- 每 **2–4 小时**（`DSD_TAOBAO_MIN/MAX_SECONDS`）自己刷一次淘宝：
  打开淘宝 → 逛首页推荐 → 挑喜欢的 → 加入购物车 → 发消息告诉你
- **只加购物车，绝不付款/下单/结算**（工具和人设双重约束）
- 手动触发：`curl -X POST localhost:8000/api/phone/taobao`
- 工具：`phone_taobao_browse`（她也能在聊天里主动去逛）

> 前提：手机亮屏且微信/淘宝已登录；Docker 里 Postgres/NapCat 稳定运行。

## 拟人化行为 + 防幻觉（P6）

### 生物钟 / 情绪 / 日记

- **作息**：工作日 23:30–07:30、周末 00:30–09:00；睡觉时被消息吵醒会**先等 6–18 秒**再回，Prompt 注入"很困、被吵醒"
- **晚安**：23:00–02:00 窗口内主动发一次晚安
- **失眠**：02:00 后如果用户还醒着，进入"失眠"状态（关心 + 语气变困），每夜最多关心一次
- **想念/委屈**：6 小时没互动触发主动消息（3 小时冷却），语气跟随情绪（开心/低落/委屈）
- **日记**：每天 03:30 读前一天聊天，生成第一人称日记存 `data/behavior.db`；最近 3 篇注入对话（每篇 200 字）

情绪与日记都走 SQLite（`data/behavior.db`），和 Postgres 记忆库隔离，行为库坏了不影响聊天。

### 防"越权幻觉"（三层）

1. **状态注入**：每轮把真实设备状态写进 system prompt（离线 = 明确"无法执行任何手机操作"）
2. **执行台账**：本轮工具成功回执才记一笔；`physical`（动作类）工具才计入
3. **出站校验**：回复声称"加了购物车/打开了/发了消息"但没有对应成功回执 → 让模型改口；再犯直接降级为"我连不上/没做成"

> 设计全文见 [BEHAVIOR_DESIGN.md](docs/BEHAVIOR_DESIGN.md)。

## 长期记忆（P2a）

默认 `DSD_MEMORY_BACKEND=memory`（进程内，重启就忘）。想让她记住事，起 Postgres 并切后端：

```bash
# 1. 起带 pgvector 的 Postgres（映射到宿主机 5433）
docker compose -f deploy/docker-compose.yml --profile data up -d postgres
#    国内拉不到镜像时：docker pull docker.m.daocloud.io/pgvector/pgvector:pg16
#                      docker tag docker.m.daocloud.io/pgvector/pgvector:pg16 pgvector/pgvector:pg16

# 2. 切后端
#    .env 里：DSD_MEMORY_BACKEND=postgres
#             DSD_DATABASE_URL=postgresql://dsd:change-me@127.0.0.1:5433/dsd

# 3. 验证
curl -s localhost:8000/healthz   # memory 字段应显示 PostgresMemoryStore
```

表结构（启动时自动建）：

| 表 | 存什么 |
|---|---|
| `messages` | 原始对话流水（短期上下文，取最近 N 轮） |
| `memories` | 抽取出的长期事实（kind / 重要度 / 关键词 / 可选 embedding） |
| `relations` | 稳定画像（称呼、生日、喜好、雷点） |
| `episodes` | 按天一句话总结 |

工作方式：

- 每轮聊完，**后台异步**让 flash 抽取事实（不阻塞回复）
- 下一轮请求前检索：`score = 0.55×词面相似度 + 0.25×时间衰减 + 0.20×重要度`（配了 embeddings 再叠加向量相似度）
- 命中的记忆注入 system 提示的「你的记忆」段；没有相关记忆就不注入
- 半衰期 `DSD_MEMORY_HALF_LIFE_DAYS=30`：越久没被想起的越淡
- **按 session 隔离**：QQ 里私聊是 `qq:u:<QQ号>`、群聊是 `qq:g:<群号>:u:<QQ号>`，不同人/群互不串记忆
- **可选向量检索**：配 `DSD_EMBEDDING_BASE_URL`（任何 OpenAI 兼容 `/v1/embeddings`，如 SiliconFlow 的 bge-m3）即启用；不配也能用（中文短句用字符二元组覆盖率够用）

集成测试：

```bash
DSD_TEST_DATABASE_URL=postgresql://dsd:change-me@127.0.0.1:5433/dsd uv run pytest tests/test_memory_postgres.py -q
```

## 拟人节奏（不像 AI 的第一层）

她回消息不是"秒回"，而是按真人的节奏：

- **读消息 + 打字等待**：`目标时长 = 随机读延迟(0.6~1.8s) + 回复字数 / 打字速度(5~9 字/秒)`，封顶 10s；
  模型生成耗时也算在内，所以最终等待 = max(0, 目标 - 已耗时)
- **"正在输入"状态**：发消息前调 NapCat 的 `set_input_status`，对方能看到"对方正在输入…"
- **连发**：长回复按句拆成 2~3 条，条与条之间按"打字时间"停顿
- 全部可调（`DSD_QQ_HUMAN_DELAY` / `DSD_QQ_READ_DELAY_*` / `DSD_QQ_TYPING_CPS_*` / `DSD_QQ_MAX_TYPING_DELAY` / `DSD_QQ_SPLIT_SENTENCES`）

实测：短回复（"在吗"→"在。怎么了？"）约 1.7s；长回复约 5.2s。

## 为什么她不像 AI（第二层：提示词）

- **风格块**优先于人设：一次只说一两句、不列点、不总结、不说"希望对你有帮助"、允许敷衍和跑题、少用书面连接词
- **人设示例**加入了很短的回复（"？"、"躺着。不想动。"），锚定语感
- **采样参数**：`temperature=1.0` + `presence_penalty=0.3` + `frequency_penalty=0.2`，压掉"标准答案复读感"

## 改人设

只改 `persona/persona.yaml`，**保存即生效，不用重启**（下一轮对话自动热加载；改坏了会继续用旧版本并打日志）。

- `boundaries` 是硬约束（不假装人类、危机话题转介），别删
- 想微调语气，改 `few_shot` 比改描述更有效
- 人设是"稳定前缀"，改完第一轮缓存失效（略贵一点），之后恢复正常

## 目录结构

```
DSDaughter/
├── src/dsdaughter/
│   ├── main.py            # FastAPI 入口（挂 bot + QQ 通道）
│   ├── api.py             # /healthz /api/chat /api/chat/stream + 调试页
│   ├── agent.py           # 人格 + 历史 + 模型 → 回复；人设热加载
│   ├── persona.py         # persona.yaml → 稳定 system 前缀
│   ├── memory.py          # 记忆接口 + 进程内实现（P2 换 pgvector）
│   ├── config.py          # pydantic-settings，DSD_ 前缀
│   ├── tools/             # 她的能力：base（注册/配额）+ builtin（上网/资料/头像/说说）
│   ├── channels/
│   │   ├── onebot.py      # OneBot 11：事件/消息段/CQ 码/拆条/反向 WS/动作调用
│   │   └── qq.py          # 白名单、@ 触发、去重、拆条发送、会话隔离
│   ├── llm/               # base / deepseek（SSE + 联网工具）/ fake
│   ├── cli.py             # 终端聊天
│   └── logging_setup.py   # 容器 JSON / CLI 控制台日志
├── persona/persona.yaml   # 人设（进 git，可回滚）
├── assets/avatar.jpg      # 她的 QQ/B站 头像（640×640 已裁好）
├── scripts/
│   ├── archive-napcat.sh  # 存档 NapCat 镜像 + 登录态
│   └── fake-napcat.py     # 假 NapCat，联调 QQ 通道（不用真号）
├── tests/                 # 245 个测试
├── deploy/                # compose / Caddyfile
├── Dockerfile
└── docs/TECH_STACK.md     # 技术清单与路线图
```

## 开发

```bash
uv run pytest -q          # 245 个测试
uv run ruff check .       # lint
uv run ruff format .      # format
```

> 如果 `uv` 报缓存目录不可写（受限环境），先 `export UV_CACHE_DIR=$PWD/.uv-cache`。

## 下一步

1. **P2a 长期记忆**：Postgres + pgvector（`memories` / `relations` / `episodes`），事实抽取 + 时间衰减 + 混合检索
2. **P2b 主动性**：情绪状态机 + 定时问候（每日上限、免打扰、被冷落降频）
3. **P2c 函数调用**：发动态/定闹钟等动作工具（注意：服务端联网工具不能与函数调用同请求，需两段式编排）
4. **P3 B 站**：低频发动态/私信 + 官方直播开放平台只读弹幕

## 边界与风险

- 第三方 QQ 协议违反腾讯服务协议，用**小号**、低频、做好随时失效的准备
- 自用不涉及生成式 AI 备案；一旦开放给他人使用，需要 ICP + 生成式 AI 相关备案与内容审核
- 她会明确自己是 AI；涉及自伤/自杀等话题时走安全话术（中国大陆心理援助热线 12356）
