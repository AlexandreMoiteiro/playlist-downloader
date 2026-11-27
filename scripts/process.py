"""GitHub Actions worker. Retrieves one job from Cloudflare using GitHub OIDC."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from curl_cffi import requests as browser_requests
import zipfile

BASE = "https://playlist-studio.alexandre-moiteiro.workers.dev"
AUDIENCE = "playlist-studio"
CHUNK = 8 * 1024 * 1024
MAX_ZIP = 1500 * 1024 * 1024

def token() -> str:
    address = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    address += ("&" if "?" in address else "?") + "audience=" + urllib.parse.quote(AUDIENCE)
    request = urllib.request.Request(
        address,
        headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
    )
    with urllib.request.urlopen(request, timeout=30) as r:
        return json.load(r)["value"]

def api(method: str, path: str, data: bytes | None = None, *, content_type: str = "application/json"):
    headers={
        "authorization": "Bearer " + token(),
        "accept": "application/json",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    }
    if data is not None:
        headers["content-type"]=content_type
    last=None
    for retry in range(3):
        try:
            response=browser_requests.request(
                method, BASE + path, data=data, headers=headers,
                timeout=90, impersonate="chrome"
            )
            if response.status_code < 400:
                return response.json()
            detail=response.text[:800]
            last=RuntimeError(f"Cloudflare {response.status_code}: {detail}")
            if response.status_code < 500:
                break
        except Exception as error:
            last=error
        time.sleep(2*(retry+1))
    raise last or RuntimeError("Pedido ao Cloudflare falhou")

def post(path: str, body: dict | None = None):
    return api("POST",path,json.dumps(body or {}).encode("utf-8"))

def publish_progress(job_id: str, phase: str, downloaded: int, total_bytes: int = 0, tracks=None) -> bool:
    try:
        payload = {"phase": phase, "downloaded": downloaded, "total_bytes": total_bytes}
        if tracks is not None:
            payload["tracks"] = tracks
        post(f"/api/processor/{job_id}/progress", payload)
        return True
    except Exception as err:
        print("Progress report failed:", str(err)[:240], flush=True)
        return False

def read_tracks(path):
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
        return items if isinstance(items, list) else None
    except (OSError, ValueError):
        return None

def main() -> int:
    pending = (
        {"id": os.environ["JOB_ID"], "url": os.environ["PLAYLIST_URL"]}
        if os.getenv("JOB_ID") and os.getenv("PLAYLIST_URL")
        else api("GET", "/api/processor/next").get("job")
    )
    if not pending:
        print("No queued playlists; exiting.")
        return 0
    jobid = pending["id"]
    work = Path("run_data") / jobid
    music = work / "mp3"
    music.mkdir(parents=True, exist_ok=True)
    print("Processing job", jobid, flush=True)
    try:
        publish_progress(jobid, "downloading", 0)
        cmd = [
            sys.executable, str(Path(__file__).with_name("track_events.py")), "download", pending["url"],
            "--format", "mp3",
            "--audio", "soundcloud", "bandcamp", "youtube-music", "youtube",
            "--playlist-retain-track-cover",
            "--output", str(music / "{artists} - {title}.{output-ext}"),
        ]
        logfile = work / "spotdl-output.log"
        tracksfile = work / "tracks.json"
        env = dict(os.environ, PLAYLIST_TRACKS_FILE=str(tracksfile.resolve()))
        with logfile.open("w", encoding="utf-8") as output:
            proc = subprocess.Popen(cmd, stdout=output, stderr=subprocess.STDOUT, env=env)
            begun = time.monotonic()
            observed: dict[str, int] = {}
            reported = 0
            previous_snapshot = None
            while proc.poll() is None:
                if time.monotonic() - begun > 6500:
                    proc.kill()
                    proc.wait()
                    raise RuntimeError("O processamento demorou demasiado tempo.")
                # Only count MP3 files whose size has stopped changing.
                sizes = {str(p): p.stat().st_size for p in music.rglob("*.mp3") if p.is_file()}
                settled = sum(1 for path, size in sizes.items()
                              if size > 100_000 and observed.get(path) == size)
                observed = sizes
                tracks = read_tracks(tracksfile)
                count = sum(t.get("state") == "done" for t in tracks) if tracks is not None else settled
                snapshot = json.dumps(tracks, ensure_ascii=False) if tracks is not None else None
                if count != reported or snapshot != previous_snapshot:
                    if publish_progress(jobid, "downloading", count, tracks=tracks):
                        if count != reported:
                            print(f"{count} MP3 files obtained", flush=True)
                        reported = count
                        previous_snapshot = snapshot
                time.sleep(5)
            exit_code = proc.wait()
        tracks = read_tracks(tracksfile)
        songs = sorted(music.rglob("*.mp3"))
        if tracks is not None:
            # If the process exits abnormally, do not mark unfinished tracks successful.
            for item in tracks:
                if item.get("state") not in ("done", "failed"):
                    item.update(state="failed", progress=0)
            publish_progress(jobid, "downloading", len(songs), tracks=tracks)
        output_text = logfile.read_text(encoding="utf-8", errors="replace")
        print("spotDL exit:", exit_code, flush=True)
        print(output_text[-2500:], flush=True)
        if not songs:
            if "AudioProviderError: YT-DLP download error" in output_text:
                raise RuntimeError(
                    "A playlist foi identificada, mas não foi possível obter MP3 das fontes externas. "
                    "O YouTube exige confirmação humana neste servidor GitHub e as outras fontes "
                    "não disponibilizaram gravações utilizáveis. Nenhum ZIP foi criado."
                )
            raise RuntimeError(
                "A playlist foi identificada, mas não foram encontrados MP3 descarregáveis "
                "nas fontes disponíveis. Nenhum ZIP foi criado."
            )
        publish_progress(jobid, "packaging", len(songs), tracks=tracks)
        outfile = work / "playlist.zip"
        with zipfile.ZipFile(outfile, "w", zipfile.ZIP_STORED) as archive:
            for musicfile in songs:
                archive.write(musicfile, arcname=musicfile.relative_to(music).as_posix())
        total = outfile.stat().st_size
        if total > MAX_ZIP:
            raise RuntimeError("O ZIP excede o limite de 1,5 GB.")
        print("Sending", len(songs), "MP3 files; ZIP bytes:", total, flush=True)
        publish_progress(jobid, "uploading", len(songs), total, tracks=tracks)
        post(f"/api/processor/{jobid}/start")
        with outfile.open("rb") as reader:
            part = 0
            while True:
                content = reader.read(CHUNK)
                if not content:
                    break
                part += 1
                if part > 200:
                    raise RuntimeError("A playlist excede o máximo de partes de transferência.")
                api("PUT", f"/api/processor/{jobid}/part/{part}",
                    content, content_type="application/octet-stream")
                print(f"Uploaded part {part}", flush=True)
        post(f"/api/processor/{jobid}/finish")
        print("Job completed.", flush=True)
        return 0
    except Exception as exc:
        print("ERROR", str(exc), file=sys.stderr, flush=True)
        try:
            post(f"/api/processor/{jobid}/error", {"error": str(exc)[:330]})
        except Exception as err:
            print("Could not report error:", err, file=sys.stderr, flush=True)
        return 1

if __name__=="__main__":
    raise SystemExit(main())
