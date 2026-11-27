"""Resolve a whole playlist against explicitly downloadable SoundCloud originals.

No streaming-format fallback, YouTube extraction, credentials or access bypass.
The user must have rights to the recordings submitted.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import io
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
from urllib.parse import urlsplit, parse_qs
import zipfile
from yt_dlp import YoutubeDL
from process import api, post, publish_progress, MAX_ZIP, CHUNK

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
WORKERS = 4
ROOT = Path(__file__).resolve().parents[1]

def norm(value):
    s = unicodedata.normalize("NFKD", str(value or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s*[\[(](?:official (?:music )?(?:video|audio)|music video|lyric video|lyrics|official video clip|hd|4k|remastered|video oficial)[\])]", "", s, flags=re.I)
    return " ".join(re.findall(r"[a-z0-9]+", s))

def channel_name(value):
    s = re.sub(r"\s*-\s*Topic$", "", str(value or ""), flags=re.I)
    s = re.sub(r"(?:VEVO|\s+official|\s+music)$", "", s, flags=re.I)
    return norm(s)

def default_channels():
    try:
        page = (ROOT / "static" / "youtube-import.html").read_text(encoding="utf-8")
        line = next(line for line in page.splitlines() if line.startswith("const DEFAULT_CSV="))
        source = json.loads(line[len("const DEFAULT_CSV="):].rstrip(";"))
        out = {}
        for row in csv.DictReader(io.StringIO(source)):
            u = urlsplit(row.get("link", ""))
            video = parse_qs(u.query).get("v", [""])[0]
            if VIDEO_ID.fullmatch(video):
                out[video] = row.get("canal", "")
        return out
    except (OSError, ValueError, StopIteration):
        return {}

def query_for(title, channel):
    if " - " in title or " – " in title:
        return title
    return f"{channel} {title}".strip()

def matches(request_title, request_channel, info):
    raw = str(request_title)
    bits = re.split(r"\s+[-–—]\s+", raw, maxsplit=1)
    artist = norm(bits[0]) if len(bits) == 2 else channel_name(request_channel)
    song = norm(bits[-1])
    candidate = norm(info.get("title"))
    uploader = channel_name(info.get("uploader") or info.get("artist"))
    if not artist or not song:
        return False
    # Keep live/remix/acoustic descriptors: never substitute an unrelated recording.
    expected = norm(artist + " " + song)
    return candidate == expected or (candidate == song and uploader == artist)

def soundcloud_url(value):
    try:
        u = urlsplit(str(value))
        return u.scheme == "https" and u.hostname in ("soundcloud.com", "www.soundcloud.com") and not u.username and not u.password
    except ValueError:
        return False

def clean_filename(title):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    return s[:120] or "track"

def find_and_download(track, music):
    video = track["id"]
    title = track["title"]
    channel = track.get("channel", "")
    result = {"id": video, "title": title, "artist": channel, "state": "failed",
              "progress": 0, "source": "", "reason": "Sem original autorizado e correspondente disponível."}
    if not VIDEO_ID.fullmatch(video):
        return result
    query = query_for(title, channel)
    search_options = {
        "quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
        "skip_download": True, "ignoreerrors": True, "socket_timeout": 12,
        "retries": 1, "playlistend": 5, "noplaylist": False,
    }
    dl_options = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        # The SoundCloud extractor adds "download" only when the original
        # file is explicitly available. Do not use bestaudio or stream fallbacks.
        "format": "download",
        "socket_timeout": 15, "retries": 1, "fragment_retries": 1,
        "max_filesize": 80 * 1024 * 1024,
        "outtmpl": str(music / (clean_filename(title) + " [" + video + "].%(ext)s")),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    }
    try:
        with YoutubeDL(search_options) as ydl:
            found = ydl.extract_info("scsearch5:" + query, download=False) or {}
            candidates = list(found.get("entries") or [])
        for candidate in candidates:
            if not candidate:
                continue
            url = candidate.get("webpage_url") or candidate.get("url")
            if not soundcloud_url(url):
                continue
            # Avoid a full extraction when a flat result clearly doesn't match.
            if candidate.get("uploader") and not matches(title, channel, candidate):
                continue
            try:
                with YoutubeDL(dl_options) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if not info or not matches(title, channel, info):
                        continue
                    if not any(f.get("format_id") == "download" for f in info.get("formats", [])):
                        continue
                    status = ydl.download([url])
                expected_path = music / (clean_filename(title) + " [" + video + "].mp3")
                if status == 0 and expected_path.is_file() and expected_path.stat().st_size > 50000:
                    result.update(state="done", progress=100, source=url, reason="")
                    return result
            except Exception as error:
                print("Unavailable original:", video, str(error)[:170], flush=True)
    except Exception as error:
        result["reason"] = "Pesquisa indisponível: " + str(error)[:180]
    return result

def main():
    jobid = os.environ["JOB_ID"]
    work = Path("run_data") / jobid
    music = work / "mp3"
    music.mkdir(parents=True, exist_ok=True)
    try:
        tracks = api("GET", f"/api/processor/{jobid}/selection")["tracks"]
        if not 1 <= len(tracks) <= 250:
            raise RuntimeError("Seleção inválida.")
        channels = default_channels()
        for t in tracks:
            t["channel"] = t.get("channel") or channels.get(t["id"], "")
        rows = [{"id": t["id"], "title": t["title"], "artist": t["channel"],
                 "state": "pending", "progress": 0} for t in tracks]
        by_id = {r["id"]: r for r in rows}
        publish_progress(jobid, "downloading", 0, tracks=rows)
        done = 0
        last = time.monotonic()
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            future_map = {pool.submit(find_and_download, t, music): t for t in tracks}
            for future in as_completed(future_map):
                t = future_map[future]
                try:
                    outcome = future.result()
                except Exception as error:
                    outcome = {"state": "failed", "progress": 0, "reason": str(error)[:200]}
                by_id[t["id"]].update(outcome)
                done += int(outcome.get("state") == "done")
                if time.monotonic() - last >= 4:
                    publish_progress(jobid, "downloading", done, tracks=rows)
                    last = time.monotonic()
        publish_progress(jobid, "downloading", done, tracks=rows)
        files = sorted(music.glob("*.mp3"))
        if not files:
            raise RuntimeError("Nenhum MP3 disponível para download autorizado nas fontes pesquisadas. Nenhum ZIP vazio foi criado.")
        archive = work / "playlist.zip"
        report = {"total": len(rows), "completed": len(files), "tracks": rows}
        publish_progress(jobid, "packaging", len(files), tracks=rows)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as z:
            for file in files:
                z.write(file, arcname=file.name)
            z.writestr("relatorio.json", json.dumps(report, ensure_ascii=False, indent=2))
        total = archive.stat().st_size
        if total > MAX_ZIP:
            raise RuntimeError("O ZIP excede o limite de 1,5 GB.")
        publish_progress(jobid, "uploading", len(files), total, tracks=rows)
        post(f"/api/processor/{jobid}/start")
        with archive.open("rb") as reader:
            part = 0
            while True:
                data = reader.read(CHUNK)
                if not data:
                    break
                part += 1
                if part > 200:
                    raise RuntimeError("Limite de partes excedido.")
                api("PUT", f"/api/processor/{jobid}/part/{part}", data, content_type="application/octet-stream")
        post(f"/api/processor/{jobid}/finish")
        print(f"Job complete: {len(files)} out of {len(rows)} MP3s.", flush=True)
        return 0
    except Exception as error:
        print("ERROR", str(error), file=sys.stderr, flush=True)
        try:
            post(f"/api/processor/{jobid}/error", {"error": str(error)[:320]})
        except Exception as secondary:
            print("Could not report failure:", secondary, file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
