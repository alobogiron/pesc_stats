"""Destrava jobs órfãos na subida do container.

Os jobs (extração/reprocessamento) são processos soltos que sinalizam estado
por lockfile + JSON em `dados_brutos/status/`. Se o container cai no meio de um
deles, os dois artefatos ficam para trás: o lockfile faz
`jobs.existe_extracao_rodando()` bloquear novas extrações e o status `running`
mantém os botões desabilitados na UI — para sempre, porque não existe mais
processo nenhum para atualizar aquilo.

Como no start do container é garantido que nenhum job está rodando (eles nunca
sobrevivem ao container que os criou), aqui dá para afirmar com segurança que
todo `running` encontrado é resíduo, e marcá-lo como erro.
"""
import glob
import os
import sys

sys.path.insert(0, "/app")

import jobs


def main():
    removidos = []
    for lock in glob.glob(os.path.join(jobs.STATUS_DIR, "*.lock")):
        os.remove(lock)
        removidos.append(os.path.basename(lock))

    interrompidos = []
    for status_path in glob.glob(os.path.join(jobs.STATUS_DIR, "*_status.json")):
        status = jobs.read_status(status_path)
        if status.get("state") != "running":
            continue
        jobs.write_status(
            status_path,
            state="error",
            started_at=status.get("started_at"),
            finished_at=jobs.now_iso(),
            error="Job interrompido: o container foi reiniciado enquanto ele "
                  "ainda estava em execução. Rode novamente.",
        )
        interrompidos.append(os.path.basename(status_path))

    if removidos or interrompidos:
        print(
            f"[bootstrap] locks removidos: {removidos or 'nenhum'} | "
            f"jobs marcados como interrompidos: {interrompidos or 'nenhum'}"
        )


if __name__ == "__main__":
    main()
