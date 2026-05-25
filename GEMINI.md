# Projeto PESC Stats - Guia de Desenvolvimento

Este projeto realiza a análise de dados acadêmicos (Lattes, periódicos, eventos) para o PESC/UFRJ.

## 🏗️ Arquitetura e Tecnologias
- **Linguagem:** Python 3.10+
- **Processamento de Dados:** Pandas, Openpyxl.
- **Interface:** Streamlit (assumindo pelo `app.py`).
- **Dados:** Arquivos `.xlsx` e `.csv` na raiz e em `dados_brutos/`.

## 🛠️ Convenções de Código
- **Estilo:** Seguir PEP 8.
- **Tipagem:** Usar type hints em todas as novas funções.
- **Documentação:** Docstrings no formato Google Style.
- **Tratamento de Erros:** Sempre validar a existência e estrutura das colunas nos DataFrames antes de processar.

## 🧪 Workflow de Testes
- Novos módulos devem ter testes unitários usando `pytest`.
- Antes de commitar, execute: `pytest` e `ruff check .`.

## 📁 Estrutura de Pastas Importante
- `dados_brutos/`: Contém os arquivos originais que não devem ser modificados.
- `app.py`: Ponto de entrada da aplicação.
- `analyse.ipynb`: Playground para novas análises antes de serem movidas para módulos.
