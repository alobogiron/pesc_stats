# PESC Stats

Plataforma Streamlit de indicadores de produção acadêmica do PESC/COPPE-UFRJ,
cruzando currículos Lattes com ORCID e Scopus.

## Como rodar

### Com Docker (recomendado)

```bash
docker compose build     # primeira vez (~3 min)
docker compose up -d     # sobe em http://localhost:8501
```

Guia completo de operação (comandos do dia a dia, variáveis, backup,
problemas comuns) em [Uso com Docker](#uso-com-docker).

### Sem Docker

```bash
source venv/bin/activate
streamlit run app.py
```

Aqui o `chromedriver` é o binário local (`scriptlattes/chromedriver`, veja a
seção de Extração) e precisa casar com a versão do Chrome instalado — é
exatamente essa dor que a imagem Docker elimina.

A aplicação lê `pesquisadores_teste.duckdb` (`jobs.caminho_duckdb_principal()`)
— esse é o banco "de produção" de fato, apesar do nome. Se o arquivo não
existir, rode um reprocessamento (veja abaixo) antes de abrir a página.

### Navegação

A barra lateral tem só a navegação, o filtro global "Fonte dos papers" e um
resumo de duas linhas (banco em uso + data do último processamento). Toda
operação de manutenção — extração de currículos, reprocessamento e gestão das
bases de comparação — vive na página **⚙️ Configurações**, dividida em duas
abas ("Base institucional" e "Bases de comparação"). A escolha de qual base
usar como Base B é feita dentro da própria página "Comparativo entre Bases".

## Uso com Docker

Tudo (Streamlit, scriptLattes + Selenium/Chromium e papermill) roda numa
imagem só, num container só. O motivo é o próprio desenho do app: ele dispara
extração e reprocessamento com `subprocess` dentro do próprio processo
(`jobs.py`), lendo e escrevendo os mesmos arquivos. Separar em dois containers
exigiria trocar isso por fila/API — mudança de arquitetura, não de
empacotamento.

### Primeiro uso

```bash
# 1. (opcional) chaves de API — sem elas o app sobe igual, só as seções
#    de ORCID/Scopus do notebook rodam vazias
cp .env.exemplo .env && nano .env

# 2. build. Se o seu usuário não for 1000:1000, passe o UID/GID para os
#    arquivos criados pelo container não virem com dono errado:
docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g)

# 3. sobe
docker compose up -d
```

Abra <http://localhost:8501>. Se o banco `pesquisadores_teste.duckdb` ainda não
existir, a página avisa: vá em **⚙️ Configurações → Base institucional** e rode
uma extração (ou só um reprocessamento, se os JSONs já estiverem em
`dados_brutos/`).

> `UID=$(id -u) docker compose build` **não** funciona — `UID` é somente-leitura
> no bash, e a atribuição falha antes do comando rodar. Use `--build-arg`.
> Com usuário 1000:1000 (padrão em desktop Linux), `docker compose build` puro
> já basta.

### Dia a dia

| O que | Comando |
|---|---|
| Subir / derrubar | `docker compose up -d` / `docker compose down` |
| Ver se está de pé | `docker compose ps` (a coluna Status mostra `healthy`) |
| Logs | `docker compose logs -f app` |
| Reiniciar | `docker compose restart app` |
| Shell dentro do container | `docker compose exec app bash` |
| Rodar em outra porta | `PESC_PORT=8600 docker compose up -d` |

O healthcheck bate em `/_stcore/health` a cada 30s — `docker compose ps` dizendo
`(healthy)` significa que o Streamlit está de fato respondendo, não só que o
processo existe.

### Rodar os jobs sem passar pela UI

Útil para agendar em cron, depurar uma extração ou rodar um reprocessamento
longo sem manter a aba aberta. Use **`exec -d`** (dentro do container que já
está de pé), não `run`:

```bash
docker compose exec -d app python run_extract.py                # extração + reprocessamento encadeado
docker compose exec -d app python run_extract.py --limpar-cache # rebaixa todos os CVs
docker compose exec -d app python run_process.py                # só o notebook
docker compose exec -d app python run_extract_comparacao.py --nome cefet_list
docker compose exec -d app python run_process_comparacao.py --nome cefet_list
```

O `-d` solta o processo: ele continua no container mesmo que você feche o
terminal. Acompanhe pela página Configurações — é o mesmo
`dados_brutos/status/` que a UI lê, então o resultado aparece lá como se o job
tivesse sido disparado por um botão.

> **Por que não `docker compose run --rm`:** ele cria um container só para
> aquele comando e o destrói assim que o processo principal termina, matando
> junto qualquer filho. Os dois scripts de extração terminam **lançando** o
> reprocessamento (`run_extract.py` → `run_process.py`;
> `run_extract_comparacao.py` → `run_process_comparacao.py`), então a extração
> rodaria inteira e o reprocessamento morreria no berço — deixando o status
> preso em `running` até o próximo start do container. `run --rm` só é seguro
> para os scripts que não encadeiam nada (`run_process*.py`), ou quando o
> container principal não está no ar.

Vale a mesma regra de sempre: só uma extração por vez (todas dividem o cache e
o chromedriver), e o lockfile garante isso mesmo entre containers diferentes.

### Variáveis de ambiente

| Variável | Onde | Padrão | Para quê |
|---|---|---|---|
| `PESC_PORT` | `up` | `8501` | Porta no host. Troque se já houver um Streamlit local ocupando a 8501. |
| `TZ` | `up` | `America/Sao_Paulo` | Fuso do container. Sem isso ele roda em UTC e as datas de extração/processamento aparecem 3h adiantadas na UI. |
| `UID` / `GID` | `build` | `1000` | Dono dos arquivos que o container cria no diretório montado. |
| `PESC_DATA_DIR` | runtime | `.` | Onde os `.duckdb` são lidos/gravados. Só precisa mexer no modo "código embutido" (abaixo). |
| `CHROME_BIN`, `CHROMEDRIVER_PATH`, `CHROME_EXTRA_ARGS` | runtime | definidos na imagem | Navegador, driver e flags do Chrome. Só mexa se for trocar o Chromium do sistema por outro. |

As chaves de ORCID/Scopus vêm do `.env` via `env_file` (opcional: o container
sobe sem o arquivo). O `.env` **nunca** entra na imagem — está no
`.dockerignore` e é lido só em runtime.

### Onde ficam os dados

O compose monta o projeto inteiro em `/app`, porque é onde já vivem tanto o
código quanto os dados: os `.duckdb` e as planilhas na raiz, `dados_brutos/`, o
cache e a saída do scriptLattes. Consequências práticas:

- Tudo que o container gera aparece direto no seu diretório de trabalho, com o
  seu usuário como dono — e sobrevive a `docker compose down`, que não apaga
  nada.
- Backup é `cp`/`rsync` normal do diretório (principalmente `*.duckdb` e
  `dados_brutos/`), sem `docker cp` nem volume nomeado no meio.
- Editar o código no host e `docker compose restart app` já reflete a mudança —
  não precisa rebuildar. **Exceção:** mexeu em `requirements.txt`, no
  `Dockerfile` ou em algo de sistema, aí é `docker compose build` de novo.

**Não rode o app no host e no container ao mesmo tempo.** Os dois enxergam os
mesmos `.duckdb` e o mesmo `dados_brutos/status/`; duas UIs abertas podem
disparar jobs concorrentes. O lockfile impede duas extrações simultâneas, mas o
reprocessamento não tem essa proteção entre instâncias.

### Deploy sem o repositório (código embutido)

A imagem já contém o código (`COPY . /app`), então ela roda sozinha — o mount
do compose existe só para o fluxo de desenvolvimento acima. Para um deploy em
outra máquina, sem clonar o projeto, tire o `- .:/app` do `docker-compose.yml` e
monte apenas os dados:

```yaml
    environment:
      PESC_DATA_DIR: /data      # onde os .duckdb passam a ser lidos/gravados
    volumes:
      - ./dados_brutos:/app/dados_brutos
      - ./scriptlattes/exemplo/comparacao:/app/scriptlattes/exemplo/comparacao
      - dbs:/data
```

Nesse modo o entrypoint cria sozinho o esqueleto de `dados_brutos/` (incluindo
os symlinks `alunos` e `professores/ufrj` que o notebook espera) na primeira
subida, e o app mostra o aviso de banco inexistente até a primeira extração.
Lembre de levar junto as planilhas de apoio que o notebook lê da raiz
(`periodicos_percentil.xlsx`, `eventos_classificados_dois_idiomas.csv`,
`lista_pessoas.csv`) — elas estão versionadas, então já vêm na imagem.

### Problemas comuns

| Sintoma | O que está acontecendo |
|---|---|
| `failed to bind host port 0.0.0.0:8501: address already in use` | Já tem algo na 8501 (provavelmente um `streamlit run` local). Suba com `PESC_PORT=8600 docker compose up -d` ou pare o outro processo. |
| Arquivos novos aparecem como `root` no host | Imagem construída com o UID padrão numa máquina cujo usuário não é 1000. Rebuilde com `--build-arg UID=$(id -u) --build-arg GID=$(id -g)`. |
| Extração falha com `session not created` / Chrome morre | `/dev/shm` pequeno demais. O compose já define `shm_size: 1gb` e a imagem passa `--disable-dev-shm-usage`; se estiver rodando via `docker run` na mão, acrescente `--shm-size=1g`. |
| `chromedriver não encontrado em '...'` | Só acontece fora do container (binário local ausente: `cd scriptlattes && make setup-chromedriver`). Dentro dele o caminho vem de `CHROMEDRIVER_PATH`. |
| Botões de extração/reprocessamento desabilitados para sempre | Job órfão: o container morreu no meio. O entrypoint destrava sozinho no próximo `docker compose up`/`restart`. |
| Datas da UI 3h adiantadas | `TZ` não chegou ao container. Confira com `docker compose exec app date`. |
| Mudei o código e nada mudou | Streamlit só recarrega o script a cada interação; force com `docker compose restart app`. Se mexeu em dependências, é rebuild. |

### Por dentro da imagem

- **Base `python:3.13-slim-trixie`** — mesma versão do venv do host (o
  `requirements.txt` fixa pandas 3 / numpy 2.4, que só têm wheel para cp313 em
  diante).
- **Chromium e chromedriver vêm dos pacotes do Debian** (`chromium`,
  `chromium-driver`), que são versionados juntos. É o que elimina o clássico
  "This version of ChromeDriver only supports Chrome version X" — e nada é
  baixado em tempo de build. O binário local `scriptlattes/chromedriver`
  (pareado com o Chrome do host) fica de fora da imagem e é ignorado dentro
  dela: `CHROMEDRIVER_PATH` e `CHROME_BIN` apontam para os do sistema.
- **`--no-sandbox` é obrigatório** em container (não há como o Chrome montar o
  sandbox de processos). Entra por `CHROME_EXTRA_ARGS`, junto de
  `--disable-dev-shm-usage`, `--disable-gpu` e `--window-size`. Fora do
  container a variável não existe e o comportamento é o de sempre.
- **`tini` como PID 1** — a extração encadeia o reprocessamento
  (`run_extract.py` lança `run_process.py` e termina), então processos ficam
  órfãos por desenho e precisam de um init de verdade para serem recolhidos.
- **Usuário não-root** (`app`, UID/GID configuráveis no build), com `HOME`
  gravável: o pybliometrics escreve `~/.config/pybliometrics.cfg` na primeira
  inicialização e o Chromium usa `~/.cache`.
- **Entrypoint** (`docker/entrypoint.sh`): cria o esqueleto de `dados_brutos/`
  e os symlinks esperados pelo notebook, e roda `docker/bootstrap_jobs.py`, que
  remove lockfiles e marca como interrompido qualquer job que ficou `running`
  de um container anterior — sem isso a UI ficaria travada para sempre,
  esperando um processo que não existe mais.
- A imagem tem ~2,5 GB (Chromium responde por metade).

## Pipeline de dados

Três etapas desacopladas — o Streamlit nunca executa scraping ou notebook no
próprio processo, apenas lê o resultado final e dispara jobs em background:

```
scriptLattes (extração)  →  analyse_organizado.ipynb (transformação)  →  app.py (apresentação)
```

### Extração (scriptLattes)

O scriptLattes está vendorizado em `scriptlattes/` (código do
[projeto original](https://github.com/jpmenachalco/scriptLattes), com uma
modificação local em `grupo.py`) e roda com o **mesmo venv** desta aplicação
— não é mais um checkout externo. Usa Selenium para raspar o Lattes, então
precisa do binário `chromedriver` em `scriptlattes/chromedriver` (git-ignorado,
baixe com `cd scriptlattes && make setup-chromedriver` — confira
`.chrome_version` para a versão do Chrome instalada). Dois configs dedicados
vivem em `scriptlattes/exemplo/`:

- `alunos_pesc.config` (lista `alunos_pesc.list`) — alunos do programa.
- `professores_ufrj.config` (lista `uff_list.list`, apesar do nome — os IDs
  batem com os professores do PESC/UFRJ) — professores.

O botão **"Re-extrair currículos"** roda as duas populações em sequência via
`run_extract.py`, copia os JSONs individuais gerados de volta para
`dados_brutos/raw/<timestamp>/{alunos,professores/ufrj}/` e só promove esse
snapshot a "atual" (repontando o symlink `dados_brutos/raw/current`) se ambas
as extrações terminarem com sucesso. Ao final, encadeia automaticamente o
reprocessamento.

**Risco de bloqueio:** o scriptLattes usa Selenium contra o site da Lattes.
Não há cooldown na UI (removido — travava até depois de extrações que
falharam, o que não fazia sentido). Em compensação, `run_extract.py`
verifica o log em busca de sinais conhecidos de bloqueio/rate-limit
(`ERR_CONNECTION_REFUSED`, CAPTCHA, "muitas requisições", etc. — ver
`SINAIS_BLOQUEIO`) e prefixa o erro exibido na UI com um aviso explícito
quando encontra algum, tanto em falhas diretas quanto em timeouts (3h).

### Reprocessamento (notebook)

O botão **"Reprocessar dados"** roda `run_process.py`, que executa
`analyse_organizado.ipynb` via `papermill`, sobrescrevendo
`ARQUIVO_DUCKDB_DESTINO` (célula tageada `parameters`) para um arquivo
temporário. Só se a execução terminar sem erro é que o resultado é promovido
atomicamente (`os.replace`) para `pesquisadores_teste.duckdb`.

`orcid_1.ipynb` e `scopus_2.ipynb` são protótipos supersedidos — a lógica de
ambos já foi incorporada dentro de `analyse_organizado.ipynb` (seções 6-13);
eles não fazem parte do pipeline automatizado.

### Jobs assíncronos

`jobs.py` fornece os helpers compartilhados (lockfile, status em disco via
JSON, `subprocess.Popen(start_new_session=True)`). Ambos os jobs sobrevivem a
fechar a aba do navegador. Status e locks ficam em `dados_brutos/status/`;
a página Configurações faz poll (2.5s) enquanto algum job está `running` (as
demais páginas não recarregam sozinhas).

## Bases de comparação

Além da base institucional (PESC/UFRJ), a página **"Comparativo entre
Bases"** permite gerar e usar bases de outras instituições/programas para
comparação, com o mesmo padrão de extração/reprocessamento assíncrono acima.

**Onde colocar a lista:** envie o arquivo `.list` pelo uploader em
**Configurações → Bases de comparação** — ele salva automaticamente em
`scriptlattes/exemplo/comparacao/<nome>.list`. Se preferir colocar o arquivo
manualmente (sem passar pela UI), é só copiar pra essa mesma pasta com
extensão `.list`. O formato é o mesmo do scriptLattes: uma linha por pessoa,
`id_lattes,Nome Completo`.

**Nome da base = nome do arquivo.** `puc-rio.list` (ou `PUC Rio.list`) vira a
base `puc_rio` (slug: minúsculo, sem acento/espaço). O banco gerado é
`pesquisadores_comparacao_puc_rio.duckdb`, na raiz do projeto.

Nessa aba, uma tabela lista todas as bases cadastradas com a data da última
extração e do último processamento. Escolha uma no seletor pra:
- **Re-extrair base de comparação**: roda o scriptLattes só pra essa lista
  (`run_extract_comparacao.py --nome <nome>`), com o mesmo checkbox
  "ignorar cache" da base principal.
- **Reprocessar base de comparação**: roda
  `analyse_organizado_comparação.ipynb` via papermill sobre os JSONs já
  extraídos (`run_process_comparacao.py --nome <nome>`), promovendo
  atomicamente pro `.duckdb` final.

Depois de gerado, o banco pode ser usado como **Base B** no topo da página
"Comparativo entre Bases", pelo seletor "Base gerida pelo sistema"
(alternativa ao upload manual de um `.duckdb` já existente, que continua
disponível).

**Recurso compartilhado:** todas as extrações (principal e de comparação)
usam o mesmo `scriptlattes/cache/` e o mesmo Chrome/chromedriver, então só
uma extração roda por vez — as demais ficam com o botão desabilitado
enquanto isso.

## Estrutura de dados

```
dados_brutos/
  raw/<timestamp>/{alunos,professores/ufrj}/   # snapshots versionados por extração
  raw/current -> raw/<timestamp>                # symlink para o snapshot oficial
  alunos -> raw/current/alunos                   # symlinks (compatibilidade com o notebook)
  professores/ufrj -> ../raw/current/professores/ufrj
  professores/uff/                               # população separada, não gerenciada por este pipeline
  raw_comparacao/<nome>/<timestamp>/, .../current  # idem, por base de comparação
  status/                                        # status/lock dos jobs (inclui comparacao_<nome>_*)
  defesas/, lista_alunos_pesc.xlsx, alias_*.csv   # dados administrativos manuais
```

`dados_brutos/` inteiro é git-ignorado — nada aí é versionado.

## Dependências

`requirements.txt` cobre tudo (app + notebook + scriptLattes), num único
venv. A única peça fora do `pip install` é o binário `chromedriver`
(git-ignorado — veja a seção de Extração acima); com Docker nem isso, o
chromedriver vem do sistema. Variáveis de API (ORCID, Scopus) ficam em `.env`
— sem elas, o notebook degrada graciosamente (seções correspondentes rodam com
0 resultados, sem falhar). O `.env` nunca entra na imagem: é lido em runtime
via `env_file` (e é opcional, o container sobe sem ele).
