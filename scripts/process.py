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
    headers={"authorization":"Bearer "+token(), "accept":"application/json"}
    if data is not None:
        headers["content-type"]=content_type
    request=urllib.request.Request(BASE+path, data=data, headers=headers, method=method)
    last=None
    for retry in range(3):
        try:
            with urllib.request.urlopen(request,timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail=error.read().decode("utf-8",errors="replace")[:800]
            last=RuntimeError(f"Cloudflare {error.code}: {detail}")
            if error.code < 500: break
        except (TimeoutError,urllib.error.URLError) as error:
            last=error
        time.sleep(2*(retry+1))
    raise last or RuntimeError("Pedido ao Cloudflare falhou")

def post(path: str, body: dict | None = None):
    return api("POST",path,json.dumps(body or {}).encode("utf-8"))

def main() -> int:
    pending=api("GET","/api/processor/next").get("job")
    if not pending:
        print("No queued playlists; exiting.")
        return 0
    jobid=pending["id"]
    work=Path("run_data") / jobid
    music=work / "mp3"
    music.mkdir(parents=True, exist_ok=True)
    print("Processing job",jobid,flush=True)
    try:
        cmd=[
            sys.executable,"-m","spotdl","download",pending["url"],
            "--format","mp3",
            "--playlist-retain-track-cover",
            "--output",str(music / "{artists} - {title}.{output-ext}"),
        ]
        # Abort if a Spotify playlist or audio source cannot be accessed.
        finished=subprocess.run(cmd,capture_output=True,text=True,errors="replace",timeout=6500)
        print("spotDL exit:",finished.returncode,flush=True)
        if finished.stdout: print(finished.stdout[-3500:],flush=True)
        if finished.stderr: print(finished.stderr[-3500:],flush=True)
        songs=sorted(music.rglob("*.mp3"))
        if not songs:
            raise RuntimeError("Não foi possível obter MP3 desta playlist. O Spotify ou a fonte de áudio podem ter limitado o acesso.")
        outfile=work / "playlist.zip"
        with zipfile.ZipFile(outfile,"w",zipfile.ZIP_STORED) as z:
            for musicfile in songs:
                z.write(musicfile,arcname=musicfile.relative_to(music).as_posix())
        total=outfile.stat().st_size
        if total>MAX_ZIP:
            raise RuntimeError("O ZIP excede o limite de 1,5 GB.")
        print("Sending",len(songs),"MP3 files; ZIP bytes:",total,flush=True)
        post(f"/api/processor/{jobid}/start")
        with outfile.open("rb") as reader:
            part=0
            while True:
                content=reader.read(CHUNK)
                if not content:
                    break
                part+=1
                if part>200:
                    raise RuntimeError("A playlist excede o máximo de partes de transferência.")
                api("PUT",f"/api/processor/{jobid}/part/{part}",content,content_type="application/octet-stream")
                print(f"Uploaded part {part}",flush=True)
        post(f"/api/processor/{jobid}/finish")
        print("Job completed.",flush=True)
        return 0
    except Exception as exc:
        print("ERROR",str(exc),file=sys.stderr,flush=True)
        try:
            post(f"/api/processor/{jobid}/error",{"error":str(exc)[:330]})
        except Exception as err:
            print("Could not report error:",err,file=sys.stderr,flush=True)
        return 1

if __name__=="__main__":
    raise SystemExit(main())
