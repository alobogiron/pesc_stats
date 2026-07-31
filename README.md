# PESC Stats

Plataforma Streamlit de indicadores de produção acadêmica do PESC/COPPE-UFRJ,
cruzando currículos Lattes com ORCID e Scopus.

## Como rodar

```bash
source venv/bin/activate
streamlit run app.py
```

A aplicação lê `pesquisadores_teste.duckdb` (constante `CAMINHO_BASE_INSTITUCIONAL`
em `app.py`) — esse é o banco "de produção" de fato, apesar do nome. Se o
arquivo não existir, rode um reprocessamento (veja abaixo) antes de abrir a
página.

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
a UI faz poll (2.5s) enquanto algum job está `running`.

## Bases de comparação

Além da base institucional (PESC/UFRJ), a página **"Comparativo entre
Bases"** permite gerar e usar bases de outras instituições/programas para
comparação, com o mesmo padrão de extração/reprocessamento assíncrono acima.

**Onde colocar a lista:** envie o arquivo `.list` pelo próprio uploader da
sidebar ("Bases de Comparação Geridas") — ele salva automaticamente em
`scriptlattes/exemplo/comparacao/<nome>.list`. Se preferir colocar o arquivo
manualmente (sem passar pela UI), é só copiar pra essa mesma pasta com
extensão `.list`. O formato é o mesmo do scriptLattes: uma linha por pessoa,
`id_lattes,Nome Completo`.

**Nome da base = nome do arquivo.** `puc-rio.list` (ou `PUC Rio.list`) vira a
base `puc_rio` (slug: minúsculo, sem acento/espaço). O banco gerado é
`pesquisadores_comparacao_puc_rio.duckdb`, na raiz do projeto.

Na sidebar, uma tabela lista todas as bases cadastradas com a data da última
extração e do último processamento. Escolha uma no seletor pra:
- **Re-extrair base de comparação**: roda o scriptLattes só pra essa lista
  (`run_extract_comparacao.py --nome <nome>`), com o mesmo checkbox
  "ignorar cache" da base principal.
- **Reprocessar base de comparação**: roda
  `analyse_organizado_comparação.ipynb` via papermill sobre os JSONs já
  extraídos (`run_process_comparacao.py --nome <nome>`), promovendo
  atomicamente pro `.duckdb` final.

Depois de gerado, o banco pode ser usado como **Base B** direto pelo
seletor "Base gerida pelo sistema" (alternativa ao upload manual de um
`.duckdb` já existente, que continua disponível).

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
(git-ignorado — veja a seção de Extração acima). Variáveis de API (ORCID,
Scopus) ficam em `.env` — sem elas, o notebook degrada graciosamente (seções
correspondentes rodam com 0 resultados, sem falhar).
