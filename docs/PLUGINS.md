# 插件开发指南（A：本地插件 / B：MCP）

这篇是"以后自己给她加能力"的完整说明书。照着 `plugins/weather_demo` 抄一遍就能上手。

---

## A. 本地插件

### 1. 插件就是一个目录

```
plugins/
└── weather_demo/
    ├── plugin.yaml    # 清单：名字、版本、权限、开关
    └── __init__.py    # 代码：register(api) 里注册工具
```

放进去之后**最多 5 秒自动生效**（热加载），不用重启。想立刻确认有没有加载上：

```bash
curl -s localhost:8000/api/plugins | python3 -m json.tool
```

### 2. plugin.yaml 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 插件名，全局唯一（也用于白名单） |
| `version` | 否 | 版本号 |
| `description` | 否 | 一句话说明，会显示在 `/api/plugins` |
| `author` | 否 | 作者 |
| `enabled` | 否 | `false` 时跳过加载（默认 true） |
| `permissions` | 是 | 权限列表，见下表 |
| `options` | 否 | 插件自己的配置（字典），代码里用 `api.options` 读 |

| 权限 | 含义 | 默认 |
|---|---|---|
| `network` | 允许联网（httpx 请求） | 允许 |
| `qq` | 允许通过 `ctx.qq` 调 OneBot（改资料/发消息等） | 允许 |
| `filesystem` | 读写文件（高危：能读 `.env`） | **拒绝**，必须写进 `DSD_PLUGINS_ALLOWLIST` |

未知权限会被直接拒绝加载。加载失败不影响主服务，原因只在 `/api/plugins` 和日志里。

### 3. 写代码：register(api)

```python
from dsdaughter.tools import ToolContext, ToolResult


async def my_handler(ctx: ToolContext, city: str) -> ToolResult:
    ...
    return ToolResult.success("给模型看的文本")


def register(api) -> None:
    api.tool(
        name="my_tool",  # 工具名：字母/数字/下划线，全局唯一
        description="一句话说明什么时候该用它",  # 这句决定模型会不会调用
        parameters={  # JSON Schema，模型按它填参数
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
        handler=my_handler,
        is_action=False,  # True = 会改变外部世界，占每日配额+冷却
    )
```

**注册动态上下文（可选）**

除了工具，插件还能往 system 提示里注入一段"当前状态"，让模型自然地表现出来（不用每次调工具）：

```python
def register(api) -> None:
    async def belly_block() -> str:
        return "【你的肚子】余额 ¥3.21，够吃 2 天，你现在很饿。"

    api.context(belly_block)  # 每轮请求前调用一次；记得自己做缓存，别每次都打网络
```

- 适合：余额/电量、当前心情、外部实时状态
- 每次请求前会调用一次，超时 5 秒，出错只打日志不影响聊天
- 返回空字符串 = 这一轮不注入
- 注意：插件出错不能拖垮主流程，provider 里务必自己兜异常

**handler 约定**

- 签名固定：`async def handler(ctx: ToolContext, **参数) -> ToolResult`
- `ctx.settings` 配置、`ctx.session_id` 当前会话、`ctx.qq` QQ 通道（未接入为 None，用 `ctx.require_qq()` 会抛错）
- 返回 `ToolResult.success("内容")` 或 `ToolResult.failure("原因")`；也可以 `raise ToolError("...")`
- `success` 的文本会原样喂回模型 —— 写成模型能直接理解的自然语言，别返回原始 JSON
- 返回内容别太长（建议 < 8000 字），超了会撑爆上下文

`api` 上还有：`api.manifest`（清单）、`api.settings`（配置）、`api.logger`（会带上插件名）。

### 4. 从"写完"到"她真的会调用"的五步

以天气插件为例：

**第 1 步**：建目录

```bash
mkdir -p plugins/my_weather
```

**第 2 步**：写 `plugin.yaml`

```yaml
name: my_weather
version: 0.1.0
description: 天气查询
permissions: [network]
```

**第 3 步**：写 `__init__.py`（核心就是个 async 函数 + register），参考 `plugins/weather_demo/__init__.py`

**第 4 步**：保存 → 等 5 秒 → 确认加载

```bash
curl -s localhost:8000/api/plugins | python3 -m json.tool
# loaded 里应该出现 my_weather，tools 里有你注册的工具名
curl -s localhost:8000/api/agent/tools | python3 -c "import sys,json;print([t['function']['name'] for t in json.load(sys.stdin)['tools']])"
```

**第 5 步**：在聊天里验证

- 在调试页（`/`）或 QQ 里直接说：「帮我查一下青岛天气」
- 模型看到工具描述后自己决定调用 → 你在 `/api/agent/tools` 的 `recent` 里能看到这次调用
- 如果她不调用：**先改 `description`**（模型靠这句判断"什么时候用"），再把参数说明写清楚

### 5. 调试与测试

- 日志：`docker logs` / `uv run uvicorn ...` 输出里搜 `插件已加载`、`tool_called`
- 单独测 handler：

```python
from dsdaughter.config import Settings
from dsdaughter.tools import ToolContext

ctx = ToolContext(settings=Settings(_env_file=None), session_id="test")
result = await get_weather(ctx, city="青岛")
assert result.ok
```

- 想加 pytest：把 handler 当普通 async 函数测，用 `respx` mock 网络（参考 `tests/test_tools.py`）

### 6. 什么时候该写插件、什么时候别写

| 适合 | 不适合 |
|---|---|
| HTTP API 封装（天气/快递/点歌/运势） | 需要长时间跑后台任务的（先用主程序调度器） |
| 纯计算/查表 | 需要读写敏感文件的（权限高危，且破坏只读根） |
| 给模型提供额外信息 | 需要改 prompt 结构的（改 `agent.py`） |

### 7. 分发插件

就是一个目录：压缩成 zip 发出去，对方解压到 `plugins/` 即可。需要额外 Python 包时，让对方先 `uv add 包名` —— 插件本身不负责装依赖（避免任意 pip 安装带来的风险）。

---

### 8. 示例二：token_belly（让她有"肚子"）

`plugins/token_belly` 是第二个示例，演示 `api.tool` + `api.context` 配合：

- 调 DeepSeek `GET /user/balance` 读余额（只读，不花钱），3 分钟缓存一次
- 按余额 ÷ 每天饭量算"还能吃几天"，映射成五档饥饿：吃饱饱 / 不饿 / 有点饿 / 很饿 / 快饿死了 / 断粮
- 每轮把"肚子状态"注入 system（带行为护栏：不许每句提钱、不许用愧疚感逼充值）
- 提供 `check_belly` 工具，她想知道具体数字时调用
- 阈值和兜底饭量在 `plugin.yaml` 的 `options` 里改：

```yaml
options:
  daily_cost: 1.5        # 每天饭量（元）
  refresh_seconds: 180   # 余额缓存秒数
```

## B. MCP（现成的 server 直接当插件）

MCP 是跨工具的开放协议，生态里已经有大量 server（时间、抓网页、文件、地图、数据库…），不用自己写适配。

### 0. 去哪找 MCP server

| 站点 | 说明 |
|---|---|
| <https://registry.modelcontextprotocol.io> | **官方 Registry**（预览）：名字空间经 GitHub/DNS 验证，最像"权威索引" |
| <https://modelscope.cn/mcp> | **魔搭 MCP 广场**（国内直连，多为 SSE/HTTP） |
| <https://bailian.console.aliyun.com/#/mcp-market> | 阿里云百炼 MCP 市场（国内，需账号） |
| <https://glama.ai/mcp/servers> | 目前最大的可搜索目录 |
| <https://smithery.ai> | Registry + 托管，很多 server 能"一键运行" |
| <https://mcp.so> / <https://pulsemcp.com> | 社区目录 / 编辑精选 |
| <https://github.com/modelcontextprotocol/servers> | 官方示例与参考实现 |
| <https://hub.docker.com/mcp> | Docker MCP Catalog（镜像形式，适合容器部署） |

> 国内访问 GitHub 系目录可能不稳，**优先试魔搭 / 百炼**；纯 stdio 的包（`uvx`/`npx`）只要 PyPI/npm 能通就能装。

### 1. 我们支持哪几种接法

| 形式 | 写法 | 适用 |
|---|---|---|
| stdio（子进程） | `name=uvx mcp-server-time` | 包管理器能装的 server，最省事 |
| Streamable HTTP | `name=https://host/mcp` | 远程托管 / 自己包一层 |
| **SSE（旧版）** | `name=sse:https://host/sse` | 国内 MCP 市场常见 |
| 带请求头 | 末尾加 `\|Authorization: Bearer xxx` | 远程 server 要鉴权 |
| 给子进程传环境变量 | 末尾加 `\|ENV:GITHUB_TOKEN=xxx` | 需要 API Key 的 server |

### 2. 打开开关

```dotenv
# .env
DSD_MCP_ENABLED=true
# 多条用 ; 分隔
DSD_MCP_SERVERS=time=uvx mcp-server-time; fetch=uvx mcp-server-fetch
```

- **stdio 形式**：`名字=命令 参数...`（会起子进程）
- **HTTP 形式**：`名字=https://example.com/mcp`（Streamable HTTP）

重启 bot 后：

```bash
curl -s localhost:8000/api/plugins | python3 -m json.tool   # mcp.servers 里看连接状态
```

工具名会自动变成 `mcp_<server名>_<工具名>`，出现在她的工具列表里。

### 3. 五步加进来

**① 找**：在目录里挑一个 server，复制它的安装命令或 SSE/HTTP 地址。

**② 先探针**（不用重启 bot 就能验证通不通）：

```bash
uv run python scripts/mcp_probe.py "time=uvx mcp-server-time"
uv run python scripts/mcp_probe.py "time=uvx mcp-server-time" --call get_current_time
uv run python scripts/mcp_probe.py "maps=sse:https://mcp.amap.com/sse?key=你的key"
```

输出会列出它所有工具和参数；能列出来就说明通了。

**③ 写进 `.env`**：

```dotenv
DSD_MCP_ENABLED=true
DSD_MCP_SERVERS=time=uvx mcp-server-time; maps=sse:https://mcp.amap.com/sse?key=你的key
```

**④ 重启 bot**（MCP 只在启动时连接，不热加载——插件才热加载）：

```bash
# 本机
uv run uvicorn dsdaughter.main:app --host 0.0.0.0 --port 8000
# 或容器
docker compose -f deploy/docker-compose.yml up -d bot
```

**⑤ 验证**：

```bash
curl -s localhost:8000/api/plugins | python3 -m json.tool   # mcp.servers[].connected 应为 true
curl -s localhost:8000/api/agent/tools | python3 -c "import sys,json;print([t['function']['name'] for t in json.load(sys.stdin)['tools'] if t['function']['name'].startswith('mcp_')])"
```

然后在聊天里直接说「用 mcp_time_get_current_time 查一下现在几点」就能看到她调用。

> **容器部署注意**：bot 镜像里只有 Python，没有 `uvx`/`npx`。所以容器里跑 MCP 有两条路：
> 1. 用 **HTTP/SSE 型** server（把它跑在另一个容器，`name=http://mcp-xxx:8000/mcp`）；
> 2. 或者在 Dockerfile 里额外装 uv/node（会让镜像变大）。
>
> 另外，如果 `uvx` 报 `~/.local/share/uv/tools` 没权限，用 `|ENV:UV_TOOL_DIR=/可写目录` 指定工具目录（我们本机沙箱里就是这么跑通的）。

### 4. 安全边界（重要）

MCP server 是**任意可执行程序**，和 bot 同权限，能读到：

- `.env` 里的 DeepSeek API Key、QQ token、数据库密码
- 宿主机文件（如果 bot 没做容器隔离）

所以：

1. 默认 `DSD_MCP_ENABLED=false`，只接你信得过的 server
2. 最好把 MCP server 跑在**单独容器**里，用 HTTP 方式接（这样它拿不到 bot 的环境变量）
3. 别接来源不明的第三方 server；接之前看一眼它的代码/命令
4. MCP 工具默认算"动作类"（占每日配额、进审计日志）

### 5. MCP vs 本地插件怎么选

| 场景 | 选 |
|---|---|
| 能力很小、要和她的记忆/人设深度配合 | 本地插件 |
| 想要现成生态、跨框架复用、跑在独立容器 | MCP |
| 需要联网/文件等重能力且不想自己写 | MCP |

---

## 附：当前已加载

- `weather_demo`：工具 `get_weather`
- `token_belly`：工具 `check_belly` + 每轮注入"肚子状态"（余额/饥饿）
- MCP：默认关闭；实测接通过 `uvx mcp-server-time`（3 个工具，她成功调了 `mcp_time_get_current_time`）

> 想看实时状态：`GET /api/plugins`；想看调用记录：`GET /api/agent/tools`。
