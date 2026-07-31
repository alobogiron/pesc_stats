import os
import subprocess
import sys

import jobs

NOTEBOOK_ENTRADA = "analyse_organizado.ipynb"
NOTEBOOK_SAIDA_LOG = "dados_brutos/status/analyse_organizado_ultima_execucao.ipynb"
DUCKDB_DESTINO_FINAL = "pesquisadores_teste.duckdb"
DUCKDB_TMP = "pesquisadores_teste.duckdb.tmp"
TIMEOUT_SECONDS = 2 * 60 * 60  # 2h


def main():
    if not jobs.acquire_lock(jobs.PROCESS_LOCK):
        jobs.write_status(
            jobs.PROCESS_STATUS,
            state="error",
            error="Reprocessamento já em andamento.",
            finished_at=jobs.now_iso(),
        )
        return

    started_at = jobs.now_iso()
    jobs.write_status(jobs.PROCESS_STATUS, state="running", started_at=started_at)

    tmp_path = os.path.abspath(DUCKDB_TMP)
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

        cmd = [
            sys.executable, "-m", "papermill",
            NOTEBOOK_ENTRADA, NOTEBOOK_SAIDA_LOG,
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
                jobs.PROCESS_STATUS,
                state="error",
                started_at=started_at,
                finished_at=jobs.now_iso(),
                error=erro or "papermill terminou sem gerar o DuckDB de destino.",
            )
            return

        os.replace(tmp_path, DUCKDB_DESTINO_FINAL)
        jobs.write_status(
            jobs.PROCESS_STATUS,
            state="done",
            started_at=started_at,
            finished_at=jobs.now_iso(),
            duckdb_path=DUCKDB_DESTINO_FINAL,
        )
    except subprocess.TimeoutExpired:
        jobs.write_status(
            jobs.PROCESS_STATUS,
            state="error",
            started_at=started_at,
            finished_at=jobs.now_iso(),
            error="Reprocessamento excedeu o tempo limite (2h).",
        )
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        jobs.release_lock(jobs.PROCESS_LOCK)


if __name__ == "__main__":
    main()
