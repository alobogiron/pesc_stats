import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime

import jobs

SCRIPTLATTES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scriptlattes")
SCRIPTLATTES_PYTHON = sys.executable
SCRIPTLATTES_TIMEOUT_SECONDS = 3 * 60 * 60  # 3h

COMPARACAO_DIR_REL = "exemplo/comparacao"  # relativo a SCRIPTLATTES_DIR
TEMPLATE_CONFIG = os.path.join(SCRIPTLATTES_DIR, "exemplo", "alunos_pesc.config")
RAW_COMPARACAO_DIR = jobs.RAW_COMPARACAO_DIR


def gerar_config(nome):
    """Gera (ou regenera) um .config do scriptLattes pra esta base de
    comparação, a partir do template alunos_pesc.config -- só troca as 3
    linhas de identificação/entrada/saída, mantém todos os demais parâmetros
    (relatorios, grafo, métricas) iguais ao resto do app."""
    linhas_saida = []
    with open(TEMPLATE_CONFIG, "r", encoding="utf-8") as f:
        for linha in f:
            if linha.startswith("global-nome_do_grupo"):
                linhas_saida.append(f"global-nome_do_grupo                      = comparacao-{nome}\n")
            elif linha.startswith("global-arquivo_de_entrada"):
                linhas_saida.append(f"global-arquivo_de_entrada                 = ./{COMPARACAO_DIR_REL}/{nome}.list\n")
            elif linha.startswith("global-diretorio_de_saida"):
                linhas_saida.append(f"global-diretorio_de_saida                 = ./{COMPARACAO_DIR_REL}/{nome}_saida/\n")
            else:
                linhas_saida.append(linha)

    config_path = os.path.join(SCRIPTLATTES_DIR, COMPARACAO_DIR_REL, f"{nome}.config")
    with open(config_path, "w", encoding="utf-8") as f:
        f.writelines(linhas_saida)
    return f"{COMPARACAO_DIR_REL}/{nome}.config"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nome", required=True)
    parser.add_argument("--limpar-cache", action="store_true")
    args = parser.parse_args()
    nome = args.nome

    status_path = jobs.comparacao_extract_status(nome)
    lock_path = jobs.comparacao_extract_lock(nome)
    lista_path = os.path.join(SCRIPTLATTES_DIR, COMPARACAO_DIR_REL, f"{nome}.list")

    if not os.path.isfile(lista_path):
        jobs.write_status(
            status_path,
            state="error",
            error=f"Lista '{nome}.list' não encontrada em {lista_path}.",
            finished_at=jobs.now_iso(),
        )
        return

    if jobs.existe_extracao_rodando():
        jobs.write_status(
            status_path,
            state="error",
            error="Já existe uma extração em andamento (principal ou de outra base de "
                  "comparação) -- compartilham o mesmo cache/chromedriver do scriptLattes.",
            finished_at=jobs.now_iso(),
        )
        return

    if not jobs.acquire_lock(lock_path):
        jobs.write_status(
            status_path,
            state="error",
            error=f"Extração da base de comparação '{nome}' já em andamento.",
            finished_at=jobs.now_iso(),
        )
        return

    started_at = jobs.now_iso()
    jobs.write_status(status_path, state="running", started_at=started_at, cache_limpo=args.limpar_cache)

    try:
        if args.limpar_cache:
            jobs.limpar_cache_scriptlattes()

        config_relativo = gerar_config(nome)

        resultado = subprocess.run(
            [SCRIPTLATTES_PYTHON, "scriptLattes.py", config_relativo],
            cwd=SCRIPTLATTES_DIR,
            timeout=SCRIPTLATTES_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )

        if resultado.returncode != 0:
            saida = resultado.stderr or resultado.stdout or ""
            aviso_bloqueio = jobs.identificar_bloqueio(saida) or ""
            jobs.write_status(
                status_path,
                state="error",
                started_at=started_at,
                finished_at=jobs.now_iso(),
                error=f"{aviso_bloqueio}Falha ao extrair base de comparação '{nome}':\n{saida[-3000:]}",
            )
            return

        pasta_json_origem = os.path.join(SCRIPTLATTES_DIR, COMPARACAO_DIR_REL, f"{nome}_saida", "json")
        if not os.path.isdir(pasta_json_origem) or not os.listdir(pasta_json_origem):
            jobs.write_status(
                status_path,
                state="error",
                started_at=started_at,
                finished_at=jobs.now_iso(),
                error=f"scriptLattes terminou mas não gerou JSONs em {pasta_json_origem}.",
            )
            return

        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        destino_absoluto = os.path.join(RAW_COMPARACAO_DIR, nome, stamp)
        os.makedirs(destino_absoluto, exist_ok=True)
        for arquivo in os.listdir(pasta_json_origem):
            if arquivo.endswith(".json"):
                shutil.copy2(
                    os.path.join(pasta_json_origem, arquivo),
                    os.path.join(destino_absoluto, arquivo),
                )

        current_link = os.path.join(RAW_COMPARACAO_DIR, nome, "current")
        tmp_link = current_link + ".tmp"
        if os.path.lexists(tmp_link):
            os.remove(tmp_link)
        os.symlink(stamp, tmp_link)
        os.replace(tmp_link, current_link)

        jobs.write_status(
            status_path,
            state="done",
            started_at=started_at,
            finished_at=jobs.now_iso(),
            raw_dir=stamp,
            cache_limpo=args.limpar_cache,
        )

        jobs.launch("run_process_comparacao.py", "--nome", nome)
    except subprocess.TimeoutExpired as e:
        saida_parcial = e.stderr or e.stdout or ""
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
                f"conhecido no log). Últimas linhas do log:\n{saida_parcial[-3000:]}"
            )
        jobs.write_status(
            status_path,
            state="error",
            started_at=started_at,
            finished_at=jobs.now_iso(),
            error=mensagem,
        )
    finally:
        jobs.release_lock(lock_path)


if __name__ == "__main__":
    main()
