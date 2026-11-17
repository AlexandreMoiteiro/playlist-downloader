# Playlist Downloader

Site simples: um link de uma playlist pública do Spotify e um botão para obter um ZIP dos MP3 encontrados.

O projeto utiliza [spotDL](https://spotdl.github.io/spotify-downloader/), que identifica faixas através do Spotify e procura gravações noutras fontes, com FFmpeg para processar o áudio e metadados. **Não descarrega os ficheiros de áudio dos servidores do Spotify.** Usa-o apenas para gravações que tenhas autorização para descarregar. Não é garantido que todas as músicas existam ou correspondam à gravação original.

## Como executar o site localmente

Precisas de Python 3.11+ e FFmpeg instalados.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```

Abre http://127.0.0.1:8000. O link da playlist de teste já está preenchido.

## GitHub Actions, sem servidor próprio

1. Abre o separador **Actions** deste repositório.
2. Escolhe **Download playlist to ZIP** e **Run workflow**.
3. Cola o link da playlist e executa.
4. No fim, descarrega o ZIP da secção **Artifacts**.

O workflow é independente do site e é iniciado manualmente. **O GitHub Actions não faz do site um servidor permanente nem permite um botão público de download imediato.** Para pôr online o site com o botão funcional, o backend terá de estar alojado num servidor que execute Python, spotDL e FFmpeg.

O GitHub Pages, por si só, aloja apenas a interface e não executa esse backend. O Cloudflare Workers gratuito também não executa FFmpeg diretamente.

## Segurança

O site foi concebido para utilização local. Para alojar publicamente o backend, define `SITE_USERNAME` e `SITE_PASSWORD`, usa HTTPS, e protege os custos de armazenamento e execução. Nunca coloques passwords no repositório.

Os downloads podem levar algum tempo. Os ficheiros gerados ficam no diretório temporário do servidor até serem removidos. Os resultados e capas dependem da disponibilidade da fonte.