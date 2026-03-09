"""Custom tools for Copilot SDK sessions.

This file is the public repo default. The private repo (byok-tg-main)
can override it by placing its own tools/ directory.
"""

from copilot import define_tool
from pydantic import BaseModel, Field


class GetWeatherParams(BaseModel):
    city: str = Field(description="City name")


@define_tool(description="Get weather for a city (mock data)")
async def get_weather(params: GetWeatherParams) -> str:
    weather_data = {
        "台北": "晴天，28°C",
        "東京": "多雲，22°C",
        "紐約": "雨天，15°C",
    }
    result = weather_data.get(params.city, f"{params.city}: no data")
    return f"{params.city}: {result}"


# All tools to register with Copilot SDK sessions
ALL_TOOLS = [get_weather]
