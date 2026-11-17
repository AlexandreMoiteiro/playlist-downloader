# Playlist Downloader

Site: [playlist-studio.alexandre-moiteiro.workers.dev](https://playlist-studio.alexandre-moiteiro.workers.dev/)

## Como funciona

1. O utilizador cola o link de uma playlist pública do Spotify e clica em **Descarregar playlist**.
2. O Cloudflare Worker cria uma tarefa numa base de dados D1.
3. O GitHub Actions verifica automaticamente a fila a cada cinco minutos (sem o utilizador entrar no GitHub).
4. O processador `scripts/process.py` utiliza spotDL e FFmpeg para procurar MP3 em fontes externas e incorporar os metadados e capas disponíveis.
5. O ZIP é transferido para o Cloudflare R2 através de um upload em partes, autenticado com GitHub OIDC.
6. O site disponibiliza o ZIP para download quando o processamento termina.

**Limites:** as tarefas dependem da disponibilidade das gravações e das permissões dos fornecedores. O Spotify não permite descarregar diretamente o seu áudio. O projeto não garante MP3 de todas as músicas, nem que uma versão encontrada seja a gravação original. Usa-o apenas quando tens direito a obter esses ficheiros.

O Github Actions é ativado por cron; pode haver atrasos antes de iniciar o download. Os ZIP ficam temporariamente armazenados em R2, com expiração lógica de 24 horas. Há limites de utilização para evitar abuso e custos.

## Código

- `static/index.html`: interface mínima com link + botão.
- `cloudflare/worker.js`: API, fila, autenticação OIDC, entrega ZIP.
- `scripts/process.py`: processamento com spotDL no GitHub Actions.
- `.github/workflows/download.yml`: execução automática agendada.

A ligação entre GitHub e Cloudflare usa **tokens OIDC curtos**, sem expor um token permanente no site. Requer a base de dados D1 `playlist-studio-jobs` e o bucket privado R2 `playlist-studio-archives`.

Para implantação manual do backend local, os ficheiros `app.py`, `Dockerfile` e `requirements.txt` permanecem incluídos.