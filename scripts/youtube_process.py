"""Process only YouTube videos for which the submitter confirmed download permission."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile
from process import api, post, publish_progress, MAX_ZIP, CHUNK

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

def main() -> int:
    jobid = os.environ["JOB_ID"]
    work = Path("run_data") / jobid
    music = work / "mp3"
    music.mkdir(parents=True, exist_ok=True)
    try:
        selection = api("GET", f"/api/processor/{jobid}/selection")["tracks"]
        if not 1 <= len(selection) <= 250:
            raise RuntimeError("Seleção inválida.")
        states = [{"id": t["id"], "title": t["title"], "artist": "",
                   "state": "pending", "progress": 0} for t in selection]
        publish_progress(jobid, "downloading", 0, tracks=states)
        for index, entry in enumerate(states):
            if not VIDEO_ID.fullmatch(entry["id"]):
                entry["state"] = "failed"
                continue
            entry["state"] = "downloading"
            publish_progress(jobid, "downloading", index, tracks=states)
            # Restrict the extractor to YouTube and never bypass authentication/DRM.
            # The request is submitted only after the user has affirmed permission.
            command = [
                sys.executable, "-m", "yt_dlp",
                "--no-playlist", "--no-overwrites", "--no-progress",
                "--max-filesize", "100M", "--socket-timeout", "20",
                "--retries", "2", "--fragment-retries", "2",
                "--extract-audio", "--audio-format", "mp3",
                "--audio-quality", "2",
                "--output", str(music / (entry["id"] + ".%(ext)s")),
                "https://www.youtube.com/watch?v=" + entry["id"]
            ]
            try:
                p = subprocess.run(command, capture_output=True, text=True,
                                   timeout=90, errors="replace")
                obtained = music / (entry["id"] + ".mp3")
                if p.returncode == 0 and obtained.exists() and obtained.stat().st_size > 50000:
                    entry["state"] = "done"
                    entry["progress"] = 100
                else:
                    entry["state"] = "failed"
                    print("Video failed:", entry["id"], (p.stderr or "")[-280:], flush=True)
            except subprocess.TimeoutExpired:
                entry["state"] = "failed"
                print("Video timed out:", entry["id"], flush=True)
            publish_progress(jobid, "downloading",
                             sum(x["state"] == "done" for x in states), tracks=states)
        obtained = sorted(music.glob("*.mp3"))
        archive = work / "playlist.zip"
        # Include a human-readable report even if no track was available.
        report = {
            "total": len(states),
            "completed": len(obtained),
            "tracks": states,
            "notice": "Only content explicitly authorized for download may be obtained."
        }
        publish_progress(jobid, "packaging", len(obtained), tracks=states)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as z:
            for mp3 in obtained:
                z.write(mp3, arcname=mp3.name)
            z.writestr("relatorio.json", json.dumps(report, ensure_ascii=False, indent=2))
        total = archive.stat().st_size
        if total > MAX_ZIP:
            raise RuntimeError("O ZIP excede o limite de 1,5 GB.")
        publish_progress(jobid, "uploading", len(obtained), total, tracks=states)
        post(f"/api/processor/{jobid}/start")
        with archive.open("rb") as reader:
            part = 0
            while True:
                data = reader.read(CHUNK)
                if not data:
                    break
                part += 1
                api("PUT", f"/api/processor/{jobid}/part/{part}",
                    data, content_type="application/octet-stream")
        post(f"/api/processor/{jobid}/finish")
        print(f"Job complete: {len(obtained)} out of {len(states)} MP3s.", flush=True)
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
