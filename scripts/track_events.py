"""Attach read-only progress callbacks to the existing spotDL CLI."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

from spotdl.download.downloader import Downloader
from spotdl import console_entry_point

target = Path(os.environ["PLAYLIST_TRACKS_FILE"])
lock = threading.RLock()
rows: dict[str, dict] = {}
last_write = 0.0

def key(song):
    identity = str(getattr(song, "url", "") or getattr(song, "song_id", "") or getattr(song, "display_name", ""))
    position = str(getattr(song, "list_position", "") or "")
    return hashlib.sha256((identity + "|" + position).encode("utf-8")).hexdigest()[:24]

def row(song):
    k = key(song)
    if k not in rows:
        artist = ", ".join(getattr(song, "artists", []) or []) or str(getattr(song, "artist", ""))
        rows[k] = {
            "id": k,
            "title": str(getattr(song, "name", "") or getattr(song, "display_name", ""))[:220],
            "artist": artist[:220],
            "state": "pending",
            "progress": 0,
        }
    return rows[k]

def write_snapshot(force=False):
    global last_write
    if not force and time.monotonic() - last_write < 0.7:
        return
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(list(rows.values()), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, target)
    last_write = time.monotonic()

def callback(tracker, message):
    try:
        with lock:
            item = row(tracker.song)
            msg = str(message).lower()
            previous = item["state"]
            if msg == "error":
                state = "failed"
            elif msg in ("done", "skipped"):
                state = "verifying"
            elif "convert" in msg:
                state = "converting"
            elif "metadata" in msg or "embedding" in msg:
                state = "metadata"
            elif "downloading" in msg:
                state = "downloading"
            else:
                state = "searching"
            item["state"] = state
            item["progress"] = max(0, min(99, int(float(tracker.progress or 0))))
            write_snapshot(force=state != previous)
    except Exception as e:
        print("Track progress callback:", str(e)[:200], file=sys.stderr)

original = Downloader.download_multiple_songs

def track_downloads(self, songs, *args, **kwargs):
    with lock:
        for song in songs:
            row(song)
        write_snapshot(force=True)
    handler = self.progress_handler
    previous = handler.update_callback
    def combined(tracker, message):
        callback(tracker, message)
        if previous:
            previous(tracker, message)
    handler.update_callback = combined
    try:
        results = original(self, songs, *args, **kwargs)
        with lock:
            for song, path in results:
                item = row(song)
                ok = path is not None and Path(path).is_file()
                item.update(state="done" if ok else "failed", progress=100 if ok else 0)
            for item in rows.values():
                if item["state"] not in ("done", "failed"):
                    item.update(state="failed", progress=0)
            write_snapshot(force=True)
        return results
    finally:
        handler.update_callback = previous

# A completed download returns its verified path, so we can report it immediately
# instead of waiting for all the parallel tasks to finish.
original_pool = Downloader.pool_download
async def track_pool(self, song, *args, **kwargs):
    result = await original_pool(self, song, *args, **kwargs)
    try:
        with lock:
            track, path = result
            item = row(track)
            ok = path is not None and Path(path).is_file()
            item.update(state="done" if ok else "failed", progress=100 if ok else 0)
            write_snapshot(force=True)
    except Exception as e:
        print("Track result callback:", str(e)[:200], file=sys.stderr)
    return result

Downloader.download_multiple_songs = track_downloads
Downloader.pool_download = track_pool

if __name__ == "__main__":
    console_entry_point()
