from __future__ import annotations

import json
from typing import Any

from repositories.database import get_db_connection
from utils.text import normalize_text


class AnimeRepository:
    def upsert_guild_channels(
        self,
        guild_id: int,
        category_id: int | None,
        feed_channel_id: int | None,
        output_channel_id: int | None,
    ) -> None:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO guild_settings (guild_id, category_id, feed_channel_id, output_channel_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    category_id = excluded.category_id,
                    feed_channel_id = excluded.feed_channel_id,
                    output_channel_id = excluded.output_channel_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, category_id, feed_channel_id, output_channel_id),
            )
            conn.commit()

    def get_guild_settings(self, guild_id: int) -> dict[str, Any] | None:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return dict(row) if row else None

    def add_manual_anime(
        self,
        guild_id: int,
        nome: str,
        dia_semana: str,
        link: str | None,
        created_by: int | None,
    ) -> tuple[bool, int | None]:
        nome_normalizado = normalize_text(nome)
        with get_db_connection() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO animes (
                        guild_id, nome, nome_normalizado, dia_semana, link, status, fonte, created_by
                    ) VALUES (?, ?, ?, ?, ?, 'ativo', 'manual', ?)
                    """,
                    (guild_id, nome.strip(), nome_normalizado, dia_semana, link or "Sem link", created_by),
                )
                conn.commit()
                return True, cursor.lastrowid
            except Exception:
                return False, None

    def add_anilist_anime(
        self,
        guild_id: int,
        anime: dict[str, Any],
        dia_semana: str | None,
        created_by: int | None,
    ) -> tuple[bool, int | None]:
        title = anime["title"]
        nome_normalizado = normalize_text(title)
        genres = json.dumps(anime.get("genres") or [], ensure_ascii=False)
        with get_db_connection() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO animes (
                        guild_id, external_id, nome, nome_normalizado, dia_semana, link, status, fonte,
                        nota, popularidade, generos_json, temporada, ano, image_url,
                        next_episode, next_airing_at, created_by
                    ) VALUES (?, ?, ?, ?, ?, ?, 'ativo', 'anilist', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, nome_normalizado) DO UPDATE SET
                        external_id = excluded.external_id,
                        link = excluded.link,
                        fonte = excluded.fonte,
                        nota = excluded.nota,
                        popularidade = excluded.popularidade,
                        generos_json = excluded.generos_json,
                        temporada = excluded.temporada,
                        ano = excluded.ano,
                        image_url = excluded.image_url,
                        next_episode = excluded.next_episode,
                        next_airing_at = excluded.next_airing_at,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        guild_id,
                        anime.get("id"),
                        title,
                        nome_normalizado,
                        dia_semana,
                        anime.get("siteUrl"),
                        anime.get("averageScore"),
                        anime.get("popularity"),
                        genres,
                        anime.get("season"),
                        anime.get("seasonYear"),
                        anime.get("coverImage"),
                        anime.get("nextEpisode"),
                        anime.get("nextAiringAt"),
                        created_by,
                    ),
                )
                conn.commit()
                return True, cursor.lastrowid
            except Exception:
                return False, None

    def list_animes(self, guild_id: int, limit: int = 25, offset: int = 0) -> list[dict[str, Any]]:
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM animes
                WHERE guild_id = ?
                ORDER BY status = 'ativo' DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (guild_id, limit, offset),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def search_local_animes(self, guild_id: int, query: str, limit: int = 20) -> list[dict[str, Any]]:
        query_norm = f"%{normalize_text(query)}%"
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM animes
                WHERE guild_id = ? AND nome_normalizado LIKE ?
                ORDER BY status = 'ativo' DESC, nome ASC
                LIMIT ?
                """,
                (guild_id, query_norm, limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_anime_by_id(self, guild_id: int, anime_id: int) -> dict[str, Any] | None:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT * FROM animes WHERE guild_id = ? AND id = ?",
                (guild_id, anime_id),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_animes_by_day(self, guild_id: int, dia_semana: str) -> list[dict[str, Any]]:
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM animes
                WHERE guild_id = ? AND dia_semana = ? AND status = 'ativo'
                ORDER BY nome ASC
                """,
                (guild_id, dia_semana),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_active_anilist_animes(self, guild_id: int) -> list[dict[str, Any]]:
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM animes
                WHERE guild_id = ? AND status = 'ativo' AND fonte = 'anilist' AND external_id IS NOT NULL
                """,
                (guild_id,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def update_anime_status(self, guild_id: int, anime_id: int, status: str) -> bool:
        with get_db_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE animes
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ? AND id = ?
                """,
                (status, guild_id, anime_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_anilist_airing_data(
        self,
        guild_id: int,
        anime_id: int,
        next_episode: int | None,
        next_airing_at: int | None,
    ) -> None:
        with get_db_connection() as conn:
            conn.execute(
                """
                UPDATE animes
                SET next_episode = ?, next_airing_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ? AND id = ?
                """,
                (next_episode, next_airing_at, guild_id, anime_id),
            )
            conn.commit()

    def delete_anime(self, guild_id: int, anime_id: int) -> bool:
        with get_db_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM animes WHERE guild_id = ? AND id = ?",
                (guild_id, anime_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def create_alert_if_not_exists(
        self,
        guild_id: int,
        anime_id: int | None,
        external_id: int | None,
        alert_type: str,
        unique_key: str,
    ) -> bool:
        with get_db_connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO sent_alerts (guild_id, anime_id, external_id, alert_type, unique_key)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (guild_id, anime_id, external_id, alert_type, unique_key),
                )
                conn.commit()
                return True
            except Exception:
                return False

    def ignore_anime(self, guild_id: int, external_id: int, title: str, created_by: int | None) -> None:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO ignored_animes (guild_id, external_id, title, created_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, external_id) DO NOTHING
                """,
                (guild_id, external_id, title, created_by),
            )
            conn.commit()

    def get_ignored_external_ids(self, guild_id: int) -> set[int]:
        with get_db_connection() as conn:
            rows = conn.execute(
                "SELECT external_id FROM ignored_animes WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
        return {int(row["external_id"]) for row in rows}

    def set_preference(self, guild_id: int, genre: str, weight: int) -> None:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO server_preferences (guild_id, genre, weight)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, genre) DO UPDATE SET
                    weight = excluded.weight,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, genre.strip(), weight),
            )
            conn.commit()

    def list_preferences(self, guild_id: int) -> list[dict[str, Any]]:
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT genre, weight FROM server_preferences
                WHERE guild_id = ?
                ORDER BY weight DESC, genre ASC
                """,
                (guild_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        if "generos_json" in data and data["generos_json"]:
            try:
                data["generos"] = json.loads(data["generos_json"])
            except Exception:
                data["generos"] = []
        else:
            data["generos"] = []
        return data
