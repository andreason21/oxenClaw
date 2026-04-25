"""weather tool — wttr.in + open-meteo lookups, SSRF-safe.

Mirrors openclaw `skills/weather`. The tool accepts either a city name
(routed through wttr) or numeric lat/lon (routed through open-meteo).
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from pydantic import BaseModel, Field, model_validator

from sampyclaw.agents.tools import FunctionTool, Tool
from sampyclaw.tools_pkg.web import assert_public_url


class _WeatherArgs(BaseModel):
    city: str | None = Field(None, description="City name (e.g. 'Seoul').")
    lat: float | None = Field(None, description="Latitude (-90..90).")
    lon: float | None = Field(None, description="Longitude (-180..180).")
    units: str = Field("metric", description="'metric' or 'imperial'.")

    @model_validator(mode="after")
    def _one_of(self) -> "_WeatherArgs":
        has_city = bool(self.city and self.city.strip())
        has_coords = self.lat is not None and self.lon is not None
        if not (has_city or has_coords):
            raise ValueError("provide either `city` or both `lat` and `lon`")
        return self


async def _wttr(city: str, units: str) -> str | None:
    u = "u" if units == "imperial" else "m"
    url = f"https://wttr.in/{aiohttp.helpers.quote(city, safe='')}?format=3&{u}"
    try:
        await assert_public_url(url)
    except Exception:
        return None
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status >= 400:
                    return None
                text = (await resp.text()).strip()
                return text or None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None


async def _open_meteo(lat: float, lon: float, units: str) -> str | None:
    temp_unit = "fahrenheit" if units == "imperial" else "celsius"
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current_weather=true&temperature_unit={temp_unit}"
    )
    try:
        await assert_public_url(url)
    except Exception:
        return None
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status >= 400:
                    return None
                data: dict[str, Any] = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
    cw = data.get("current_weather") or {}
    if not cw:
        return None
    unit = "°F" if units == "imperial" else "°C"
    return (
        f"({lat:.3f},{lon:.3f}) "
        f"{cw.get('temperature', '?')}{unit}, "
        f"wind {cw.get('windspeed', '?')} kph from {cw.get('winddirection', '?')}°"
    )


def weather_tool() -> Tool:
    async def _h(args: _WeatherArgs) -> str:
        if args.city:
            text = await _wttr(args.city, args.units)
            if text:
                return text
            return f"weather: wttr.in lookup for {args.city!r} failed"
        # coords path
        text = await _open_meteo(args.lat, args.lon, args.units)  # type: ignore[arg-type]
        if text:
            return text
        return "weather: open-meteo lookup failed"

    return FunctionTool(
        name="weather",
        description=(
            "Get the current weather. Provide either `city` or both `lat`+`lon`. "
            "Free, no-auth providers (wttr.in / open-meteo)."
        ),
        input_model=_WeatherArgs,
        handler=_h,
    )


__all__ = ["weather_tool"]
