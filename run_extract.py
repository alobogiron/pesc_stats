import os
import shutil
import subprocess
import sys
from datetime import datetime

import jobs

# scriptLattes vive vendorizado dentro deste repo (scriptlattes/), usando o
# mesmo venv/interpretador da aplicação -- não é mais um checkout externo.
SCRIPTLATTES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scriptlattes")
SCRIPTLATTES_PYTHON = sys.executable
SCRIPTLATTES_TIMEOUT_SECONDS = 3 * 60 * 60  # 3h por população

# (nome_populacao, config_relativo_ao_scriptLattes, subpasta_de_saida_do_scriptLattes, destino_dentro_do_raw)
POPULACOES = [
    ("alunos", "exemplo/alunos_pesc.config", "exemplo/alunos_pesc/json", "alunos"),
    ("professores_ufrj", "exemplo/professores_ufrj.config", "exemplo/professores_ufrj/json", "professores/ufrj"),
]

RAW_DIR = "dados_brutos/raw"
CURRENT_LINK = os.path.join(RAW_DIR, "current")


def rodar_scriptlattes(config_relativo):
    cmd = [SCRIPTLATTES_PYTHON, "scriptLattes.py", config_relativo]
    return subprocess.run(
        cmd,
        cwd=SCRIPTLATTES_DIR,
        timeout=SCRIPTLATTES_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
    )


def main():
    if jobs.existe_extracao_rodando():
        jobs.write_status(
            jobs.EXTRACT_STATUS,
            state="error",
            error="Já existe uma extração em andamento (principal ou de comparação) -- "
                  "compartilham o mesmo cache/chromedriver do scriptLattes.",
            finished_at=jobs.now_iso(),
        )
        return

    if not jobs.acquire_lock(jobs.EXTRACT_LOCK):
        jobs.write_status(
            jobs.EXTRACT_STATUS,
            state="error",
            error="Extração já em andamento.",
            finished_at=jobs.now_iso(),
        )
        return

    limpar_cache_antes = "--limpar-cache" in sys.argv
    started_at = jobs.now_iso()
    jobs.write_status(
        jobs.EXTRACT_STATUS,
        state="running",
        started_at=started_at,
        cache_limpo=limpar_cache_antes,
    )

    try:
        if limpar_cache_antes:
            jobs.limpar_cache_scriptlattes()

        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        novo_dir = os.path.join(RAW_DIR, stamp)
        os.makedirs(os.path.join(novo_dir, "professores"), exist_ok=True)
        os.makedirs(os.path.join(novo_dir, "alunos"), exist_ok=True)

        for nome, config, saida_json, destino in POPULACOES:
            resultado = rodar_scriptlattes(config)
            if resultado.returncode != 0:
                saida_completa = resultado.stderr or resultado.stdout or ""
                erro = saida_completa[-3000:]
                aviso_bloqueio = jobs.identificar_bloqueio(saida_completa) or ""
                jobs.write_status(
                    jobs.EXTRACT_STATUS,
                    state="error",
                    started_at=started_at,
                    finished_at=jobs.now_iso(),
                    error=f"{aviso_bloqueio}Falha ao extrair '{nome}':\n{erro}",
                )
                return

            pasta_json_origem = os.path.join(SCRIPTLATTES_DIR, saida_json)
            if not os.path.isdir(pasta_json_origem) or not os.listdir(pasta_json_origem):
                jobs.write_status(
                    jobs.EXTRACT_STATUS,
                    state="error",
                    started_at=started_at,
                    finished_at=jobs.now_iso(),
                    error=f"scriptLattes terminou mas não gerou JSONs para '{nome}' em {pasta_json_origem}.",
                )
                return

            destino_absoluto = os.path.join(novo_dir, destino)
            os.makedirs(destino_absoluto, exist_ok=True)
            for arquivo in os.listdir(pasta_json_origem):
                if arquivo.endswith(".json"):
                    shutil.copy2(
                        os.path.join(pasta_json_origem, arquivo),
                        os.path.join(destino_absoluto, arquivo),
                    )

        # Promove atomicamente: repointa dados_brutos/raw/current para o novo snapshot.
        # dados_brutos/alunos e dados_brutos/professores/ufrj já são symlinks para
        # raw/current/..., então essa única troca promove tudo de uma vez.
        tmp_link = CURRENT_LINK + ".tmp"
        if os.path.lexists(tmp_link):
            os.remove(tmp_link)
        os.symlink(stamp, tmp_link)
        os.replace(tmp_link, CURRENT_LINK)

        jobs.write_status(
            jobs.EXTRACT_STATUS,
            state="done",
            started_at=started_at,
            finished_at=jobs.now_iso(),
            raw_dir=stamp,
            cache_limpo=limpar_cache_antes,
        )

        jobs.launch("run_process.py")
    except subprocess.TimeoutExpired as e:
        saida_parcial = (e.stderr or e.stdout or "")
        if isinstance(saida_parcial, bytes):
            saida_parcial = saida_parcial.decode("utf-8", errors="replace")
        aviso_bloqueio = jobs.identificar_bloqueio(saida_parcial)
        if aviso_bloqueio:
            mensagem = (
                f"{aviso_bloqueio}scriptLattes excedeu o tempo limite (3h) -- "
                f"provavelmente ficou preso re-tentando após bloqueio. "
                f"Últimas linhas do log:\n{saida_parcial[-3000:]}"
            )
        else:
            mensagem = (
                f"scriptLattes excedeu o tempo limite (3h) (sem sinal de bloqueio "
                f"conhecido no log -- pode ser só uma lista grande). "
                f"Últimas linhas do log:\n{saida_parcial[-3000:]}"
            )
        jobs.write_status(
            jobs.EXTRACT_STATUS,
            state="error",
            started_at=started_at,
            finished_at=jobs.now_iso(),
            error=mensagem,
        )
    finally:
        jobs.release_lock(jobs.EXTRACT_LOCK)


if __name__ == "__main__":
    main()
