import glob
import json
import os
import re
import shutil
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
# Snapshots extraídos das bases de comparação. Definido aqui (e não em cada
# run_*_comparacao.py) porque renomear/excluir uma base precisa mexer nesta
# pasta: se o caminho existisse em dois lugares, uma divergência silenciosa
# faria a renomeação deixar o snapshot para trás.
RAW_COMPARACAO_DIR = "dados_brutos/raw_comparacao"

# Onde ficam os .duckdb gerados. Padrão "." = raiz do projeto, exatamente como
# sempre foi. Existe para o caso de a imagem Docker rodar com o código embutido
# (sem bind mount do projeto): aí basta apontar PESC_DATA_DIR para um volume
# e os bancos passam a ser gravados/lidos lá, sem editar código.
DATA_DIR = os.environ.get("PESC_DATA_DIR", ".")


def caminho_duckdb_principal():
    """Banco institucional (Base A), gerado por run_process.py."""
    return os.path.join(DATA_DIR, "pesquisadores_teste.duckdb")


def caminho_duckdb_comparacao(nome):
    """Banco de uma base de comparação, gerado por run_process_comparacao.py."""
    return os.path.join(DATA_DIR, f"pesquisadores_comparacao_{nome}.duckdb")

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


# O nome de uma base entra em vários arquivos derivados; o mais longo é
# `comparacao_<nome>_ultima_execucao.ipynb`, que acrescenta 33 caracteres. Como
# NAME_MAX costuma ser 255 bytes, um nome muito longo faria o sistema de
# arquivos recusar a criação -- e, pior, recusar no *meio* de uma renomeação,
# deixando a base partida entre dois nomes. Por isso o limite é conferido
# antes de qualquer arquivo ser movido, e com folga sobre o mínimo necessário.
MAX_NOME_COMPARACAO = 120


def _validar_nome_comparacao(nome):
    """Recusa nomes que o sistema de arquivos não aceitaria, como ValueError —
    a UI já sabe mostrar ValueError; um OSError cru subiria como traceback."""
    if len(nome) > MAX_NOME_COMPARACAO:
        raise ValueError(
            f"Nome longo demais ({len(nome)} caracteres). "
            f"O limite é {MAX_NOME_COMPARACAO}."
        )


def artefatos_comparacao(nome):
    """Todos os caminhos que pertencem a uma base de comparação.

    Existe uma função só para isso porque renomear e excluir precisam tratar
    exatamente o mesmo conjunto. Um artefato esquecido vira lixo órfão na
    exclusão e, pior, é *herdado* por uma base futura que reuse o nome — um
    `process_status.json` antigo com `state=done` faria a UI anunciar um
    processamento que nunca aconteceu para aquela base."""
    return {
        "lista": os.path.join(COMPARACAO_LISTS_DIR, f"{nome}.list"),
        "config": os.path.join(COMPARACAO_LISTS_DIR, f"{nome}.config"),
        "saida_scriptlattes": os.path.join(COMPARACAO_LISTS_DIR, f"{nome}_saida"),
        "raw": os.path.join(RAW_COMPARACAO_DIR, nome),
        "duckdb": caminho_duckdb_comparacao(nome),
        "extract_status": comparacao_extract_status(nome),
        "process_status": comparacao_process_status(nome),
        "extract_lock": comparacao_extract_lock(nome),
        "process_lock": comparacao_process_lock(nome),
        "log_notebook": os.path.join(STATUS_DIR, f"comparacao_{nome}_ultima_execucao.ipynb"),
    }


def salvar_lista_comparacao(nome_arquivo, conteudo, sobrescrever=False):
    """Grava a lista `.list` de uma base de comparação a partir do nome do
    arquivo enviado. Devolve `(slug, sobrescreveu)`.

    O slug vem do nome do arquivo, então dois arquivos diferentes podem apontar
    para a mesma base ("PUC-Rio.list" e "puc rio.list" viram ambos `puc_rio`).
    Quando isso acontece a gravação é **recusada** a menos que
    `sobrescrever=True`: trocar a lista por baixo dos panos deixaria o `.duckdb`
    e os snapshots já extraídos descrevendo um conjunto de pessoas que não é
    mais o da lista — a base passaria a mentir sobre si mesma até alguém
    reextrair, sem nada na tela indicando isso."""
    nome = slugify(os.path.splitext(os.path.basename(nome_arquivo))[0])
    _validar_nome_comparacao(nome)
    ja_existe = nome in listar_nomes_comparacao()
    if ja_existe:
        if comparacao_em_uso(nome):
            raise ValueError(
                f"A base '{nome}' tem um job em andamento. Espere terminar."
            )
        if not sobrescrever:
            raise ValueError(
                f"Já existe uma base chamada '{nome}'. Substituir a lista deixa o "
                f"banco e os snapshots já extraídos descrevendo as pessoas antigas — "
                f"confirme a substituição e reextraia depois."
            )
    os.makedirs(COMPARACAO_LISTS_DIR, exist_ok=True)
    destino = os.path.join(COMPARACAO_LISTS_DIR, f"{nome}.list")
    with open(destino, "w", encoding="utf-8") as f:
        f.write(conteudo)
    return nome, ja_existe


def comparacao_em_uso(nome):
    """True enquanto algum job desta base está de fato em andamento.

    Checa os lockfiles, não o campo `state`: a UI grava "running" no status
    antes mesmo de o processo existir, e um status preso por um container que
    morreu no meio travaria a base para sempre — o lockfile, esse, o
    entrypoint limpa na subida."""
    return os.path.exists(comparacao_extract_lock(nome)) or os.path.exists(
        comparacao_process_lock(nome)
    )


def renomear_comparacao(nome_atual, novo_nome):
    """Renomeia uma base de comparação movendo todos os seus artefatos.
    Devolve o slug efetivamente usado.

    O `.config` do scriptLattes é apagado em vez de movido: ele embute o nome
    da base em três linhas (nome do grupo, arquivo de entrada, diretório de
    saída) e é regerado do zero por `run_extract_comparacao.gerar_config` a
    cada extração. Levá-lo para o nome novo só criaria um arquivo apontando
    para caminhos que não existem mais.

    Usa `shutil.move` (e não `os.replace`) porque o `.duckdb` pode estar em
    outro sistema de arquivos: no modo "código embutido" documentado no README,
    `PESC_DATA_DIR` aponta para um volume separado, e `os.replace` falharia
    com EXDEV."""
    novo = slugify(novo_nome)
    if not novo:
        raise ValueError("Nome inválido: não sobrou nada depois de normalizar.")
    _validar_nome_comparacao(novo)
    existentes = listar_nomes_comparacao()
    if nome_atual not in existentes:
        raise ValueError(f"A base '{nome_atual}' não existe.")
    if novo == nome_atual:
        raise ValueError(f"O nome normalizado ('{novo}') é igual ao atual.")
    if novo in existentes:
        raise ValueError(f"Já existe uma base chamada '{novo}'.")
    if comparacao_em_uso(nome_atual):
        raise ValueError(
            "Há um job em andamento nesta base. Espere terminar antes de renomear."
        )

    origem = artefatos_comparacao(nome_atual)
    destino = artefatos_comparacao(novo)

    if os.path.exists(origem["config"]):
        os.remove(origem["config"])  # regerado na próxima extração

    for chave, caminho_origem in origem.items():
        if chave == "config" or not os.path.lexists(caminho_origem):
            continue
        pasta_destino = os.path.dirname(os.path.abspath(destino[chave]))
        os.makedirs(pasta_destino, exist_ok=True)
        shutil.move(caminho_origem, destino[chave])
    return novo


def excluir_comparacao(nome):
    """Apaga uma base de comparação e todos os seus artefatos, inclusive o
    `.duckdb` já gerado e os snapshots extraídos. É irreversível — quem chama
    deve confirmar com o usuário antes."""
    if nome not in listar_nomes_comparacao():
        raise ValueError(f"A base '{nome}' não existe.")
    if comparacao_em_uso(nome):
        raise ValueError(
            "Há um job em andamento nesta base. Espere terminar antes de excluir."
        )

    for caminho in artefatos_comparacao(nome).values():
        if os.path.isdir(caminho) and not os.path.islink(caminho):
            shutil.rmtree(caminho, ignore_errors=True)
        elif os.path.lexists(caminho):
            os.remove(caminho)


def limpar_cache_scriptlattes():
    """Apaga o cache de CVs já baixados do scriptLattes (compartilhado entre
    a extração principal e todas as de comparação) -- sem isso, quem já está
    no cache nunca é rebaixado, então CVs desatualizados de quem já foi
    extraído antes não são renovados."""
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
