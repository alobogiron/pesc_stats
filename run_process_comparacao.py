import argparse
import os
import subprocess
import sys

import jobs

NOTEBOOK_ENTRADA = "analyse_organizado_comparação.ipynb"
RAW_COMPARACAO_DIR = "dados_brutos/raw_comparacao"
TIMEOUT_SECONDS = 2 * 60 * 60  # 2h


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nome", required=True)
    args = parser.parse_args()
    nome = args.nome

    status_path = jobs.comparacao_process_status(nome)
    lock_path = jobs.comparacao_process_lock(nome)

    caminho_current = os.path.join(RAW_COMPARACAO_DIR, nome, "current")
    if not os.path.isdir(caminho_current):
        jobs.write_status(
            status_path,
            state="error",
            error=f"Nenhum snapshot extraído encontrado para '{nome}' em {caminho_current}. "
                  "Rode a extração desta base de comparação primeiro.",
            finished_at=jobs.now_iso(),
        )
        return

    if not jobs.acquire_lock(lock_path):
        jobs.write_status(
            status_path,
            state="error",
            error=f"Reprocessamento da base de comparação '{nome}' já em andamento.",
            finished_at=jobs.now_iso(),
        )
        return

    started_at = jobs.now_iso()
    jobs.write_status(status_path, state="running", started_at=started_at)

    duckdb_destino_final = f"pesquisadores_comparacao_{nome}.duckdb"
    tmp_path = os.path.abspath(f"{duckdb_destino_final}.tmp")
    notebook_saida_log = os.path.join("dados_brutos", "status", f"comparacao_{nome}_ultima_execucao.ipynb")

    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

        cmd = [
            sys.executable, "-m", "papermill",
            NOTEBOOK_ENTRADA, notebook_saida_log,
            "-p", "CAMINHO_JSONS_PROFESSORES", os.path.join(caminho_current, "*.json"),
            "-p", "ARQUIVO_DUCKDB_DESTINO", tmp_path,
        ]
        resultado = subprocess.run(
            cmd,
            timeout=TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )

        if resultado.returncode != 0 or not os.path.exists(tmp_path):
            erro = (resultado.stderr or resultado.stdout or "")[-3000:]
            jobs.write_status(
                status_path,
                state="error",
                started_at=started_at,
                finished_at=jobs.now_iso(),
                error=erro or "papermill terminou sem gerar o DuckDB de destino.",
            )
            return

        os.replace(tmp_path, duckdb_destino_final)
        jobs.write_status(
            status_path,
            state="done",
            started_at=started_at,
            finished_at=jobs.now_iso(),
            duckdb_path=duckdb_destino_final,
        )
    except subprocess.TimeoutExpired:
        jobs.write_status(
            status_path,
            state="error",
            started_at=started_at,
            finished_at=jobs.now_iso(),
            error=f"Reprocessamento da base de comparação '{nome}' excedeu o tempo limite (2h).",
        )
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        jobs.release_lock(lock_path)


if __name__ == "__main__":
    main()
