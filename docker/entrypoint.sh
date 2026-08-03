#!/usr/bin/env bash
# Preparação de ambiente antes de subir o Streamlit (ou qualquer outro comando
# passado ao container). Idempotente: roda a cada start.
set -euo pipefail

cd /app

# 1. Esqueleto de dados_brutos/. Num volume novo (deploy do zero) essas pastas
#    não existem e tanto o notebook quanto jobs.py assumem que sim.
mkdir -p \
    dados_brutos/status \
    dados_brutos/raw \
    dados_brutos/raw_comparacao \
    dados_brutos/defesas \
    dados_brutos/professores \
    scriptlattes/cache \
    scriptlattes/exemplo/comparacao

# 2. Symlinks que o notebook espera (dados_brutos/alunos e
#    dados_brutos/professores/ufrj apontam para o snapshot promovido em
#    raw/current). Só cria se não houver nada no lugar -- inclusive symlink
#    quebrado, que é o estado normal antes da primeira extração, e que o
#    teste -e sozinho não detecta.
criar_symlink() {
    local alvo="$1" destino="$2"
    if [ ! -e "$destino" ] && [ ! -L "$destino" ]; then
        ln -s "$alvo" "$destino"
    fi
}
criar_symlink "raw/current/alunos" "dados_brutos/alunos"
criar_symlink "../raw/current/professores/ufrj" "dados_brutos/professores/ufrj"

# 3. Destrava jobs órfãos. Se o container morreu no meio de uma extração, o
#    lockfile continua no disco e o status continua "running" -- a UI
#    desabilita os botões para sempre e não há processo algum para terminar.
python docker/bootstrap_jobs.py

exec "$@"
