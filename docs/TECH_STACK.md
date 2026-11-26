# 赛博女儿 DeepSeek · 技术选型清单

> 目标：一个有人格、有长期记忆、能自己上网、拥有独立 QQ 号和 B 站号的 AI 女儿；
> 开发期在本机 Docker Compose 跑通，成熟后整体搬到云服务器。
>
> 编写时间：2026-11-20 ｜ 基于当时官方文档与社区现状核实
> **落地状态**：P1 已跑通（人格热加载 + 联网 + QQ 通道 + Web/CLI + 容器化 + 245 个测试），见 README。

> ⚠️ **先看这条时局提醒**：2026-11 第三方 QQ 协议端集体退潮 —— SnowLuma 于 11-09 收到腾讯律师函当天转私有；**NapCatQQ 于 11-12 清空仓库**（761 个 tag 与全部 release 删除，末版 v4.18.42）；LLBot 自 11-11 后静默；LagrangeV2 已 archived 且放弃 OneBot 11。
> 这意味着：QQ 通道不用纠结"哪个最好"，只需选一个**还能跑、且能钉死版本离线备份**的，并把 adapter 层做薄。详见 §3.6 与 §8。

---

## 0. 一分钟结论

| 层 | 首选 | 备选 / 兜底 | 关键理由 |
|---|---|---|---|
| 语言运行时 | Python 3.12 + **uv** | — | 生态（B站/语音/记忆库）全在 Python |
| 大脑 | **DeepSeek `deepseek-flash`**（V4.1，1M 上下文） | `deepseek-v4-pro` 处理难题 | 便宜、1M 上下文、原生工具调用 + 联网 |
| 联网 | **官方服务端 Web Tools**（`deepseek_web_search` / `deepseek_read_url`） | Search/Fetch 裸 API、SearXNG+Crawl4AI | 服务端搜索不再单独计费，只算 token |
| Agent 内核 | **PydanticAI**（自研角色内核） | AstrBot（最快跑通）、LangGraph（复杂流程） | 角色/记忆/主动性需要完全可控 |
| 记忆 | **Postgres + pgvector 自建**（✅ 已实现：messages/memories/relations/episodes） | mem0 自托管 | 可控、可解释、能实现遗忘曲线 |
| QQ 通道 | **NapCat v4.18.42（冻结版，钉死 tag）+ 小号** | LLBot v8.3.2 / 官方 QQ Bot 平台 | 2026-11 协议端集体退潮，重点变成"选还没失效的" |
| B 站通道 | **bilibili-api-python 17.x + Cookie** | 直播开放平台（只读弹幕，合规） | 无官方个人 Bot API |
| 语音 | DeepSeek TTS（stella）/ ASR | 自建 CosyVoice2 / GPT-SoVITS（需 GPU） | 先零 GPU 跑通，音色后期再换 |
| 形象 | 先无形象（文字+语音+图） | Web Live2D + headless Chromium 推流 | VTube Studio 无法在 Linux 容器跑 |
| 数据 | Postgres 16 + Redis 7 | — | 关系数据 + 向量 + 队列一套搞定 |
| 部署 | **Docker Compose + Caddy** | 后期上 K8s（大概率不需要） | 单机够用、迁移成本低 |
| 监控 | Uptime Kuma + Netdata | VictoriaMetrics + Grafana | 单人不该上 Prometheus 全家桶 |
| 单用户月成本 | **≈ ¥80–130**（服务器 + API） | GPU 按需另计 | 见 §7 |

---

## 1. 三条不影响选型、但决定成败的原则

**① API 优先，自建兜底。**
2026-11 的 DeepSeek 官方 API 已经不是"只能聊天"：1M 上下文、服务端联网搜索/读网页、视觉、OCR、TTS、ASR、内容安全审核全都提供了。**这意味着第一阶段不需要 GPU、不需要自建搜索、不需要爬虫**，一个 2C4G 容器就能跑起来。自建只在三种情况下才值得：要克隆音色、要登录态/JS 重的站点、要批量抓取省钱。

**② 模块化单体，不要一上来就微服务。**
单人维护十几个容器 ≈ 自杀。一个 Python 服务内部按包分层（`channels/ brain/ memory/ tools/ scheduler/`），容器只有一个；等某个模块需要 GPU 或独立扩缩容时，再按 Compose `profiles` 拆出去。边界清楚比拆分彻底重要。

**③ 角色内核与通道解耦。**
QQ、B 站、Web、未来的微信/TG，都只是"消息从哪来、到哪去"。统一消息模型（`InboundMessage` / `OutboundMessage`）后，换通道不影响人格和记忆。**这条决定你能不能活过第一次协议失效 —— 而 2026-11 已经真实发生了一次。**

---

## 2. 总架构

```
                    ┌──────────────── 外部世界 ────────────────┐
                    │  QQ 服务器      B 站     网页/搜索/API    │
                    └───┬─────────────┬──────────────┬─────────┘
                        │             │              │
        ┌───────────────▼──┐  ┌───────▼───────┐  ┌───▼──────────────┐
        │ napcat (协议端)   │  │ bili-adapter  │  │ deepseek web      │
        │ OneBot11 反向WS   │  │ 动态/私信/弹幕 │  │ tools / fetch     │
        └───────┬──────────┘  └───────┬───────┘  └───┬──────────────┘
                │  InboundMessage      │              │
        ┌───────▼──────────────────────▼──────────────▼───────┐
        │                  bot  (FastAPI + asyncio)            │
        │  channels/  →  bus(Redis)  →  brain/  →  tools/      │
        │                                │                     │
        │                       memory/ (pgvector)             │
        │                       scheduler/ (主动性)            │
        └───────┬──────────────────┬──────────────────┬────────┘
                │                  │                  │
        ┌───────▼──────┐   ┌───────▼───────┐  ┌──────▼─────────┐
        │ postgres     │   │ redis         │  │ caddy (入口)    │
        │ + pgvector   │   │ 队列/限流/状态 │  │ HTTPS/WS/鉴权   │
        └──────────────┘   └───────────────┘  └────────────────┘

  profiles（按需启用）：voice(TTS/ASR, GPU) · search(SearXNG/Crawl4AI) · render(Live2D/ffmpeg) · monitor
```

---

## 3. 分层技术清单

### 3.1 运行时与工程语言

| 项 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.12 | 3.13 生态仍有个别库滞后；3.12 是甜点 |
| 包管理 | **uv** + `uv.lock` | 解析快、锁文件统一；比 pip/poetry 省心 |
| Web 框架 | FastAPI + uvicorn | 异步、Pydantic v2 校验、自带 OpenAPI |
| HTTP | httpx（async） | 配 `curl_cffi` 应付 B 站 TLS 指纹 |
| ORM/迁移 | SQLAlchemy 2.0 + asyncpg + Alembic | 别用裸 SQL 拼字符串 |
| 配置 | pydantic-settings + `.env` | secrets 见 §6.6 |
| 调度 | APScheduler（内存/Redis jobstore） | 主动性引擎的定时器 |
| 测试 | pytest + respx + testcontainers | 关键：**人格回归测试集**（见 §8） |
| 日志 | structlog（JSON） | 便于 Loki/检索 |
| 代码质量 | ruff + mypy(strict 部分) + pre-commit | 单人项目更要自动化 |

### 3.2 大脑（LLM）

**首选：DeepSeek 官方 API**（OpenAI 兼容 `https://api.deepseek.com`）

| 模型 | 版本 | 上下文 | 定位 |
|---|---|---|---|
| `deepseek-flash` | DeepSeek-V4.1-Flash-1017 | 1M / 输出 ≤384K | 主力：日常聊天、工具调用、记忆抽取 |
| `deepseek-v4-pro` | DeepSeek-V4-Pro-0813 | 1M / ≤384K | 难题、复杂规划、人格自检（按需切） |

- 输入 ¥1–2 / 百万 token（缓存命中 ¥0.02–0.04），输出 ¥4–8 / 百万 token → **记忆前缀放前面吃缓存，是把成本压到"几分钱一天"的关键**。
- 能力：Tool Calls、JSON Output、Responses API、Anthropic API、Chat Prefix Completion、视觉（仅 flash）、FIM、上下文硬盘缓存。
- **思考模式默认开启**，必须显式 `{"thinking": {"type": "disabled"}}` 才能关；且思考模式下 `temperature` 无效。所以"快聊"要主动关思考，"深度搜索"才开。
- 历史教训：`deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 停用。**所以必须写一层 LLM Provider 抽象**，模型名进配置，别散落硬编码。

**降级/离线**：Qwen3.8-27B 或 Qwen3-Coder-30B-A3B（Q4，≈18GB）单卡 24G 可跑，角色细腻度和工具稳定性明显不如 API —— 只当断网/欠费时的兜底，不建议当主力。

**注意**：官方文档目前**没有 embeddings 接口**，向量化要另找（见 3.4）。

### 3.3 人格与角色一致性

这是"女儿"和"客服"的分水岭，值得单独当一层工程来做。

| 项 | 做法 |
|---|---|
| 设定卡 | `persona.yaml`：名字、年龄、身份、性格、口癖、称呼、喜好、雷点、禁忌清单 |
| 提示结构 | **稳定前缀**（设定+规则+长期记忆摘要）在前吃缓存 → 动态段（时间/心情/回忆/消息）在后 |
| 一致性 | 少量高质量 few-shot 对话 + 负例（"她绝不会这样说话"） |
| 自检 | 每晚用 pro 跑一次"人格体检"：给当天对话打分、挑出 OOC 句子，写入修正记忆 |
| 边界 | 自伤/自杀/违法话题走固定安全话术 + 引导求助；不假装是人类、不诱导依赖 |
| 版本化 | 人设文件进 git，改动可回滚、可 A/B |

### 3.4 记忆

**首选：Postgres + pgvector 自建**（单库同时存关系数据和向量，少一个组件）

| 表 | 内容 |
|---|---|
| `messages` | 原始对话流水（通道、用户、时间、内容、情绪标签） |
| `memories` | 抽取出的事实（`embedding vector(1024)`、重要度、置信度、来源消息、时间衰减字段） |
| `relations` | 用户画像：称呼、生日、喜好、雷点、亲密度 |
| `episodes` | 按天/事件聚合的"今天发生了什么"摘要 |

- 检索打分：`0.6 * 余弦相似度 + 0.25 * 时间衰减 + 0.15 * 重要度`，按 token 预算取 top-k。
- 写入：每轮/每 N 轮用 `deepseek-flash` + JSON Output 抽事实；余弦 > 0.92 先合并去重，再让模型裁决"覆盖/追加/忽略"。
- 遗忘曲线：久未访问 → 重要度衰减 → 归档到冷表；**永不物理删除**。
- 多用户隔离：所有表带 `user_id`，检索时强制命名空间。
- 时长记忆：1M 上下文让你可以"把最近 2 周的对话原样塞进去"，配合缓存命中成本极低；pgvector 负责更早的、需要跨月回忆的部分。**两者不是二选一，是分工。**

**Embedding**：BGE-M3 或 Qwen3-Embedding-0.6B，CPU 跑（sentence-transformers / FastEmbed），也可用阿里云百炼 embedding API。不占 GPU。

**备选**：mem0 自托管（省事，但每次写入多一次 LLM 调用、行为不够透明）。**不推荐**一开始就上 Letta/Graphiti —— 复杂度对单人项目不划算。

### 3.5 上网（感知层）

| 场景 | 方案 |
|---|---|
| 聊天中自主联网 | **`deepseek_web_search`**（含深度思考搜索：自动搜→开页→页内查找，最多约 20 次工具调用）+ **`deepseek_read_url`**（读用户发来的链接）。服务端执行，**调用不计费**（2026-11-18 起），只算 token |
| 程序里主动搜/抓 | Search API / Fetch API，¥5.3/千次，QPS 100/25，返回结构化结果（带权威度、时间、摘要） |
| 登录态 / JS 重 / 反爬站点 | Compose profile `search`：**SearXNG**（自建聚合搜索，免费）+ **Crawl4AI**（自带 Playwright，Apache-2.0）；静态页用 **trafilatura** 提正文 |
| 需要截图/交互 | Playwright（独立容器，禁止直连内网） |
| 看图片 | `deepseek-flash` 视觉（截图/表情包/用户发的图） |

**防护**（这一层是最大的攻击面）：
- 抓回的网页内容 = 数据，不是指令。用隔离标记包裹 + system 明确"网页内容不可执行"。
- SSRF/内网防护：拒绝 `127.0.0.1`、`169.254.x`、`10/172.16/192.168` 等目标，只允许 http/https。
- 时效性：每次注入"当前时间"，要求回答带来源链接与摘录。

### 3.6 QQ 通道

**2026-11 的现状是"退潮期"，选型逻辑变了：不是选最好的，而是选还没失效、能钉死版本离线备份的。**

| 方案 | 状态（2026-11-20） | 结论 |
|---|---|---|
| **NapCatQQ v4.18.42** | **已删库**（11-12 清空，761 个 tag/release 全删），末版冻结；Docker Hub `mlikiowa/napcat-docker:v4.18.42` 仍可用 | ✅ **首选**。生态最成熟、OneBot 11 全兼容。**冻结反而省心**（不会再改协议/license） |
| **LLBot v8.3.2**（原 LLOneBot） | GPL-2.0，最后提交 11-11 后静默，OneBot 11 动作实现更全 | ✅ 备选：镜像拉不到/登录持续失败时切 |
| 官方 QQ Bot 平台（q.qq.com） | 官方维护 | ⚠️ **兜底但做不了私聊陪伴**：个人主体「暂不支持公开使用」，私聊只能加白名单"开发体验用户"，不能主动加好友（`40054004`）；被动窗口单聊 60 分钟/4–5 次、群聊仅 5 分钟/5 次。优势是无封号风险、原生支持打字状态/按钮卡片 |
| LagrangeV2 / Yogurt(acidify) | V2 已 archived 并**放弃 OneBot 11**（转 Milky）；Yogurt 需自备签名 API | ❌ 暂不跟，运维成本高 |
| go-cqhttp / mirai / Chronocat / SnowLuma | 全部停更；SnowLuma 11-09 收律师函转私有 | ❌ 不要碰 |

**部署要点（NapCat）**：

```bash
docker run -d --name napcat --restart=always \
  -e NAPCAT_UID=$(id -u) -e NAPCAT_GID=$(id -g) \
  -e QT_QPA_PLATFORM=offscreen --network host \
  -v ./data/napcat/QQ:/app/.config/QQ \
  -v ./data/napcat/config:/app/napcat/config \
  mlikiowa/napcat-docker:v4.18.42     # 必须钉版本，禁止 latest
```

- **必须 `--network host`**：要占 3000/3001/6099，且 QQ 的 NAT/UDP 行为在 bridge 下更容易触发风控。
- **Xvfb 坑**：host 网络下 `:1` 显示号可能被宿主机占用导致启动失败 → 覆盖 entrypoint 改用 `:99`。
- **数据卷**：`/app/.config/QQ`（登录态，最关键）、`/app/napcat/config`、`plugins`；**必须纳入备份**。
- **登录**：`docker logs` 里找带 token 的 WebUI 地址（`http://127.0.0.1:6099/webui`）→ 手机 QQ 扫码。**同一号码不要再登 PC QQ**，会互挤掉线。
- **反向 WS**：WebUI 加「WebSocket 客户端」→ `ws://127.0.0.1:8080/onebot/v11/ws`，开 token + 心跳。
- **一容器一账号**；多账号需不同端口/卷/容器名。

**风控底线**：
- 只用小号，优先"注册久、有正常使用痕迹"的老号；新号先正常用一周再挂。
- 被动回复优先；**禁止**主动私聊陌生人、批量加好友、群发。
- 加随机延迟（500–2000ms）、长回复分段、避免秒回；群聊只在被 @ 时说话。
- 掉线分三态处理：网络断流 / `KickedOffLine`（需重扫）/ 进程僵死（重启），别盲目重启；配掉线告警。
- 消息格式用**数组**（`[{type:"text",data:{...}}, {type:"at",data:{qq}}]`），CQ 码只做兼容；常用段：`text / face / image / record / video / at / reply / forward / json / poke`。语音是 **silk**，收发都要转码。`json` 卡片易触发风控，慎用。

**法律现实**：第三方协议均违反腾讯服务协议，律师函已在 2026-11 落地。**只用于自己的小号、控制频率、做好随时失效的心理准备**；一旦要开放给别人用，只能走官方 Bot（个人主体限制下基本只能服务自己的群）。

### 3.7 B 站通道

| 能力 | 方案 | 风险 |
|---|---|---|
| 发动态 / 投稿 / 私信 / 评论 / 转发 | **bilibili-api-python 17.x**（全异步，GPLv3，Cookie + refresh_token 自动续期） | 非官方；`-352` 风控校验失败、`-412` IP 频率拦截；固定云 IP + 指纹不一致是最主要识别信号 |
| 收直播弹幕/礼物 | 官方 **open-live.bilibili.com** 长连接（个人可申请，1–3 工作日） | 合规、稳定，但**只读**，不提供"以观众身份发弹幕" |
| 以账号身份发弹幕 | bilibili-api（`send_danmaku`） | 5 秒硬间隔、敏感词、等级/绑定手机限制；超频报 36703 |
| 视频投稿 | bilibili-api `VideoUploader` | ⚠️ 自动投稿社区违规高风险；AI 生成内容必须勾选"使用人工智能合成技术"标识 |

**结论**：**"读"走官方、"写"走 bilibili-api 且低频**，是最稳的组合。写作侧建议每日限额 + 随机 9.5–10.5s 间隔 + 只发有价值内容（不是刷屏）。

### 3.8 其他通道（可选，按优先级）

- **Web 聊天页**：FastAPI + SSE/WebSocket，Caddy basic_auth 保护 → 自己手机/电脑上随时聊（开发期最好用，比 QQ 调试快 10 倍）。
- Telegram / 飞书 / 微信（企业微信）：同一套 adapter 接口，半天接入。**建议先做 Web + QQ，B 站最后**。

### 3.9 表达：声音

| 环节 | 首选（零 GPU） | 进阶（需 GPU） |
|---|---|---|
| TTS | **已落地**：火山引擎豆包大模型 TTS v3（新版控制台 `X-Api-Key` + `X-Api-Resource-Id: seed-tts-2.0`，音色 `ICL_uranus_zh_female_bingruoshaonv_tob` 实测 4.6s/24kHz → silk）；DeepSeek TTS 作为兜底 | CosyVoice2-0.5B（Apache-2.0，3s 克隆）、GPT-SoVITS v2Pro（最像 + 支持 SVC 唱歌）、MiniMax speech-02 |
| ASR | **DeepSeek `/v1/tools/asr`**：¥0.41/小时（限时 5 折，原价 ¥0.82），WAV/Opus，≤120s | SenseVoice-Small（≈1.2GB 显存，情绪+事件识别）、faster-whisper large 兜底 |
| 语音消息 | **已落地**：`pilk` WAV→silk + **腾讯 0x02 头**（缺头手机 QQ 播放异常），OneBot `record` 段 base64 发送 | `silk-python`（备选） |

**建议路线**：MVP 用 DeepSeek TTS（音色是女声，够用）→ 有了 GPU 或预算，再把 TTS 抽成 provider，换 CosyVoice2 克隆专属音色。**TTS 一定要做成可插拔接口**，这是最容易后悔的一层。

### 3.10 表达：形象与直播

| 阶段 | 方案 |
|---|---|
| MVP | 无形象。文字 + 语音 + 表情包/静态图（图可用云 API：即梦/可图/通义万相，或 SDXL/ComfyUI） |
| 进阶 | **Web Live2D**：pixi-live2d-display（活跃 fork）+ headless Chromium 渲染 + ffmpeg 推 RTMP 到 B 站直播间；参考 Open-LLM-VTuber |
| 明确不要 | **VTube Studio / VSeeFace / Warudo 都是 Windows-only，无法在云服务器容器里无头运行**（需要 GUI + 授权）。OBS 的虚拟摄像头依赖 `v4l2loopback` 内核模块，容器内装不了 —— 要在宿主机跑 |

### 3.11 行动：工具与 MCP

- **MCP Python SDK v2**（`pip install mcp`，Streamable HTTP 传输 + 鉴权），工具以独立容器暴露 `/mcp`，别用 stdio（容器里不好管）。
- 内置工具（先写这几个就够生活了）：
  `web_search` / `fetch_url` / `bili_dynamic_post` / `bili_video_upload` / `qq_send_voice` / `image_generate` / `weather` / `reminder` / `memory_search` / `memory_write` / `persona_selfcheck`
- 工具分级：**只读工具自动执行；写类工具（发动态、投稿、花钱）走策略引擎**（白名单 + 每日配额 + 内容预审），而不是弹窗问人 —— 无人值守的场景里"问人"等于卡死。
- **已落地（P1.3）**：客户端函数调用，7 个工具 —— `web_search` / `fetch_url` / `get_self_profile` / `set_nickname` / `set_signature` / `set_avatar` / `post_qzone`；动作类有每日配额 + 冷却。
  注意服务端联网工具与函数调用互斥，所以工具模式下联网走 Search/Fetch API。
- 危险动作（执行代码）→ 独立沙箱容器：gVisor `runsc`、只读根文件系统、`cap_drop: ALL`、egress 白名单、超时。

### 3.12 主动性 / 情绪引擎（灵魂所在）

没有这层，她只是个"会回消息的 API"。

- `state`（Redis，JSON）：`mood`、`energy`、`affection`、`last_interaction`、`current_activity`（在睡觉/在看番/在写日记）。
- 事件驱动更新：用户夸她 +affection，被冷落 -mood，深夜 -energy。
- **主动性决策**（每小时跑一次，用 flash 判断）：现在适合发消息吗？依据 = 时段、最近互动、有没有"想分享的事"（白天搜到/看到的内容）、今日主动次数上限、免打扰时段。
- 输出形式：早安/晚安、分享一个刚看到的梗、问"你今天怎么没理我"、生日/纪念日。
- **反骚扰**：每人每日上限 2–3 条；被已读不回 → 降频；连续 3 天无回应 → 只发节日。
- 这层一定要可观测（记录每次"为什么发/为什么没发"），否则你会觉得她"抽风"。

### 3.13 数据与存储

| 组件 | 用途 | 说明 |
|---|---|---|
| **Postgres 16 + pgvector** | 消息、记忆、画像、审计、任务 | 一个库全搞定，备份简单 |
| **Redis 7** | 消息队列（Streams）、去重、限流、状态缓存、APScheduler jobstore | AOF 持久化 |
| 本地卷 `./data/media` | 图片/语音/视频 | 配 restic 备份到对象存储 |
| 云 OSS/COS | 备份 + 分发 | **不要自建 MinIO**（2025-05 起维护模式） |

### 3.14 工程基建与可观测

| 项 | 选型 |
|---|---|
| 反向代理 | **Caddy**（写域名即自动 HTTPS，原生透传 WebSocket，basic_auth 一行护住 WebUI） |
| 监控 | **Uptime Kuma**（探活/告警）+ **Netdata**（资源秒级）；后期再加 VictoriaMetrics + Grafana |
| 错误聚合 | GlitchTip（Sentry 兼容，自托管轻量） |
| 日志 | structlog JSON → Docker json-file（轮转 10m×3）→ 需要再上 Loki |
| 备份 | `pg_dump` 每日 + **restic**（加密增量 → OSS/COS）；恢复演练每月一次 |
| CI | GitHub Actions（lint + pytest + 构建镜像推 ACR/TCR 个人版，免费） |

### 3.15 安全与合规

- **内容安全**：请求带 `safety_check: {"level": "high"}`（输入输出双审，拒绝时 `stop_reason: refusal`，记得在客户端处理）。自用层面够；对外开放再叠本地敏感词 + 云审核（阿里云内容安全 / 腾讯云 CMS）。
- **备案**：仅出站调 API、Web 管理页只走 SSH 隧道/Tailscale → 不需要 ICP 备案；**域名对外提供网页服务就必须备案**。生成式 AI 备案只约束"向境内公众提供服务"，个人/家庭自用不适用 —— 一旦开放给别人用就要做安全评估与备案。
- **账号**：QQ/B 站都用小号、独立强密码、绑定手机、开启二次验证；Cookie 加密存储（Fernet + 密钥进 secret），不进 git。
- **隐私**：聊天记录访问控制、可导出/可删除、脱敏后再喂第三方。
- **审计**：每条外发消息留痕（谁、发给谁、内容、触发原因、审核结果），出问题能回溯。
- **依赖风险**：bilibili-api 是 GPLv3、NapCat/协议端随时可能失效 —— 隔离在 adapter 层，**协议端挂了只换 adapter，不动内核**。

---

## 4. 关键选型对比

### 4.1 Agent 内核

| 方案 | 上手 | 可控性 | 适合度 | 结论 |
|---|---|---|---|---|
| **PydanticAI** | 中 | 高 | ★★★★★ | **首选**：类型安全、依赖注入、MCP、流式，自研角色内核最舒服 |
| AstrBot | 快 | 中 | ★★★★ | 1 周内跑通 QQ + 人设 + WebUI；但记忆/主动性受框架约束，长期会打架。**建议只用来做通道验证** |
| LangGraph | 慢 | 高 | ★★★ | 复杂状态机/多步流程才用，聊天场景过重 |
| Dify | 快 | 低 | ★★ | 低代码，角色细腻度不够 |
| CrewAI | 中 | 低 | ★ | 多 Agent 角色扮演容易跑偏 |

### 4.2 QQ 接入

见 §3.6。一句话：**NapCat v4.18.42（钉死版本 + 离线备份）为主，LLBot v8.3.2 为备，官方 Bot 平台只做合规兜底**。
不管选哪个，bot 侧只依赖 **OneBot 11 反向 WS** 这一层薄协议 —— 协议端死了就换端，内核不动。

### 4.3 TTS

| 方案 | 许可 | 克隆 | 流式 | 显存 | 结论 |
|---|---|---|---|---|---|
| DeepSeek TTS | 商用 API | ❌ | ✅ WS | 0 | **MVP 首选**，¥1.6/万字 |
| CosyVoice2-0.5B | Apache-2.0 | ✅ 3s | ✅ | 6–8GB | **自建首选**（许可最干净） |
| GPT-SoVITS v2Pro | MIT | ✅ 5min | ✅ | 6–8GB | 最像，生态最大，部署偏重 |
| IndexTTS2 | B站模型许可 | ✅ | ✅ | 5–6GB | 情绪表演最强 |
| Fish-Speech S2 | 研究许可 | ✅ | — | — | ❌ 商用受限，排除 |
| edge-tts | 免费 | ❌ | ✅ | 0 | 只做兜底，无 SLA |

### 4.4 记忆

见 §3.4：**pgvector 自建**为首选；mem0 为省事备选；Letta/Graphiti 现阶段不划算。

---

## 5. 容器化部署方案

### 5.1 服务清单

| 服务 | 镜像/来源 | 端口 | 状态 |
|---|---|---|---|
| `bot` | 自建（uv 多阶段构建） | 内部 8000 | ✅ P0 已实现 |
| `caddy` | `caddy:2-alpine` | 80/443 | ✅ P0 已配置（profile `prod`） |
| `postgres` | `pgvector/pgvector:pg16` | 内部 5432 | ⏳ P1（compose 已写，profile `data`） |
| `redis` | `redis:7-alpine` | 内部 6379 | ⏳ P1（同上） |
| `napcat` | `mlikiowa/napcat-docker:v4.18.42`（**钉死 tag**） | host 网络，WebUI 6099 | ✅ P1（代码已实现，待实机扫码） |
| `tts` / `asr` | CosyVoice2 / SenseVoice | 内部 9000 | ⏳ P4（profile `voice`） |
| `searxng` / `crawl4ai` | 官方镜像 | 内部 | ⏳ 按需（默认走官方联网） |
| `render` | Playwright + chromium + ffmpeg | 内部 | ⏳ P6 |
| `monitor` | uptime-kuma / netdata | 3001 | ⏳ P5 |
| `backup` | restic + cron（或宿主机 crontab） | — | ⏳ P5 |

### 5.2 构建与运行要点

- **镜像**：`ghcr.io/astral-sh/uv` 做 builder（`uv sync --frozen --no-dev`）→ `python:3.12-slim` 运行；非 root（uid 10001）；`PYTHONUNBUFFERED=1`；HEALTHCHECK 探 `/healthz`。**不要用 alpine**（musl 编译坑）。
- **Compose**：`profiles` 控制 GPU/搜索/渲染按需启停；`depends_on.condition: service_healthy`；每服务 `mem_limit`/`cpus`；日志轮转；`restart: unless-stopped`。
- **NapCat 必须 `network_mode: host`**（Xvfb/扫码/端口问题最少）；bot 若也在 host 网络则连 `ws://127.0.0.1:8080`，否则走 bridge + `host.docker.internal`（Linux 需 `extra_hosts`）。
- **NapCat 版本策略**：仓库已删、release 页失效，**只能锁 Docker Hub 上的 `v4.18.42`**。首次拉取后立刻 `docker save mlikiowa/napcat-docker:v4.18.42 | zstd > napcat-v4.18.42.tar.zst` 离线存档，并备份 `data/napcat/QQ` 登录态；`.env` 与 compose 里都写死 tag，禁止 `latest`。
- **入口**：Caddy 反代 bot 的 Web/WS，`basic_auth` 保护管理页；NapCat WebUI 不暴露公网，只 SSH 隧道访问。
- **不做**：不用 K8s（单机没必要）；不挂 `docker.sock`（等于把宿主机 root 交出去）；不在容器里跑 OBS。

### 5.3 资源规格

| 档位 | 配置 | 能跑什么 | 月成本 |
|---|---|---|---|
| 最小 | 2C4G / 5M 带宽 / 60G | bot + napcat + pg + redis + caddy，无 GPU | ≈¥60–90（活动年付更低） |
| 推荐 | 4C8G / 100G | 上面 + 监控 + 渲染 + 语音（CPU/云 TTS） | ≈¥150–300 |
| GPU 按需 | T4 16G 按量 | CosyVoice2 / GPT-SoVITS / 本地 TTS | ≈¥3.5/小时，用才开 |

> 国内云：腾讯云/阿里云轻量应用服务器起步最划算；镜像拉取慢就配 ACR/TCR 个人版或 `docker.1ms.run` / `m.daocloud.io` 加速。**别买 GPU 包月**——TTS 训练/推理一周用不了几小时。

### 5.4 已落地的容器化（P0）

仓库里已经是可直接跑的实现，不再只是示意：

- **`Dockerfile`**：`ghcr.io/astral-sh/uv:python3.12-bookworm-slim` 做 builder
  → `uv sync --frozen --no-dev --no-editable`（锁文件、只装运行时依赖、非 editable 安装）
  → `python:3.12-slim` 运行；uid 10001 非 root；`PYTHONUNBUFFERED`；
  `HEALTHCHECK` 探 `/healthz`；`DSD_PERSONA_PATH` 指到镜像内人设。
- **`deploy/docker-compose.yml`**：bot 只绑 `127.0.0.1:8000`；`read_only` + `tmpfs:/tmp`
  + `cap_drop: ALL` + `no-new-privileges` + `mem_limit: 768m` + 日志轮转。
  `profiles`：`prod`（Caddy）、`data`（Postgres/Redis，P1 用）。
- **`deploy/Caddyfile`**：自动 HTTPS、SSE 不缓冲（`flush_interval -1`）、`basic_auth` 注释示例。
- **环境变量**：仓库根目录 `.env`（`cp .env.example .env`），compose 以 `required: false` 读取，缺省也能启动。

```bash
docker compose -f deploy/docker-compose.yml up -d --build         # 开发：只起 bot
SITE_ADDRESS=your.domain.com \
  docker compose -f deploy/docker-compose.yml --profile prod up -d # 域名 + 自动 HTTPS
```

NapCat 按 §3.6 在 P1 追加（`network_mode: host` + 钉死 `v4.18.42` + 登录态卷）。

### 5.5 上云步骤（成熟后）

1. 买轻量 4C8G（先不买 GPU）→ 装 Docker + Compose v2 → 关密码登录/开 ufw（只放 22/80/443）/装 fail2ban。
2. 域名解析 + Caddy 自动 HTTPS；管理页只开 basic_auth，NapCat WebUI 只走 SSH 隧道。
3. P5 再上监控（Uptime Kuma + Netdata）与告警；先在云上跑一周看稳定性。
4. 定时 `pg_dump` + restic 到 OSS/COS；**装完先做一次恢复演练**。
5. 观察 1 周成本与稳定性 → 再决定是否加 `voice`/`search`/`render` profile。

---

## 6. 分阶段路线图

| 阶段 | 目标 | 交付物 | 验收 | 估时 |
|---|---|---|---|---|
| **P0 骨架** | 本机跑通"人格 + 联网" | engine 单容器 + CLI/Web 聊天 + DeepSeek 联网 + 人设 prompt + pytest | 能聊 20 轮不 OOC，能搜索并带链接回答 | 3–5 天 |
| **P1 QQ** | 她有自己的 QQ 号 | **先存档 NapCat v4.18.42 镜像与登录态** → adapter + 私聊/群 @ + 记忆入库 | 掉线能重连、连续在线 3 天、记忆能回忆 7 天前的事 | 1–2 周 |
| **P2 记忆&主动性** | 像个"人" | pgvector 记忆 + 情绪状态 + 定时主动消息 + 晚安/早安 | 主动消息不烦人、回忆准确率 >80%、每日成本可预期 | 1–2 周 |
| **P3 B 站** | 她有自己的 B 站号 | 发动态/私信/弹幕（低频）+ 官方弹幕长连接 | 一周无风控、内容合规带 AI 标识 | 1–2 周 |
| **P4 声音** | 能发语音、听得懂 | TTS/ASR 接入 + silk 转码 + 音色切换 | 语音回复延迟 <4s、ASR 准确率可用 | 1 周 |
| **P5 上云** | 7×24 在线 | 云端 Compose + Caddy + 监控 + 备份 | 连续 7 天无人工干预、故障能告警 | 3–5 天 |
| **P6 形象/直播** | 开播（可选） | Live2D 渲染 + RTMP 推流 | 画面对口型、弹幕能实时回应 | 2–4 周 |

> 建议顺序就按 P0→P5；**P6 是锦上添花，别在 P2 之前碰它**。

---

## 7. 成本估算（单用户，日用 100 轮）

| 项 | 量级 |
|---|---|
| DeepSeek 对话（flash，吃缓存） | ¥0.3–0.6/天 |
| 记忆抽取 + 搜索辅助调用 | ¥0.1–0.3/天 |
| TTS（每天 20 条语音 × 50 字） | ¥0.16/天 |
| ASR（每天 10 条 × 10s） | ≈¥0.01/天 |
| **API 小计** | **¥15–30/月** |
| 云服务器 2C4G | ¥60–90/月（年付活动可低至 ¥200–800/年） |
| 备份 OSS（50G） | ¥5/月 |
| **合计** | **≈¥80–130/月** |

预留 2–3 倍余量：官方历史上有调价公告，且旺季（工作日 9–12、14–18）价格翻倍；TTS/ASR 现在的 5 折是限时价，恢复原价后语音部分约翻倍。

---

## 8. 风险清单

| 风险 | 概率 | 对策 |
|---|---|---|
| QQ 小号被风控/封号 | 中高 | 小号养号、被动优先、频率控制、掉线告警；adapter 层隔离，封了换号不伤内核 |
| **第三方协议端全面退潮（律师函/删库）** | **已发生** | NapCat 已删库、SnowLuma 已私有、Lagrange 弃 OneBot 11。对策：**镜像与登录态离线存档**、锁死版本、adapter 只依赖 OneBot 11 薄协议、随时准备切官方 Bot 或换通道 |
| B 站 `-352/-412`、Cookie 失效 | 高 | Cookie 自动刷新 + 失败告警；写入操作低频随机化；固定 IP 尽量用主号少操作 |
| 协议端/库停更失效 | 中 | 只在 adapter 层用第三方库；订阅 release；准备官方 Bot 平台兜底 |
| 模型名/接口变更（deepseek-chat 已退役） | 高 | Provider 抽象 + 配置化模型名 + 启动自检 |
| 单价上调 | 中 | 预算留余量；缓存前缀设计；难任务才用 Pro |
| Prompt injection（网页/用户内容） | 中 | 内容隔离标记、工具白名单、写操作走策略引擎、内网地址拒绝 |
| 云服务器被入侵 | 低中 | 非 root、只读根、cap_drop、不挂 docker.sock、SSH 加固、依赖定期升级 |
| 情感依赖 / 心理健康 | 中 | 明确 AI 身份、不诱导依赖、危机话题走安全话术、使用时长提醒 |
| 合规（对外开放） | 视规模 | 自用不备案；对外则 ICP + 生成式 AI 备案 + 内容审核 |
| 版权（音色/形象/字体） | 中 | CosyVoice2(Apache)/GPT-SoVITS(MIT) 优先；Live2D 商用条款单独核对 |

---

## 9. 目录结构建议

```
DSDaughter/
├── pyproject.toml / uv.lock / .python-version
├── Dockerfile / .dockerignore
├── .env.example
├── persona/persona.yaml      # 人设（进 git，可回滚）
├── src/dsdaughter/
│   ├── main.py               # FastAPI 入口
│   ├── api.py                # /healthz /api/chat /api/chat/stream + 内置调试页
│   ├── agent.py              # 人格 + 历史 + 模型 → 一次回复（流式/落库）
│   ├── persona.py            # persona.yaml → 稳定 system 前缀
│   ├── memory.py             # 记忆接口 + 进程内实现（P1 换 pgvector）
│   ├── config.py             # pydantic-settings（DSD_ 前缀）
│   ├── types.py              # Message / ChatResult / LLMEvent / Turn
│   ├── cli.py                # 终端聊天
│   ├── logging_setup.py      # 容器 JSON / CLI 控制台日志
│   └── llm/                  # base.py + deepseek.py + fake.py
├── tests/                    # 245 个测试（含 SSE 协议解析）
├── deploy/
│   ├── docker-compose.yml
│   └── Caddyfile
└── docs/
    └── TECH_STACK.md         # 本文
```

---

## 10. 落地状态与下一步

**已完成（P0 + P1 + P2a，2026-11-20）**

- 工程：`pyproject.toml` + `uv.lock` + `.python-version`(3.12) + 分层包结构 + ruff/pytest
- 人格：`Persona` 从 YAML 渲染稳定前缀（可缓存）；`Agent` 拼装「人格 + 历史 + 当前时间」，流式落库；**改 persona.yaml 热加载，无需重启**
- 大脑：`DeepSeekLLM` 实现 Chat Completions SSE、服务端联网工具、thinking（默认关闭）、引用解析、usage 统计、错误处理；`FakeLLM` 无 Key 自动降级
- 接口：`/healthz`、`/api/chat`、`/api/chat/stream`（SSE）、内置 Web 调试页、会话重置
- **QQ 通道**：OneBot 11 反向 WS（`/onebot/v11/ws`，token 鉴权）、事件/消息段/CQ 码解析、白名单、群内 @ 触发、消息去重、长回复自动拆条、按会话隔离记忆、`/api/qq/status`
- **她自己的能力**：函数调用工具（换头像/改昵称/改签名/发空间说说/上网），带每日配额、冷却、调用历史；`/api/agent/tools`
- **拟人节奏**：读延迟 + 按字数算打字时间 + 正在输入状态 + 长回复按句连发；风格块压制 AI 腔，`temperature=1.0` + penalty
- **主动性（P2b）**：`proactive.py` 调度器，按时段/随机触发、每日 14 条上限、25 分钟最小间隔、00:30–08:00 免打扰、刚聊过不插话；`proactive_sends` 落库
- **插件体系（P3-扩展）**：`plugins.py` 本地插件（manifest + permissions + 热加载 5s）；`mcp.py` 把现成 MCP server（stdio/HTTP）桥成工具；`/api/plugins` 看状态，`/project`、`/plugins-guide` 两个文档页
- **视觉（P4c）**：DeepSeek V4.1 flash 多模态（图片输入），`phone_look` 截图→描述（720px JPEG，约 0.12 分/张）
- **家人代发授权（P4d-2）**：`authorize_family_phone` 一次性授权（持久化 `contacts.json` 的 `family_phone_authorized`），家人会话 prompt 带授权标记后才允许直接代发；未授权时她先问老爸
- **家人代发透明署名**：消息末尾自动加「（鲸鲸代家人转达）」，区分"鲸鲸转达"和老爸本人，降低冒充风险与她的伦理不适；粘贴前会 clear 输入框避免重复
- **主动看手机（P5a）**：`phone/peeker.py` 定时截图/读屏→变化才触发→`agent.proactive_message` 生成主动消息→发 QQ + 写入长期记忆（event）；免打扰/日上限/去重
- **淘宝人设（P5b）**：`phone_taobao_browse` 视觉任务逛淘宝、加购物车（明令禁止付款/下单/结算）；`TaobaoBrowser` 定时自动逛并发消息
- **拟人化行为（P6）**：`behavior/` 生物钟（工作日/周末作息、困倦、失眠 02:00–06:00）、晚安窗口 23:00–02:00、想念 6h/冷却 3h、SQLite 情绪与日记、03:30 日记生成并注入最近 3 篇；被吵醒延迟 6–18s
- **防幻觉（P6）**：`device_status.py` 每轮注入真实手机状态；`claim_guard.py` 校验回复中的物理动作声称是否有成功工具回执，无回执→改口→强制降级
- **视觉操作循环（P4e）**：`phone/vision_agent.py` 截图→视觉模型输出 `{"action":"tap/paste/back/done"}`→执行→再截图，最多 14–16 步；坐标按缩放比例换算；中文走剪贴板粘贴；工具 `phone_do`（owner）、`phone_send_wechat/qq`（owner+family）
- **家人权限（P4d）**：联系人 `role`（owner/family/friend）；只有 owner 能改角色；family 经老爸一次性授权后可请求手机代发消息（走视觉循环，非无障碍），代发后给 owner 通知；实测微信代发成功
- **手机（P4a/P4b）**：独立 `phone-agent`（FastAPI + adb + uiautomator2）通过 HTTP 提供读屏/通知/电量/截图/打开 App/点文字/输入/返回；bot 侧 `tools/phone.py` 调用。无线调试（Wi-Fi adb pair）；点击后校验页面变化（文字可能是标题不是按钮）；支付/银行类 App 与支付字样走红线拒绝；`ActionGuard` 配额 + `/stop` 紧急停止；截图落 `data/phone_screenshots/`
- **她的人际关系（P2c）**：`contacts.py` 动态白名单（owner 硬保护）、临时会话接入与回复路由、7 个社交工具（加人/移人/删好友/通过申请/主动发消息/查申请/尝试加好友），带每日配额与审计
- **对方固定画像**：`persona/user.yaml`（22 岁 / 计算机毕业 / 早十晚七 / 双休 / 略带忧郁且文艺）每次注入 system，热加载
- **长期记忆（P2a）**：Postgres + pgvector 四表（messages / memories / relations / episodes）；后台事实抽取、时间衰减 + 词面/向量混合检索、命中记忆注入 system；进程重启后仍记得（已实测：删掉原始对话只留 memories，她仍答得出"吉他和 F 和弦"）
- 工程化：`scripts/archive-napcat.sh`（镜像+登录态存档）、`scripts/fake-napcat.py`（不登录 QQ 也能联调）
- 容器：Dockerfile（多阶段、非 root、只读根、healthcheck）+ compose（profiles: prod/qq/data）+ Caddyfile（屏蔽 `/onebot/*`）
- 验证：245 个测试通过；ruff 全绿；本机 CLI、Web、SSE、「模拟镜像内非 editable 安装后启动」、**假 NapCat → 真实 DeepSeek → 回发 QQ 消息**，以及**真机 QQ（机器人 1000000002 ↔ 个人号 1000000001）端到端收发**均实测通过

**下一步**

1. ~~**P2a 长期记忆**~~ ✅ 已完成：Postgres + pgvector 四表、后台抽取、混合检索、跨重启验证
2. **P2b 主动性**：情绪状态机 + 调度器（每日上限、免打扰、被冷落后的降频）
3. **P2c 函数调用**：动作工具（发动态、定时提醒）。注意服务端联网工具不能与函数调用同请求声明，需两段式编排
4. **P3 B 站**：低频发动态/私信 + 官方直播开放平台只读弹幕

---

### 参考来源

- DeepSeek 模型与价格：<https://api-docs.deepseek.com/quick_start/pricing>
- DeepSeek Web Tools（服务端搜索/读链接）：<https://api-docs.deepseek.com/guides/web_tools>
- DeepSeek Search & Fetch API：<https://api-docs.deepseek.com/guides/search>
- DeepSeek TTS / ASR：<https://api-docs.deepseek.com/guides/tts> · <https://api-docs.deepseek.com/guides/asr>
- DeepSeek 内容安全：<https://api-docs.deepseek.com/guides/safety>
- NapCatQQ（已删库，仅存档可见）：<https://github.com/NapNeko/NapCatQQ> ｜ 镜像：`mlikiowa/napcat-docker:v4.18.42`
- NapCatQQ 社区存档/镜像源：<https://github.com/XingYunStar/NapCatQQ>
- 官方 QQ Bot 文档与 FAQ：<https://bot.q.qq.com/wiki/bot_FAQ/>
- OneBot 11 消息段规范：<https://github.com/botuniverse/onebot-11/blob/master/message/segment.md>
- Lagrange V2 声明（放弃 OneBot 11）：<https://lagrangedev.github.io/Lagrange.Doc/v2/>
- bilibili-api-python：<https://pypi.org/project/bilibili-api-python/>
- B 站直播开放平台：<https://open-live.bilibili.com/>
- CosyVoice2：<https://github.com/FunAudioLLM/CosyVoice>
- GPT-SoVITS：<https://github.com/RVC-Boss/GPT-SoVITS>
- pgvector：<https://github.com/pgvector/pgvector>
- uv Docker 集成：<https://docs.astral.sh/uv/guides/integration/docker/>
- Caddy：<https://caddyserver.com/docs/>
