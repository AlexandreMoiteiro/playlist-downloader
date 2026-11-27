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
## Importação do histórico do YouTube (seleção autorizada)

A página `/youtube` inclui uma lista inicial deduplicada e suporta importação local de CSV, revisão, pesquisa e exportação. Por razões de privacidade, a revisão do CSV acontece apenas no navegador. **A lista de demonstração incorporada na página é visível publicamente**: não coloques históricos pessoais adicionais no repositório público.

O botão **Preparar MP3 autorizados** envia ao backend até 250 IDs e títulos escolhidos, após confirmação explícita de autorização para descarregar estes conteúdos. O GitHub Actions usa `yt-dlp` e FFmpeg para tentar obter áudio, regista sucessos/falhas e cria um ZIP com os ficheiros disponíveis e um `relatorio.json`. Um ZIP só é criado quando a tarefa é processada; não é garantido nenhum MP3 e não há suporte para contornar DRM, autenticação ou outras restrições de acesso.

A base de dados D1 exige a migração:
```sql
ALTER TABLE jobs ADD COLUMN youtube_tracks TEXT;
```
O tipo de tarefa é determinado pela presença de `youtube_tracks`. Os fluxos Spotify existentes mantêm-se separados. O endpoint autenticado `/api/processor/:id/selection` só fornece a seleção ao GitHub Actions durante a tarefa.

Limites: até 250 vídeos/tarefa, 8 tarefas em 24 h e até 2 tarefas na fila/em execução. O resultado fica no R2 temporariamente. Uma confirmação do utilizador não substitui a licença efetiva dos vídeos ou as regras do YouTube.
