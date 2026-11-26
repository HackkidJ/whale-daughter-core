"""配置：全部来自环境变量（DSD_ 前缀）或 .env。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行时配置。

    DeepSeek Key 同时接受 `DEEPSEEK_API_KEY` 和 `DSD_DEEPSEEK_API_KEY`，
    其余配置统一使用 `DSD_` 前缀，避免和系统里其他变量冲突。
    """

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        env_prefix="DSD_",
        extra="ignore",
    )

    # --- 大脑 ---
    deepseek_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "DSD_DEEPSEEK_API_KEY"),
    )
    llm_mode: Literal["deepseek", "fake"] = "deepseek"
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    web_tools: bool = True
    thinking: bool = False
    # 思考强度（仅在 thinking=true 时生效）：low / high / max
    reasoning_effort: Literal["low", "high", "max"] | None = None
    safety_check: Literal["low", "high"] | None = None
    temperature: float = 1.0
    presence_penalty: float | None = 0.3
    frequency_penalty: float | None = 0.2
    max_tokens: int | None = None
    request_timeout: float = 180.0
    # 是否请求流式 usage（OpenAI 兼容字段；个别网关不支持时设为 false）
    stream_usage: bool = True

    # --- 语音 ---
    voice_enabled: bool = True
    tts_provider: Literal["deepseek", "fake", "volcengine"] = "deepseek"
    # DeepSeek TTS 只有两个音色：stella（女声，默认）/ tide（男声）
    tts_voice: str = "stella"
    voice_probability: float = 0.25
    voice_reply_to_voice: float = 0.85
    voice_max_chars: int = 120
    voice_max_per_hour: int = 4
    # 火山引擎豆包语音（v1 精品音色 HTTP 接口；复刻音色走 v3，见 media/tts.py 注释）
    volc_app_id: str = ""
    volc_token: str = ""
    # 新版控制台：API Key（X-Api-Key）+ 资源 ID（seed-tts-2.0 / seed-icl-2.0）
    volc_api_key: str = ""
    volc_resource_id: str = "seed-tts-2.0"
    volc_cluster: str = "volcano_tts"
    volc_voice_type: str = "BV700_streaming"
    volc_encoding: str = "wav"
    volc_speech_rate: float = 1.0
    songs_dir: Path = Path("assets/songs")
    sing_enabled: bool = True

    # --- 视觉（她的眼睛）---
    vision_enabled: bool = True
    vision_model: str = "deepseek-flash"
    vision_max_side: int = 720
    vision_jpeg_quality: int = 80
    vision_max_tokens: int = 900

    # --- 行为调度（生物钟/情绪/日记/主动消息）---
    behavior_enabled: bool = True
    behavior_tick_seconds: float = 60.0
    behavior_db_path: Path = Path("data/behavior.db")
    sleep_start: str = "23:30"
    sleep_end: str = "07:30"
    weekend_sleep_start: str = "00:30"
    weekend_sleep_end: str = "09:00"
    goodnight_window_start: str = "23:00"
    goodnight_window_end: str = "02:00"
    insomnia_after: str = "02:00"
    insomnia_until: str = "06:00"
    miss_hours: float = 6.0
    miss_cooldown_hours: float = 3.0
    behavior_proactive_daily_limit: int = 6
    wake_reply_delay_min: float = 6.0
    wake_reply_delay_max: float = 18.0
    diary_at: str = "03:30"
    diary_min_turns: int = 6
    diary_inject_limit: int = 3
    diary_inject_chars: int = 200
    # --- 防幻觉：设备状态与执行回执 ---
    device_status_enabled: bool = True
    device_status_ttl: float = 5.0
    device_status_timeout: float = 2.0
    claim_guard_enabled: bool = True

    # --- 手机（独立设备代理服务）---
    phone_enabled: bool = False
    phone_agent_url: str = "http://127.0.0.1:8765"
    phone_timeout: float = 20.0
    # 动作类：这些包一律拒绝（支付/银行/钱包）
    # 主动看手机：隔一段时间偷偷看一眼，看到有意思的就找他说话
    phone_peek_enabled: bool = False
    phone_peek_min_seconds: int = 900
    phone_peek_max_seconds: int = 2400
    phone_peek_daily_limit: int = 8
    phone_peek_quiet_hours: str = "00:30-08:00"
    phone_peek_use_vision: bool = True
    # 爱逛淘宝：定期用他的手机刷淘宝，看到喜欢的加购物车（只加购，绝不付款）
    taobao_enabled: bool = False
    taobao_min_seconds: int = 10800
    taobao_max_seconds: int = 21600
    taobao_daily_limit: int = 3
    taobao_quiet_hours: str = "00:30-08:00"

    phone_blocked_packages: str = "alipay,unionpay,bank,wallet,pay"
    # 点击/输入里出现这些词就拒绝（支付确认红线）
    phone_blocked_texts: str = (
        "确认支付,立即支付,付款,转账,输入密码,指纹支付,免密支付,提现,充值,余额"
    )

    # --- 她的人际关系（动态白名单 / 临时会话）---
    # 可写目录：contacts.json 等运行状态放这里（容器里记得挂卷）
    data_dir: Path = Path("data")
    # 老爸的 QQ：永远在白名单、不能被删好友、不能被移出
    owner_qq: str = ""
    # 允许陌生人的临时会话（群里点她发起的会话）——不自动变好友，但可以聊
    qq_accept_temp_sessions: bool = True

    # --- 插件（A：本地插件目录）---
    plugins_enabled: bool = True
    plugins_dir: Path = Path("plugins")
    # 非空时只加载名单里的插件；带 filesystem 等高危权限的插件必须在此名单内
    plugins_allowlist: str = ""

    # --- MCP（B：现成的 MCP server 当插件）---
    mcp_enabled: bool = False
    # 形如 "time=uvx mcp-server-time; fetch=http://127.0.0.1:9000/mcp"
    mcp_servers: str = ""
    mcp_timeout: float = 30.0

    # --- 文档页（首页的项目结构说明）---
    docs_dir: Path = Path("docs")

    # --- 主动性（她自己找你说话）---
    proactive_enabled: bool = True
    proactive_tick_seconds: float = 180.0
    proactive_daily_limit: int = 14
    proactive_min_gap_minutes: float = 25.0
    proactive_miss_probability: float = 0.18
    proactive_miss_gap_minutes: float = 60.0
    proactive_quiet_hours: str = "00:30-08:00"
    # 发给谁（默认用 QQ 白名单里的第一个；可单独指定）
    proactive_target_users: str = ""

    # --- 长期记忆 ---
    memory_backend: Literal["memory", "postgres"] = "memory"
    database_url: str = "postgresql://dsd:change-me@127.0.0.1:5433/dsd"
    memory_recall_limit: int = 6
    memory_extract: bool = True
    memory_half_life_days: float = 30.0
    memory_max_candidates: int = 300
    # 可选：OpenAI 兼容的 /v1/embeddings（配了才启向量检索）
    embedding_base_url: str = ""
    embedding_model: str = "BAAI/bge-m3"
    embedding_api_key: str = ""
    embedding_dim: int = 1024

    # --- 工具（她自己的能力）---
    tools_enabled: bool = True
    tool_max_rounds: int = 4
    # 动作类工具每日上限：tool:limit，逗号分隔
    tool_daily_limits: str = (
        "set_avatar:1,set_nickname:3,set_signature:3,post_qzone:2,"
        "allow_contact:3,remove_contact:3,delete_friend:2,add_friend:3,"
        "send_message:10,approve_friend_request:5,speak:20,sing:6,"
        "phone_open_app:20,phone_tap_text:50,phone_type_text:30,phone_back:50,"
        "phone_list_apps:30"
    )
    tool_action_cooldown: float = 60.0
    search_count: int = 5

    # --- 人格与记忆 ---
    persona_path: Path = Path("persona/persona.yaml")
    user_profile_path: Path = Path("persona/user.yaml")
    history_turns: int = 20
    timezone: str = "Asia/Shanghai"

    # --- QQ 通道（NapCat / OneBot 11 反向 WS）---
    qq_enabled: bool = True
    qq_token: str = ""
    qq_allowed_users: str = ""
    qq_allowed_groups: str = ""
    qq_group_require_at: bool = True
    qq_auto_accept_friends: bool = True
    qq_reply_max_chars: int = 900
    qq_send_interval: float = 1.0
    qq_action_timeout: float = 30.0

    # --- 拟人节奏（回复别快得像 AI）---
    qq_human_delay: bool = True
    qq_read_delay_min: float = 0.6
    qq_read_delay_max: float = 1.8
    qq_typing_cps_min: float = 5.0
    qq_typing_cps_max: float = 9.0
    qq_max_typing_delay: float = 10.0
    qq_typing_status: bool = True
    qq_split_sentences: bool = True

    # --- 服务 ---
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    @property
    def action_limits(self) -> dict[str, int]:
        """把 `set_avatar:1,post_qzone:2` 解析成字典。"""
        limits: dict[str, int] = {}
        for item in (self.tool_daily_limits or "").split(","):
            name, _, value = item.partition(":")
            if name.strip() and value.strip().isdigit():
                limits[name.strip()] = int(value.strip())
        return limits

    @field_validator(
        "deepseek_api_key",
        "safety_check",
        "max_tokens",
        "reasoning_effort",
        "presence_penalty",
        "frequency_penalty",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """`.env` 里 `KEY=` 这种留空写法要当成"没配"，而不是空字符串。"""
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
