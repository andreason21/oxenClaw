"""weather tool — wttr.in + open-meteo lookups, SSRF-safe.

Mirrors openclaw `skills/weather`. The tool accepts either a city name
(routed through wttr) or numeric lat/lon (routed through open-meteo).
"""

from __future__ import annotations

from typing import Any

import aiohttp
from pydantic import BaseModel, Field, model_validator

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.tools_pkg._desc import hermes_desc
from oxenclaw.tools_pkg.web import assert_public_url


class _WeatherArgs(BaseModel):
    city: str | None = Field(None, description="City name (e.g. 'Seoul').")
    lat: float | None = Field(None, description="Latitude (-90..90).")
    lon: float | None = Field(None, description="Longitude (-180..180).")
    units: str = Field("metric", description="'metric' or 'imperial'.")

    @model_validator(mode="before")
    @classmethod
    def _absorb_aliases(cls, data: Any) -> Any:
        # Small local models (gemma/qwen/llama-3.2) emit weather tool
        # calls with drifted key names — `{location: "Seoul"}`,
        # `{place: "Seoul"}`, `{query: "Seoul weather"}`. Without this
        # absorber the strict `_one_of` validator below rejects with
        # "provide either `city` or both `lat` and `lon`" and the model
        # rarely retries with the right shape, so the tool effectively
        # never fires. Mirror _SearchArgs in tools_pkg/web.py.
        if not isinstance(data, dict):
            return data
        out = dict(data)
        # Single-value city aliases. We only fold onto `city` when the
        # caller didn't already set it (and didn't supply lat+lon), so
        # genuine `{city: "X"}` calls pass through untouched.
        has_city = bool(out.get("city"))
        has_coords = out.get("lat") is not None and out.get("lon") is not None
        if not has_city and not has_coords:
            # Direct city aliases — copy as-is.
            for alias in (
                "location",
                "place",
                "name",
                "city_name",
                "cityName",
                "where",
                "region",
                "area",
            ):
                v = out.get(alias)
                if isinstance(v, str) and v.strip():
                    out["city"] = v.strip()
                    break
        if not out.get("city") and not has_coords:
            # Freetext aliases (`query`, `q`, `topic`) carry phrasings
            # like "Suwon weather" / "서울 날씨" — strip the weather
            # keywords so wttr.in receives the bare location.
            for alias in ("query", "q", "topic"):
                v = out.get(alias)
                if not (isinstance(v, str) and v.strip()):
                    continue
                cleaned = v
                for kw in (
                    "의 날씨",
                    "날씨",
                    "weather in",
                    "weather for",
                    "weather",
                    "기온",
                    "temperature",
                    "forecast",
                ):
                    cleaned = cleaned.replace(kw, "")
                cleaned = cleaned.strip(" ,?!.")
                if cleaned:
                    out["city"] = cleaned
                    break
        # Latitude/longitude aliases.
        if out.get("lat") is None and out.get("latitude") is not None:
            out["lat"] = out["latitude"]
        if out.get("lon") is None:
            for alias in ("longitude", "lng", "long"):
                if out.get(alias) is not None:
                    out["lon"] = out[alias]
                    break
        # `coordinates: [lat, lon]` shape.
        coords = out.get("coordinates") or out.get("coords")
        if (
            out.get("lat") is None
            and out.get("lon") is None
            and isinstance(coords, list | tuple)
            and len(coords) == 2
        ):
            try:
                out["lat"] = float(coords[0])
                out["lon"] = float(coords[1])
            except (TypeError, ValueError):
                pass
        for alias in (
            "location",
            "place",
            "name",
            "city_name",
            "cityName",
            "where",
            "region",
            "area",
            "query",
            "q",
            "topic",
            "latitude",
            "longitude",
            "lng",
            "long",
            "coordinates",
            "coords",
        ):
            out.pop(alias, None)
        return out

    @model_validator(mode="after")
    def _one_of(self) -> _WeatherArgs:
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
        except (TimeoutError, aiohttp.ClientError):
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
        except (TimeoutError, aiohttp.ClientError):
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
        description=hermes_desc(
            "Get the current weather via wttr.in (city) or open-meteo (lat/lon). Free, no-auth.",
            when_use=[
                "the user asks about temperature / forecast / 날씨",
                "you have a place name or coordinates",
            ],
            when_skip=[
                "you don't have a location yet (ask the user)",
                "the user wants long-range climate stats (use web_search)",
            ],
            alternatives={"web_search": "non-current-weather questions"},
            notes="Provide either `city` OR both `lat`+`lon`, not neither.",
        ),
        input_model=_WeatherArgs,
        handler=_h,
    )


__all__ = ["weather_tool"]
