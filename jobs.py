import glob
import json
import os
import re
import sys
import subprocess
import unicodedata
from datetime import datetime, timezone

STATUS_DIR = "dados_brutos/status"
EXTRACT_STATUS = os.path.join(STATUS_DIR, "extract_status.json")
PROCESS_STATUS = os.path.join(STATUS_DIR, "process_status.json")
EXTRACT_LOCK = os.path.join(STATUS_DIR, "extract.lock")
PROCESS_LOCK = os.path.join(STATUS_DIR, "process.lock")

COMPARACAO_LISTS_DIR = "scriptlattes/exemplo/comparacao"
SCRIPTLATTES_CACHE_DIR = "scriptlattes/cache"

# Sinais conhecidos de bloqueio/rate-limit da Lattes (visto em ERR_CONNECTION_REFUSED
# e no backoff de 5min já embutido em scriptlattes/scriptLattes/baixaLattes.py:baixaCVLattes).
SINAIS_BLOQUEIO = [
    "err_connection_refused",
    "dormindo 5 minutos",
    "captcha",
    "recaptcha",
    "muitas requisições",
    "too many requests",
    "acesso negado",
    "429",
]


def identificar_bloqueio(texto):
    """Procura sinais conhecidos de bloqueio/rate-limit da Lattes no log.
    Retorna um aviso pra prefixar a mensagem de erro, ou None se não achar nada."""
    texto_lower = (texto or "").lower()
    achados = [s for s in SINAIS_BLOQUEIO if s in texto_lower]
    if achados:
        return f"POSSÍVEL BLOQUEIO/RATE-LIMIT DA LATTES DETECTADO (sinais: {', '.join(achados)}).\n"
    return None


def slugify(nome):
    """Converte um nome arbitrário (ex.: nome de arquivo enviado) num slug
    seguro pra usar em nomes de arquivo/diretório -- sem espaços, acentos,
    maiúsculas ou caracteres que permitam path traversal (`/`, `..`)."""
    nome_sem_acento = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", nome_sem_acento).strip("_").lower()
    return slug or "base"


def comparacao_extract_status(nome):
    return os.path.join(STATUS_DIR, f"comparacao_{nome}_extract_status.json")


def comparacao_process_status(nome):
    return os.path.join(STATUS_DIR, f"comparacao_{nome}_process_status.json")


def comparacao_extract_lock(nome):
    return os.path.join(STATUS_DIR, f"comparacao_{nome}_extract.lock")


def comparacao_process_lock(nome):
    return os.path.join(STATUS_DIR, f"comparacao_{nome}_process.lock")


def listar_nomes_comparacao():
    """Nomes de base de comparação disponíveis, a partir dos arquivos .list
    em scriptlattes/exemplo/comparacao/ (um por base, nome = slug do arquivo)."""
    caminhos = sorted(glob.glob(os.path.join(COMPARACAO_LISTS_DIR, "*.list")))
    return [os.path.splitext(os.path.basename(c))[0] for c in caminhos]


def limpar_cache_scriptlattes():
    """Apaga o cache de CVs já baixados do scriptLattes (compartilhado entre
    a extração principal e todas as de comparação) -- sem isso, quem já está
    no cache nunca é rebaixado, então CVs desatualizados de quem já foi
    extraído antes não são renovados."""
    import shutil
    if os.path.isdir(SCRIPTLATTES_CACHE_DIR):
        shutil.rmtree(SCRIPTLATTES_CACHE_DIR)
    os.makedirs(SCRIPTLATTES_CACHE_DIR, exist_ok=True)


def existe_extracao_rodando():
    """True se a extração principal OU qualquer extração de comparação
    estiver de fato em andamento -- todas compartilham scriptlattes/cache e o
    mesmo chromedriver, então só uma pode rodar por vez.

    Verifica os LOCKFILES (não o status "running"): a UI grava state=running
    no status antes mesmo de lançar o processo, então checar o status aqui
    faria um job recém-lançado se ver "bloqueado por si mesmo" -- o lockfile
    só existe de fato enquanto algum processo o mantém (acquire_lock já
    aconteceu, release_lock ainda não), então não tem esse problema."""
    if os.path.exists(EXTRACT_LOCK):
        return True
    for nome in listar_nomes_comparacao():
        if os.path.exists(comparacao_extract_lock(nome)):
            return True
    return False


def existe_algum_job_rodando():
    """True se qualquer job (extração ou reprocessamento, principal ou de
    qualquer base de comparação) estiver em andamento -- usado pra decidir
    se a UI deve continuar fazendo poll, independente da página atual."""
    if is_running(EXTRACT_STATUS) or is_running(PROCESS_STATUS):
        return True
    for nome in listar_nomes_comparacao():
        if is_running(comparacao_extract_status(nome)) or is_running(comparacao_process_status(nome)):
            return True
    return False


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_status(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"state": "idle"}


def write_status(path, **kw):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(kw, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def is_running(path):
    return read_status(path).get("state") == "running"


def acquire_lock(lock_path):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def release_lock(lock_path):
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


def launch(script, *args):
    return subprocess.Popen(
        [sys.executable, script, *args],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
