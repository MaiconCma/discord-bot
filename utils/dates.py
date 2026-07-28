from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import config

DIAS_VALIDOS = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]

DAY_TRANSLATIONS = {
    "monday": "segunda",
    "tuesday": "terca",
    "wednesday": "quarta",
    "thursday": "quinta",
    "friday": "sexta",
    "saturday": "sabado",
    "sunday": "domingo",
}


def get_timezone() -> ZoneInfo:
    return ZoneInfo(getattr(config, "ANIME_TIMEZONE", "America/Bahia"))


def now_local() -> datetime:
    return datetime.now(get_timezone())


def today_weekday_pt() -> str:
    return DAY_TRANSLATIONS[now_local().strftime("%A").lower()]


def weekday_from_timestamp(timestamp: int | None) -> str | None:
    if not timestamp:
        return None
    dt = datetime.fromtimestamp(timestamp, tz=get_timezone())
    return DAY_TRANSLATIONS[dt.strftime("%A").lower()]


def format_timestamp(timestamp: int | None) -> str:
    if not timestamp:
        return "Não informado"
    dt = datetime.fromtimestamp(timestamp, tz=get_timezone())
    return dt.strftime("%d/%m/%Y %H:%M")
