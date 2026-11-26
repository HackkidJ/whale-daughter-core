# 项目结构说明

> 这个页面就是给"以后想改点什么"用的地图。改人设不用看代码；加能力看 `plugins/`；换部署看 `deploy/`。

## 一句话架构

```
QQ / Web / B站  ──►  channels（通道）  ──►  Agent（人格 + 记忆 + 工具）
                                              │
                         ┌────────────────────┼────────────────────┐
                     记忆（Postgres）      工具（内置/插件/MCP）   主动性调度
```

## 目录树

```
DSDaughter/
├── src/dsdaughter/            # 她的大脑：所有核心代码
│   ├── main.py                # FastAPI 入口：挂路由、启动主动性和 MCP、关服务时收尾
│   ├── api.py                 # HTTP 接口：/healthz /api/chat(SSE) /api/agent/tools /api/plugins
│   │                          #   + 内置聊天调试页 + 本文档页 /project、/plugins-guide
│   ├── agent.py               # 核心：拼 system 提示（人设+画像+风格+能力+记忆+时间）、工具循环、落库
│   ├── config.py              # 所有配置（DSD_ 前缀的环境变量），一处改全生效
│   ├── types.py               # 跨层数据结构：Message / ChatResult / LLMEvent / Turn / ToolCall
│   ├── persona.py             # 读 persona.yaml → 稳定的 system 前缀（吃缓存、热加载）
│   ├── user_profile.py        # 读 persona/user.yaml → "关于老爸"的固定画像（热加载）
│   ├── plugins.py             # A：本地插件系统（扫描 plugins/、权限校验、热加载）
│   ├── mcp.py                 # B：MCP 桥接（把现成 MCP server 变成她的工具）
│   ├── proactive.py           # 主动性调度：时段触发、配额、免打扰、发送记录
│   ├── contacts.py            # 她的人际关系：动态白名单（contacts.json）+ 临时会话记录
│   ├── logging_setup.py       # 日志：容器里 JSON、CLI 里可读文本
│   ├── cli.py                 # 终端聊天入口（uv run dsd）
│   ├── behavior/              # 拟人化：生物钟/情绪/日记/调度
│   ├── device_status.py       # 真实设备状态探测（Prompt 注入 + 防幻觉依据）
│   ├── claim_guard.py         # 出站回执校验（防"越权幻觉"）
│   ├── phone/                 # 手机设备代理：service（adb+uiautomator2）/ client
│   ├── media/                 # 语音：tts（可插拔）/ silk 编码 / 歌库 / 发语音的策略
│   ├── channels/              # 消息通道：平台事件 ↔ 统一格式
│   │   ├── onebot.py          #   OneBot 11：事件/消息段/CQ 码/拆条/反查 WS/动作调用
│   │   └── qq.py              #   QQ 通道：白名单、@ 触发、去重、拟人节奏、发送
│   ├── llm/                   # 模型接入层（换模型只改这里）
│   │   ├── base.py            #   接口协议 + LLMError
│   │   ├── deepseek.py        #   DeepSeek SSE 流式、联网工具、函数调用、引用解析
│   │   └── fake.py            #   假模型：无 Key 也能跑通全链路、跑测试
│   ├── memory/                # 长期记忆（P2a）
│   │   ├── base.py            #   记忆条目/检索结果的数据结构
│   │   ├── scoring.py         #   打分：词面相似度 + 时间衰减 + 重要度（+向量）
│   │   ├── inmemory.py        #   进程内实现（开发/测试）
│   │   ├── postgres.py        #   Postgres+pgvector 实现（正式）
│   │   ├── extract.py         #   每轮聊完，让 flash 抽取事实/画像/日报
│   │   ├── embedding.py       #   可选向量化（OpenAI 兼容 /v1/embeddings）
│   │   └── schema.sql         #   建表语句（启动时自动执行）
│   └── tools/                 # 她的"手脚"
│       ├── base.py            #   Tool/ToolRegistry/配额 ActionGuard/调用历史
│       ├── builtin.py         #   内置：上网、改资料、换头像、发说说
│       ├── contacts.py        #   社交工具：加人/移人/删好友/通过申请/主动发消息
│       ├── media.py           #   语音工具：speak（说一句）/ sing（唱一首）
│       └── phone.py           #   手机工具：看屏幕/通知/电量/截图（只读）
├── persona/                   # 她的人设（改这里最安全）
│   ├── persona.yaml           #   性格/口癖/底线/示例对话（热加载）
│   └── user.yaml              #   "关于老爸"的固定画像（热加载）
├── plugins/                   # A：下载来的插件放这里
│   ├── weather_demo/          #   天气插件示例（api.tool）
│   └── token_belly/           #   余额/饥饿插件示例（api.tool + api.context）
├── docs/                      # 技术文档 + 本站的两个说明页
│   ├── PROJECT_MAP.md         #   本页（/project）
│   ├── PLUGINS.md             #   插件开发与 MCP 指南（/plugins-guide）
│   └── TECH_STACK.md          #   完整技术选型清单
├── deploy/                    # 部署
│   ├── docker-compose.yml     #   bot / napcat / postgres / redis / caddy
│   ├── docker-compose.macos.yml #  macOS 覆盖层（Docker Desktop 不支持 host 网络）
│   └── Caddyfile              #   反向代理 + 自动 HTTPS + 屏蔽 /onebot
├── scripts/                   # 运维脚本
│   ├── archive-napcat.sh      #   存档 NapCat 镜像 + 登录态（仓库已删，必须早做）
│   └── fake-napcat.py         #   假 NapCat：不登录 QQ 也能联调通道/延迟
├── tests/                     # 245 个测试（含真实 Postgres 集成，按环境变量开关）
├── data/                      # 运行状态（不入库）：contacts.json 等
├── assets/songs/              # 她唱的歌（离线 SVC 生成，silk/wav + manifest.json）
├── assets/                    # 她的头像等静态素材
├── Dockerfile                 # 多阶段构建：uv 装依赖、非 root、只读根、healthcheck
└── README.md                  # 快速开始 / 功能说明 / 配置表
```

## 一次对话发生了什么

1. QQ 收到消息 → `channels/onebot.py` 解析成 `MessageEvent`
2. `channels/qq.py` 白名单/去重/@ 判断 → 调 `Agent`
3. `Agent` 取最近对话 + 检索长期记忆（`memory/`）→ 拼 system（人设/画像/风格/能力/记忆/时间）
4. 调 `llm/deepseek.py` 流式回答；需要动手时走工具循环（`tools/` + 插件 + MCP）
5. 回复落库（messages），后台异步抽取新记忆（`memory/extract.py`）
6. `channels/qq.py` 按拟人节奏等够了再发（读延迟 + 打字时长 + 拆条）

## 想改东西，去哪

| 想做的事 | 改哪里 |
|---|---|
| 改她的性格/口癖/底线 | `persona/persona.yaml`（保存即生效） |
| 补"关于老爸"的事实 | `persona/user.yaml`（保存即生效） |
| 加一项能力（天气、运势、点歌…） | `plugins/<名字>/`（见 /plugins-guide，热加载） |
| 调她的"饥饿"阈值/饭量 | `plugins/token_belly/plugin.yaml` 的 `options` |
| 她的人际关系（谁能找她） | `data/contacts.json`（也可让她自己用工具改） |
| 接现成 MCP server | `.env` 的 `DSD_MCP_SERVERS`（见 /plugins-guide） |
| 加一个新的聊天平台 | `src/dsdaughter/channels/` |
| 调主动性频率/免打扰 | `.env` 的 `DSD_PROACTIVE_*` |
| 记忆策略（半衰期/条数/向量） | `.env` 的 `DSD_MEMORY_*` / `DSD_EMBEDDING_*` |
| 换模型/加模型 | `src/dsdaughter/llm/` + `DSD_MODEL` |
| 部署到云服务器 | `deploy/` + `Dockerfile`（见 README 的 Docker 段） |

## 常看的接口

| 接口 | 用途 |
|---|---|
| `GET /healthz` | 状态：人设 / 模型 / 记忆后端 / QQ 连接 / 插件 |
| `GET /api/chat/stream` | SSE 聊天（调试页用的就是它） |
| `GET /api/agent/tools` | 她当前所有工具 + 最近 50 次调用记录 |
| `GET /api/plugins` | 插件加载情况（含被跳过的原因）+ MCP 状态 |
| `GET /project` | 本页 |
| `GET /plugins-guide` | 插件/MCP 编写指南 |

更多：`README.md`（快速开始）、`docs/TECH_STACK.md`（完整选型与风险）、`docs/PLUGINS.md`（插件开发）。
