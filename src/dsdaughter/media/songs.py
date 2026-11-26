"""歌库：她"唱歌"用的音频素材。

为什么不实时合成唱歌：TTS 只能说话。唱歌要用歌声转换（SVC）或歌声合成，提前离线做好，
存成 silk 后由她随机挑一首发出去（省算力、音质稳定、也符合"偶尔唱一下"的拟人感）。

目录约定：

    assets/songs/
      manifest.json
      your-song.silk      # 或 .wav，发送时自动转 silk
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class Song:
    song_id: str
    title: str
    file: Path
    duration: float = 0.0
    tags: tuple[str, ...] = ()


class SongLibrary:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.songs: list[Song] = []
        self._load()

    def _load(self) -> None:
        manifest = self.directory / "manifest.json"
        if not manifest.exists():
            return
        try:
            raw: Any = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("歌库清单读取失败", error=str(exc))
            return
        entries = raw.get("songs") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return
        for index, item in enumerate(entries):
            if not isinstance(item, dict):
                continue
            file_name = str(item.get("file") or "")
            path = self.directory / file_name
            if not file_name or not path.exists():
                continue
            self.songs.append(
                Song(
                    song_id=str(item.get("id") or f"song{index}"),
                    title=str(item.get("title") or path.stem),
                    file=path,
                    duration=float(item.get("duration") or 0.0),
                    tags=tuple(str(tag) for tag in item.get("tags") or ()),
                )
            )

    def pick(self, query: str = "", rng: random.Random | None = None) -> Song | None:
        if not self.songs:
            return None
        query = (query or "").strip()
        if query:
            matched = [
                song
                for song in self.songs
                if query in song.title or query in song.song_id or query in " ".join(song.tags)
            ]
            if matched:
                return (rng or random.Random()).choice(matched)
        return (rng or random.Random()).choice(self.songs)

    def titles(self) -> list[str]:
        return [song.title for song in self.songs]
