"""Minimal playlist ZIP site with spotDL. Only use for audio you may download."""
from __future__ import annotations
import os, re, sys, time, uuid, secrets, threading, tempfile, subprocess, zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
DATA = Path(os.getenv("DATA_DIR", str(Path(tempfile.gettempdir()) / "playlist-downloader")))
DATA.mkdir(parents=True, exist_ok=True)
PATTERN = re.compile(r"https://open\\.spotify\\.com/playlist/([A-Za-z0-9]{22})(?:[/?#].*)?")
app = FastAPI(title="Playlist Downloader", docs_url=None, redoc_url=None)
security = HTTPBasic(auto_error=False)
executor = ThreadPoolExecutor(max_workers=1)
jobs: dict[str, dict] = {}
lock = threading.RLock()

class Request(BaseModel):
    url: str

def secure(credentials: HTTPBasicCredentials | None = Depends(security)):
    user, password = os.getenv("SITE_USERNAME"), os.getenv("SITE_PASSWORD")
    if not user or not password: return
    if not credentials or not (secrets.compare_digest(credentials.username, user) and secrets.compare_digest(credentials.password, password)):
        raise HTTPException(401, "Autenticação necessária", headers={"WWW-Authenticate": 'Basic realm="Playlist"'})

def valid_url(raw: str) -> str | None:
    m = PATTERN.fullmatch(raw.strip())
    return f"https://open.spotify.com/playlist/{m[1]}" if m else None

def state(job: str, value: str, message: str):
    with lock:
        jobs[job].update(state=value, message=message, updated=time.time())

def process(job: str, url: str):
    folder = DATA / job
    music = folder / 'music'
    music.mkdir(parents=True, exist_ok=True)
    state(job, 'working', 'A procurar e descarregar as músicas…')
    try:
        result = subprocess.run([
            sys.executable, '-m', 'spotdl', 'download', url,
            '--format', 'mp3', '--output', str(music / '{artists} - {title}.{output-ext}'),
            '--playlist-retain-track-cover',
        ], cwd=folder, capture_output=True, text=True, errors='replace', timeout=7200)
        mp3s = sorted(music.rglob('*.mp3'))
        if not mp3s:
            error = (result.stdout + '\n' + result.stderr).strip()[-650:]
            raise RuntimeError('Não foi possível obter MP3. ' + error)
        archive = folder / 'playlist.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as z:
            for mp3 in mp3s:
                z.write(mp3, arcname=str(mp3.relative_to(music)))
        state(job, 'ready', f'ZIP preparado com {len(mp3s)} músicas.')
    except subprocess.TimeoutExpired:
        state(job, 'failed', 'O processamento excedeu o tempo disponível.')
    except Exception as e:
        state(job, 'failed', str(e)[:700])

@app.get('/')
def index(_: None = Depends(secure)):
    return FileResponse(HERE / 'static' / 'index.html', media_type='text/html')

@app.get('/health')
def health(): return {'ok': True}

@app.post('/api/jobs')
def start(item: Request, _: None = Depends(secure)):
    url = valid_url(item.url)
    if not url: raise HTTPException(400, 'Link de playlist inválido.')
    with lock:
        if sum(j['state'] in ('queued', 'working') for j in jobs.values()) >= 3:
            raise HTTPException(429, 'Existem downloads em curso. Tenta novamente mais tarde.')
        job = uuid.uuid4().hex
        jobs[job] = {'state': 'queued', 'message': 'A preparar…', 'updated': time.time()}
    executor.submit(process, job, url)
    return {'id': job}

@app.get('/api/jobs/{job}')
def progress(job: str, _: None = Depends(secure)):
    with lock:
        if job not in jobs: raise HTTPException(404, 'Download não encontrado.')
        return dict(jobs[job])

@app.get('/api/jobs/{job}/download')
def download(job: str, _: None = Depends(secure)):
    with lock:
        if job not in jobs or jobs[job]['state'] != 'ready':
            raise HTTPException(404, 'ZIP ainda não disponível.')
    p = DATA / job / 'playlist.zip'
    if not p.is_file(): raise HTTPException(404, 'ZIP não encontrado.')
    return FileResponse(p, filename='playlist.zip', media_type='application/zip')
