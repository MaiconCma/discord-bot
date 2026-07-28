from __future__ import annotations

import random
from typing import Any

import aiohttp

import config
from utils.text import clean_anilist_description


class AniListError(RuntimeError):
    pass


class AniListService:
    def __init__(self) -> None:
        self.api_url = getattr(config, "ANILIST_API_URL", "https://graphql.anilist.co")

    async def _request(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            async with session.post(self.api_url, json={"query": query, "variables": variables}) as response:
                if response.status >= 400:
                    body = await response.text()
                    raise AniListError(f"AniList retornou HTTP {response.status}: {body[:300]}")
                payload = await response.json()

        if payload.get("errors"):
            raise AniListError(str(payload["errors"][:1]))
        return payload["data"]

    async def search_anime(self, search: str, per_page: int = 5) -> list[dict[str, Any]]:
        query = """
        query ($search: String, $perPage: Int) {
          Page(page: 1, perPage: $perPage) {
            media(type: ANIME, search: $search, sort: POPULARITY_DESC) {
              id
              title { romaji english native }
              description(asHtml: false)
              genres
              averageScore
              popularity
              season
              seasonYear
              status
              episodes
              siteUrl
              coverImage { large }
              nextAiringEpisode { episode airingAt }
            }
          }
        }
        """
        data = await self._request(query, {"search": search, "perPage": per_page})
        return [self._normalize_media(item) for item in data["Page"]["media"]]

    async def get_anime_by_id(self, media_id: int) -> dict[str, Any] | None:
        query = """
        query ($id: Int) {
          Media(id: $id, type: ANIME) {
            id
            title { romaji english native }
            description(asHtml: false)
            genres
            averageScore
            popularity
            season
            seasonYear
            status
            episodes
            siteUrl
            coverImage { large }
            nextAiringEpisode { episode airingAt }
          }
        }
        """
        data = await self._request(query, {"id": media_id})
        media = data.get("Media")
        return self._normalize_media(media) if media else None

    async def get_season_radar(self, genre: str | None = None, per_page: int = 10) -> list[dict[str, Any]]:
        query = """
        query ($genre: String, $perPage: Int) {
          Page(page: 1, perPage: $perPage) {
            media(
              type: ANIME,
              status_in: [RELEASING, NOT_YET_RELEASED],
              genre_in: [$genre],
              sort: [POPULARITY_DESC, SCORE_DESC]
            ) {
              id
              title { romaji english native }
              description(asHtml: false)
              genres
              averageScore
              popularity
              season
              seasonYear
              status
              episodes
              siteUrl
              coverImage { large }
              nextAiringEpisode { episode airingAt }
            }
          }
        }
        """

        if genre:
            variables = {"genre": genre, "perPage": per_page}
            data = await self._request(query, variables)
        else:
            query_without_genre = """
            query ($perPage: Int) {
              Page(page: 1, perPage: $perPage) {
                media(
                  type: ANIME,
                  status_in: [RELEASING, NOT_YET_RELEASED],
                  sort: [POPULARITY_DESC, SCORE_DESC]
                ) {
                  id
                  title { romaji english native }
                  description(asHtml: false)
                  genres
                  averageScore
                  popularity
                  season
                  seasonYear
                  status
                  episodes
                  siteUrl
                  coverImage { large }
                  nextAiringEpisode { episode airingAt }
                }
              }
            }
            """
            data = await self._request(query_without_genre, {"perPage": per_page})

        return [self._normalize_media(item) for item in data["Page"]["media"]]

    async def recommend(self, genre: str | None, ignored_ids: set[int], per_page: int = 15) -> dict[str, Any] | None:
        candidates = await self.get_season_radar(genre=genre, per_page=per_page)
        candidates = [anime for anime in candidates if anime["id"] not in ignored_ids]
        if not candidates:
            return None

        candidates.sort(key=lambda item: ((item.get("averageScore") or 0), (item.get("popularity") or 0)), reverse=True)
        top_slice = candidates[: min(5, len(candidates))]
        return random.choice(top_slice)

    def _normalize_media(self, media: dict[str, Any]) -> dict[str, Any]:
        title_data = media.get("title") or {}
        title = title_data.get("english") or title_data.get("romaji") or title_data.get("native") or "Sem título"
        airing = media.get("nextAiringEpisode") or {}
        cover = media.get("coverImage") or {}
        return {
            "id": media.get("id"),
            "title": title,
            "romajiTitle": title_data.get("romaji"),
            "nativeTitle": title_data.get("native"),
            "description": clean_anilist_description(media.get("description")),
            "genres": media.get("genres") or [],
            "averageScore": media.get("averageScore"),
            "popularity": media.get("popularity"),
            "season": media.get("season"),
            "seasonYear": media.get("seasonYear"),
            "status": media.get("status"),
            "episodes": media.get("episodes"),
            "siteUrl": media.get("siteUrl"),
            "coverImage": cover.get("large"),
            "nextEpisode": airing.get("episode"),
            "nextAiringAt": airing.get("airingAt"),
        }
