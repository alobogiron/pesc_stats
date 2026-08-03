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

**Atenção ao escopo da extração:** o JSON gerado para um docente depende de
quem mais está na mesma população — o scriptLattes funde publicações
semelhantes entre membros do grupo e mistura os metadados. Veja
[Limitações conhecidas](#metadados-contaminados-entre-coautores-causa-raiz-no-scriptlattes)
antes de comparar números entre duas extrações de listas diferentes.

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

A regra de deduplicação **não** vive dentro dos notebooks: está em
`dedup_publicacoes.py`, importado pelos dois (veja a seção
[Deduplicação e qualidade dos DOIs](#deduplicação-e-qualidade-dos-dois)).

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

### Renomear e excluir

No mesmo painel, o expander **"Renomear ou excluir a base ..."** faz as duas
operações. Ambas ficam desabilitadas enquanto houver job em andamento naquela
base — mexer nos arquivos no meio de uma extração deixaria artefatos órfãos —
e a exclusão exige marcar uma caixa de confirmação, que lista antes todos os
caminhos que serão apagados.

Uma base não é só o `.list`: renomear ou excluir precisa tratar **todos** os
artefatos que carregam o nome dela. `jobs.artefatos_comparacao(nome)` é a
definição única desse conjunto — se for acrescentar um artefato novo por base,
acrescente ali, senão renomear/excluir passam a deixar rastro:

| Artefato | Caminho |
|---|---|
| Lista de pessoas | `scriptlattes/exemplo/comparacao/<nome>.list` |
| Config do scriptLattes | `scriptlattes/exemplo/comparacao/<nome>.config` |
| Saída do scriptLattes | `scriptlattes/exemplo/comparacao/<nome>_saida/` |
| Snapshots extraídos | `dados_brutos/raw_comparacao/<nome>/` |
| Banco gerado | `pesquisadores_comparacao_<nome>.duckdb` |
| Status e locks dos jobs | `dados_brutos/status/comparacao_<nome>_*` |
| Log do papermill | `dados_brutos/status/comparacao_<nome>_ultima_execucao.ipynb` |

Duas decisões que valem registro:

- **O `.config` é apagado na renomeação, não movido.** Ele embute o nome da
  base em três linhas (nome do grupo, arquivo de entrada, diretório de saída) e
  `run_extract_comparacao.gerar_config` o regera do zero a cada extração —
  levá-lo adiante só criaria um arquivo apontando para caminhos inexistentes.
- **Os status vão junto.** Se ficassem para trás, uma base futura que reusasse
  o nome herdaria um `state=done` antigo e a UI anunciaria uma extração que
  nunca aconteceu para ela.

O nome novo passa pelo mesmo `jobs.slugify` do upload (minúsculo, sem acento
nem espaço, sem caracteres que permitam path traversal), e a operação é
recusada se o slug resultante já pertencer a outra base.

**Recurso compartilhado:** todas as extrações (principal e de comparação)
usam o mesmo `scriptlattes/cache/` e o mesmo Chrome/chromedriver, então só
uma extração roda por vez — as demais ficam com o botão desabilitado
enquanto isso.

## Deduplicação e qualidade dos DOIs

A regra que decide **"estas duas linhas são a mesma publicação?"** vive em
`dedup_publicacoes.py`, na raiz. Os dois pipelines importam dela:
`analyse_organizado.ipynb` (institucional, Lattes + ORCID + Scopus) e
`analyse_organizado_comparação.ipynb` (comparação, só Lattes).

**Por que um módulo e não uma cópia em cada notebook.** As duas bases existem
para serem comparadas entre si. Enquanto cada uma tinha a sua própria cópia
das funções, uma correção aplicada em só um dos lados fazia os números
divergirem por causa do código — exatamente aquilo que a comparação deveria
detectar como diferença de produção. Se você for mexer nessas funções, mexa
**no módulo**; nunca reintroduza uma definição local num notebook.

### As regras, em ordem de aplicação

1. **`normalizar_doi`** — o campo `doi` do Lattes é texto livre, e com
   frequência guarda outra coisa. A função repara o recuperável e recusa o
   resto, devolvendo `<NA>` (a linha então casa só por título, nunca é
   descartada):

   | Entrada real encontrada na base | Resultado |
   |---|---|
   | `http://www.scopus.com/...&doi=10.1145/1530873.1530886&md5=...` | `10.1145/1530873.1530886` (extrai do parâmetro) |
   | `http://www.scopus.com/...&doi=null&md5=...` | `<NA>` — a **mesma** string aparecia em 11 docentes |
   | `http://dx.doi.org/http://dx.doi.org/10.1109/...` | desfaz o prefixo repetido em laço |
   | `http://dx.doi.org/http://doi.ieeecomputersociety.org/10.1109/...` | idem, resolvedor alternativo |
   | `http://www.geoinfo.info/.../s7p3.pdf` | `<NA>` — link de PDF não é DOI |
   | `10.18420/ ecscw2024_ep13` | remove o espaço interno |

   O porteiro final é `^10\.\d{4,9}/\S+$`.

2. **`_neutralizar_dois_contestados`** — se **uma mesma fonte** usa o mesmo DOI
   em dois títulos diferentes do mesmo docente, o DOI está errado em pelo menos
   um deles e sai do casamento. O desempate é a corroboração: o título que
   aparece com aquele DOI em outra fonte responde pelo DOI; os demais caem no
   casamento por título.

   > O teste é **dentro de uma fonte**, e isso é essencial. Fontes diferentes
   > escrevem o mesmo título de formas diferentes (Lattes em português, Scopus
   > em inglês) e casar essas variações é justamente o trabalho do DOI. Comparar
   > títulos *entre* fontes marcaria como conflito o caso normal: partiria ao
   > meio ~144 publicações corretamente unificadas e geraria ~146 avisos falsos
   > de "esta publicação falta no seu Lattes".

3. **`calcular_chave_dedup`** — agrupa por DOI ou por título (componentes
   conexos), **sempre dentro de um único docente**. A chave carrega o
   `id_lattes` como prefixo, então coautoria entre docentes do quadro nunca
   funde ninguém. No máximo um título por docente pode reter um dado DOI — dois
   grupos com o mesmo DOI receberiam a mesma chave e voltariam a se fundir.

4. **`sanear_doi_gravado`** — apaga da coluna `doi` o que foi recusado e grava
   o que sobrou na forma canônica (`10.1007/x`, não
   `http://dx.doi.org/10.1007/X`). Devolve o relatório do que foi descartado.

5. **`auditar_duplicatas`** — segunda passada conferindo o resultado. Roda em
   toda execução e imprime `OK` ou `AVISO` na saída do notebook.

### `tb_dois_descartados` e o relatório

O que foi recusado vira a tabela **`tb_dois_descartados`** (nos dois bancos) e
alimenta o relatório **"DOIs inconsistentes no currículo Lattes"** em
*Geração de Relatórios* — um HTML imprimível por docente, com o valor exatamente
como está no currículo, para que ele localize e corrija a entrada.

Bancos gerados antes desta tabela existir continuam abrindo normalmente: a
validação de arquitetura exige só quatro tabelas, e o relatório avisa que
precisa de reprocessamento em vez de quebrar.

## Limitações conhecidas

Documentadas porque foram investigadas e medidas, e a decisão consciente foi
**não corrigir agora** — o custo/benefício não fecha.

### Metadados contaminados entre coautores (causa raiz, no scriptLattes)

O JSON de um docente **não é função apenas do currículo dele**: depende de
quais outros pesquisadores foram extraídos na mesma população.

Em `scriptlattes/scriptLattes/producoesBibliograficas/trabalhoCompletoEmCongresso.py`
(`compararCom`), quando dois membros do grupo têm títulos similares o
scriptLattes funde os registros e, para cada campo (`doi`, `autores`, `titulo`,
`nomeDoEvento`, `paginas`), mantém **a string mais longa** — não a correta. A
fusão muta o objeto no lugar, e `grupo.py` (`gerarArquivosJSONIndividuais`)
serializa esses mesmos objetos já mutados.

Efeito medido, comparando o mesmo currículo processado em duas populações
(PESC/UFRJ e uma lista do CEFET), mesma data de atualização do CV:

- **43 de 104** registros de congresso com `doi`/`autores`/`evento`/`paginas`
  diferentes entre as duas extrações;
- **~5%** dos papers com **estrato Qualis diferente**, porque o `nomeDoEvento`
  veio do currículo de um coautor.

É o item com maior impacto real, porque `evento` alimenta o estrato, que
alimenta as páginas de Avaliação Quadrienal e de Credenciamento. Corrigir exige
separar a fusão de identidade (`idMembro.update()`, que alimenta o grafo de
coautoria) da fusão de campos.

> Medição feita sobre **um** docente com muitos coautores nas duas populações;
> não é necessariamente a média do quadro.

### Quatro publicações "quimera" que a regra não detecta

Quando o DOI errado aparece em **um único título** do currículo do docente, não
há contradição interna e a regra de `_neutralizar_dois_contestados` não tem como
disparar — a forma do dado é idêntica à do caso legítimo (um título Lattes em
português + um título Scopus em inglês, mesmo DOI).

O resultado é uma linha que funde a publicação recente do Lattes com um registro
externo antigo e sem relação:

| Docente | Ano no Lattes | Ano na Scopus |
|---|---|---|
| Claudio Esperança | 2025 | 1997 |
| Geraldo Bonorino Xexéo | 2025 | 2012 |
| Rosa Maria Meri Leão | 2026 | 2013 |
| Luidi Gelabert Simonetti | 2026 | 2015 |

**Dano:** 4 publicações históricas (1997–2015) somem da base, e 4 linhas recentes
alegam em `fontes` uma cobertura externa que não têm.

**Por que não corrigimos:** são 4 linhas em 5.212 (0,08%), e as publicações
perdidas são todas anteriores ao período que alimenta a avaliação vigente — o
impacto é sobre registro histórico, não sobre pontuação. A publicação recente
que aparece na linha é real e conta legitimamente; o que está errado é o rótulo
de proveniência.

**Se um dia for corrigir**, o sinal já está medido. A divergência de ano entre a
linha do Lattes e a externa, em todas as publicações hoje fundidas, é bimodal
com uma faixa vazia no meio:

```
congressos (n=1737)  gap 0: 1653 | 1: 78 | 2: 2 |·· vazio ··| 11: 1 | 13: 2 | 28: 1
periódicos (n=1710)  gap 0: 1589 | 1: 102 | 2: 16 | 3: 2 |·· vazio ··| 9: 1
```

Mas **ano sozinho não serve**: o outlier de periódicos (gap 9) é o mesmo artigo
com o ano divergente (um *dynamic survey* redatado), e uma regra por ano o
partiria em dois. O critério correto é a **conjunção** — ligados *apenas* por
DOI (títulos normalizados não batem) **e** anos divergindo ≥ 5. Assim pega os 4,
com zero falso positivo.

## Estrutura de dados

O banco institucional tem **13 tabelas**: 6 principais (`tb_professores`,
`tb_alunos`, `tb_aluno_titulos`, `tb_orientacoes`, `tb_artigo_periodico`,
`tb_artigo_conferencia`), 6 por fonte (`tb_artigo_{periodico,conferencia}_{lattes,orcid,scopus}`,
com o dado pré-deduplicação) e `tb_dois_descartados`. Há ainda
`tb_situacao_orientandos`, gravada em conexão própria na Seção 14. O banco de
comparação tem 5 (`tb_professores`, `tb_artigo_periodico`,
`tb_artigo_conferencia`, `tb_orientacoes`, `tb_dois_descartados`).

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
