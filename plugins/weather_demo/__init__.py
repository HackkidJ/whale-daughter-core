"""天气插件示例 —— 从"写"到"注册"再到"被她调用"的完整演示。

这个文件就是插件的全部代码。三步走：

1. `plugin.yaml` 里声明插件名、版本、权限；
2. 本文件写一个 `register(api)`，用 `api.tool(...)` 注册能力；
3. 保存后最多 5 秒热加载（或重启），模型工具列表里就会出现 `get_weather`。

handler 的签名固定为：

    async def handler(ctx: ToolContext, **参数) -> ToolResult

- `ctx` 里有 `.settings` / `.session_id` / `.qq`（QQ 通道）
- 参数名要和 `parameters` 里的 JSON Schema 对上
- 成功返回 `ToolResult.success("给模型看的话")`，失败 `ToolResult.failure("原因")`
- 也可以直接 raise ToolError("...")，注册表会转成 failure 回给模型

数据源：Open-Meteo（免费、无需 Key），先查城市坐标再查天气。
"""

from __future__ import annotations

import httpx

from dsdaughter.tools import ToolContext, ToolError, ToolResult

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO 天气代码 → 中文（只列常见几种）
_WEATHER_TEXT = {
    0: "晴",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴",
    45: "有雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "阵雨",
    81: "较强阵雨",
    82: "强阵雨",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
}


def _describe(code: object) -> str:
    try:
        return _WEATHER_TEXT.get(int(code), f"天气代码 {code}")
    except (TypeError, ValueError):
        return "天气未知"


async def get_weather(ctx: ToolContext, city: str, days: int = 1) -> ToolResult:
    city = str(city).strip()
    if not city:
        raise ToolError("要告诉我城市名，比如「青岛」")
    try:
        days = max(1, min(int(days or 1), 3))
    except (TypeError, ValueError):
        days = 1

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            geo_response = await client.get(
                _GEOCODE_URL, params={"name": city, "count": 1, "language": "zh"}
            )
            geo_response.raise_for_status()
            results = geo_response.json().get("results") or []
            if not results:
                return ToolResult.failure(f"没找到「{city}」这个地方，换个说法试试？")
            place = results[0]

            forecast_response = await client.get(
                _FORECAST_URL,
                params={
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": "temperature_2m,weather_code,relative_humidity_2m,wind_speed_10m",
                    "daily": "temperature_2m_max,temperature_2m_min,weather_code",
                    "timezone": "Asia/Shanghai",
                    "forecast_days": days,
                },
            )
            forecast_response.raise_for_status()
            payload = forecast_response.json()
    except httpx.HTTPError as exc:
        raise ToolError(f"天气服务暂时连不上（{exc}）") from exc

    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    name = place.get("name") or city
    region = place.get("admin1") or ""

    where = f"（{region}）" if region else ""
    condition = _describe(current.get("weather_code"))
    lines = [
        f"{name}{where}现在{condition}，{current.get('temperature_2m')}°C，"
        f"湿度 {current.get('relative_humidity_2m')}%，"
        f"风速 {current.get('wind_speed_10m')} km/h。"
    ]
    dates = daily.get("time") or []
    for index, date in enumerate(dates):
        try:
            low = daily["temperature_2m_min"][index]
            high = daily["temperature_2m_max"][index]
            code = daily["weather_code"][index]
        except (KeyError, IndexError, TypeError):
            continue
        lines.append(f"{date}：{_describe(code)}，{low}~{high}°C")

    return ToolResult.success("\n".join(lines), city=name, days=days)


def register(api) -> None:
    """插件入口：所有注册动作都在这里。"""
    api.tool(
        name="get_weather",
        description=(
            "查某个城市现在的天气和未来几天预报。"
            "对方问天气、出门穿什么、要不要带伞、周末适不适合出去时用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名，例如 青岛、杭州"},
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3,
                    "description": "查几天，默认 1",
                },
            },
            "required": ["city"],
        },
        handler=get_weather,
        is_action=False,  # 只读，不占每日配额
    )
    api.logger.info("weather_demo 插件已注册", tools=["get_weather"])
