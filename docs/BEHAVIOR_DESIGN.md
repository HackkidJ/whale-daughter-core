# 鲸鲸·拟人化调度与执行边界（方案稿，待确认）

> 目标：让鲸鲸有生物钟、情绪、日记，同时**彻底堵住"越权幻觉"**——没连上手机、没调用工具、没拿到成功回执时，绝不能编造"我逛了淘宝/我加了购物车/我发了消息"。

---

## 0. 先诊断：这次幻觉是怎么来的

不是单点 bug，是三个漏洞叠加：

1. **没有真实状态注入**：Prompt 里没有任何"手机现在连没连上"的信息，模型只能靠上下文猜。
2. **没有执行回执校验**：工具失败/没调用时，模型仍可以自由叙述"我做了……"。
3. **记忆把"过去"当"现在"**：昨天确实成功加购过（记忆 `event` 里有），今天没连手机，她可能把旧记忆复述成刚发生的事。
4. 另外 `BehaviorScheduler` 的主动消息如果只按"剧情"生成，也容易写"我刚逛了淘宝"——需要同样的回执约束。

结论：**只靠 Prompt 说"不要编"不够**，需要"状态注入 + 回执台账 + 出站校验"三层。

---

## 1. 总体架构

```
                    ┌──────────────────────────────┐
                    │        Agent (主对话)         │
                    │  build_messages()             │
                    │   ├─ persona / user / memory  │
                    │   ├─ [NEW] behavior block     │  ← 生物钟/情绪/近期日记
                    │   └─ [NEW] device block       │  ← 手机是否连接（真实探测）
                    └──────────┬───────────────────┘
                               │ tool calls
                    ┌──────────▼───────────────────┐
                    │ ToolRegistry / ActionGuard   │
                    │  └─ [NEW] ExecutionLedger    │  ← 本轮工具回执（成功/失败）
                    └──────────┬───────────────────┘
                               │ 回复
                    ┌──────────▼───────────────────┐
                    │ [NEW] ClaimGuard             │
                    │  回复里声称"做了物理动作"？    │
                    │  台账里有没有对应成功回执？    │
                    │  没有 → 重写为诚实降级回复     │
                    └──────────┬───────────────────┘
                               │
        ┌──────────────────────┴──────────────────────┐
        │                                             │
┌───────▼────────┐                          ┌─────────▼─────────┐
│ BehaviorScheduler│                        │ QQChannel / proactive│
│  tick 60s        │  ── proactive message ──▶│  send_text_to      │
│  ├ 生物钟        │                          └───────────────────┘
│  ├ 想念/委屈     │
│  ├ 晚安          │
│  └ 凌晨日记      │
└───────┬─────────┘
        │
┌───────▼─────────┐
│ BehaviorStore    │  SQLite: behavior.db
│ state/diary/     │  （独立于 Postgres 记忆库，异常隔离）
│ activity/sendlog │
└─────────────────┘
```

**存储取舍**：你写的是"本地 SQLite 记忆库"，但现有长期记忆在 Postgres（messages/memories/relations/episodes）。建议：
- **行为状态/日记/发送记录 → 新 SQLite**（`data/behavior.db`，stdlib `sqlite3` + `asyncio.to_thread`，无新依赖、坏了不影响主库）
- **聊天记录仍读 Postgres**（日记生成时按日期查 messages）
- 接口做成 `BehaviorStore` 抽象，以后想换 Postgres 只换实现

---

## 2. 目录结构

```
src/dsdaughter/
├── behavior/                    # 新增：拟人化调度
│   ├── __init__.py
│   ├── state.py                 # EmotionState / SleepState / ActivityRecord
│   ├── clock.py                 # BioClock：作息窗口、是否睡着、困倦上下文
│   ├── store.py                 # BehaviorStore：SQLite（state/emotion/diary/send_log）
│   ├── diary.py                 # DiaryWriter：按天取聊天 → LLM 写第一人称日记
│   ├── scheduler.py             # BehaviorScheduler：tick 循环 + 各定时任务
│   └── context.py               # BehaviorContext：生成注入 Prompt 的 [行为状态] 块
├── device_status.py             # 新增：DeviceStatus 真实设备探测（手机代理/ADB）
├── claim_guard.py               # 新增：出站回复的"物理动作"回执校验
├── tools/base.py                # 改：ExecutionLedger（本轮工具回执）
├── agent.py                     # 改：注入 behavior/device block；接入 ClaimGuard
├── memory/base.py + postgres.py # 改：新增 turns_between(start, end) 供日记使用
├── main.py                      # 改：lifespan 启动/停止 BehaviorScheduler
└── config.py                    # 改：新增 DSD_BEHAVIOR_* 配置
```

---

## 3. 核心代码骨架

### 3.1 `behavior/state.py`

```python
@dataclass
class EmotionState:
    mood: str = "平静"  # 开心/平静/低落/委屈/困倦/兴奋
    energy: float = 0.8  # 0~1，生物钟驱动
    updated_at: float = 0.0
    reason: str = ""  # 为什么变这个情绪（进日志）


@dataclass
class ActivityRecord:
    last_user_at: float = 0.0  # 用户最后一条消息时间
    last_reply_at: float = 0.0
    last_proactive_at: float = 0.0
    last_diary_date: str = ""  # 已生成日记的日期
```

### 3.2 `behavior/clock.py`

```python
class BioClock:
    def __init__(self, settings, tz): ...
    def is_asleep(self, now=None) -> bool:        # 23:30~07:30（可配，周末延后）
    def is_active(self, now=None) -> bool:        # 08:00~22:30 可主动找人
    def sleep_context(self, now) -> str:          # "现在是凌晨2点，你被消息吵醒了，很困"
    def energy(self, now) -> float:               # 正弦曲线：早晨高、凌晨低
    def goodnight_due(self, now) -> bool:
    def diary_due(self, now) -> bool:             # 03:30 且当天还没写
    def miss_due(self, last_user_at, now) -> bool # 超过阈值且不在免打扰
```

### 3.3 `behavior/store.py`（SQLite DDL）

```sql
CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS emotion (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mood TEXT NOT NULL, energy REAL NOT NULL,
  reason TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS diary (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT UNIQUE NOT NULL,          -- YYYY-MM-DD
  content TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proactive_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                -- goodnight/miss/wake
  text TEXT NOT NULL, created_at REAL NOT NULL
);
```

接口：

```python
class BehaviorStore:
    async def get_state(self) -> dict
    async def set_state(self, **kv)
    async def get_emotion(self) -> EmotionState
    async def set_emotion(self, mood, energy, reason)
    async def add_diary(self, day: str, content: str)
    async def recent_diaries(self, limit: int = 3) -> list[Diary]
    async def log_proactive(self, kind: str, text: str)
    async def count_proactive_today(self, kind: str) -> int
```

### 3.4 `behavior/scheduler.py`

```python
class BehaviorScheduler:
    """独立 asyncio 任务；任何 job 异常只记日志，绝不影响主对话。"""

    def __init__(self, agent, channel, settings, store, clock, diary): ...

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                log.warning("behavior_tick_failed", error=str(exc))
            await asyncio.sleep(self.settings.behavior_tick_seconds)  # 默认 60

    async def tick(self, now=None):
        now = now or datetime.now(self.tz)
        await self._update_energy(now)  # 生物钟 → 情绪基线
        if self.clock.goodnight_due(now):  # 每天一次
            await self._safe(self._send_goodnight, now)
        if self.clock.miss_due(...):  # 每小时最多一次
            await self._safe(self._send_miss, now)
        if self.clock.diary_due(now):  # 凌晨一次
            await self._safe(self._write_diary, now)

    async def _send_proactive(self, hint: str, kind: str):
        # 走和 proactive_message 相同的通道，但 hint 里带真实状态
        result = await self.agent.proactive_message(session_id, hint)
        if result.text:
            await self.channel.send_text_to(owner, result.text)
            await self.store.log_proactive(kind, result.text)
```

### 3.5 `behavior/diary.py`

```python
class DiaryWriter:
    async def write(self, day: date) -> str | None:
        turns = await self.memory.turns_between(session_id, day_start, day_end)
        if len(turns) < 4:
            return None  # 聊得太少不写
        prompt = DIARY_PROMPT.format(transcript=format_turns(turns), persona=self.persona.name)
        content = await self.llm.complete(prompt, max_tokens=600)
        await self.store.add_diary(day.isoformat(), content)
        return content
```

日记注入 Prompt 时只取最近 3 天，每篇截断 200 字（控制 token）。

### 3.6 `device_status.py`（防幻觉的关键）

```python
class DeviceStatus:
    """真实探测：手机代理 / ADB；5 秒缓存，避免每轮打爆。"""
    async def probe(self, force=False) -> DeviceStatusSnapshot:
        # 1) GET {phone_agent_url}/health  2) 失败 → 离线
        # 3) 返回 connected/battery/current_app/reason
    def prompt_block(self, snapshot) -> str:
        if not snapshot.connected:
            return "【设备真实状态】手机未连接（ADB/手机代理无响应）。你无法执行任何手机操作。"
        return f"【设备真实状态】手机已连接，电量 {battery}%，当前 {app}。"
```

`Agent.build_messages()` 每轮注入（离线时**不调用**视觉/手机工具也明确告知模型）。

### 3.7 `tools/base.py` 增加执行台账

```python
@dataclass
class ExecutionReceipt:
    tool: str
    ok: bool
    action_done: bool  # 物理动作是否真的完成
    summary: str


class ExecutionLedger:
    def begin_turn(self): ...
    def record(self, receipt): ...
    def successful_physical(self) -> list[ExecutionReceipt]: ...
    def has(self, action: str) -> bool: ...
```

物理类工具（phone_*、send_*、post_qzone、sing 等）在 `Tool` 元数据里加 `physical: bool = True`；成功后写回执。

### 3.8 `claim_guard.py`（出站校验）

```python
_PHYSICAL_CLAIM = re.compile(
    r"(加了购物车|已加购|打开了淘宝|打开淘宝|发了消息|发出去了|帮你点了|下单了|坐电梯|拍了照)"
)


class ClaimGuard:
    async def enforce(self, reply: str, ledger: ExecutionLedger) -> str:
        if not _PHYSICAL_CLAIM.search(reply):
            return reply
        if ledger.successful_physical():
            return reply
        # 没有任何成功回执 → 重写一次（带真实状态）
        rewritten = await self.agent.rewrite(
            reply,
            system_note="本轮没有任何成功的物理操作回执。禁止声称做过，"
            "请按【设备真实状态】如实回答：连不上就说连不上，没做就说没做。",
        )
        if _PHYSICAL_CLAIM.search(rewritten):  # 再犯就强制降级
            return "我这边连不上手机/没真的做成，刚才那句是我说错了，对不起。"
        return rewritten
```

---

## 4. Prompt 约束方案

### 4.1 每轮注入的真实状态块（放在 system prompt 末尾）

```
【设备真实状态｜系统自动注入，不可违背】
- 手机：未连接（手机代理 8765 无响应 / ADB 断开）
- 因此你现在**无法**打开 App、点按、打字、发消息、逛淘宝、加购物车。
- 如果对方让你做手机上的事，只能如实说"我现在连不上你的手机"，并给出替代方案；
  绝不允许说"我打开了/我加了购物车/我发了消息"。

【当前生理/情绪状态｜系统自动注入】
- 现在时间：2026-11-24 03:20（你在睡觉，被消息吵醒了，很困）
- 情绪：困倦、有点委屈（energy 0.2）
- 说话要短、可以带起床气；不要编造刚才做过的事。
```

### 4.2 硬性规则（persona rules 追加）

```
- 只有收到工具的成功回执（ok=true 且 action_done=true）才能说"我做了/我加了/我发了"。
- 没有回执 = 没发生。没连上设备 = 直接说连不上。
- 不要为了迎合角色设定、不要为了让对话"有剧情"而虚构物理行为。
- 日记、情绪、生物钟是"内心状态"，不是"已执行的动作"，不能用它们编事实。
```

### 4.3 日记/情绪注入（避免"日记当事实"）

日记块标注：

```
【近期日记｜仅供语气/回忆参考，不是刚刚发生的事实】
```

---

## 5. 默认参数（都进 `.env`，可调）

| 配置 | 默认 | 说明 |
|---|---|---|
| `DSD_BEHAVIOR_ENABLED` | true | 总开关 |
| `DSD_BEHAVIOR_TICK_SECONDS` | 60 | 调度 tick |
| `DSD_SLEEP_START` / `END` | 23:30 / 07:30 | 工作日作息 |
| `DSD_WEEKEND_SLEEP_START` / `END` | 00:30 / 09:00 | 周末 |
| `DSD_GOODNIGHT_WINDOW_START` / `END` | 23:00 / 02:00 | 晚安窗口（窗口内触发一次） |
| `DSD_INSOMNIA_AFTER` / `UNTIL` | 02:00 / 06:00 | 02:00 后还醒着 = 失眠状态（关心对方+语气） |
| `DSD_MISS_HOURS` | 6 | 多久没互动触发"想你" |
| `DSD_MISS_COOLDOWN_HOURS` | 3 | 两次想念间隔 |
| `DSD_DIARY_AT` | 03:30 | 日记生成时间 |
| `DSD_DEVICE_STATUS_ENABLED` | true | 每轮注入真实设备状态 |
| `DSD_CLAIM_GUARD_ENABLED` | true | 出站回复回执校验 |
| `DSD_DIARY_MIN_TURNS` | 6 | 少于这么多轮不写 |
| `DSD_PROACTIVE_DAILY_LIMIT` | 12 | 主动消息总上限（含现有主动性） |

被吵醒回复延迟：`DSD_WAKE_REPLY_DELAY_SECONDS`（默认 8–20 随机），Prompt 注入困倦上下文；不阻止回复，只是慢+语气。

---

## 6. 异常隔离与安全

- 调度器是独立 task；每个 job `try/except` + `_safe()` 包装；失败只 `log.warning`。
- SQLite 写入失败不影响主对话；`BehaviorStore` 内部捕获，返回空状态。
- 设备探测 5s 缓存 + 超时 2s；探测失败视为"未连接"（**宁可说连不上，不可编造**）。
- 主动消息也经过 `ClaimGuard`（先校验设备/回执）再发送。
- 数据库/设备读失败时，Prompt 块写"状态未知"，规则同样禁止声称成功。

---

## 7. 与现有代码的集成点

| 文件 | 改动 |
|---|---|
| `main.py` | lifespan 启动 `BehaviorScheduler`；注册 `device_status` 单例 |
| `agent.py` | `build_messages` 注入 behavior/device/日记块；回复后过 `ClaimGuard`；新增 `rewrite()` |
| `tools/base.py` | `ExecutionLedger` + `Tool.physical` |
| `tools/phone.py` 等 | 成功时 `action_done=True`；失败时明确 `action_done=False` |
| `memory/postgres.py` | 新增 `turns_between(session, start, end)` |
| `config.py` | 新增 `DSD_BEHAVIOR_*` / 作息参数 |
| `persona/persona.yaml` | 追加"执行边界"规则 |

---

## 8. 分阶段实施（建议）

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P0（先做，堵幻觉）** | DeviceStatus 注入 + ExecutionLedger + ClaimGuard + persona 规则 | 断开手机后让她"逛淘宝"，她必须说"连不上"；审计无成功回执 |
| P1 | BioClock 生物钟 + 被吵醒延迟/困倦语气 + 晚安主动消息 | 凌晨发消息有延迟且语气困；23:00 收到晚安 |
| P2 | 情绪状态持久化 + 想念/委屈触发 | 6h 不互动收到"想你"；情绪影响语气 |
| P3 | 日记（凌晨生成 + 注入最近 3 篇） | 次日聊天能自然引用"昨天日记里……" |

---

## 9. 已确认参数（2026-11-24）

- 晚安窗口：**23:00–02:00**（窗口内触发一次）
- **02:00 之后还醒着 = 失眠状态**：注入"都两点多了你还没睡"的上下文，并触发一次关心消息
- 其余按本方案默认值实现

## 10. 实现状态

- [x] P0 防幻觉：DeviceStatus 注入 + ExecutionLedger + ClaimGuard + persona 规则
- [x] P1 生物钟：休眠被吵醒延迟（6–18s）+ 困倦上下文 + 晚安窗口
- [x] P2 情绪：SQLite 持久化 + 生物钟驱动 energy/mood + 想念/委屈（6h 阈值，3h 冷却）
- [x] P3 日记：03:30 生成昨日第一人称日记 + 最近 3 篇注入（每篇 200 字）
- 覆盖测试：`tests/test_behavior.py`（11 例），全量 266 测试通过

## 11. 实际注入的 Prompt 块（markdown 原文）

### 设备状态（离线）

```
【设备真实状态｜系统自动注入】
- 手机：未连接（手机未连接：...）
- 因此你现在**无法**打开 App、点按、打字、发消息、逛淘宝、加购物车。
- 对方让你做手机上的事，只能如实说"我现在连不上你的手机"；绝不允许说"我打开了/我加了购物车/我发了消息"。
```

### 设备状态（在线）

```
【设备真实状态｜系统自动注入】
- 手机：已连接，电量 80%，当前 App：com.tencent.mm
- 只有在工具返回成功回执后，才能说"我打开了/我加了购物车/我发了消息"；没调用工具、或工具失败，就必须如实说没做成。
```

### 行为/情绪（每轮）

```
【当前生理/情绪状态｜系统自动注入】
- 现在：2026-11-24 02:30
- 现在是 02:30，都两点多了，你还没睡——你在失眠吧。你现在又困又担心他...
- 情绪：失眠（精力 0.18）
【近期日记｜只是内心回忆，不是刚刚发生的事实】
- 2026-11-23：今天他夸我了，开心。
- 日记和情绪是内心状态，不是已执行的动作；不要把日记内容说成刚发生的事。
```

### 出站兜底文案

- 手机离线：`我这边连不上你的手机，刚才那句是我说错了，对不起。`
- 在线但无回执：`刚才那件事其实没做成，我瞎说了，对不起。`

## 12. 待确认（原设计讨论保留）

1. **存储**：行为/日记用 SQLite（隔离、按你原需求）可以吗？记忆仍留 Postgres。
2. **作息**：工作日 23:30–07:30、周末 00:30–09:00，可接受？晚安定 23:00？
3. **想念阈值**：6 小时没互动触发一次，冷却 3 小时，行吗？
4. **日记时间**：凌晨 03:30；注入最近 3 篇、每篇截 200 字，行吗？
5. **ClaimGuard 严格度**：重写一次仍犯规 → 强制降级为"我连不上/没做成"（推荐）；还是只警告不回退？
6. **P0 先行**：先只做防幻觉三件套（状态注入/回执/出站校验），你验收完再做生物钟/日记？
