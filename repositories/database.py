from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import config


def get_db_connection() -> sqlite3.Connection:
    db_path = getattr(config, "ANIME_DB_PATH", "data/animes.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_db_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                category_id INTEGER,
                feed_channel_id INTEGER,
                output_channel_id INTEGER,
                auto_recommend_enabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS animes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                external_id INTEGER,
                nome TEXT NOT NULL,
                nome_normalizado TEXT NOT NULL,
                dia_semana TEXT,
                link TEXT,
                status TEXT NOT NULL DEFAULT 'ativo',
                fonte TEXT NOT NULL DEFAULT 'manual',
                nota INTEGER,
                popularidade INTEGER,
                generos_json TEXT,
                temporada TEXT,
                ano INTEGER,
                image_url TEXT,
                next_episode INTEGER,
                next_airing_at INTEGER,
                created_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, nome_normalizado)
            );

            CREATE TABLE IF NOT EXISTS sent_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                anime_id INTEGER,
                external_id INTEGER,
                alert_type TEXT NOT NULL,
                unique_key TEXT NOT NULL UNIQUE,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(anime_id) REFERENCES animes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ignored_animes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                external_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                created_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, external_id)
            );

            CREATE TABLE IF NOT EXISTS server_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                genre TEXT NOT NULL,
                weight INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, genre)
            );
            """
        )
        conn.commit()
