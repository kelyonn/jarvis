"""Weather from Open-Meteo (free, no API key)."""

from __future__ import annotations

from jarvis_core.briefing.env import Env, Place, resolve_place
from jarvis_core.briefing.models import Weather

WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "violent showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


def condition(code: int) -> str:
    return WMO.get(int(code), "unsettled")


def parse_forecast(place: Place, data: dict) -> Weather:
    cur = data["current"]
    hourly = data.get("hourly", {})
    daily = data["daily"]
    times: list[str] = hourly.get("time", [])
    probs: list[int] = hourly.get("precipitation_probability", [])
    now_hour = cur["time"][:13]  # 'YYYY-MM-DDTHH'
    start = next((i for i, t in enumerate(times) if t[:13] == now_hour), 0)
    nxt = [(off, int(probs[start + off] or 0)) for off in range(1, 7) if start + off < len(probs)]
    today_times = [i for i, t in enumerate(times) if t[:10] == cur["time"][:10] and i >= start]
    remaining = [int(probs[i] or 0) for i in today_times if i < len(probs)]
    rain_today = max(remaining) if remaining else int((daily.get("precipitation_probability_max") or [0])[0] or 0)
    sunset = ""
    if daily.get("sunset"):
        sunset = daily["sunset"][0][11:16]
    w = Weather(
        city=place.city,
        temp_c=float(cur["temperature_2m"]),
        feels_c=float(cur.get("apparent_temperature", cur["temperature_2m"])),
        condition=condition(cur.get("weather_code", 0)),
        high_c=float(daily["temperature_2m_max"][0]),
        low_c=float(daily["temperature_2m_min"][0]),
        rain_chance_pct=rain_today,
        rain_next_hours=nxt,
        wind_kmh=float(cur.get("wind_speed_10m", 0.0)),
        sunset=sunset,
    )
    uv = daily.get("uv_index_max") or []
    if uv and uv[0] is not None:
        w.uv_max = float(uv[0])
    if len(daily["temperature_2m_max"]) > 1:
        w.tomorrow_high_c = float(daily["temperature_2m_max"][1])
        w.tomorrow_low_c = float(daily["temperature_2m_min"][1])
        w.tomorrow_condition = condition((daily.get("weather_code") or [0, 0])[1])
        w.tomorrow_rain_pct = int((daily.get("precipitation_probability_max") or [0, 0])[1] or 0)
    return w


async def collect(env: Env, city: str | None = None) -> Weather:
    from jarvis_core.briefing.env import geocode

    place = (await geocode(env, city)) if city else None
    if place is None:
        place = await resolve_place(env)
    data, _stale = await env.get_json(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": round(place.lat, 3),
            "longitude": round(place.lon, 3),
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "hourly": "precipitation_probability",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code,sunset,uv_index_max",
            "timezone": "auto",
            "forecast_days": 2,
        },
        cache_key=f"weather2:{place.lat:.2f},{place.lon:.2f}",
        ttl_s=600,
    )
    w = parse_forecast(place, data)
    try:  # air quality is a nicety: never let it fail the weather
        aq, _ = await env.get_json(
            "https://air-quality-api.open-meteo.com/v1/air-quality",
            params={"latitude": round(place.lat, 3), "longitude": round(place.lon, 3), "current": "us_aqi"},
            cache_key=f"aqi:{place.lat:.2f},{place.lon:.2f}",
            ttl_s=1800,
        )
        val = (aq.get("current") or {}).get("us_aqi")
        w.aqi = int(val) if val is not None else None
    except Exception:  # noqa: BLE001
        pass
    return w


def describe(w: Weather) -> str:
    """One spoken-friendly sentence group (also the template fallback)."""
    s = f"{w.temp_c:.0f} degrees and {w.condition} in {w.city}"
    if abs(w.feels_c - w.temp_c) >= 3:
        s += f", feeling like {w.feels_c:.0f}"
    s += f". A high of {w.high_c:.0f} and a low of {w.low_c:.0f}."
    if w.rain_chance_pct >= 40:
        s += f" There's a {w.rain_chance_pct} percent chance of rain today."
    if w.uv_max is not None and w.uv_max >= 6:
        s += f" The UV index peaks at {w.uv_max:.0f}, so sunscreen is wise."
    if w.aqi is not None and w.aqi >= 101:
        s += f" Air quality is {'unhealthy' if w.aqi >= 151 else 'poor'}, an index of {w.aqi}."
    return s

