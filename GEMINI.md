# Projeto PESC Stats - Guia de Desenvolvimento

Plataforma de indicadores de produção acadêmica do PESC/COPPE-UFRJ, cruzando
currículos Lattes com ORCID e Scopus. O `README.md` é a documentação de
referência — este arquivo cobre só as convenções de desenvolvimento.

## 🏗️ Arquitetura e Tecnologias

- **Linguagem:** Python 3.13 (o `requirements.txt` fixa pandas 3 / numpy 2.4,
  que só têm wheel para cp313 em diante).
- **Interface:** Streamlit (`app.py`), 11 páginas.
- **Processamento:** notebooks executados por `papermill`
  (`analyse_organizado.ipynb` e `analyse_organizado_comparação.ipynb`).
- **Extração:** scriptLattes vendorizado em `scriptlattes/`, via Selenium.
- **Armazenamento:** DuckDB. O banco de produção é `pesquisadores_teste.duckdb`
  (apesar do nome), resolvido por `jobs.caminho_duckdb_principal()`. Os `.xlsx`
  e `.csv` da raiz são **insumos** do notebook, não o armazenamento.

## 🛠️ Convenções de Código

- **Estilo:** PEP 8.
- **Idioma:** nomes e docstrings em português, acompanhando o código existente.
- **Documentação:** as docstrings explicam o **porquê** da regra, não o quê —
  é o padrão do projeto (veja `dedup_publicacoes.py`).
- **Tratamento de erros:** validar existência e estrutura das colunas nos
  DataFrames antes de processar; bancos de arquitetura antiga devem degradar
  com aviso, nunca quebrar a página.

## ⚠️ Regras específicas deste projeto

- **A deduplicação vive em `dedup_publicacoes.py`**, importada pelos dois
  notebooks. **Nunca** reintroduza uma cópia local dessas funções: as duas
  bases existem para ser comparadas, e definições divergentes fazem os números
  diferirem por causa do código, não dos dados.
- **Deduplicação é sempre exata e sempre dentro de um mesmo docente.** Nada de
  limiar de similaridade — o casamento é por DOI normalizado ou título
  normalizado, com `id_lattes` como prefixo da chave.
- **Notebooks são grandes** (`analyse_organizado.ipynb` tem ~430 KB): editar via
  script sobre o JSON bruto, não com ferramentas que carregam o arquivo inteiro.

## 🧪 Workflow de Testes

Não há suíte `pytest` no projeto. A verificação é feita assim:

```bash
# 1. pipeline institucional ponta a ponta (~1m40s; Scopus vem do cache local)
python -m papermill analyse_organizado.ipynb /tmp/out.ipynb \
  -p ARQUIVO_DUCKDB_DESTINO /tmp/teste.duckdb

# 2. pipeline de comparação
python -m papermill analyse_organizado_comparação.ipynb /tmp/out_comp.ipynb \
  -p CAMINHO_JSONS_PROFESSORES "dados_brutos/raw_comparacao/<nome>/current/*.json" \
  -p ARQUIVO_DUCKDB_DESTINO /tmp/comp.duckdb

# 3. smoke test das 11 páginas do app (streamlit.testing.v1.AppTest)
PESC_DATA_DIR=<dir com pesquisadores_teste.duckdb> python t_app_tmp.py
```

O pipeline é **determinístico**: duas execuções sobre os mesmos insumos geram
bancos com as mesmas contagens. Ao mudar a lógica, compare contagens por tabela
antes/depois — um refactor que altera número é um refactor quebrado.

Confira também a saída de `auditar_duplicatas` no notebook executado: ela deve
dizer `OK` para periódicos e congressos.

## 📁 Estrutura de Pastas Importante

- `app.py` — ponto de entrada da aplicação.
- `dedup_publicacoes.py` — regra única de deduplicação (compartilhada).
- `jobs.py` — lockfiles, status em disco e disparo de jobs assíncronos.
- `run_extract*.py` / `run_process*.py` — entrypoints dos jobs.
- `dados_brutos/` — snapshots da extração e dados administrativos.
  Git-ignorado; nada aí é versionado.
- `analyse.ipynb`, `orcid_1.ipynb`, `scopus_2.ipynb` — protótipos supersedidos,
  fora do pipeline automatizado.
