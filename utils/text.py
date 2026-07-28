from __future__ import annotations

import html
import re
import unicodedata


def normalize_text(value: str) -> str:
    """Normaliza texto para comparar nomes sem depender de maiúsculas, acentos e espaços extras."""
    value = value.strip().lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_anilist_description(value: str | None, max_length: int = 700) -> str:
    if not value:
        return "Sem descrição disponível."

    value = html.unescape(value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()

    if len(value) > max_length:
        return value[: max_length - 3].rstrip() + "..."
    return value


def safe_join(items: list[str] | None, fallback: str = "Não informado") -> str:
    if not items:
        return fallback
    return ", ".join(items)
