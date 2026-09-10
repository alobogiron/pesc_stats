import streamlit as st
import duckdb
import pandas as pd
import tempfile
import os
import re
import time
import html as html_lib
from datetime import datetime

import jobs
# Normalizações canônicas de DOI e título -- a mesma regra exata que o pipeline
# usa para deduplicar, reaproveitada pelo relatório de coautoria discente.
import dedup_publicacoes as dp
# Regra única dos anos de credenciamento (mesma que a Seção 15 do notebook usa
# para montar `tb_credenciamento_anos`); aqui só o caminho do CSV é consultado,
# para que a página de vigência diga de onde os anos vieram.
import credenciamento as cred
# Regra única da pontuação e da alocação de papers entre docentes, usada só
# pela página "Alocação Ótima de Papers". Fica em módulo para ser exercitada
# pela suíte de robustez sem subir o Streamlit.
import alocacao_papers as aloc

# ==========================================
# 1. CONFIGURAÇÃO DA INTERFACE INSTITUCIONAL
# ==========================================
st.set_page_config(page_title="Sistema de Avaliação de Produtividade Acadêmica", layout="wide")

# ==========================================
# 2. CONEXÃO COM O BANCO DE DADOS
# ==========================================
# Base institucional no schema consolidado (11 tabelas), gerada pelo notebook
# `analyse_organizado.ipynb`. Precisa conter as colunas `fontes` (tabelas de
# artigos) e `data_ingresso` (tb_professores), usadas pelo credenciamento e
# pela geração de relatórios. Regenere o banco pelo notebook se ele não existir.
CAMINHO_BASE_INSTITUCIONAL = jobs.caminho_duckdb_principal()

def assinatura_arquivo(caminho):
    """(mtime_ns, inode) do arquivo — a identidade da versão publicada.

    Serve de chave de cache da conexão. O `.duckdb` nunca é atualizado no
    lugar: tanto `run_process.py` quanto o editor de credenciamento montam o
    arquivo novo à parte e publicam com `os.replace`, o que troca o inode. Uma
    conexão aberta antes disso continua lendo o arquivo antigo (o descritor
    aponta para o inode substituído, que fica `(deleted)` no sistema), e o
    `.clear()` do cache sozinho não garante a reabertura. Com a assinatura na
    chave, a troca do arquivo muda a chave e a conexão é reaberta sozinha."""
    try:
        st_info = os.stat(caminho)
        return (st_info.st_mtime_ns, st_info.st_ino)
    except OSError:
        return (0, 0)


@st.cache_resource
def _registro_conexao_institucional():
    """Caixa que guarda a última conexão entregue, para poder fechá-la quando o
    arquivo do banco for republicado.

    Precisa ser um recurso em cache, e não uma variável de módulo: o Streamlit
    reexecuta o script inteiro a cada rerun, o que zeraria uma variável de
    módulo e perderia justamente a referência que se quer fechar."""
    return {"con": None}


@st.cache_resource
def _abrir_base_institucional(caminho, assinatura):
    # Fechar a conexão anterior não é opcional: enquanto uma conexão daquele
    # caminho continua viva, o DuckDB devolve a MESMA instância a qualquer
    # `connect()` seguinte -- inclusive depois de o arquivo ter sido
    # substituído por `os.replace`. Sem este fechamento, o app seguiria lendo o
    # inode antigo (o que aparece como `(deleted)` nos descritores do
    # processo), por mais que a conexão fosse "reaberta".
    #
    # Só se chega aqui quando a assinatura muda, ou seja, quando o arquivo foi
    # republicado -- então a conexão anterior aponta para uma versão que não
    # existe mais e ninguém deveria estar lendo.
    registro = _registro_conexao_institucional()
    anterior = registro.get("con")
    if anterior is not None:
        try:
            anterior.close()
        except Exception:
            pass
        registro["con"] = None

    try:
        conexao = duckdb.connect(database=caminho, read_only=True)
        registro["con"] = conexao
        return conexao
    except Exception as e:
        st.error(
            f"Falha na conexão com a base de dados institucional "
            f"('{caminho}'): {e}. "
            "Verifique se o arquivo existe — ele é gerado ao executar o notebook "
            "`analyse_organizado.ipynb`."
        )
        st.stop()


def get_db_connection():
    """Conexão de leitura da base institucional, reaberta automaticamente
    sempre que o arquivo for republicado (ver `assinatura_arquivo`)."""
    return _abrir_base_institucional(
        CAMINHO_BASE_INSTITUCIONAL, assinatura_arquivo(CAMINHO_BASE_INSTITUCIONAL)
    )


# Mantém `get_db_connection.clear()` funcionando nos pontos que descartam a
# conexão explicitamente (fim de reprocessamento, gravação do credenciamento).
get_db_connection.clear = _abrir_base_institucional.clear

con = get_db_connection()

def get_year_bounds(conexao):
    """Obtém os limites de anos disponíveis (artigos e orientações) em uma
    conexão DuckDB qualquer. Cada tabela é consultada em seu próprio
    try/except -- uma base enviada para comparação pode ter arquitetura
    mais antiga sem alguma dessas tabelas, e isso não deve derrubar as
    demais, só ser ignorado silenciosamente."""
    candidatos_min, candidatos_max = [], []

    for query in [
        "SELECT MIN(ano_pub), MAX(ano_pub) FROM tb_artigo_periodico",
        "SELECT MIN(ano), MAX(ano) FROM tb_artigo_conferencia",
        "SELECT MIN(ano_inicio), MAX(COALESCE(ano_conclusao, ano_inicio)) FROM tb_orientacoes",
    ]:
        try:
            res = conexao.execute(query).fetchone()
            if res and res[0] is not None:
                candidatos_min.append(res[0])
            if res and res[1] is not None:
                candidatos_max.append(res[1])
        except Exception:
            pass

    a_min = min(candidatos_min) if candidatos_min else 2000
    a_max = max(candidatos_max) if candidatos_max else 2026
    return int(a_min), int(a_max)

ANO_MIN, ANO_MAX = get_year_bounds(con)

def tem_coluna(conexao, tabela, coluna):
    """Verifica se `coluna` existe em `tabela` numa conexão (para bases enviadas
    que podem ter arquitetura antiga, sem a coluna `fontes`)."""
    try:
        n = conexao.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ?", [tabela, coluna]
        ).fetchone()[0]
        return n > 0
    except Exception:
        return False


def tem_tabela(conexao, tabela):
    """Verifica se `tabela` existe na conexão. Mesma finalidade de `tem_coluna`,
    um nível acima: bancos gerados antes da Seção 15 do notebook não têm
    `tb_credenciamento_anos`, e o regime de vigência precisa saber disso."""
    try:
        n = conexao.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [tabela]
        ).fetchone()[0]
        return n > 0
    except Exception:
        return False


def tabela_tem_linhas(conexao, tabela):
    """`tem_tabela` + pelo menos uma linha. `tb_credenciamento_anos` existente
    mas vazia (CSV de credenciamento ausente no reprocessamento) zeraria tudo
    sob o regime de vigência, então vale tratar como indisponível."""
    if not tem_tabela(conexao, tabela):
        return False
    try:
        return conexao.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0] > 0
    except Exception:
        return False


# Se a base traz a vigência do credenciamento carregada (Seção 15 do notebook).
# Lido aqui, antes da navegação, porque decide se a página "Credenciamento
# (vigência)" entra no menu e se o regime de contagem é oferecido na barra lateral.
credenciamento_disponivel = tabela_tem_linhas(con, "tb_credenciamento_anos")

def renderizar_filtro_periodo(ano_min, ano_max, chave_pagina, titulo_extra=""):
    """Renderiza o par de campos 'Ano de Início'/'Ano de Fim'.

    A intenção do usuário é compartilhada globalmente entre todas as páginas
    via st.session_state['filtro_ano_inicio'/'filtro_ano_fim'] -- mudar o
    período em qualquer página propaga para as demais. Cada página, porém,
    usa uma chave de widget própria (`chave_pagina`) e recorta (clampa) o
    valor herdado para os seus próprios limites válidos (ano_min/ano_max),
    já que páginas diferentes podem ter intervalos de dados diferentes (ex.:
    a base de comparação enviada pelo usuário). Valida que início <= fim,
    corrigindo automaticamente -- nunca deixa passar um intervalo
    invertido/negativo para as consultas.

    A troca acontece num `on_change`, e não depois de os campos existirem:
    escrever em `st.session_state[chave]` depois que o widget de mesma chave
    foi instanciado levanta `StreamlitAPIException`, e era exatamente isso que
    acontecia -- em qualquer página -- ao digitar um Ano de Fim menor que o Ano
    de Início. Callback roda antes do script, quando a atribuição ainda é
    permitida.
    """
    def _clamp(valor, padrao):
        if valor is None:
            valor = padrao
        return min(max(int(valor), ano_min), ano_max)

    chave_inicio = f"{chave_pagina}_ano_inicio"
    chave_fim = f"{chave_pagina}_ano_fim"
    chave_troca = f"{chave_pagina}_periodo_trocado"

    def _trocar_se_invertido():
        inicio = st.session_state.get(chave_inicio)
        fim = st.session_state.get(chave_fim)
        if inicio is None or fim is None or inicio <= fim:
            return
        st.session_state[chave_inicio], st.session_state[chave_fim] = fim, inicio
        # Guarda o par original só para a mensagem: o aviso precisa dizer o que
        # o usuário digitou, não o valor já corrigido que os campos exibem.
        st.session_state[chave_troca] = (inicio, fim)

    if chave_inicio not in st.session_state:
        st.session_state[chave_inicio] = _clamp(
            st.session_state.get("filtro_ano_inicio"), max(ano_min, ano_max - 4)
        )
    if chave_fim not in st.session_state:
        st.session_state[chave_fim] = _clamp(
            st.session_state.get("filtro_ano_fim"), ano_max
        )

    col1, col2 = st.columns(2)
    with col1:
        ano_inicio = st.number_input(
            f"Ano de Início{titulo_extra}", min_value=ano_min, max_value=ano_max,
            key=chave_inicio, on_change=_trocar_se_invertido,
        )
    with col2:
        ano_fim = st.number_input(
            f"Ano de Fim{titulo_extra}", min_value=ano_min, max_value=ano_max,
            key=chave_fim, on_change=_trocar_se_invertido,
        )

    digitado = st.session_state.pop(chave_troca, None)
    if digitado:
        st.error(
            f"Ano de Início ({digitado[0]}) não pode ser maior que Ano de Fim "
            f"({digitado[1]}); os valores foram trocados automaticamente."
        )

    if ano_inicio > ano_fim:
        # Caminho defensivo: intervalo invertido que não veio de uma edição nos
        # campos -- herdado de outra página com outros limites, por exemplo, em
        # que o `_clamp` acima puxou só uma das pontas. O callback não roda
        # nesse caso e os widgets já existem, então corrige-se apenas o valor
        # que vai para as consultas.
        st.error(
            f"Ano de Início ({ano_inicio}) não pode ser maior que Ano de Fim ({ano_fim}); "
            "os valores foram trocados automaticamente."
        )
        ano_inicio, ano_fim = ano_fim, ano_inicio

    st.session_state["filtro_ano_inicio"] = ano_inicio
    st.session_state["filtro_ano_fim"] = ano_fim

    return ano_inicio, ano_fim

@st.cache_resource(show_spinner=False)
def carregar_base_comparacao(arquivo_bytes, nome_arquivo):
    """
    Recebe os bytes de um arquivo .duckdb enviado via upload, persiste em um
    arquivo temporário e abre uma conexão DuckDB somente leitura a partir dele.
    O cache é mantido por conteúdo do arquivo (arquivo_bytes), então reenviar
    o mesmo arquivo não reabre a conexão.

    Valida, logo após conectar, se as tabelas mínimas exigidas pelo restante do
    app existem — isso cobre tanto arquivos corrompidos/inválidos (a conexão
    sequer abre) quanto arquivos .duckdb válidos mas com arquitetura diferente
    (a conexão abre, mas falta alguma tabela esperada).
    """
    sufixo = os.path.splitext(nome_arquivo)[1] or ".duckdb"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=sufixo)
    tmp.write(arquivo_bytes)
    tmp.flush()
    tmp.close()

    conexao_b = duckdb.connect(database=tmp.name, read_only=True)

    tabelas_obrigatorias = [
        "tb_professores", "tb_artigo_periodico", "tb_artigo_conferencia", "tb_orientacoes"
    ]
    for tabela in tabelas_obrigatorias:
        try:
            conexao_b.execute(f"SELECT 1 FROM {tabela} LIMIT 1")
        except Exception:
            raise ValueError(
                f"A base enviada não possui a tabela obrigatória '{tabela}' (ou está corrompida). "
                "Verifique se o arquivo tem a mesma arquitetura da base institucional."
            )
    return conexao_b

@st.cache_resource(show_spinner=False)
def abrir_base_comparacao_gerida(caminho, _mtime):
    """Abre (somente leitura) um DuckDB de comparação gerido pelo sistema
    (gerado por run_process_comparacao.py). Cache chaveado por (caminho,
    mtime) -- reabre sozinho quando o arquivo é regenerado por um
    reprocessamento novo."""
    return duckdb.connect(database=caminho, read_only=True)

def listar_bases_comparacao_info():
    """Uma linha por base de comparação (.list em scriptlattes/exemplo/comparacao/):
    nome, status de extração/processamento e se já existe um DuckDB gerado."""
    linhas = []
    for nome in jobs.listar_nomes_comparacao():
        status_extract = jobs.read_status(jobs.comparacao_extract_status(nome))
        status_process = jobs.read_status(jobs.comparacao_process_status(nome))
        duckdb_path = jobs.caminho_duckdb_comparacao(nome)
        duckdb_existe = os.path.exists(duckdb_path)
        linhas.append({
            "nome": nome,
            "última extração": status_extract.get("finished_at") or ("em execução" if status_extract.get("state") == "running" else "nunca"),
            "último processamento": status_process.get("finished_at") or ("em execução" if status_process.get("state") == "running" else "nunca"),
            "banco gerado": "sim" if duckdb_existe else "não",
            "_duckdb_path": duckdb_path,
            "_duckdb_existe": duckdb_existe,
        })
    return linhas

def _fmt_ts(iso_str):
    """Timestamp ISO (UTC, gravado por jobs.now_iso) -> 'dd/mm/aaaa hh:mm' no
    fuso local. Devolve a string original se não for um ISO reconhecível."""
    if not iso_str:
        return "—"
    try:
        return datetime.fromisoformat(iso_str).astimezone().strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return str(iso_str)


def linha_status(rotulo, status, container=st):
    """Uma linha de status de job (extração/reprocessamento) em `container`."""
    estado = status.get("state", "idle")
    sufixo_cache = " (cache ignorado — rebaixou tudo)" if status.get("cache_limpo") else ""
    if estado == "running":
        container.caption(f"⏳ {rotulo}: em execução desde {_fmt_ts(status.get('started_at'))}{sufixo_cache}")
    elif estado == "done":
        container.caption(f"✅ {rotulo}: última execução OK em {_fmt_ts(status.get('finished_at'))}{sufixo_cache}")
    elif estado == "error":
        container.caption(f"❌ {rotulo}: falhou em {_fmt_ts(status.get('finished_at'))}")
        with container.expander(f"Ver log de erro ({rotulo})"):
            st.code(status.get("error", "(sem detalhes)"))
    else:
        container.caption(f"○ {rotulo}: nunca executado")


# ==========================================
# 3. ESTADO DOS JOBS (LIDO CEDO, SEM UI)
# ==========================================
# Os status são arquivos JSON pequenos em dados_brutos/status/. São lidos em
# toda execução (não só na página Configurações) porque o rodapé da barra
# lateral os resume e porque a conexão em cache precisa ser descartada assim
# que um reprocessamento termina, esteja o usuário em qual página estiver.
status_extract = jobs.read_status(jobs.EXTRACT_STATUS)
status_process = jobs.read_status(jobs.PROCESS_STATUS)

if status_process.get("state") == "done":
    if st.session_state.get("_ultimo_process_done") != status_process.get("finished_at"):
        st.session_state["_ultimo_process_done"] = status_process.get("finished_at")
        get_db_connection.clear()
        st.rerun()

# ==========================================
# 4. NAVEGAÇÃO (BARRA LATERAL ENXUTA)
# ==========================================
# A barra lateral guarda só o que é usado em praticamente toda página: a
# navegação e o filtro global de fonte dos papers. Toda operação de manutenção
# (extração de currículos, reprocessamento, cadastro/extração de bases de
# comparação) foi movida para a página "Configurações"; a escolha da Base B
# vive dentro da própria página "Comparativo entre Bases".
PAGINA_COMPARATIVO = "Comparativo entre Bases"
PAGINA_CONFIGURACOES = "Configurações"
PAGINA_VIGENCIA = "Credenciamento por Vigência"
PAGINA_ALOCACAO = "Alocação Ótima de Papers"

# Chave longa = identificador usado no restante do arquivo; valor = rótulo
# curto exibido no menu (o título completo continua no topo de cada página).
ROTULOS_PAGINAS = {
    "Indicadores Institucionais": "Indicadores",
    "Análise por Docente": "Por docente",
    "Série Histórica da Produção": "Série histórica",
    "Repositório Geral de Artigos": "Artigos",
    "Avaliação Quadrienal Geral (A1-A8)": "Quadrienal A1–A8",
    "Avaliação Quadrienal Restrita (A1-A4)": "Quadrienal A1–A4",
    "Relatório de Credenciamento Consolidado": "Credenciamento",
    # A página da vigência só entra no menu quando a base tem
    # `tb_credenciamento_anos` carregada -- bancos gerados antes da Seção 15 do
    # notebook continuam com exatamente o menu de sempre.
    **({PAGINA_VIGENCIA: "Credenciamento (vigência)"} if credenciamento_disponivel else {}),
    PAGINA_ALOCACAO: "Alocação ótima",
    "Panorama de Orientações Acadêmicas": "Orientações",
    "Geração de Relatórios": "Relatórios",
    PAGINA_COMPARATIVO: "Comparativo",
    PAGINA_CONFIGURACOES: "⚙️ Configurações",
}


def ir_para_pagina(pagina):
    """Callback de navegação. Trocar de página só é seguro dentro de um
    `on_click`: alterar a chave de um widget já instanciado no meio da mesma
    execução levantaria StreamlitAPIException."""
    st.session_state["pagina_atual"] = pagina


st.sidebar.markdown("### Produtividade Acadêmica")
pagina_selecionada = st.sidebar.radio(
    "Navegação",
    list(ROTULOS_PAGINAS),
    format_func=lambda p: ROTULOS_PAGINAS[p],
    key="pagina_atual",
    label_visibility="collapsed",
)

st.sidebar.divider()

# ==========================================
# 4.1 FONTE DOS PAPERS (FILTRO GLOBAL)
# ==========================================
# Alterna, em todas as visualizações que envolvem papers, entre a base
# unificada (todas as fontes já deduplicadas) e apenas as publicações que
# constam do Lattes. "Apenas Lattes" filtra as tabelas unificadas por
# `fontes LIKE '%LATTES%'` (a coluna `fontes` registra em quais bases cada
# publicação foi encontrada). Vive na sidebar, com key própria, então o
# valor escolhido persiste ao trocar de página.
fonte_papers_opcao = st.sidebar.selectbox(
    "Fonte dos papers",
    ["Base unificada (todas as fontes)", "Apenas cadastradas no Lattes"],
    key="fonte_papers",
    help="Aplica-se a todos os gráficos/indicadores de periódicos e conferências, "
         "inclusive o modo Comparativo. 'Apenas Lattes' considera somente publicações "
         "cujo campo `fontes` contém LATTES. Não se aplica à página de Relatórios, "
         "em que cada relatório já define as fontes que examina."
)
apenas_lattes = fonte_papers_opcao == "Apenas cadastradas no Lattes"

def sql_fonte(coluna="fontes", conector="AND", ativo=None):
    """Fragmento SQL do filtro de fonte. Retorna '' quando a base unificada é
    escolhida (ou `ativo` é False); caso contrário, o predicado de Lattes com
    o conector desejado ('AND' para acrescentar a um WHERE existente, 'WHERE'
    quando a consulta ainda não tem cláusula WHERE)."""
    usar = apenas_lattes if ativo is None else ativo
    return f" {conector} {coluna} LIKE '%LATTES%'" if usar else ""


def sql_ingresso(alias_tabela, coluna_ano, conector="AND"):
    """Fragmento SQL que restringe `coluna_ano` de `alias_tabela` (nome de
    tabela ou alias de JOIN) à produção/orientação posterior à data de
    ingresso do professor no programa (`tb_professores.data_ingresso`) —
    currículos Lattes trazem a vida acadêmica inteira, mas a apresentação
    institucional só deve considerar o período em que o docente já fazia
    parte do quadro. `COALESCE` faz o corte virar no-op quando
    `data_ingresso` é nulo (docente sem essa informação, ou Base B do
    Comparativo, que não tem `lista_pessoas.csv`).

    Não é chamado direto pelas páginas: quem escolhe entre este recorte e o de
    vigência é `sql_recorte_docente`."""
    return (
        f" {conector} {alias_tabela}.{coluna_ano} >= COALESCE("
        f"(SELECT data_ingresso FROM tb_professores pr_ing WHERE pr_ing.id_lattes = {alias_tabela}.id_lattes), "
        f"{alias_tabela}.{coluna_ano})"
    )


def sql_vigencia(alias_tabela, coluna_ano, conector="AND"):
    """Fragmento SQL do recorte por **anos de credenciamento**: só conta a
    linha cujo ano está entre os anos em que aquele docente esteve credenciado
    (`tb_credenciamento_anos`, uma linha por par docente/ano, gerada pela Seção
    15 de `analyse_organizado.ipynb`).

    Diferença importante de comportamento em relação a `sql_ingresso`: lá o
    `COALESCE` faz o corte virar no-op para quem não tem a informação; aqui,
    docente sem nenhum ano registrado não pontua em ano nenhum. É o que a
    regra de credenciamento pede -- a produção só conta nos anos de vigência,
    e "nenhum ano de vigência" é uma resposta legítima --, mas significa que
    uma planilha de credenciamento incompleta zera quem faltar nela. Por isso
    a página "Credenciamento (vigência)" lista explicitamente quem está sem
    vigência registrada."""
    return (
        f" {conector} EXISTS (SELECT 1 FROM tb_credenciamento_anos cred_vig "
        f"WHERE cred_vig.id_lattes = {alias_tabela}.id_lattes "
        f"AND cred_vig.ano = {alias_tabela}.{coluna_ano})"
    )


def sql_recorte_docente(alias_tabela, coluna_ano, conector="AND", aplicar=True, regime=None):
    """Ponto único do recorte temporal por docente — todas as páginas passam
    por aqui, e é ele que decide qual das duas regras aplicar.

    `regime` é o regime em vigor (`REGIME_INGRESSO` ou `REGIME_VIGENCIA`);
    `None` usa o escolhido na barra lateral. `aplicar=False` devolve '' e
    dispensa o recorte por completo -- usado quando a base consultada não tem
    o insumo da regra (Base B do Comparativo sem `data_ingresso` ou sem
    `tb_credenciamento_anos`); verificar antes com `tem_coluna`/
    `tabela_tem_linhas`."""
    if not aplicar:
        return ""
    regime_efetivo = regime_recorte if regime is None else regime
    if regime_efetivo == REGIME_VIGENCIA:
        return sql_vigencia(alias_tabela, coluna_ano, conector)
    return sql_ingresso(alias_tabela, coluna_ano, conector)


# ==========================================
# 4.1.1 REGIME DE CONTAGEM (FILTRO GLOBAL)
# ==========================================
# Qual regra decide se a produção de um docente conta num determinado ano:
#
#   - Data de ingresso: conta tudo a partir do ano em que ele entrou no
#     programa (`tb_professores.data_ingresso`). É a regra histórica do app e
#     segue sendo o padrão -- nenhum número muda sem que alguém troque isto.
#   - Anos de credenciamento: conta só nos anos em que ele esteve efetivamente
#     credenciado (`tb_credenciamento_anos`), o que dá conta de
#     descredenciamento, recredenciamento e lacunas no meio.
#
# O seletor só aparece quando o banco tem a tabela de vigência carregada;
# bancos gerados antes da Seção 15 do notebook continuam funcionando como
# sempre, sem nem tomar conhecimento do regime novo.
REGIME_INGRESSO = "Data de ingresso (atual)"
REGIME_VIGENCIA = "Anos de credenciamento"

if credenciamento_disponivel:
    regime_recorte = st.sidebar.radio(
        "Regime de contagem",
        [REGIME_INGRESSO, REGIME_VIGENCIA],
        key="regime_recorte",
        help="Define, em todas as páginas, a partir de quando a produção de cada docente "
             "conta. 'Data de ingresso' usa o ano de entrada no programa (comportamento "
             "histórico). 'Anos de credenciamento' usa a vigência ano a ano registrada em "
             "`tb_credenciamento_anos` — nesse regime, docente sem vigência registrada não "
             "pontua. A página 'Credenciamento (vigência)' compara os dois lado a lado.",
    )
else:
    regime_recorte = REGIME_INGRESSO


def frase_recorte_docente(regime=None):
    """Frase que descreve, em português, o recorte por docente em vigor. Usada
    nos blocos "Como estes valores são calculados" para que a explicação nunca
    descreva uma regra diferente da que produziu os números logo acima."""
    regime_efetivo = regime_recorte if regime is None else regime
    if regime_efetivo == REGIME_VIGENCIA:
        return (
            "só conta a produção nos anos em que o docente esteve credenciado "
            "(`tb_credenciamento_anos`), e docente sem nenhum ano de vigência "
            "registrado não pontua"
        )
    return (
        "a produção anterior à data de ingresso do docente no programa fica de fora "
        "(currículos Lattes trazem a vida acadêmica inteira)"
    )


def _serie_por_docente(valores):
    """Converte para `pd.Series` de float a série com uma entrada por docente."""
    return pd.Series(list(valores), dtype="float64")


def stats_dispersao(valores):
    """Média, desvio padrão e mediana de uma série com uma entrada por docente
    cadastrado -- inclusive 0 para quem nada produziu no recorte.

    Incluir os zeros é o que faz a média devolvida aqui ser exatamente o per
    capita da convenção do app (soma ÷ docentes cadastrados): mediana e desvio
    passam a descrever a mesma população que o divisor da média, e não um
    subconjunto dela. Uma mediana 0,00 não é defeito do cálculo -- diz que mais
    da metade do quadro não pontuou naquele recorte.

    `ddof=0` (desvio populacional) porque o quadro de docentes é a população
    inteira do programa, não uma amostra dela."""
    serie = _serie_por_docente(valores)
    if serie.empty:
        return 0.0, 0.0, 0.0
    return float(serie.mean()), float(serie.std(ddof=0)), float(serie.median())


def _num_br(valor, casas=2, sinal=False):
    """Número no formato brasileiro (vírgula decimal), como nas demais tabelas.
    Com `sinal`, força o '+' nos positivos (usado nas colunas de diferença)."""
    formato = f"{{:+.{casas}f}}" if sinal else f"{{:.{casas}f}}"
    return formato.format(valor).replace(".", ",")


LEGENDA_DISPERSAO = (
    "Média (= o per capita: total ÷ docentes cadastrados), desvio padrão populacional "
    "e mediana da série por docente, contando 0 para quem não produziu no recorte. "
    "Desvio muito acima da média, ou mediana bem abaixo dela, indicam produção "
    "concentrada em poucos docentes. Ver \"Como estes valores são calculados\" abaixo."
)


def renderizar_tabela_dispersao(linhas, casas=2, legenda=None, titulo=None,
                                rotulo_delta="Δ Média"):
    """Tabela com Total, Média (o per capita), Desvio Padrão e Mediana de cada
    série por docente.

    `linhas` é uma lista de `(rótulo, série)` ou `(rótulo, série, série de
    referência)`; havendo referência, entra uma coluna `rotulo_delta` com a
    diferença entre as médias (usada no Comparativo, para a Base B medir-se
    contra a Base A). Cada série precisa ter uma entrada por docente cadastrado,
    zeros inclusive (ver `stats_dispersao`).

    A coluna Total é o somatório da série -- o número absoluto que dá escala à
    média e que antes vivia no tooltip das métricas. Sai sem casas decimais
    quando a série é de contagens (papers, orientações) e com a mesma precisão
    das demais colunas quando é de scores fracionários."""
    if titulo:
        st.markdown(titulo)

    tem_delta = any(len(linha) > 2 and linha[2] is not None for linha in linhas)
    registros = []
    for linha in linhas:
        rotulo, valores = linha[0], linha[1]
        referencia = linha[2] if len(linha) > 2 else None
        serie = _serie_por_docente(valores)
        media, desvio, mediana = stats_dispersao(serie)
        total = float(serie.sum())
        casas_total = 0 if float(total).is_integer() else casas

        registro = {
            "Indicador": rotulo,
            "Total": _num_br(total, casas_total),
            "Média": _num_br(media, casas),
            "Desvio Padrão": _num_br(desvio, casas),
            "Mediana": _num_br(mediana, casas),
        }
        if tem_delta:
            registro[rotulo_delta] = (
                "—" if referencia is None
                else _num_br(media - stats_dispersao(referencia)[0], casas, sinal=True)
            )
        registros.append(registro)

    st.dataframe(pd.DataFrame(registros), use_container_width=True, hide_index=True)
    st.caption(legenda if legenda is not None else LEGENDA_DISPERSAO)


def renderizar_explicacao_calculos(descricao_x, recorte=None, observacoes=(),
                                   chave=None):
    """Expansor com a memória de cálculo da tabela de dispersão: o que é a
    série por docente, como saem as três estatísticas e como lê-las.

    `descricao_x` completa a frase "x_i é ..." (o que cada docente contribui);
    `recorte` descreve os filtros aplicados na página; `observacoes` são
    ressalvas extras de interpretação."""
    with st.expander("Como estes valores são calculados"):
        st.markdown(
            "**1. A série por docente.** Monta-se um vetor com **uma entrada por "
            "docente cadastrado** ($n$ = total de linhas de `tb_professores`), em que "
            f"$x_i$ é {descricao_x} "
            "Docente sem produção no recorte entra com $x_i = 0$, em vez de ficar de "
            "fora: as três estatísticas passam a descrever exatamente a mesma "
            "população que o divisor do per capita."
        )
        st.markdown(
            "**2. Média** — é o próprio índice per capita, apenas reescrito como média "
            "da série:"
        )
        st.latex(r"\bar{x} \;=\; \frac{1}{n}\sum_{i=1}^{n} x_i \;=\; \frac{\text{total do programa}}{\text{docentes cadastrados}}")
        st.markdown(
            "**3. Desvio padrão populacional** — dispersão em torno da média, na mesma "
            "unidade dela. Divide-se por $n$, e não por $n-1$, porque o quadro de "
            "docentes é a população inteira do programa e não uma amostra sorteada dela:"
        )
        st.latex(r"\sigma \;=\; \sqrt{\frac{1}{n}\sum_{i=1}^{n}\left(x_i - \bar{x}\right)^{2}}")
        st.markdown(
            "**4. Mediana** — valor que parte a série ordenada ao meio: metade dos "
            "docentes fica abaixo dele, metade acima (com $n$ par, é a média dos dois "
            "valores centrais). Diferente da média, não é puxada por casos extremos."
        )
        st.markdown(
            "**Como ler.** Média e mediana próximas indicam produção distribuída de "
            "forma homogênea pelo quadro. Mediana bem abaixo da média, ou desvio padrão "
            "da ordem da média (ou maior), indicam o oposto: poucos docentes muito "
            "produtivos puxam a média para cima, e ela deixa de representar o docente "
            "típico — que é o que a mediana mostra. Mediana 0,00 significa que mais da "
            "metade do quadro não pontuou no recorte."
        )
        if recorte:
            st.markdown(f"**Recorte considerado.** {recorte}")
        for observacao in observacoes:
            st.markdown(f"**Observação.** {observacao}")


OBS_DUPLA_CONTAGEM = (
    "Um paper assinado por dois docentes do quadro entra uma vez para cada um deles, "
    "e portanto é contado duas vezes no total. Isso é inerente à base — a chave de "
    "deduplicação leva o `id_lattes` como prefixo, então a deduplicação nunca "
    "atravessa professores — e vale igualmente para os demais indicadores agregados "
    "do app."
)


def contar_papers_per_capita(conexao, ano_ini, ano_fim_, restrito=False):
    """Conta os papers de cada docente do programa na janela, devolvendo uma
    série por docente cadastrado para cada um dos seis indicadores. A média
    dessa série é o per capita na convenção já usada pelo Comparativo entre
    Bases (denominador = todo o quadro de `tb_professores`, sem nenhum recorte
    por docente), e a mesma série sustenta o desvio padrão e a mediana
    exibidos ao lado da média.

    O `LEFT JOIN` a partir de `tb_professores` (em vez de um `COUNT(*)` solto
    sobre as tabelas de artigos) é o que garante que docentes sem produção
    entrem como 0 e que a contagem case com a tabela por docente exibida logo
    acima nas páginas que chamam esta função.

    Os recortes de janela, fonte e regime de contagem são exatamente os das
    páginas que chamam esta função, para o per capita bater com a tabela
    exibida logo acima. `restrito=True` limita aos estratos A1-A4 (percentil
    >= 50 em periódicos), reproduzindo o corte da Quadrienal Restrita e o da
    opção "Pontuação Restrita" do Credenciamento.

    "Com discentes" usa `coautoria_aluno`, a coluna que
    `coauthorship_detection.py` grava comparando a string de autores do artigo
    com os nomes e formas de citação dos alunos. A comparação com TRUE também
    resolve o caso de bases antigas em que a coluna ficou nula.

    ATENÇÃO ao interpretar: um artigo coassinado por dois docentes do quadro
    entra como duas linhas e é contado duas vezes. Isso é inerente à base --
    `chave_dedup` leva o `id_lattes` como prefixo (ver `dedup_publicacoes.py`),
    então a deduplicação nunca atravessa professores -- e é o mesmo
    comportamento dos demais indicadores agregados do app.
    """
    restricao_p = " AND maior_percentil >= 50.0" if restrito else ""
    restricao_c = " AND estrato IN ('A1', 'A2', 'A3', 'A4')" if restrito else ""

    query_p = f"""
        SELECT id_lattes,
               COUNT(*) AS total_p,
               COUNT(CASE WHEN coautoria_aluno = TRUE THEN 1 END) AS disc_p
        FROM tb_artigo_periodico
        WHERE ano_pub BETWEEN ? AND ?{restricao_p}{sql_fonte()}{sql_recorte_docente('tb_artigo_periodico', 'ano_pub')}
        GROUP BY id_lattes
    """
    query_c = f"""
        SELECT id_lattes,
               COUNT(*) AS total_c,
               COUNT(CASE WHEN coautoria_aluno = TRUE THEN 1 END) AS disc_c
        FROM tb_artigo_conferencia
        WHERE ano BETWEEN ? AND ?{restricao_c}{sql_fonte()}{sql_recorte_docente('tb_artigo_conferencia', 'ano')}
        GROUP BY id_lattes
    """

    # Quem entra no divisor (e, portanto, na série que sustenta desvio e
    # mediana). Sob o regime de ingresso é o quadro inteiro, como sempre foi.
    # Sob vigência, é quem esteve credenciado em ao menos um ano da janela: um
    # docente descredenciado durante todo o período não teve produção contada
    # -- o `EXISTS` da vigência zera tudo dele --, então mantê-lo no divisor
    # seria tratá-lo como alguém que estava lá e não produziu, rebaixando o per
    # capita de todo mundo. A vigência parcial (credenciado em 2 dos 5 anos)
    # continua pesando como um docente inteiro aqui; a leitura normalizada por
    # ano fica na tabela anual, ao lado.
    if regime_recorte == REGIME_VIGENCIA and tabela_tem_linhas(conexao, "tb_credenciamento_anos"):
        query_docentes = """
            SELECT p.id_lattes FROM tb_professores p
            WHERE EXISTS (SELECT 1 FROM tb_credenciamento_anos c
                          WHERE c.id_lattes = p.id_lattes AND c.ano BETWEEN ? AND ?)
        """
        df = conexao.execute(query_docentes, [ano_ini, ano_fim_]).df()
        base_divisor = "credenciados"
    else:
        df = conexao.execute("SELECT id_lattes FROM tb_professores").df()
        base_divisor = "cadastrados"

    df = df.merge(conexao.execute(query_p, [ano_ini, ano_fim_]).df(), on="id_lattes", how="left")
    df = df.merge(conexao.execute(query_c, [ano_ini, ano_fim_]).df(), on="id_lattes", how="left")
    for coluna in ("total_p", "disc_p", "total_c", "disc_c"):
        df[coluna] = df[coluna].fillna(0)

    return {
        "docentes": len(df),
        "base_divisor": base_divisor,
        "series": {
            "conferencia": df["total_c"],
            "periodico": df["total_p"],
            "geral": df["total_c"] + df["total_p"],
            "conferencia_discentes": df["disc_c"],
            "periodico_discentes": df["disc_p"],
            "geral_discentes": df["disc_c"] + df["disc_p"],
        },
    }


def frase_divisor(docentes, base_divisor, ano_ini, ano_fim_):
    """Descreve o divisor que a média usou de fato. Existe para que a memória
    de cálculo nunca anuncie um divisor diferente do que produziu o número --
    sob vigência ele deixa de ser o quadro inteiro."""
    if base_divisor == "credenciados":
        return (
            f"O divisor é {docentes}, o número de docentes que estiveram credenciados em "
            f"pelo menos um ano de {ano_ini} a {ano_fim_}. Quem não esteve credenciado em "
            "nenhum ano da janela fica fora do divisor **e** da série: a vigência já zera "
            "toda a produção dele no período, então mantê-lo aqui seria contá-lo como um "
            "docente presente que nada produziu, rebaixando o per capita de todos os demais."
        )
    return (
        f"O divisor é {docentes}, o total de docentes cadastrados, sem nenhum recorte por "
        "docente."
    )


def contar_papers_por_ano(conexao, ano_ini, ano_fim_, restrito=False):
    """Uma linha por ano da janela: papers contados naquele ano e quantos
    docentes estavam credenciados nele. Só faz sentido sob o regime de
    vigência, que é o único em que "docentes credenciados naquele ano" existe.

    Anos sem ninguém credenciado são devolvidos com `docentes = 0` e tratados
    pelo chamador -- não são raros: `ANO_MIN` vem dos dados (1974 nesta base) e
    a planilha de credenciamento começa muito depois, então uma janela larga
    tem dezenas de anos vazios, e dividir por eles seria uma divisão por zero.
    """
    restricao_p = " AND maior_percentil >= 50.0" if restrito else ""
    restricao_c = " AND estrato IN ('A1', 'A2', 'A3', 'A4')" if restrito else ""

    query = f"""
        WITH anos AS (SELECT UNNEST(range(?, ? + 1)) AS ano),
        credenciados AS (
            SELECT ano, COUNT(DISTINCT id_lattes) AS docentes
            FROM tb_credenciamento_anos WHERE ano BETWEEN ? AND ? GROUP BY ano
        ),
        papers AS (
            SELECT ano_pub AS ano, COUNT(*) AS n
            FROM tb_artigo_periodico
            WHERE ano_pub BETWEEN ? AND ?{restricao_p}{sql_fonte()}
                  {sql_recorte_docente('tb_artigo_periodico', 'ano_pub')}
            GROUP BY ano_pub
            UNION ALL
            SELECT ano, COUNT(*) AS n
            FROM tb_artigo_conferencia
            WHERE ano BETWEEN ? AND ?{restricao_c}{sql_fonte()}
                  {sql_recorte_docente('tb_artigo_conferencia', 'ano')}
            GROUP BY ano
        )
        SELECT a.ano AS "Ano",
               COALESCE((SELECT SUM(n) FROM papers WHERE papers.ano = a.ano), 0) AS "Papers",
               COALESCE(c.docentes, 0) AS "Docentes Credenciados"
        FROM anos a LEFT JOIN credenciados c ON c.ano = a.ano
        ORDER BY a.ano
    """
    return conexao.execute(query, [ano_ini, ano_fim_] * 4).df()


def renderizar_tabela_anual_vigencia(conexao, ano_ini, ano_fim_, restrito=False):
    """Bloco "Por ano": papers e docentes credenciados em cada ano da janela,
    fechando com a média das médias anuais.

    É a leitura complementar à tabela de dispersão acima. Aquela responde
    "quanto produziu o docente típico na janela inteira"; esta responde "como
    foi o ano típico", e é a única das duas em que a vigência parcial pesa
    menos -- um docente credenciado em 2 dos 5 anos entra em 2 linhas, não em
    5."""
    df_ano = contar_papers_por_ano(conexao, ano_ini, ano_fim_, restrito)

    com_docentes = df_ano[df_ano["Docentes Credenciados"] > 0].copy()
    if com_docentes.empty:
        st.info(
            f"Nenhum docente esteve credenciado entre {ano_ini} e {ano_fim_}, "
            "então não há média anual para exibir nesta janela."
        )
        return

    com_docentes["Papers / Docente"] = (
        com_docentes["Papers"] / com_docentes["Docentes Credenciados"]
    )
    anos_vazios = len(df_ano) - len(com_docentes)

    st.markdown("##### Por Ano")
    st.dataframe(
        com_docentes.style.format({
            "Ano": "{:.0f}",
            "Papers": "{:.0f}",
            "Docentes Credenciados": "{:.0f}",
            "Papers / Docente": lambda v: _num_br(v, 3),
        }),
        use_container_width=True,
        hide_index=True,
    )

    media_anual = float(com_docentes["Papers / Docente"].mean())
    total_papers = int(com_docentes["Papers"].sum())
    total_docente_anos = int(com_docentes["Docentes Credenciados"].sum())
    razao_totais = total_papers / total_docente_anos if total_docente_anos else 0.0

    st.markdown(
        f"**Média das médias anuais: {_num_br(media_anual, 3)} papers por docente, por ano.** "
        f"É a média simples da última coluna, sobre os {len(com_docentes)} ano(s) com pelo "
        "menos um docente credenciado"
        + (f" ({anos_vazios} ano(s) da janela sem ninguém credenciado ficaram de fora)." if anos_vazios else ".")
    )
    st.caption(
        f"Cada ano pesa igual nessa média, independentemente de quantos docentes estavam "
        f"credenciados nele. A leitura alternativa é a razão dos totais — {total_papers} papers "
        f"÷ {total_docente_anos} docente-anos = {_num_br(razao_totais, 3)} —, em que cada "
        "docente-ano pesa igual e, portanto, anos com mais docentes credenciados puxam mais. "
        "As duas divergem quando o tamanho do corpo credenciado varia ao longo da janela: "
        "anos magros costumam ter taxa por docente mais alta e, na média das médias, valem "
        "tanto quanto os anos cheios. Atenção também ao último ano da janela, que costuma "
        "estar incompleto na base e entra aqui como um ponto de peso inteiro."
    )


def renderizar_papers_per_capita(conexao, ano_ini, ano_fim_, restrito=False, nota=""):
    """Bloco dos seis índices per capita de papers: uma única tabela com total,
    média (o per capita), desvio padrão e mediana da série por docente de cada
    indicador, seguida da memória de cálculo. As métricas separadas que antes
    exibiam só a média saíram -- a coluna Média da tabela é o mesmo número."""
    indices = contar_papers_per_capita(conexao, ano_ini, ano_fim_, restrito)
    docentes = indices["docentes"]
    base_divisor = indices["base_divisor"]
    series = indices["series"]

    rotulo_divisor = ("Docentes Credenciados na Janela" if base_divisor == "credenciados"
                      else "Docentes Cadastrados")
    st.markdown(f"#### Índices Per Capita (Papers ÷ {rotulo_divisor})")

    ROTULOS = {
        "conferencia": "Papers de Conferência",
        "periodico": "Papers de Periódicos",
        "geral": "Papers Geral",
        "conferencia_discentes": "Conferência c/ Discentes",
        "periodico_discentes": "Periódicos c/ Discentes",
        "geral_discentes": "Geral c/ Discentes",
    }

    renderizar_tabela_dispersao(
        [(ROTULOS[chave], series[chave]) for chave in ROTULOS], casas=2,
    )

    # Sob vigência, a leitura por ano vem logo abaixo: é a única das duas em
    # que a vigência parcial pesa menos. Sob ingresso ela não existe -- não há
    # "docentes credenciados naquele ano" para servir de divisor.
    if base_divisor == "credenciados":
        renderizar_tabela_anual_vigencia(conexao, ano_ini, ano_fim_, restrito)

    renderizar_explicacao_calculos(
        descricao_x="o número de papers daquele docente no recorte abaixo.",
        recorte=(
            f"Janela de {ano_ini} a {ano_fim_}; filtro de fonte da barra lateral; e "
            f"{frase_recorte_docente()}. São os mesmos "
            f"recortes da tabela por docente acima. "
            + frase_divisor(docentes, base_divisor, ano_ini, ano_fim_)
            + " \"Com discentes\" são os papers em que a detecção de coautoria encontrou "
            "ao menos um aluno entre os autores."
            + (f" {nota}" if nota else "")
        ),
        observacoes=[OBS_DUPLA_CONTAGEM] + ([
            "A tabela **Por Ano** responde uma pergunta diferente da tabela de dispersão. "
            "Na dispersão, cada docente é uma observação e a vigência parcial não pesa: "
            "quem esteve credenciado em 2 dos 5 anos entra como um docente inteiro. Na "
            "tabela por ano ele entra em 2 linhas, não em 5 — por isso a média das médias "
            "anuais (papers por docente **por ano**) não é a média da dispersão dividida "
            "pelo tamanho da janela.",
        ] if base_divisor == "credenciados" else []),
    )


# Filtro de período compartilhado entre as páginas padrão: inicializado uma
# única vez para que o intervalo escolhido persista ao alternar de dataview.
if "filtro_ano_inicio" not in st.session_state:
    st.session_state["filtro_ano_inicio"] = max(ANO_MIN, ANO_MAX - 4)
if "filtro_ano_fim" not in st.session_state:
    st.session_state["filtro_ano_fim"] = ANO_MAX

# ==========================================
# 4.2 RODAPÉ DA BARRA LATERAL (RESUMO DE ESTADO)
# ==========================================
# Duas linhas discretas: de onde vêm os dados e quando foram atualizados pela
# última vez. O detalhe (botões, logs, bases de comparação) fica em Configurações.
st.sidebar.divider()
st.sidebar.caption(f"Base: `{CAMINHO_BASE_INSTITUCIONAL}`")
if status_process.get("state") == "done":
    st.sidebar.caption(f"Dados processados em {_fmt_ts(status_process.get('finished_at'))}")
elif status_process.get("state") == "error":
    st.sidebar.caption("Último processamento falhou — ver Configurações")
else:
    st.sidebar.caption("Dados nunca reprocessados por aqui")
if jobs.existe_algum_job_rodando():
    st.sidebar.caption("⏳ Atualização em andamento (ver Configurações)")

def montar_query_credenciamento(restrito, regime=None):
    """SQL do score consolidado de credenciamento — a regra de pontuação da
    página "Credenciamento", extraída para cá porque a página
    "Credenciamento (vigência)" precisa rodar exatamente a mesma conta sob os
    dois regimes de contagem e compará-las. Duas cópias da tabela de pesos
    divergiriam no primeiro ajuste de critério.

    `restrito=False` soma os oito estratos (com o bônus de 1,25 para
    periódicos da área de computação e o de 1,5 para coautoria discente);
    `restrito=True` conta apenas A1-A4, zerando o resto. `regime` escolhe o
    recorte por docente (`None` = o da barra lateral).

    A query espera quatro parâmetros, nesta ordem: ano_inicio, ano_fim,
    ano_inicio, ano_fim (a janela entra uma vez para periódicos e uma para
    conferências)."""
    if not restrito:
        return f"""
            WITH cte_periodicos AS (
                SELECT id_lattes,
                    COUNT(*) AS total_p,
                    COUNT(CASE WHEN computation_area = TRUE THEN 1 END) AS comp_p,
                    SUM(CASE 
                        WHEN computation_area = TRUE THEN 
                            (CASE WHEN maior_percentil >= 87.5 THEN 1.000 WHEN maior_percentil >= 75.0 THEN 0.875 WHEN maior_percentil >= 62.5 THEN 0.750 WHEN maior_percentil >= 50.0 THEN 0.625 WHEN maior_percentil >= 37.5 THEN 0.500 WHEN maior_percentil >= 25.0 THEN 0.375 WHEN maior_percentil >= 12.5 THEN 0.250 ELSE 0.125 END) * 1.25
                        ELSE 
                            (CASE WHEN maior_percentil >= 87.5 THEN 1.000 WHEN maior_percentil >= 75.0 THEN 0.875 WHEN maior_percentil >= 62.5 THEN 0.750 WHEN maior_percentil >= 50.0 THEN 0.625 WHEN maior_percentil >= 37.5 THEN 0.500 WHEN maior_percentil >= 25.0 THEN 0.375 WHEN maior_percentil >= 12.5 THEN 0.250 ELSE 0.125 END)
                    END * CASE WHEN coautoria_aluno = TRUE THEN 1.5 ELSE 1.0 END) AS pontos_p
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?{sql_fonte()}{sql_recorte_docente('tb_artigo_periodico', 'ano_pub', regime=regime)}
                GROUP BY id_lattes
            ),
            cte_conferencias AS (
                SELECT id_lattes,
                    COUNT(*) AS total_c,
                    SUM(CASE WHEN estrato = 'A1' THEN 1.000 WHEN estrato = 'A2' THEN 0.875 WHEN estrato = 'A3' THEN 0.750 WHEN estrato = 'A4' THEN 0.625 WHEN estrato = 'A5' THEN 0.500 WHEN estrato = 'A6' THEN 0.375 WHEN estrato = 'A7' THEN 0.250 ELSE 0.125 END * CASE WHEN coautoria_aluno = TRUE THEN 1.5 ELSE 1.0 END) AS pontos_c
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?{sql_fonte()}{sql_recorte_docente('tb_artigo_conferencia', 'ano', regime=regime)}
                GROUP BY id_lattes
            )
            SELECT 
                p.nome_completo AS "Docente Pesquisador",
                COALESCE(cp.total_p, 0) AS "Total Periódicos",
                COALESCE(cc.total_c, 0) AS "Total Conferências",
                COALESCE(cp.total_p, 0) + COALESCE(cc.total_c, 0) AS "Total Papers",
                COALESCE(cp.comp_p, 0) AS "Qtd Periódicos Computação",
                COALESCE(ROUND(cp.pontos_p, 3), 0) AS "Score Periódicos",
                COALESCE(ROUND(cc.pontos_c, 3), 0) AS "Score Conferências",
                COALESCE(ROUND(cp.pontos_p, 3), 0) + COALESCE(ROUND(cc.pontos_c, 3), 0) AS "Score Institucional Consolidado"
            FROM tb_professores p
            LEFT JOIN cte_periodicos cp ON p.id_lattes = cp.id_lattes
            LEFT JOIN cte_conferencias cc ON p.id_lattes = cc.id_lattes
            ORDER BY "Score Institucional Consolidado" DESC;
        """
    else:
        return f"""
            WITH cte_p_class AS (
                SELECT id_lattes, computation_area, coautoria_aluno, -- << CORRIGIDO AQUI (Inclusão da coluna na CTE)
                    CASE 
                        WHEN maior_percentil >= 87.5 THEN 1.000 WHEN maior_percentil >= 75.0 THEN 0.875
                        WHEN maior_percentil >= 62.5 THEN 0.750 WHEN maior_percentil >= 50.0 THEN 0.625
                        ELSE 0.000 
                    END AS peso_base
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ? AND maior_percentil >= 50.0{sql_fonte()}{sql_recorte_docente('tb_artigo_periodico', 'ano_pub', regime=regime)}
            ),
            cte_p_agg AS (
                SELECT id_lattes,
                    COUNT(*) AS total_p,
                    COUNT(CASE WHEN computation_area = TRUE THEN 1 END) AS comp_p,
                    SUM(CASE WHEN computation_area = TRUE THEN peso_base * 1.25 ELSE peso_base END * CASE WHEN coautoria_aluno = TRUE THEN 1.5 ELSE 1.0 END) AS pontos_p
                FROM cte_p_class GROUP BY id_lattes
            ),
            cte_c_class AS (
                SELECT id_lattes,
                    CASE 
                        WHEN estrato = 'A1' THEN 1.000 WHEN estrato = 'A2' THEN 0.875
                        WHEN estrato = 'A3' THEN 0.750 WHEN estrato = 'A4' THEN 0.625
                        ELSE 0.000 
                    END AS peso_base,
                    coautoria_aluno
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ? AND estrato IN ('A1', 'A2', 'A3', 'A4'){sql_fonte()}{sql_recorte_docente('tb_artigo_conferencia', 'ano', regime=regime)}
            ),
            cte_c_agg AS (
                SELECT id_lattes,
                    COUNT(*) AS total_c,
                    SUM(peso_base * CASE WHEN coautoria_aluno = TRUE THEN 1.5 ELSE 1.0 END) AS pontos_c
                FROM cte_c_class GROUP BY id_lattes
            )
            SELECT 
                p.nome_completo AS "Docente Pesquisador",
                COALESCE(cp.total_p, 0) AS "Total Periódicos",
                COALESCE(cc.total_c, 0) AS "Total Conferências",
                COALESCE(cp.total_p, 0) + COALESCE(cc.total_c, 0) AS "Total Papers",
                COALESCE(cp.comp_p, 0) AS "Qtd Periódicos Computação",
                COALESCE(ROUND(cp.pontos_p, 3), 0) AS "Score Periódicos",
                COALESCE(ROUND(cc.pontos_c, 3), 0) AS "Score Conferências",
                COALESCE(ROUND(cp.pontos_p, 3), 0) + COALESCE(ROUND(cc.pontos_c, 3), 0) AS "Score Institucional Consolidado"
            FROM tb_professores p
            LEFT JOIN cte_p_agg cp ON p.id_lattes = cp.id_lattes
            LEFT JOIN cte_c_agg cc ON p.id_lattes = cc.id_lattes
            ORDER BY "Score Institucional Consolidado" DESC;
        """


# ==========================================
# 5. DESENVOLVIMENTO DOS MÓDULOS (DATAVIEWS)
# ==========================================

# ------------------------------------------
# PÁGINA 1: INDICADORES INSTITUCIONAIS
# ------------------------------------------
if pagina_selecionada == "Indicadores Institucionais":
    st.title("Indicadores Institucionais (Métricas Globais)")
    st.markdown("Consolidação estatística descritiva da base total de dados da instituição.")

    st.subheader("Filtro de Período")
    f_ano_inicio, f_ano_fim = renderizar_filtro_periodo(ANO_MIN, ANO_MAX, "indicadores")

    res_docentes = con.execute("SELECT COUNT(id_lattes) FROM tb_professores").fetchone()
    
    # Métricas filtradas por ano
    query_p = f"SELECT COUNT(id_artigo_periodico) FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?{sql_fonte()}{sql_recorte_docente('tb_artigo_periodico', 'ano_pub')}"
    res_periodicos = con.execute(query_p, [f_ano_inicio, f_ano_fim]).fetchone()

    query_c = f"SELECT COUNT(id_artigo_conferencia) FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?{sql_fonte()}{sql_recorte_docente('tb_artigo_conferencia', 'ano')}"
    res_conferencias = con.execute(query_c, [f_ano_inicio, f_ano_fim]).fetchone()
    
    total_docentes = res_docentes[0] if res_docentes else 0
    total_periodicos = res_periodicos[0] if res_periodicos else 0
    total_conferencias = res_conferencias[0] if res_conferencias else 0
    total_producoes = total_periodicos + total_conferencias
    
    # Mesma janela, fonte e regime de contagem das métricas acima, agora
    # abertos por docente: a coluna Média da tabela abaixo é a antiga métrica
    # "Média de Produções / Docente", que saiu daqui para não ficar repetida.
    indices_indicadores = contar_papers_per_capita(con, f_ano_inicio, f_ano_fim)
    series_indicadores = indices_indicadores["series"]

    col1, col2 = st.columns(2)
    col1.metric("Docentes Cadastrados", total_docentes)
    col2.metric("Total de Produções Bibliográficas", total_producoes)
    if indices_indicadores["base_divisor"] == "credenciados":
        # O quadro cadastrado continua sendo 31; quem divide a média, sob
        # vigência, é outro número. Dizer isso aqui evita que a métrica ao lado
        # da tabela pareça o divisor dela.
        col1.caption(
            f"{indices_indicadores['docentes']} estiveram credenciados em algum ano de "
            f"{f_ano_inicio} a {f_ano_fim} — é esse o divisor das médias abaixo."
        )

    st.markdown("##### Produção por Docente")
    renderizar_tabela_dispersao(
        [
            ("Produções / Docente", series_indicadores["geral"]),
            ("Periódicos / Docente", series_indicadores["periodico"]),
            ("Conferências / Docente", series_indicadores["conferencia"]),
        ],
        casas=2,
    )
    renderizar_explicacao_calculos(
        descricao_x="o número de produções bibliográficas daquele docente no recorte abaixo.",
        recorte=(
            f"Janela de {f_ano_inicio} a {f_ano_fim}; filtro de fonte da barra lateral; e "
            f"{frase_recorte_docente()}. "
            + frase_divisor(
                indices_indicadores["docentes"], indices_indicadores["base_divisor"],
                f_ano_inicio, f_ano_fim,
            )
        ),
        observacoes=[OBS_DUPLA_CONTAGEM],
    )

    st.markdown("---")
    st.subheader("Distribuição do Corpo Docente por Tipo de Veículo")
    col_p, col_c = st.columns(2)
    col_p.metric("Artigos Publicados em Periódicos", total_periodicos)
    col_c.metric("Trabalhos Publicados em Conferências", total_conferencias)

# ------------------------------------------
# PÁGINA 2: ANÁLISE POR DOCENTE
# ------------------------------------------
elif pagina_selecionada == "Análise por Docente":
    st.title("Análise por Docente")
    st.markdown("Visão analítica quantitativa de publicações segregadas por pesquisador.")

    st.subheader("Filtro de Período")
    f_ano_inicio, f_ano_fim = renderizar_filtro_periodo(ANO_MIN, ANO_MAX, "docente")

    # Tabela Mestra Unificada. A segunda coluna acompanha o regime de contagem
    # em vigor: de nada adianta mostrar o ano de ingresso ao lado de números
    # que foram recortados pela vigência do credenciamento.
    if regime_recorte == REGIME_VIGENCIA:
        COLUNA_REGIME = "Anos Vigentes na Janela"
        select_regime = (
            f'(SELECT COUNT(*) FROM tb_credenciamento_anos c WHERE c.id_lattes = p.id_lattes '
            f'AND c.ano BETWEEN {f_ano_inicio} AND {f_ano_fim}) AS "{COLUNA_REGIME}"'
        )
        # O GROUP BY precisa carregar a chave, já que a subconsulta depende dela.
        group_by_regime = "p.id_lattes, p.nome_completo"
    else:
        COLUNA_REGIME = "Ano de Ingresso"
        select_regime = f'p.data_ingresso AS "{COLUNA_REGIME}"'
        group_by_regime = "p.nome_completo, p.data_ingresso"

    query_mestra = f"""
        SELECT
            p.nome_completo AS Docente,
            {select_regime},
            COUNT(DISTINCT a_p.id_artigo_periodico) AS Periodicos,
            COUNT(DISTINCT a_c.id_artigo_conferencia) AS Conferencias,
            (COUNT(DISTINCT a_p.id_artigo_periodico) + COUNT(DISTINCT a_c.id_artigo_conferencia)) AS Total
        FROM tb_professores p
        LEFT JOIN tb_artigo_periodico a_p ON p.id_lattes = a_p.id_lattes AND a_p.ano_pub BETWEEN {f_ano_inicio} AND {f_ano_fim}{sql_fonte('a_p.fontes')}{sql_recorte_docente('a_p', 'ano_pub')}
        LEFT JOIN tb_artigo_conferencia a_c ON p.id_lattes = a_c.id_lattes AND a_c.ano BETWEEN {f_ano_inicio} AND {f_ano_fim}{sql_fonte('a_c.fontes')}{sql_recorte_docente('a_c', 'ano')}
        GROUP BY {group_by_regime}
        ORDER BY Total DESC
    """
    df_mestra = con.execute(query_mestra).df()

    def _destacar_fora_do_periodo(linha):
        """Sinaliza (linha inteira) o pesquisador que não fazia parte do quadro
        durante a janela em análise -- ingresso posterior ao fim do período, ou
        nenhum ano de credenciamento dentro dele, conforme o regime. A produção
        exibida (sempre recortada por `sql_recorte_docente`) fica zerada aqui."""
        valor = linha[COLUNA_REGIME]
        if regime_recorte == REGIME_VIGENCIA:
            fora = valor == 0
        else:
            fora = pd.notna(valor) and valor > f_ano_fim
        cor = "background-color: rgba(255, 75, 75, 0.25)" if fora else ""
        return [cor] * len(linha)

    tabela_mestra_estilizada = (
        df_mestra.style
        .format({COLUNA_REGIME: lambda v: "—" if pd.isna(v) else str(int(v))})
        .apply(_destacar_fora_do_periodo, axis=1)
    )

    st.subheader("Volume de Produção por Pesquisador")
    st.dataframe(tabela_mestra_estilizada, use_container_width=True, hide_index=True)
    if regime_recorte == REGIME_VIGENCIA:
        st.caption(
            "Linhas destacadas: pesquisador sem nenhum ano de credenciamento dentro do "
            "período selecionado — nenhuma produção dele conta nesta janela. A coluna "
            "conta os anos de vigência que caem dentro do período, não a vigência total."
        )
    else:
        st.caption(
            "Linhas destacadas: pesquisador ingressou no programa depois do fim do período "
            "selecionado — não fazia parte dele durante a janela em análise, por isso não é contado."
        )

    st.markdown("---")
    st.subheader("Análise Gráfica de Produção")
    
    tipo_grafico = st.radio(
        "Selecione a métrica para visualização:", 
        ["Total Somado", "Apenas Periódicos", "Apenas Conferências"], 
        horizontal=True
    )
    
    if tipo_grafico == "Total Somado":
        st.bar_chart(df_mestra.set_index('Docente')['Total'], use_container_width=True)
    elif tipo_grafico == "Apenas Periódicos":
        st.bar_chart(df_mestra.set_index('Docente')['Periodicos'], use_container_width=True)
    else:
        st.bar_chart(df_mestra.set_index('Docente')['Conferencias'], use_container_width=True)

# ------------------------------------------
# PÁGINA 3: SÉRIE HISTÓRICA DA PRODUÇÃO
# ------------------------------------------
elif pagina_selecionada == "Série Histórica da Produção":
    st.title("Série Histórica da Produção Científica")
    st.markdown("Evolução temporal do volume de publicações institucionais.")
    
    aba_p, aba_c = st.tabs(["Linha de Tempo - Periódicos", "Linha de Tempo - Conferências"])
    
    with aba_p:
        st.subheader("Histórico de Publicações em Periódicos")
        query_p = f"SELECT CAST(ano_pub AS VARCHAR) AS Ano, COUNT(*) AS Quantidade FROM tb_artigo_periodico WHERE ano_pub IS NOT NULL{sql_fonte()}{sql_recorte_docente('tb_artigo_periodico', 'ano_pub')} GROUP BY ano_pub ORDER BY ano_pub"
        df_p = con.execute(query_p).df()
        if not df_p.empty: st.bar_chart(data=df_p, x='Ano', y='Quantidade', use_container_width=True)

    with aba_c:
        st.subheader("Histórico de Publicações em Conferências")
        query_c = f"SELECT CAST(ano AS VARCHAR) AS Ano, COUNT(*) AS Quantidade FROM tb_artigo_conferencia WHERE ano IS NOT NULL{sql_fonte()}{sql_recorte_docente('tb_artigo_conferencia', 'ano')} GROUP BY ano ORDER BY ano"
        df_c = con.execute(query_c).df()
        if not df_c.empty: st.bar_chart(data=df_c, x='Ano', y='Quantidade', use_container_width=True)

# ------------------------------------------
# PÁGINA 4: REPOSITÓRIO GERAL DE ARTIGOS
# ------------------------------------------
elif pagina_selecionada == "Repositório Geral de Artigos":
    st.title("Repositório Geral de Artigos")
    st.markdown("Mecanismo de auditoria e detalhamento individual dos registros bibliográficos.")
    
    aba_p, aba_c = st.tabs(["Base de Dados - Periódicos", "Base de Dados - Conferências"])
    
    with aba_p:
        query_p = f"""
            SELECT 
                p.nome_completo AS Docente, 
                CAST(a.ano_pub AS VARCHAR) AS Ano, 
                a.titulo_artigo AS Titulo, 
                a.autores AS Autores,
                a.titulo_revista_lattes AS Revista, 
                a.maior_percentil AS Percentil,
                CASE WHEN a.computation_area = TRUE THEN 'Sim' ELSE 'Não' END AS "Área Computação",
                CASE WHEN a.coautoria_aluno = TRUE THEN 'Sim' ELSE 'Não' END AS "Coautoria Aluno"
            FROM tb_artigo_periodico a 
            INNER JOIN tb_professores p ON a.id_lattes = p.id_lattes {sql_fonte('a.fontes', 'WHERE')}
            ORDER BY a.ano_pub DESC
        """
        df_p = con.execute(query_p).df()
        st.dataframe(df_p, use_container_width=True, hide_index=True)
        
    with aba_c:
        query_c = f"""
            SELECT 
                p.nome_completo AS Docente, 
                CAST(a.ano AS VARCHAR) AS Ano, 
                a.titulo_artigo AS Titulo, 
                a.autores AS Autores,
                a.titulo_evento_lattes AS Evento, 
                a.estrato AS Estrato, 
                a.tipo_match AS "Tipo Match",
                CASE WHEN a.coautoria_aluno = TRUE THEN 'Sim' ELSE 'Não' END AS "Coautoria Aluno"
            FROM tb_artigo_conferencia a 
            INNER JOIN tb_professores p ON a.id_lattes = p.id_lattes {sql_fonte('a.fontes', 'WHERE')}
            ORDER BY a.ano DESC
        """
        df_c = con.execute(query_c).df()
        st.dataframe(df_c, use_container_width=True, hide_index=True)

# ------------------------------------------
# PÁGINA 5: AVALIAÇÃO QUADRIENAL GERAL (A1-A8)
# ------------------------------------------
elif pagina_selecionada == "Avaliação Quadrienal Geral (A1-A8)":
    st.title("Índice de Produtividade Intelectual Geral (A1-A8)")
    st.markdown("Modelo quantitativo abrangente baseado na ponderação integral da produção bibliográfica.")

    st.subheader("Configuração da Janela Temporal")
    ano_inicio, ano_fim = renderizar_filtro_periodo(ANO_MIN, ANO_MAX, "quadrienal_geral")

    st.markdown(f"**Período sob análise regulamentar: {ano_inicio} a {ano_fim}**")
    
    aba_p, aba_c = st.tabs(["Indicadores de Periódicos", "Indicadores de Conferências"])
    
    with aba_p:
        st.subheader("Índice de Produção Bibliográfica em Periódicos")
        query_ranking_p = f"""
            WITH cte_classificacao AS (
                SELECT id_lattes, maior_percentil, computation_area,
                    CASE 
                        WHEN maior_percentil >= 87.5 THEN 'A1' WHEN maior_percentil >= 75.0 THEN 'A2'
                        WHEN maior_percentil >= 62.5 THEN 'A3' WHEN maior_percentil >= 50.0 THEN 'A4'
                        WHEN maior_percentil >= 37.5 THEN 'A5' WHEN maior_percentil >= 25.0 THEN 'A6'
                        WHEN maior_percentil >= 12.5 THEN 'A7' ELSE 'A8' 
                    END AS estrato,
                    CASE 
                        WHEN maior_percentil >= 87.5 THEN 1.000 WHEN maior_percentil >= 75.0 THEN 0.875
                        WHEN maior_percentil >= 62.5 THEN 0.750 WHEN maior_percentil >= 50.0 THEN 0.625
                        WHEN maior_percentil >= 37.5 THEN 0.500 WHEN maior_percentil >= 25.0 THEN 0.375
                        WHEN maior_percentil >= 12.5 THEN 0.250 ELSE 0.125 
                    END AS peso_base
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?{sql_fonte()}{sql_recorte_docente('tb_artigo_periodico', 'ano_pub')}
            ),
            cte_pontuacao AS (
                SELECT id_lattes, estrato, peso_base AS pontos_artigo FROM cte_classificacao
            )
            SELECT p.nome_completo AS "Pesquisador",
                COUNT(CASE WHEN a.estrato = 'A1' THEN 1 END) AS A1, COUNT(CASE WHEN a.estrato = 'A2' THEN 1 END) AS A2,
                COUNT(CASE WHEN a.estrato = 'A3' THEN 1 END) AS A3, COUNT(CASE WHEN a.estrato = 'A4' THEN 1 END) AS A4,
                COUNT(CASE WHEN a.estrato = 'A5' THEN 1 END) AS A5, COUNT(CASE WHEN a.estrato = 'A6' THEN 1 END) AS A6,
                COUNT(CASE WHEN a.estrato = 'A7' THEN 1 END) AS A7, COUNT(CASE WHEN a.estrato = 'A8' THEN 1 END) AS A8,
                COUNT(a.estrato) AS "Total Itens", COALESCE(ROUND(SUM(a.pontos_artigo), 3), 0) AS "Índice Final P."
            FROM tb_professores p LEFT JOIN cte_pontuacao a ON p.id_lattes = a.id_lattes GROUP BY p.nome_completo ORDER BY "Índice Final P." DESC;
        """
        df_ranking_p = con.execute(query_ranking_p, [ano_inicio, ano_fim]).df()
        if not df_ranking_p.empty:
            st.dataframe(df_ranking_p.style.background_gradient(subset=['Índice Final P.'], cmap='Blues').format({'Índice Final P.': lambda x: f"{x:.2f}".replace('.', ',')}), use_container_width=True, hide_index=True)
            st.markdown("#### Distribuição Qualitativa por Estrato (A1-A8)")
            st.bar_chart(df_ranking_p.set_index('Pesquisador')[['A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7', 'A8']], use_container_width=True, stack=True)
            st.markdown("#### Pontuação Total Atingida")
            st.bar_chart(df_ranking_p.set_index('Pesquisador')['Índice Final P.'], use_container_width=True)

    with aba_c:
        st.subheader("Índice de Produção Bibliográfica em Conferências")
        query_ranking_c = f"""
            WITH cte_classificacao AS (
                SELECT id_lattes, estrato,
                    CASE 
                        WHEN estrato = 'A1' THEN 1.000 WHEN estrato = 'A2' THEN 0.875
                        WHEN estrato = 'A3' THEN 0.750 WHEN estrato = 'A4' THEN 0.625
                        WHEN estrato = 'A5' THEN 0.500 WHEN estrato = 'A6' THEN 0.375
                        WHEN estrato = 'A7' THEN 0.250 ELSE 0.125 
                    END AS pontos_artigo
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?{sql_fonte()}{sql_recorte_docente('tb_artigo_conferencia', 'ano')}
            )
            SELECT p.nome_completo AS "Pesquisador",
                COUNT(CASE WHEN a.estrato = 'A1' THEN 1 END) AS A1, COUNT(CASE WHEN a.estrato = 'A2' THEN 1 END) AS A2,
                COUNT(CASE WHEN a.estrato = 'A3' THEN 1 END) AS A3, COUNT(CASE WHEN a.estrato = 'A4' THEN 1 END) AS A4,
                COUNT(CASE WHEN a.estrato = 'A5' THEN 1 END) AS A5, COUNT(CASE WHEN a.estrato = 'A6' THEN 1 END) AS A6,
                COUNT(CASE WHEN a.estrato = 'A7' THEN 1 END) AS A7, COUNT(CASE WHEN a.estrato = 'A8' THEN 1 END) AS A8,
                COUNT(a.pontos_artigo) AS "Total Itens", COALESCE(ROUND(SUM(a.pontos_artigo), 3), 0) AS "Índice Final C."
            FROM tb_professores p LEFT JOIN cte_classificacao a ON p.id_lattes = a.id_lattes GROUP BY p.nome_completo ORDER BY "Índice Final C." DESC;
        """
        df_ranking_c = con.execute(query_ranking_c, [ano_inicio, ano_fim]).df()
        if not df_ranking_c.empty:
            st.dataframe(df_ranking_c.style.background_gradient(subset=['Índice Final C.'], cmap='Oranges').format({'Índice Final C.': lambda x: f"{x:.2f}".replace('.', ',')}), use_container_width=True, hide_index=True)
            st.markdown("#### Distribuição Qualitativa por Estrato (A1-A8)")
            st.bar_chart(df_ranking_c.set_index('Pesquisador')[['A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7', 'A8']], use_container_width=True, stack=True)
            st.markdown("#### Pontuação Total Atingida")
            st.bar_chart(df_ranking_c.set_index('Pesquisador')['Índice Final C.'], use_container_width=True)

    st.markdown("---")
    renderizar_papers_per_capita(con, ano_inicio, ano_fim, restrito=False)

# ------------------------------------------
# PÁGINA 6: AVALIAÇÃO QUADRIENAL RESTRITA (A1-A4)
# ------------------------------------------
elif pagina_selecionada == "Avaliação Quadrienal Restrita (A1-A4)":
    st.title("Índice de Produtividade Intelectual Restrito (A1-A4)")
    st.markdown("Critério normativo estrito limitando a contagem de pontos aos quatro estratos superiores da CAPES/Scopus.")

    st.subheader("Configuração da Janela Temporal")
    ano_inicio, ano_fim = renderizar_filtro_periodo(ANO_MIN, ANO_MAX, "quadrienal_restrita")

    st.markdown(f"**Período sob análise regulamentar estrita: {ano_inicio} a {ano_fim}**")
    
    aba_p, aba_c = st.tabs(["Indicadores Restritos de Periódicos", "Indicadores Restritos de Conferências"])
    
    with aba_p:
        st.subheader("Índice Restrito em Periódicos (A1-A4)")
        query_ranking_restrito_p = f"""
            WITH cte_classificacao AS (
                SELECT id_lattes, maior_percentil, computation_area,
                    CASE 
                        WHEN maior_percentil >= 87.5 THEN 'A1' WHEN maior_percentil >= 75.0 THEN 'A2'
                        WHEN maior_percentil >= 62.5 THEN 'A3' WHEN maior_percentil >= 50.0 THEN 'A4'
                        ELSE 'Outros' 
                    END AS estrato,
                    CASE 
                        WHEN maior_percentil >= 87.5 THEN 1.000 WHEN maior_percentil >= 75.0 THEN 0.875
                        WHEN maior_percentil >= 62.5 THEN 0.750 WHEN maior_percentil >= 50.0 THEN 0.625
                        ELSE 0.000 
                    END AS peso_base
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?{sql_fonte()}{sql_recorte_docente('tb_artigo_periodico', 'ano_pub')}
            ),
            cte_pontuacao AS (
                SELECT id_lattes, estrato, peso_base AS pontos_artigo FROM cte_classificacao WHERE estrato IN ('A1', 'A2', 'A3', 'A4')
            )
            SELECT p.nome_completo AS "Pesquisador",
                COUNT(CASE WHEN a.estrato = 'A1' THEN 1 END) AS A1, COUNT(CASE WHEN a.estrato = 'A2' THEN 1 END) AS A2,
                COUNT(CASE WHEN a.estrato = 'A3' THEN 1 END) AS A3, COUNT(CASE WHEN a.estrato = 'A4' THEN 1 END) AS A4,
                COUNT(a.estrato) AS "Total Itens (A1-A4)", COALESCE(ROUND(SUM(a.pontos_artigo), 3), 0) AS "Índice Restrito P."
            FROM tb_professores p LEFT JOIN cte_pontuacao a ON p.id_lattes = a.id_lattes GROUP BY p.nome_completo ORDER BY "Índice Restrito P." DESC;
        """
        df_ranking_restrito_p = con.execute(query_ranking_restrito_p, [ano_inicio, ano_fim]).df()
        if not df_ranking_restrito_p.empty:
            st.dataframe(df_ranking_restrito_p.style.background_gradient(subset=['Índice Restrito P.'], cmap='Blues').format({'Índice Restrito P.': lambda x: f"{x:.2f}".replace('.', ',')}), use_container_width=True, hide_index=True)
            st.markdown("#### Distribuição Qualitativa Restrita (A1-A4)")
            st.bar_chart(df_ranking_restrito_p.set_index('Pesquisador')[['A1', 'A2', 'A3', 'A4']], use_container_width=True, stack=True)
            st.markdown("#### Pontuação Restrita Atingida")
            st.bar_chart(df_ranking_restrito_p.set_index('Pesquisador')['Índice Restrito P.'], use_container_width=True)

    with aba_c:
        st.subheader("Índice Restrito em Conferências (A1-A4)")
        query_ranking_restrito_c = f"""
            WITH cte_classificacao AS (
                SELECT id_lattes, estrato,
                    CASE 
                        WHEN estrato = 'A1' THEN 1.000 WHEN estrato = 'A2' THEN 0.875
                        WHEN estrato = 'A3' THEN 0.750 WHEN estrato = 'A4' THEN 0.625
                        ELSE 0.000 
                    END AS pontos_artigo
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ? AND estrato IN ('A1', 'A2', 'A3', 'A4'){sql_fonte()}{sql_recorte_docente('tb_artigo_conferencia', 'ano')}
            )
            SELECT p.nome_completo AS "Pesquisador",
                COUNT(CASE WHEN a.estrato = 'A1' THEN 1 END) AS A1, COUNT(CASE WHEN a.estrato = 'A2' THEN 1 END) AS A2,
                COUNT(CASE WHEN a.estrato = 'A3' THEN 1 END) AS A3, COUNT(CASE WHEN a.estrato = 'A4' THEN 1 END) AS A4,
                COUNT(a.pontos_artigo) AS "Total Itens (A1-A4)", COALESCE(ROUND(SUM(a.pontos_artigo), 3), 0) AS "Índice Restrito C."
            FROM tb_professores p LEFT JOIN cte_classificacao a ON p.id_lattes = a.id_lattes GROUP BY p.nome_completo ORDER BY "Índice Restrito C." DESC;
        """
        df_ranking_restrito_c = con.execute(query_ranking_restrito_c, [ano_inicio, ano_fim]).df()
        if not df_ranking_restrito_c.empty:
            st.dataframe(df_ranking_restrito_c.style.background_gradient(subset=['Índice Restrito C.'], cmap='Oranges').format({'Índice Restrito C.': lambda x: f"{x:.2f}".replace('.', ',')}), use_container_width=True, hide_index=True)
            st.markdown("#### Distribuição Qualitativa Restrita (A1-A4)")
            st.bar_chart(df_ranking_restrito_c.set_index('Pesquisador')[['A1', 'A2', 'A3', 'A4']], use_container_width=True, stack=True)
            st.markdown("#### Pontuação Restrita Atingida")
            st.bar_chart(df_ranking_restrito_c.set_index('Pesquisador')['Índice Restrito C.'], use_container_width=True)

    st.markdown("---")
    renderizar_papers_per_capita(
        con, ano_inicio, ano_fim, restrito=True,
        nota="Contam apenas os papers nos estratos A1-A4, como no restante desta página.",
    )

# ------------------------------------------
# PÁGINA 7: RELATÓRIO DE CREDENCIAMENTO CONSOLIDADO
# ------------------------------------------
elif pagina_selecionada == "Relatório de Credenciamento Consolidado":
    st.title("Relatório de Credenciamento Consolidado")
    st.markdown("Módulo unificado para apuração final do score de credenciamento acadêmico, somando as pontuações obtidas nas categorias de Periódicos e Conferências.")

    st.subheader("Configuração da Janela de Consolidação")
    ano_inicio, ano_fim = renderizar_filtro_periodo(ANO_MIN, ANO_MAX, "credenciamento")

    st.markdown(f"**Janela regulamentar consolidada activa: {ano_inicio} a {ano_fim}**")
    
    filtro_tipo_avaliacao = st.radio("Selecione o Critério de Apuração institucional:", ["Pontuação Integral (A1-A8)", "Pontuação Restrita (A1-A4)"], horizontal=True)
    
    query_consolidada = montar_query_credenciamento(
        restrito=(filtro_tipo_avaliacao == "Pontuação Restrita (A1-A4)")
    )
    df_consolidado = con.execute(query_consolidada, [ano_inicio, ano_fim, ano_inicio, ano_fim]).df()
    
    if not df_consolidado.empty:
        aba_tab, aba_graf = st.tabs(["Relatório Estruturado (Matriz de Resultados)", "Gráfico Comparativo Institucional"])
        with aba_tab:
            st.subheader(f"Matriz Consolidada de Desempenho Científico ({filtro_tipo_avaliacao})")
            st.dataframe(
                df_consolidado.style
                .background_gradient(subset=['Score Institucional Consolidado'], cmap='Purples')
                .format({
                    'Score Periódicos': lambda x: f"{x:.2f}".replace('.', ','),
                    'Score Conferências': lambda x: f"{x:.2f}".replace('.', ','),
                    'Score Institucional Consolidado': lambda x: f"{x:.2f}".replace('.', ',')
                }),
                use_container_width=True,
                hide_index=True
            )
        with aba_graf:
            st.subheader("Métrica de Classificação e Desempenho Global do Corpo Docente")
            st.bar_chart(
                data=df_consolidado.set_index('Docente Pesquisador')[['Score Periódicos', 'Score Conferências']],
                use_container_width=True,
                stack=True
            )
    else:
        st.warning("Nenhum dado integrado foi localizado na janela temporal estipulada.")

    st.markdown("---")
    renderizar_papers_per_capita(
        con, ano_inicio, ano_fim,
        restrito=(filtro_tipo_avaliacao == "Pontuação Restrita (A1-A4)"),
        nota=f"Acompanha o critério selecionado acima ({filtro_tipo_avaliacao}).",
    )

# ------------------------------------------
# PÁGINA 7.1: CREDENCIAMENTO POR VIGÊNCIA
# ------------------------------------------
# Página nova, aditiva: a "Credenciamento" original continua exatamente como
# era. Aqui a vigência ano a ano (`tb_credenciamento_anos`) é o assunto, e não
# um filtro de fundo -- primeiro quem esteve credenciado em quais anos, depois
# quanto o score de credenciamento muda ao trocar a data de ingresso pela
# vigência. Só aparece no menu quando a base traz a tabela carregada.
elif pagina_selecionada == PAGINA_VIGENCIA:
    st.title("Credenciamento por Vigência (Anos Credenciados)")
    st.markdown(
        "Enquanto o restante do app pergunta *desde quando* a produção de um docente conta, "
        "esta página trabalha com *em quais anos* ela conta — a vigência do credenciamento, "
        "ano a ano, com descredenciamento, recredenciamento posterior e lacunas no meio."
    )

    st.subheader("Configuração da Janela de Consolidação")
    vig_ano_inicio, vig_ano_fim = renderizar_filtro_periodo(ANO_MIN, ANO_MAX, "vigencia")

    st.markdown(f"**Janela sob análise: {vig_ano_inicio} a {vig_ano_fim}**")

    # ---------- Bloco 1: quem esteve credenciado em quais anos ----------
    st.markdown("---")
    st.subheader("Vigência do Credenciamento por Docente")

    df_vigencia = con.execute(
        """
        SELECT
            p.nome_completo AS "Docente",
            p.data_ingresso AS "Ingresso",
            COUNT(c.ano) FILTER (WHERE c.ano BETWEEN ? AND ?) AS "Anos na Janela",
            COUNT(c.ano) AS "Anos no Total",
            MIN(c.ano) AS "Primeiro",
            MAX(c.ano) AS "Último"
        FROM tb_professores p
        LEFT JOIN tb_credenciamento_anos c ON p.id_lattes = c.id_lattes
        GROUP BY p.nome_completo, p.data_ingresso
        ORDER BY "Anos na Janela" DESC, "Docente"
        """,
        [vig_ano_inicio, vig_ano_fim],
    ).df()

    # Vigência contínua = o número de anos registrados fecha exatamente com o
    # intervalo entre o primeiro e o último. Qualquer diferença é lacuna.
    def _classificar_vigencia(linha):
        if linha["Anos no Total"] == 0:
            return "sem vigência registrada"
        extensao = int(linha["Último"]) - int(linha["Primeiro"]) + 1
        return "contínua" if extensao == linha["Anos no Total"] else "com lacunas"

    df_vigencia["Situação"] = df_vigencia.apply(_classificar_vigencia, axis=1)

    sem_vigencia = int((df_vigencia["Anos no Total"] == 0).sum())
    com_lacunas = int((df_vigencia["Situação"] == "com lacunas").sum())
    fora_da_janela = int(
        ((df_vigencia["Anos na Janela"] == 0) & (df_vigencia["Anos no Total"] > 0)).sum()
    )

    col_m1, col_m2, col_m3 = st.columns(3)
    col_m1.metric("Docentes sem vigência registrada", sem_vigencia)
    col_m2.metric("Docentes com lacunas na vigência", com_lacunas)
    col_m3.metric("Credenciados, mas fora da janela", fora_da_janela)

    def _destacar_sem_vigencia(linha):
        """Sinaliza quem não pontua nesta janela sob o regime de vigência --
        seja por não ter vigência nenhuma, seja por tê-la toda fora do
        período selecionado."""
        cor = "background-color: rgba(255, 75, 75, 0.25)" if linha["Anos na Janela"] == 0 else ""
        return [cor] * len(linha)

    st.dataframe(
        df_vigencia.style
        .apply(_destacar_sem_vigencia, axis=1)
        .format({
            "Ingresso": lambda v: "—" if pd.isna(v) else str(int(v)),
            "Primeiro": lambda v: "—" if pd.isna(v) else str(int(v)),
            "Último": lambda v: "—" if pd.isna(v) else str(int(v)),
        }),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "Linhas destacadas: docentes sem nenhum ano de credenciamento dentro da janela — "
        "no regime de vigência, nada do que publicaram no período conta. "
        f"Fonte: `{cred.CAMINHO_CSV_PADRAO}`, carregado pela Seção 15 de "
        "`analyse_organizado.ipynb`."
    )

    # ---------- Bloco 2: a grade ano a ano ----------
    st.markdown("---")
    st.subheader("Grade Ano a Ano")
    st.markdown(
        "Uma coluna por ano da janela; **●** marca os anos em que aquele docente esteve "
        "credenciado. É onde as lacunas e os recredenciamentos ficam visíveis de relance."
    )

    df_grade_bruta = con.execute(
        """
        SELECT p.nome_completo AS docente, c.ano AS ano
        FROM tb_professores p
        JOIN tb_credenciamento_anos c ON p.id_lattes = c.id_lattes
        WHERE c.ano BETWEEN ? AND ?
        """,
        [vig_ano_inicio, vig_ano_fim],
    ).df()

    anos_janela = list(range(vig_ano_inicio, vig_ano_fim + 1))
    if df_grade_bruta.empty:
        st.info("Nenhum docente tem anos de credenciamento dentro desta janela.")
    else:
        # Reindexa pelas duas pontas: todo docente cadastrado vira linha (mesmo
        # quem não tem nenhum ano vigente) e todo ano da janela vira coluna
        # (mesmo aquele em que ninguém esteve credenciado).
        grade = (
            df_grade_bruta.assign(marca="●")
            .pivot_table(index="docente", columns="ano", values="marca", aggfunc="first")
            .reindex(index=sorted(df_vigencia["Docente"]), columns=anos_janela)
            .fillna("·")
        )
        grade.columns = [str(a) for a in grade.columns]
        grade.index.name = "Docente"
        st.dataframe(grade, use_container_width=True)

    # ---------- Bloco 3: o impacto sobre o score ----------
    st.markdown("---")
    st.subheader("Impacto sobre o Score de Credenciamento")
    st.markdown(
        "O mesmo score consolidado da página \"Credenciamento\", calculado duas vezes: uma "
        "recortando a produção pela data de ingresso (a regra histórica) e outra pelos anos "
        "de vigência. A diferença é o que a mudança de regra custa ou rende a cada docente."
    )

    vig_tipo_avaliacao = st.radio(
        "Selecione o Critério de Apuração institucional:",
        ["Pontuação Integral (A1-A8)", "Pontuação Restrita (A1-A4)"],
        horizontal=True,
        key="vigencia_tipo_avaliacao",
    )
    vig_restrito = vig_tipo_avaliacao == "Pontuação Restrita (A1-A4)"
    vig_parametros = [vig_ano_inicio, vig_ano_fim, vig_ano_inicio, vig_ano_fim]

    # A regra de pontuação é uma só (`montar_query_credenciamento`); o que muda
    # entre as duas execuções é exclusivamente o regime do recorte por docente.
    df_por_ingresso = con.execute(
        montar_query_credenciamento(vig_restrito, regime=REGIME_INGRESSO), vig_parametros
    ).df()
    df_por_vigencia = con.execute(
        montar_query_credenciamento(vig_restrito, regime=REGIME_VIGENCIA), vig_parametros
    ).df()

    COL_SCORE = "Score Institucional Consolidado"
    COL_PAPERS = "Total Papers"

    df_impacto = (
        df_por_ingresso[["Docente Pesquisador", COL_PAPERS, COL_SCORE]]
        .rename(columns={COL_PAPERS: "Papers (ingresso)", COL_SCORE: "Score (ingresso)"})
        .merge(
            df_por_vigencia[["Docente Pesquisador", COL_PAPERS, COL_SCORE]]
            .rename(columns={COL_PAPERS: "Papers (vigência)", COL_SCORE: "Score (vigência)"}),
            on="Docente Pesquisador",
            how="outer",
        )
        .fillna(0)
    )
    df_impacto["Δ Score"] = df_impacto["Score (vigência)"] - df_impacto["Score (ingresso)"]
    df_impacto["Δ Papers"] = df_impacto["Papers (vigência)"] - df_impacto["Papers (ingresso)"]
    df_impacto = df_impacto.sort_values("Δ Score")

    total_ing = float(df_impacto["Score (ingresso)"].sum())
    total_vig = float(df_impacto["Score (vigência)"].sum())
    afetados = int((df_impacto["Δ Score"].abs() > 1e-9).sum())

    col_s1, col_s2, col_s3 = st.columns(3)
    col_s1.metric("Score total (data de ingresso)", _num_br(total_ing))
    col_s2.metric(
        "Score total (anos de credenciamento)",
        _num_br(total_vig),
        delta=_num_br(total_vig - total_ing, sinal=True),
    )
    col_s3.metric("Docentes com score alterado", f"{afetados} de {len(df_impacto)}")

    st.dataframe(
        df_impacto.style
        .background_gradient(subset=["Δ Score"], cmap="RdYlGn")
        .format({
            "Papers (ingresso)": "{:.0f}",
            "Papers (vigência)": "{:.0f}",
            "Δ Papers": "{:+.0f}",
            "Score (ingresso)": lambda v: _num_br(v),
            "Score (vigência)": lambda v: _num_br(v),
            "Δ Score": lambda v: _num_br(v, sinal=True),
        }),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "Δ negativo: a vigência tira produção que a data de ingresso deixava entrar "
        "(anos em que o docente já estava no programa mas não estava credenciado). "
        "Δ positivo não deveria ocorrer com os dados atuais — a vigência é sempre um "
        "recorte mais estreito —, e se aparecer indica ano de credenciamento anterior "
        "ao ingresso registrado."
    )

    renderizar_explicacao_calculos(
        descricao_x=(
            "o score consolidado de credenciamento daquele docente — a soma dos pesos por "
            "estrato de periódicos e conferências, com os bônus de área de computação e de "
            "coautoria discente, exatamente como na página \"Credenciamento\"."
        ),
        recorte=(
            f"Janela de {vig_ano_inicio} a {vig_ano_fim} e filtro de fonte da barra lateral, "
            "iguais nas duas colunas. A única diferença entre elas é o recorte por docente: "
            "\"ingresso\" mantém a produção a partir do ano de entrada no programa; "
            "\"vigência\" mantém apenas a dos anos credenciados, e zera quem não tem "
            f"vigência registrada. Critério de apuração: {vig_tipo_avaliacao}."
        ),
        observacoes=[
            "Esta página ignora o seletor \"Regime de contagem\" da barra lateral de "
            "propósito: ela mostra sempre os dois regimes, para que a comparação não "
            "dependa de qual está selecionado.",
        ],
    )

# ------------------------------------------
# PÁGINA 7.2: ALOCAÇÃO ÓTIMA DE PAPERS
# ------------------------------------------
# Página de decisão, e não de indicador: as demais respondem "quanto o programa
# produziu"; esta responde "qual paper cada docente deve declarar" quando o
# formulário pede uma quantidade fixa de papers por docente e um artigo
# coassinado por dois docentes do quadro só pode ser usado por um deles.
#
# A regra -- pontuação e alocação -- vive em `alocacao_papers.py`. Aqui só se
# monta a consulta, se colhem os parâmetros e se exibe o resultado, como nas
# demais páginas em relação a `dedup_publicacoes.py` e `credenciamento.py`.
elif pagina_selecionada == PAGINA_ALOCACAO:
    st.title("Alocação Ótima de Papers")
    st.markdown(
        "Dados os docentes selecionados e quantos papers cada um precisa declarar, este "
        "módulo decide **qual paper vai para qual docente** de modo a maximizar a pontuação "
        "total do conjunto. Só entram **artigos publicados em periódicos**. Cada paper é "
        "único: um artigo assinado por dois docentes do quadro pode ir para qualquer um dos "
        "dois, mas depois de usado sai da mesa."
    )

    CRITERIO_GERAL = "Geral (A1-A8)"
    CRITERIO_RESTRITO = "Restrita (A1-A4)"

    # A coluna de citações só existe em bancos gerados a partir da versão do
    # notebook que a propaga para a tabela unificada. Sem ela a página continua
    # funcionando -- Qualis e coautoria discente não dependem dela --, mas todo
    # paper entra com zero citação, e isso precisa estar dito na tela.
    tem_citacoes = tem_coluna(con, "tb_artigo_periodico", "citacoes_scopus")
    if not tem_citacoes:
        st.warning(
            "Este banco foi gerado antes de a coluna `citacoes_scopus` chegar a "
            "`tb_artigo_periodico`: **todos os papers entram com 0 citação**. O Qualis e a "
            "coautoria discente seguem valendo. Rode um reprocessamento para trazer as "
            "citações da Scopus."
        )
        st.button(
            "Abrir Configurações",
            on_click=ir_para_pagina,
            args=(PAGINA_CONFIGURACOES,),
            key="btn_ir_config_alocacao",
        )

    st.subheader("Janela de Apuração")
    ano_inicio, ano_fim = renderizar_filtro_periodo(ANO_MIN, ANO_MAX, "alocacao")
    criterio = st.radio(
        "Critério de apuração",
        [CRITERIO_GERAL, CRITERIO_RESTRITO],
        horizontal=True,
        key="alocacao_criterio",
        help="Geral considera os oito estratos. Restrita descarta o que está fora de "
             "A1-A4 (percentil Scopus < 50), como nas páginas Quadrienal Restrita e "
             "Credenciamento restrito — o descarte é por exclusão, não por peso zero, "
             "senão um A8 muito citado ainda seria escolhido pelo termo de citações.",
    )
    restrito = criterio == CRITERIO_RESTRITO

    df_docentes = con.execute(
        "SELECT id_lattes, nome_completo FROM tb_professores ORDER BY nome_completo"
    ).df()

    if df_docentes.empty:
        st.warning("Nenhum docente cadastrado nesta base — não há o que alocar.")
    else:
        nomes_por_id = dict(zip(df_docentes["id_lattes"], df_docentes["nome_completo"]))

        st.subheader("Docentes e Cotas")
        selecionados = st.multiselect(
            "Docentes considerados",
            options=list(nomes_por_id),
            default=list(nomes_por_id),
            format_func=lambda i: nomes_por_id.get(i, i),
            key="alocacao_docentes",
        )
        cota_padrao = st.number_input(
            "Papers por docente", min_value=0, max_value=200, value=4, step=1,
            key="alocacao_cota_padrao",
            help="Quantos papers cada docente precisa declarar. Vale para todos; a cota "
                 "de cada um pode ser ajustada logo abaixo.",
        )

        cotas = {i: int(cota_padrao) for i in selecionados}
        if selecionados:
            with st.expander("Ajustar a cota docente a docente"):
                st.caption(
                    "A cota global acima preenche a coluna. Mexer nela aqui vale só para "
                    "aquele docente — e trocar a cota global recompõe a tabela inteira."
                )
                grade_inicial = pd.DataFrame({
                    "Docente": [nomes_por_id[i] for i in selecionados],
                    "Cota": [int(cota_padrao)] * len(selecionados),
                })
                # A chave carrega a cota global e a seleção: `data_editor` guarda
                # as edições por chave e ignoraria um novo valor-padrão, então
                # mudar a cota global tem de produzir um widget novo.
                grade_editada = st.data_editor(
                    grade_inicial,
                    hide_index=True,
                    use_container_width=True,
                    disabled=["Docente"],
                    column_config={
                        "Cota": st.column_config.NumberColumn(
                            "Cota", min_value=0, max_value=200, step=1, format="%d"),
                    },
                    key=f"alocacao_grade_{cota_padrao}_{len(selecionados)}_"
                        f"{abs(hash(tuple(sorted(selecionados)))) % 100000}",
                )
                cotas = {
                    i: int(linha["Cota"]) if pd.notna(linha["Cota"]) else 0
                    for i, (_, linha) in zip(selecionados, grade_editada.iterrows())
                }

        with st.expander("Modelo de pontuação"):
            st.markdown(
                "A pontuação de cada par (docente, paper) reaproveita a tabela de pesos do "
                "score de credenciamento e acrescenta o termo de citações:"
            )
            st.latex(
                r"\text{pontuação} = \underbrace{p_{\text{Qualis}} \times "
                r"1{,}25^{\,\text{computação}} \times 1{,}5^{\,\text{discente}}}"
                r"_{\text{como no Credenciamento}} \;+\; w_{\text{cit}} \times \text{citações}"
            )
            st.markdown(
                f"$p_{{Qualis}}$ vale 1,000 (A1), 0,875 (A2), 0,750 (A3), 0,625 (A4), 0,500 "
                f"(A5), 0,375 (A6), 0,250 (A7) e {aloc.PESO_SEM_PERCENTIL:.3f}".replace(".", ",")
                + " (A8, e também o periódico sem percentil casado). O termo de citações é "
                "**somado**, não multiplicado: os bônus de área e de coautoria discente não "
                "o amplificam."
            )
            peso_citacao = st.number_input(
                "Pontos por citação", min_value=0.0, max_value=1.0,
                value=aloc.PESO_CITACAO_PADRAO, step=0.005, format="%.3f",
                key="alocacao_peso_citacao",
                help="Com 0,010, cerca de 87 citações valem um A1 com coautoria discente "
                     "e bônus de computação. Em janelas longas o termo de citações domina "
                     "o ranking; em uma janela quadrienal ele costuma só desempatar.",
            )
            usar_teto = st.checkbox(
                "Limitar as citações contadas por paper", value=False,
                key="alocacao_usar_teto",
                help="Trava contra o outlier: nesta base há periódico com mais de 2.000 "
                     "citações, que sozinho valeria mais de 20 pontos.",
            )
            teto_citacoes = st.number_input(
                "Teto de citações por paper", min_value=1, max_value=10000, value=100, step=10,
                key="alocacao_teto_citacoes",
            ) if usar_teto else None

        if not selecionados:
            st.info("Selecione ao menos um docente para calcular a alocação.")
        elif not any(cotas.values()):
            st.info("Todas as cotas estão em zero — nada a alocar.")
        else:
            marcadores = ", ".join("?" for _ in selecionados)
            expressao_citacoes = "a.citacoes_scopus" if tem_citacoes else "NULL"
            query_pares = f"""
                SELECT
                    a.id_lattes,
                    p.nome_completo AS docente,
                    a.titulo_artigo,
                    a.ano_pub AS ano,
                    a.doi,
                    COALESCE(NULLIF(a.titulo_revista_scopus, ''), a.titulo_revista_lattes) AS veiculo,
                    a.maior_percentil,
                    a.computation_area,
                    a.coautoria_aluno,
                    {expressao_citacoes} AS citacoes
                FROM tb_artigo_periodico a
                JOIN tb_professores p ON p.id_lattes = a.id_lattes
                WHERE a.ano_pub BETWEEN ? AND ?
                  AND a.id_lattes IN ({marcadores})
                  {sql_fonte('a.fontes')}{sql_recorte_docente('a', 'ano_pub')}
            """
            df_pares = con.execute(query_pares, [ano_inicio, ano_fim, *selecionados]).df()

            linhas_antes_do_corte = len(df_pares)
            if restrito:
                df_pares = aloc.filtrar_restrito(df_pares)

            if df_pares.empty:
                # Distinguir "não há paper nenhum na janela" de "havia, mas o
                # critério restrito levou todos": o conserto é outro em cada caso.
                if restrito and linhas_antes_do_corte:
                    st.warning(
                        f"Os {linhas_antes_do_corte} artigo(s) de periódico da janela estão "
                        "todos fora de A1-A4 — nada sobrou para alocar no critério restrito."
                    )
                else:
                    st.warning(
                        "Nenhum artigo de periódico elegível na janela e nos filtros atuais. "
                        "Confira o período e os dois filtros globais da barra lateral "
                        "(fonte dos papers e regime de contagem)."
                    )
            else:
                df_pontuado = aloc.pontuar(
                    df_pares, peso_citacao=peso_citacao, teto_citacoes=teto_citacoes)
                df_agrupado = aloc.agrupar(df_pontuado)
                resultado = aloc.resolver(df_agrupado, cotas, nomes=nomes_por_id)

                st.subheader("Resultado da Alocação")
                total_faltando = int(resultado.por_docente["faltando"].sum())
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Pontuação total", _num_br(resultado.total, 3))
                col2.metric("Papers alocados",
                            f"{len(resultado.alocacao)} de {sum(cotas.values())}")
                col3.metric("Cotas em aberto", total_faltando)
                col4.metric("Ganho sobre a alocação gulosa",
                            _num_br(resultado.ganho_sobre_guloso, 3))

                if tem_citacoes:
                    com_citacao = int(pd.to_numeric(
                        df_pares["citacoes"], errors="coerce").notna().sum())
                    st.caption(
                        f"Cobertura de citações: {com_citacao} de {len(df_pares)} "
                        f"({com_citacao / len(df_pares):.0%}) dos pares (docente, paper) "
                        "elegíveis têm contagem vinda da Scopus — é por par que a "
                        "pontuação é feita. Os demais entram com **zero citação**, "
                        "não como \"desconhecido\" — é um viés conhecido contra o que não "
                        "está indexado na Scopus."
                    )
                if total_faltando:
                    st.info(
                        f"{total_faltando} vaga(s) ficaram em aberto: os docentes abaixo com "
                        "\"Faltando\" positivo não têm papers elegíveis suficientes na janela "
                        "(ou os que tinham foram para um coautor). A coluna \"Elegíveis\" diz "
                        "quantos papers distintos cada um poderia usar."
                    )

                st.markdown("#### Fechamento por Docente")
                fechamento = pd.DataFrame({
                    "Docente": resultado.por_docente["docente"],
                    "Cota": resultado.por_docente["cota"],
                    "Elegíveis": resultado.por_docente["elegiveis"],
                    "Alocados": resultado.por_docente["alocados"],
                    "Faltando": resultado.por_docente["faltando"],
                    "Pontuação": resultado.por_docente["score"].round(3),
                })
                st.dataframe(fechamento, use_container_width=True, hide_index=True)

                st.markdown("#### Papers Escolhidos")
                candidatos_por_grupo = (
                    df_agrupado.groupby("grupo")["docente"].apply(lambda s: sorted(set(s)))
                )
                detalhe = resultado.alocacao.sort_values(
                    ["docente", "pontuacao"], ascending=[True, False])
                escolhidos = pd.DataFrame({
                    "Docente": detalhe["docente"],
                    "Título": detalhe["titulo_artigo"],
                    "Ano": detalhe["ano"],
                    "Veículo": detalhe["veiculo"],
                    "Estrato": detalhe["estrato"],
                    "Percentil": detalhe["maior_percentil"],
                    "Citações": detalhe["citacoes_consideradas"].astype(int),
                    # `aloc.e_verdadeiro` e não `v is True`: o booleano que o
                    # DuckDB devolve é `numpy.bool_`, para o qual `is True` é
                    # falso -- a coluna sairia "não" em todas as linhas.
                    "Discente": detalhe["coautoria_aluno"].map(
                        lambda v: "sim" if aloc.e_verdadeiro(v) else "não"),
                    "Computação": detalhe["computation_area"].map(
                        lambda v: "sim" if aloc.e_verdadeiro(v) else "não"),
                    "Pontuação": detalhe["pontuacao"].round(3),
                    "Também podia ir para": [
                        ", ".join(d for d in candidatos_por_grupo.get(grupo, []) if d != docente)
                        for grupo, docente in zip(detalhe["grupo"], detalhe["docente"])
                    ],
                    "DOI": detalhe["doi"],
                })
                st.dataframe(escolhidos, use_container_width=True, hide_index=True)
                st.download_button(
                    "Baixar a alocação (CSV)",
                    data=escolhidos.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"alocacao_papers_{ano_inicio}_{ano_fim}.csv",
                    mime="text/csv",
                    key="dl_alocacao_papers",
                )

                st.markdown("#### Papers Disputados")
                if resultado.disputas.empty:
                    st.caption(
                        "Nenhum paper elegível é assinado por mais de um dos docentes "
                        "selecionados — sem disputa, a alocação é só \"cada um com os seus "
                        "melhores\"."
                    )
                else:
                    st.caption(
                        f"{len(resultado.disputas)} paper(s) que mais de um docente "
                        "selecionado poderia usar. **Δ pontuação** é a diferença entre o "
                        "candidato de maior pontuação e o segundo — é o que se perde ao "
                        "entregar o paper ao segundo, e não o efeito no total (o docente "
                        "preterido costuma ter outro paper para pôr no lugar)."
                    )
                    disputados = pd.DataFrame({
                        "Título": resultado.disputas["titulo_artigo"],
                        "Ano": resultado.disputas["ano"],
                        "Candidatos": resultado.disputas["candidatos"],
                        "Ficou com": resultado.disputas["vencedor"],
                        "Δ pontuação": resultado.disputas["delta"].round(3),
                        "DOI": resultado.disputas["doi"],
                    })
                    st.dataframe(disputados, use_container_width=True, hide_index=True)

                with st.expander("Como esta alocação é calculada"):
                    st.markdown(
                        "**1. Os candidatos.** Uma linha por par (docente, paper) de "
                        "`tb_artigo_periodico` na janela, já sob os dois filtros globais da "
                        "barra lateral — fonte dos papers e regime de contagem. Como o "
                        "recorte por docente é aplicado linha a linha, um mesmo paper pode "
                        "ser elegível para um coautor e não para o outro."
                    )
                    st.markdown(
                        "**2. A pontuação** é a do bloco *Modelo de pontuação* acima, e é do "
                        "**par**, não do paper: `coautoria_aluno` e `maior_percentil` podem "
                        "divergir entre as linhas de dois coautores do quadro (metadados "
                        "contaminados na extração — veja *Limitações conhecidas* no README)."
                    )
                    st.markdown(
                        "**3. Um paper é um paper.** Duas linhas de docentes diferentes são o "
                        "mesmo artigo quando têm o mesmo DOI normalizado **ou** o mesmo título "
                        "normalizado no mesmo ano (`dedup_publicacoes."
                        "agrupar_papers_entre_docentes`, casamento sempre exato). É a única "
                        "comparação do sistema que atravessa docentes, e existe justamente "
                        "porque aqui o paper só pode ser usado uma vez."
                    )
                    st.markdown(
                        "**4. A escolha é ótima, não heurística.** Maximiza-se a soma das "
                        "pontuações com duas restrições — cada docente recebe no máximo a sua "
                        "cota, cada paper é usado no máximo uma vez. É um problema de "
                        "atribuição bipartida com cota, resolvido pelo algoritmo húngaro "
                        "(`scipy.optimize.linear_sum_assignment`): o resultado é o **máximo "
                        "exato**, não uma aproximação. A métrica *ganho sobre a alocação "
                        "gulosa* mostra quanto se ganharia a menos entregando a cada docente "
                        "os seus melhores papers ainda livres, um docente de cada vez."
                    )
                    st.markdown(
                        "**5. Cota é teto, não meta.** Docente sem papers elegíveis "
                        "suficientes fica com a cota incompleta; a alocação nunca inventa "
                        "paper nem toma emprestado de quem não o assina."
                    )
                    st.markdown(f"**Recorte considerado.** {frase_recorte_docente()}.")
                    st.caption(
                        f"Pares avaliados: {resultado.diagnostico.get('pares_elegiveis', 0)} | "
                        f"papers distintos: {resultado.papers_distintos} | "
                        f"disputados: {resultado.diagnostico.get('papers_disputados', 0)} | "
                        f"vagas consideradas: {resultado.vagas}"
                    )

# ------------------------------------------
# PÁGINA 8: PANORAMA DE ORIENTAÇÕES
# ------------------------------------------
elif pagina_selecionada == "Panorama de Orientações Acadêmicas":
    st.title("Panorama Analítico de Orientações Acadêmicas")
    st.markdown("Módulo dedicado ao monitoramento da formação de recursos humanos. O recorte temporal considera orientações ativas ou concluídas que interceptem a janela selecionada.")
    
    query_anos = "SELECT MIN(ano_inicio), MAX(COALESCE(ano_conclusao, 2026)) FROM tb_orientacoes"
    resultado_anos = con.execute(query_anos).fetchone()

    ano_min_ori = int(resultado_anos[0]) if resultado_anos and resultado_anos[0] else 2000
    ano_max_ori = int(resultado_anos[1]) if resultado_anos and resultado_anos[1] else 2026

    st.subheader("Configuração da Janela Temporal")
    ano_inicio_filtro, ano_fim_filtro = renderizar_filtro_periodo(ano_min_ori, ano_max_ori, "orientacoes")

    st.markdown(f"**Analisando vínculos ativos em qualquer momento entre {ano_inicio_filtro} e {ano_fim_filtro}**")
    
    condicao_intersecao = (
        "ano_inicio <= ? AND COALESCE(ano_conclusao, 2026) >= ?"
        f"{sql_recorte_docente('tb_orientacoes', 'ano_inicio')}"
    )
    parametros_filtro = [ano_fim_filtro, ano_inicio_filtro]
    
    query_kpis = f"""
        SELECT 
            COUNT(*), 
            SUM(CASE WHEN status = 'Concluída' THEN 1 ELSE 0 END),
            SUM(CASE WHEN status = 'Em Andamento' THEN 1 ELSE 0 END)
        FROM tb_orientacoes
        WHERE {condicao_intersecao}
    """
    res_kpis = con.execute(query_kpis, parametros_filtro).fetchone()
    
    total_orientacoes = res_kpis[0] if res_kpis else 0
    total_concluidas = res_kpis[1] if res_kpis and res_kpis[1] else 0
    total_andamento = res_kpis[2] if res_kpis and res_kpis[2] else 0
    
    col1, col2, col3 = st.columns(3)
    col1.metric("Total de Orientações no Período", total_orientacoes)
    col2.metric("Concluídas no Período", total_concluidas)
    col3.metric("Ativas/Em Andamento no Período", total_andamento)
    
    st.markdown("---")
    
    aba_nivel, aba_docente = st.tabs([
        "Distribuição por Nível de Formação", 
        "Matriz de Desempenho Docente"
    ])
    
    with aba_nivel:
        st.subheader("Concentração de Recursos Humanos por Nível Acadêmico")
        query_nivel = f"""
            SELECT 
                nivel AS "Nível Acadêmico", 
                COUNT(*) AS "Volume de Orientações" 
            FROM tb_orientacoes 
            WHERE nivel IS NOT NULL AND {condicao_intersecao}
            GROUP BY nivel 
            ORDER BY "Volume de Orientações" DESC
        """
        df_nivel = con.execute(query_nivel, parametros_filtro).df()
        
        if not df_nivel.empty:
            col_tab, col_graf = st.columns([1, 2])
            with col_tab:
                st.dataframe(df_nivel, use_container_width=True, hide_index=True)
            with col_graf:
                st.bar_chart(df_nivel.set_index("Nível Acadêmico"), use_container_width=True)
        else:
            st.info("Nenhum dado de nível formativo localizado na janela temporal.")

    with aba_docente:
        st.subheader("Carga e Detalhamento de Orientações por Pesquisador")
        st.markdown("Tabela consolidada com o volume total, status do vínculo e divisão hierárquica por nível acadêmico.")
        
        query_docente_ori = f"""
            WITH cte_ativas AS (
                SELECT * FROM tb_orientacoes
                WHERE {condicao_intersecao}
            )
            SELECT 
                p.nome_completo AS "Pesquisador", 
                COUNT(o.id_orientacao) AS "Total",
                COUNT(CASE WHEN o.status = 'Concluída' THEN 1 END) AS "Concluídas",
                COUNT(CASE WHEN o.status = 'Em Andamento' THEN 1 END) AS "Em Andamento",
                COUNT(CASE WHEN o.nivel = 'Doutorado' THEN 1 END) AS "Doutorado",
                COUNT(CASE WHEN o.nivel = 'Mestrado' THEN 1 END) AS "Mestrado",
                COUNT(CASE WHEN o.nivel = 'Especialização' THEN 1 END) AS "Especialização",
                COUNT(CASE WHEN o.nivel = 'TCC' THEN 1 END) AS "TCC",
                COUNT(CASE WHEN o.nivel = 'Iniciação Científica' THEN 1 END) AS "IC",
                COUNT(CASE WHEN o.nivel = 'Pós-Doutorado' THEN 1 END) AS "Pós-Doc",
                COUNT(CASE WHEN o.nivel = 'Outros' THEN 1 END) AS "Outros"
            FROM tb_professores p
            LEFT JOIN cte_ativas o ON p.id_lattes = o.id_lattes
            GROUP BY p.nome_completo
            ORDER BY "Total" DESC
        """
        df_docente_ori = con.execute(query_docente_ori, parametros_filtro).df()
        
        if not df_docente_ori.empty:
            st.dataframe(
                df_docente_ori.style.background_gradient(subset=['Total'], cmap='Greens'),
                use_container_width=True, 
                hide_index=True
            )
            st.markdown("#### Composição Hierárquica da Orientação Docente")
            colunas_niveis = ["Doutorado", "Mestrado", "Especialização", "TCC", "IC", "Pós-Doc", "Outros"]
            st.bar_chart(
                df_docente_ori.set_index("Pesquisador")[colunas_niveis], 
                use_container_width=True, 
                stack=True
            )
        else:
            st.warning("Nenhuma associação docente localizada na janela estipulada.")

# ------------------------------------------
# PÁGINA: GERAÇÃO DE RELATÓRIOS
# ------------------------------------------
elif pagina_selecionada == "Geração de Relatórios":
    st.title("Geração de Relatórios")
    st.markdown(
        "Módulo de emissão de relatórios individuais por docente. Cada relatório é gerado como um "
        "documento **HTML pronto para impressão**: clique em *Baixar HTML*, abra o arquivo no "
        "navegador e use *Imprimir → Salvar como PDF* (Ctrl+P) para obter o PDF final."
    )

    RELATORIOS_DISPONIVEIS = [
        "Papers faltantes na base do Lattes (periódicos e conferências)",
        "Alunos do programa faltando no Lattes do orientador",
        "Papers com coautoria discente",
    ]
    relatorio_selecionado = st.selectbox("Selecione o relatório:", RELATORIOS_DISPONIVEIS)

    # Mesmo filtro de período das páginas de visualização (estado compartilhado
    # via renderizar_filtro_periodo): recorta o conteúdo dos relatórios e é
    # impresso no cabeçalho do HTML gerado, para que o PDF diga a que janela se
    # refere. Cada relatório aplica o intervalo à sua própria coluna de ano
    # (ano de publicação nos papers; ano de ingresso nos alunos).
    st.subheader("Filtro de Período")
    rel_ano_inicio, rel_ano_fim = renderizar_filtro_periodo(ANO_MIN, ANO_MAX, "relatorios")
    st.caption(
        f"Os relatórios abaixo consideram apenas o intervalo {rel_ano_inicio}–{rel_ano_fim}. "
        "O filtro 'Fonte dos papers' da barra lateral não se aplica aqui: cada relatório já "
        "define, por definição, quais fontes examina."
    )

    def _pred_ano(coluna, incluir_sem_ano):
        """Predicado SQL do recorte de período para `coluna` (sempre 2 parâmetros:
        início e fim). Com `incluir_sem_ano`, registros sem ano informado entram
        no relatório em vez de sumirem silenciosamente do recorte."""
        if incluir_sem_ano:
            return f"({coluna} BETWEEN ? AND ? OR {coluna} IS NULL)"
        return f"({coluna} BETWEEN ? AND ?)"

    st.divider()

    # --- Utilitários de formatação/HTML compartilhados pelos relatórios ---
    def _fmt_txt(valor):
        try:
            if pd.isna(valor):
                return "—"
        except (TypeError, ValueError):
            pass
        texto = str(valor).strip()
        return texto if texto else "—"

    def _fmt_ano(valor):
        try:
            if pd.isna(valor):
                return "—"
        except (TypeError, ValueError):
            pass
        try:
            return str(int(valor))
        except (TypeError, ValueError):
            return _fmt_txt(valor)

    def _fmt_fontes(valor):
        # 'ORCID,SCOPUS' -> 'ORCID, SCOPUS'
        texto = _fmt_txt(valor)
        return texto if texto == "—" else texto.replace(",", ", ")

    def _tabela_html(df, colunas):
        """Monta uma <table> HTML. `colunas`: lista de (rótulo, nome_coluna, formatador)."""
        ths = "".join(f"<th>{html_lib.escape(rot)}</th>" for rot, _, _ in colunas)
        linhas = []
        for _, row in df.iterrows():
            tds = "".join(
                f"<td>{html_lib.escape(fmt(row[col]))}</td>" for _, col, fmt in colunas
            )
            linhas.append(f"<tr>{tds}</tr>")
        return (
            f"<table><thead><tr>{ths}</tr></thead>"
            f"<tbody>{''.join(linhas)}</tbody></table>"
        )

    _CSS_RELATORIO = """
      * { box-sizing: border-box; }
      body { font-family: 'Segoe UI', Arial, sans-serif; color: #1a1a1a; margin: 32px; }
      h1 { font-size: 20px; margin: 0 0 4px; }
      h2 { font-size: 15px; margin: 26px 0 8px; border-bottom: 2px solid #444; padding-bottom: 4px; }
      .sub { color: #555; margin: 0 0 16px; font-size: 12px; }
      .cont { color: #777; font-weight: normal; font-size: 12px; }
      table { border-collapse: collapse; width: 100%; font-size: 11px; }
      table.meta { width: auto; margin-bottom: 8px; }
      table.meta td { border: none; padding: 1px 12px 1px 0; }
      thead th { background: #f0f0f0; text-align: left; }
      th, td { border: 1px solid #ccc; padding: 5px 7px; vertical-align: top; word-break: break-word; }
      td:last-child, th:last-child { white-space: nowrap; }
      .vazio { color: #777; font-style: italic; font-size: 12px; }
      footer { margin-top: 28px; color: #777; font-size: 10px; border-top: 1px solid #ddd; padding-top: 8px; }
      button.noprint { margin-top: 20px; padding: 8px 16px; font-size: 13px; cursor: pointer; }
      @media print { button.noprint { display: none; } body { margin: 0; } }
    """

    if relatorio_selecionado == "Papers faltantes na base do Lattes (periódicos e conferências)":
        st.markdown(
            "Lista, para cada docente, as publicações **não encontradas no Lattes** mas presentes "
            "em outras bases (ORCID/Scopus), indicando **em qual base** cada uma foi rastreada — "
            "assim o docente pode localizá-la e incluí-la no Lattes. A origem é a coluna `fontes` "
            "das tabelas unificadas (linhas cujo `fontes` não contém `LATTES`)."
        )

        # Publicações sem ano informado ficariam de fora de qualquer recorte de
        # período. Como o objetivo do relatório é justamente não deixar passar
        # pendência, oferecemos a opção de incluí-las -- mas só quando existem,
        # para não poluir a tela à toa.
        sem_ano_faltantes = con.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM tb_artigo_periodico
                   WHERE ano_pub IS NULL AND fontes IS NOT NULL AND fontes NOT LIKE '%LATTES%')
              + (SELECT COUNT(*) FROM tb_artigo_conferencia
                   WHERE ano IS NULL AND fontes IS NOT NULL AND fontes NOT LIKE '%LATTES%')
            """
        ).fetchone()[0]

        incluir_sem_ano_papers = False
        if sem_ano_faltantes:
            incluir_sem_ano_papers = st.checkbox(
                f"Incluir também as {sem_ano_faltantes} publicação(ões) sem ano informado",
                value=True,
                key="incluir_sem_ano_papers",
                help="Publicações sem ano não pertencem a nenhum intervalo; desmarque "
                     "para restringir o relatório estritamente ao período selecionado.",
            )

        pred_p = _pred_ano("ano_pub", incluir_sem_ano_papers)
        pred_c = _pred_ano("ano", incluir_sem_ano_papers)

        def _dados_faltantes(id_lattes):
            df_p = con.execute(
                f"""
                SELECT titulo_artigo, ano_pub, doi, fontes
                FROM tb_artigo_periodico
                WHERE id_lattes = ? AND fontes IS NOT NULL AND fontes NOT LIKE '%LATTES%'
                  AND {pred_p}
                ORDER BY ano_pub DESC NULLS LAST, titulo_artigo
                """, [id_lattes, rel_ano_inicio, rel_ano_fim]).df()
            df_c = con.execute(
                f"""
                SELECT titulo_artigo, titulo_evento_lattes, ano, doi, fontes
                FROM tb_artigo_conferencia
                WHERE id_lattes = ? AND fontes IS NOT NULL AND fontes NOT LIKE '%LATTES%'
                  AND {pred_c}
                ORDER BY ano DESC NULLS LAST, titulo_artigo
                """, [id_lattes, rel_ano_inicio, rel_ano_fim]).df()
            return df_p, df_c

        def _html_faltantes(nome, id_lattes, df_p, df_c):
            gerado_em = datetime.now().strftime("%d/%m/%Y %H:%M")
            col_p = [
                ("Título", "titulo_artigo", _fmt_txt), ("Ano", "ano_pub", _fmt_ano),
                ("DOI", "doi", _fmt_txt), ("Rastreado em", "fontes", _fmt_fontes),
            ]
            col_c = [
                ("Título", "titulo_artigo", _fmt_txt), ("Evento", "titulo_evento_lattes", _fmt_txt),
                ("Ano", "ano", _fmt_ano), ("DOI", "doi", _fmt_txt),
                ("Rastreado em", "fontes", _fmt_fontes),
            ]

            def secao(titulo, df, colunas):
                corpo = (
                    "<p class='vazio'>Nenhum item faltante encontrado.</p>"
                    if df.empty else _tabela_html(df, colunas)
                )
                return f"<h2>{html_lib.escape(titulo)} <span class='cont'>({len(df)})</span></h2>{corpo}"

            return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>Papers faltantes no Lattes — {html_lib.escape(str(nome))}</title>
<style>{_CSS_RELATORIO}</style></head>
<body>
<header>
  <h1>Papers faltantes na base do Lattes</h1>
  <p class="sub">Publicações localizadas em outras bases (ORCID/Scopus) e ausentes no currículo Lattes.</p>
  <table class="meta">
    <tr><td><strong>Docente</strong></td><td>{html_lib.escape(str(nome))}</td></tr>
    <tr><td><strong>ID Lattes</strong></td><td>{html_lib.escape(str(id_lattes))}</td></tr>
    <tr><td><strong>Período</strong></td><td>{rel_ano_inicio} a {rel_ano_fim}{' (inclui itens sem ano informado)' if incluir_sem_ano_papers else ''}</td></tr>
    <tr><td><strong>Gerado em</strong></td><td>{gerado_em}</td></tr>
  </table>
</header>
{secao("Periódicos", df_p, col_p)}
{secao("Conferências", df_c, col_c)}
<footer>Sistema de Avaliação de Produtividade Acadêmica — para incluir estes itens no Lattes,
localize cada publicação na base indicada na coluna "Rastreado em".</footer>
<button class="noprint" onclick="window.print()">Imprimir / Salvar como PDF</button>
</body></html>"""

        # Contagem de pendências por docente (subconsultas correlacionadas),
        # já recortada pelo período selecionado.
        df_profs = con.execute(
            f"""
            SELECT p.id_lattes, p.nome_completo,
                (SELECT COUNT(*) FROM tb_artigo_periodico ap
                   WHERE ap.id_lattes = p.id_lattes AND ap.fontes IS NOT NULL
                     AND ap.fontes NOT LIKE '%LATTES%'
                     AND {_pred_ano('ap.ano_pub', incluir_sem_ano_papers)}) AS falt_p,
                (SELECT COUNT(*) FROM tb_artigo_conferencia ac
                   WHERE ac.id_lattes = p.id_lattes AND ac.fontes IS NOT NULL
                     AND ac.fontes NOT LIKE '%LATTES%'
                     AND {_pred_ano('ac.ano', incluir_sem_ano_papers)}) AS falt_c
            FROM tb_professores p
            ORDER BY p.nome_completo
            """, [rel_ano_inicio, rel_ano_fim, rel_ano_inicio, rel_ano_fim]
        ).df()

        mostrar_todos = st.checkbox("Mostrar também docentes sem pendências", value=False)

        st.markdown("#### Docentes")
        h1, h2, h3, h4 = st.columns([4, 1, 1, 2])
        h1.markdown("**Docente**")
        h2.markdown("**Periódicos**")
        h3.markdown("**Conferências**")
        h4.markdown("**Relatório**")

        algum_exibido = False
        for _, prof in df_profs.iterrows():
            falt_p = int(prof["falt_p"])
            falt_c = int(prof["falt_c"])
            total = falt_p + falt_c
            if total == 0 and not mostrar_todos:
                continue
            algum_exibido = True
            c1, c2, c3, c4 = st.columns([4, 1, 1, 2])
            c1.write(prof["nome_completo"])
            c2.write(falt_p)
            c3.write(falt_c)
            if total == 0:
                c4.caption("Sem pendências")
            else:
                df_p, df_c = _dados_faltantes(prof["id_lattes"])
                doc_html = _html_faltantes(prof["nome_completo"], prof["id_lattes"], df_p, df_c)
                slug = re.sub(r"[^A-Za-z0-9]+", "_", str(prof["nome_completo"])).strip("_")
                c4.download_button(
                    "Baixar HTML",
                    data=doc_html.encode("utf-8"),
                    file_name=f"faltantes_lattes_{slug}.html",
                    mime="text/html",
                    key=f"dl_falt_{prof['id_lattes']}",
                )

        if not algum_exibido:
            st.success(
                f"Nenhum docente possui papers faltantes no Lattes nesta base "
                f"no período {rel_ano_inicio}–{rel_ano_fim}."
            )

    elif relatorio_selecionado == "Alunos do programa faltando no Lattes do orientador":
        st.markdown(
            "Lista, para cada orientador, os alunos do programa (registro administrativo em "
            "`dados_brutos/lista_alunos_pesc.xlsx`) que **ainda não constam no currículo Lattes "
            "dele** como orientando — para que o orientador possa incluí-los. O casamento entre a "
            "planilha administrativa, `tb_orientacoes` e o que o próprio aluno declara no Lattes "
            "dele é feito pelo notebook `analyse_organizado.ipynb` (Seção 14) e persistido em "
            "`tb_situacao_orientandos`; rode-o novamente para atualizar este relatório.\n\n"
            "Cada pendência traz um **nível de confiança**, cruzando o Lattes do próprio aluno "
            "(Seção 11.2.2) com o do orientador:\n"
            "- **Divergente** — o aluno declarou, para aquele nível, um orientador que a planilha "
            "nem lista como (co)orientador dele — revisar antes de mais nada;\n"
            "- **Confirmado** / **Confirmado parcialmente** — o aluno já declara esse orientador no "
            "próprio Lattes (título/ano ao lado) — pendência de fácil resolução, é só incluir;\n"
            "- **Sem confirmação** — só a planilha administrativa registra o vínculo."
        )

        tabela_situacao_ok = True
        try:
            con.execute("SELECT 1 FROM tb_situacao_orientandos LIMIT 1")
        except Exception:
            tabela_situacao_ok = False

        colunas_confianca_ok = True
        if tabela_situacao_ok:
            try:
                con.execute(
                    "SELECT nivel_confianca, titulo_trabalho_aluno, ano_obtencao_aluno "
                    "FROM tb_situacao_orientandos LIMIT 1"
                )
            except Exception:
                colunas_confianca_ok = False

        if not tabela_situacao_ok:
            st.warning(
                "A tabela `tb_situacao_orientandos` não existe nesta base ainda. Rode a Seção 14 "
                "de `analyse_organizado.ipynb` para gerá-la antes de usar este relatório."
            )
        elif not colunas_confianca_ok:
            st.warning(
                "A tabela `tb_situacao_orientandos` existe, mas ainda não tem as colunas de "
                "confiança (`nivel_confianca`, `titulo_trabalho_aluno`, `ano_obtencao_aluno`). "
                "Rode novamente a Seção 14 de `analyse_organizado.ipynb` para adicioná-las "
                "(migração aditiva — não apaga nada) antes de usar este relatório."
            )
        else:
            _ROTULOS_CONFIANCA = {
                "confirmado": "Confirmado (Lattes do orientador + do aluno)",
                "confirmado_parcial": "Confirmado parcialmente (1 das 2 fontes)",
                "sem_confirmacao": "Sem confirmação",
                "divergente": "Divergente — revisar",
            }

            def _fmt_confianca(valor):
                return _ROTULOS_CONFIANCA.get(valor, _fmt_txt(valor))

            # Aqui o recorte de período é pelo **ano de ingresso do aluno** no
            # programa (única data disponível em tb_situacao_orientandos).
            sem_ano_alunos = con.execute(
                "SELECT COUNT(*) FROM tb_situacao_orientandos "
                "WHERE ano_ingresso IS NULL AND encontrado_no_lattes = FALSE"
            ).fetchone()[0]

            incluir_sem_ano_alunos = False
            if sem_ano_alunos:
                incluir_sem_ano_alunos = st.checkbox(
                    f"Incluir também os {sem_ano_alunos} aluno(s) sem ano de ingresso informado",
                    value=True,
                    key="incluir_sem_ano_alunos",
                    help="Alunos sem ano de ingresso não pertencem a nenhum intervalo; desmarque "
                         "para restringir o relatório estritamente ao período selecionado.",
                )

            pred_ingresso = _pred_ano("ano_ingresso", incluir_sem_ano_alunos)
            pred_ingresso_s = _pred_ano("s.ano_ingresso", incluir_sem_ano_alunos)

            def _dados_alunos_faltantes(id_lattes):
                return con.execute(
                    f"""
                    SELECT nome_aluno, nivel, ano_ingresso, nivel_confianca,
                        titulo_trabalho_aluno, ano_obtencao_aluno
                    FROM tb_situacao_orientandos
                    WHERE id_lattes_professor = ? AND encontrado_no_lattes = FALSE
                      AND {pred_ingresso}
                    ORDER BY
                        CASE nivel_confianca
                            WHEN 'divergente' THEN 0
                            WHEN 'confirmado' THEN 1
                            WHEN 'confirmado_parcial' THEN 2
                            ELSE 3
                        END,
                        ano_ingresso DESC NULLS LAST, nome_aluno
                    """, [id_lattes, rel_ano_inicio, rel_ano_fim]).df()

            def _html_alunos_faltantes(nome, id_lattes, df_alunos_falt):
                gerado_em = datetime.now().strftime("%d/%m/%Y %H:%M")
                col_alunos = [
                    ("Aluno", "nome_aluno", _fmt_txt), ("Nível", "nivel", _fmt_txt),
                    ("Ano de Ingresso", "ano_ingresso", _fmt_ano),
                    ("Confiança", "nivel_confianca", _fmt_confianca),
                    ("Título (Lattes do aluno)", "titulo_trabalho_aluno", _fmt_txt),
                    ("Ano de Obtenção (Lattes do aluno)", "ano_obtencao_aluno", _fmt_ano),
                ]

                corpo = (
                    "<p class='vazio'>Nenhum aluno faltante encontrado.</p>"
                    if df_alunos_falt.empty else _tabela_html(df_alunos_falt, col_alunos)
                )

                return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>Alunos faltantes no Lattes — {html_lib.escape(str(nome))}</title>
<style>{_CSS_RELATORIO}</style></head>
<body>
<header>
  <h1>Alunos do programa faltando no Lattes do orientador</h1>
  <p class="sub">Alunos cadastrados no programa (planilha administrativa) e ausentes do currículo Lattes deste orientador.
  A coluna "Confiança" cruza o que o próprio aluno declara no Lattes dele -- "Divergente" merece revisão antes de mais nada; "Confirmado"/"Confirmado parcialmente" já tem título/ano prontos ao lado, só falta incluir no Lattes.</p>
  <table class="meta">
    <tr><td><strong>Orientador</strong></td><td>{html_lib.escape(str(nome))}</td></tr>
    <tr><td><strong>ID Lattes</strong></td><td>{html_lib.escape(str(id_lattes))}</td></tr>
    <tr><td><strong>Período (ano de ingresso)</strong></td><td>{rel_ano_inicio} a {rel_ano_fim}{' (inclui alunos sem ano de ingresso)' if incluir_sem_ano_alunos else ''}</td></tr>
    <tr><td><strong>Gerado em</strong></td><td>{gerado_em}</td></tr>
  </table>
</header>
<h2>Alunos <span class="cont">({len(df_alunos_falt)})</span></h2>
{corpo}
<footer>Sistema de Avaliação de Produtividade Acadêmica — para resolver estas pendências,
inclua cada aluno na seção de Orientações do seu currículo Lattes.</footer>
<button class="noprint" onclick="window.print()">Imprimir / Salvar como PDF</button>
</body></html>"""

            df_profs_alunos = con.execute(
                f"""
                SELECT p.id_lattes, p.nome_completo,
                    (SELECT COUNT(*) FROM tb_situacao_orientandos s
                       WHERE s.id_lattes_professor = p.id_lattes
                         AND s.encontrado_no_lattes = FALSE
                         AND {pred_ingresso_s}) AS falt_alunos,
                    (SELECT COUNT(*) FROM tb_situacao_orientandos s
                       WHERE s.id_lattes_professor = p.id_lattes
                         AND s.encontrado_no_lattes = FALSE
                         AND s.nivel_confianca = 'divergente'
                         AND {pred_ingresso_s}) AS div_alunos
                FROM tb_professores p
                ORDER BY p.nome_completo
                """, [rel_ano_inicio, rel_ano_fim, rel_ano_inicio, rel_ano_fim]
            ).df()

            mostrar_todos_alunos = st.checkbox(
                "Mostrar também orientadores sem pendências", value=False, key="mostrar_todos_alunos"
            )

            st.markdown("#### Orientadores")
            h1, h2, h3, h4 = st.columns([5, 1, 1, 2])
            h1.markdown("**Orientador**")
            h2.markdown("**Alunos Faltantes**")
            h3.markdown("**Divergentes**")
            h4.markdown("**Relatório**")

            algum_exibido_alunos = False
            for _, prof in df_profs_alunos.iterrows():
                falt_alunos = int(prof["falt_alunos"])
                div_alunos = int(prof["div_alunos"])
                if falt_alunos == 0 and not mostrar_todos_alunos:
                    continue
                algum_exibido_alunos = True
                c1, c2, c3, c4 = st.columns([5, 1, 1, 2])
                c1.write(prof["nome_completo"])
                c2.write(falt_alunos)
                c3.write(div_alunos if div_alunos else "—")
                if falt_alunos == 0:
                    c4.caption("Sem pendências")
                else:
                    df_alunos_falt = _dados_alunos_faltantes(prof["id_lattes"])
                    doc_html = _html_alunos_faltantes(prof["nome_completo"], prof["id_lattes"], df_alunos_falt)
                    slug = re.sub(r"[^A-Za-z0-9]+", "_", str(prof["nome_completo"])).strip("_")
                    c4.download_button(
                        "Baixar HTML",
                        data=doc_html.encode("utf-8"),
                        file_name=f"alunos_faltantes_lattes_{slug}.html",
                        mime="text/html",
                        key=f"dl_falt_alunos_{prof['id_lattes']}",
                    )

            if not algum_exibido_alunos:
                st.success(
                    f"Nenhum orientador possui alunos faltantes no Lattes nesta base "
                    f"com ingresso entre {rel_ano_inicio} e {rel_ano_fim}."
                )

    elif relatorio_selecionado == "Papers com coautoria discente":
        st.markdown(
            "Lista os papers do programa em que a detecção de coautoria encontrou **ao menos um "
            "aluno entre os autores** (coluna `coautoria_aluno`, gravada por "
            "`coauthorship_detection.py` comparando a string de autores com os nomes e formas de "
            "citação dos alunos). Diferente dos demais relatórios, é um **documento único do "
            "programa**, não um arquivo por docente."
        )

        MODO_POR_DOCENTE = "Uma linha por docente (o mesmo paper repete)"
        MODO_POR_PAPER = "Uma linha por paper (sem nome de docente)"
        modo_coautoria = st.radio(
            "Papers assinados por mais de um docente do quadro:",
            [MODO_POR_DOCENTE, MODO_POR_PAPER],
            key="modo_coautoria",
            help="A base guarda uma linha por docente: um paper coassinado por dois docentes do "
                 "quadro aparece duas vezes. O modo por paper une essas linhas.",
        )
        por_paper = modo_coautoria == MODO_POR_PAPER

        sem_ano_coautoria = con.execute(
            """
            SELECT (SELECT COUNT(*) FROM tb_artigo_periodico
                      WHERE coautoria_aluno = TRUE AND ano_pub IS NULL)
                 + (SELECT COUNT(*) FROM tb_artigo_conferencia
                      WHERE coautoria_aluno = TRUE AND ano IS NULL)
            """
        ).fetchone()[0]

        incluir_sem_ano_coautoria = False
        if sem_ano_coautoria:
            incluir_sem_ano_coautoria = st.checkbox(
                f"Incluir também os {sem_ano_coautoria} paper(s) sem ano informado",
                value=False,
                key="incluir_sem_ano_coautoria",
                help="Papers sem ano não pertencem a nenhum intervalo. No modo por paper eles "
                     "só se unem a outros papers sem ano, já que o ano compõe a chave por título.",
            )

        df_coaut_p = con.execute(
            f"""
            SELECT pr.nome_completo AS docente, a.titulo_artigo, a.titulo_revista_lattes AS veiculo,
                   a.ano_pub AS ano, a.doi
            FROM tb_artigo_periodico a
            JOIN tb_professores pr ON pr.id_lattes = a.id_lattes
            WHERE a.coautoria_aluno = TRUE AND {_pred_ano('a.ano_pub', incluir_sem_ano_coautoria)}
            ORDER BY a.ano_pub DESC NULLS LAST, a.titulo_artigo
            """, [rel_ano_inicio, rel_ano_fim]
        ).df()
        df_coaut_c = con.execute(
            f"""
            SELECT pr.nome_completo AS docente, a.titulo_artigo, a.titulo_evento_lattes AS veiculo,
                   a.ano AS ano, a.doi
            FROM tb_artigo_conferencia a
            JOIN tb_professores pr ON pr.id_lattes = a.id_lattes
            WHERE a.coautoria_aluno = TRUE AND {_pred_ano('a.ano', incluir_sem_ano_coautoria)}
            ORDER BY a.ano DESC NULLS LAST, a.titulo_artigo
            """, [rel_ano_inicio, rel_ano_fim]
        ).df()

        linhas_por_docente = len(df_coaut_p) + len(df_coaut_c)

        def _unir_papers_entre_docentes(df):
            """Colapsa em uma linha as várias linhas do mesmo paper — uma por
            docente do quadro que o assina.

            A regra do agrupamento vive em `dedup_publicacoes`
            (`agrupar_papers_entre_docentes` + `escolher_representantes`), que é
            a única comparação do sistema a **atravessar docentes** — a chave de
            deduplicação da base leva o `id_lattes` como prefixo justamente para
            nunca fundir currículos. Aqui a pergunta é outra (quantos papers
            distintos o programa produziu com discentes), e na página de
            Alocação Ótima é outra ainda (a quem entregar cada paper); as três
            precisam concordar sobre o que é "o mesmo paper", então a regra é
            uma só.
            """
            if df.empty:
                return df.drop(columns=["docente"])

            grupos = dp.agrupar_papers_entre_docentes(df, coluna_ano="ano")
            posicoes = dp.escolher_representantes(df, grupos)
            return (
                df.iloc[posicoes]
                .drop(columns=["docente"])
                .reset_index(drop=True)
            )

        if por_paper:
            df_coaut_p = _unir_papers_entre_docentes(df_coaut_p)
            df_coaut_c = _unir_papers_entre_docentes(df_coaut_c)

        total_p, total_c = len(df_coaut_p), len(df_coaut_c)
        total_geral = total_p + total_c

        if total_geral == 0:
            st.success(
                f"Nenhum paper com coautoria discente no período {rel_ano_inicio}–{rel_ano_fim}."
            )
        else:
            if por_paper:
                st.caption(
                    f"{total_geral} paper(s) distinto(s) — {total_p} em periódicos e {total_c} em "
                    f"conferências. As {linhas_por_docente} linhas por docente foram unidas em "
                    f"{total_geral}: {linhas_por_docente - total_geral} eram o mesmo paper "
                    f"assinado por mais de um docente do quadro."
                )
            else:
                st.caption(
                    f"{total_geral} linha(s) — {total_p} em periódicos e {total_c} em conferências. "
                    f"Um paper coassinado por dois docentes do quadro aparece duas vezes."
                )

            colunas_comuns = [
                ("Ano", "ano", _fmt_ano), ("Título", "titulo_artigo", _fmt_txt),
                ("Veículo", "veiculo", _fmt_txt), ("DOI", "doi", _fmt_txt),
            ]
            colunas_coaut = (
                colunas_comuns if por_paper
                else [("Docente", "docente", _fmt_txt)] + colunas_comuns
            )

            aba_p, aba_c = st.tabs([f"Periódicos ({total_p})", f"Conferências ({total_c})"])
            with aba_p:
                if df_coaut_p.empty:
                    st.info("Nenhum periódico com coautoria discente no período.")
                else:
                    st.dataframe(df_coaut_p, use_container_width=True, hide_index=True)
            with aba_c:
                if df_coaut_c.empty:
                    st.info("Nenhuma conferência com coautoria discente no período.")
                else:
                    st.dataframe(df_coaut_c, use_container_width=True, hide_index=True)

            gerado_em_coaut = datetime.now().strftime("%d/%m/%Y %H:%M")
            periodo_coaut = (
                f"{rel_ano_inicio} a {rel_ano_fim}"
                + (" (inclui itens sem ano informado)" if incluir_sem_ano_coautoria else "")
            )

            def _secao_coaut(titulo, df):
                corpo = (
                    "<p class='vazio'>Nenhum item no período.</p>"
                    if df.empty else _tabela_html(df, colunas_coaut)
                )
                return f"<h2>{html_lib.escape(titulo)} <span class='cont'>({len(df)})</span></h2>{corpo}"

            html_coautoria = f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>Papers com coautoria discente — {rel_ano_inicio} a {rel_ano_fim}</title>
<style>{_CSS_RELATORIO}</style></head>
<body>
<header>
  <h1>Papers com coautoria discente</h1>
  <p class="sub">Publicações do programa com ao menos um aluno entre os autores.</p>
  <table class="meta">
    <tr><td><strong>Período</strong></td><td>{periodo_coaut}</td></tr>
    <tr><td><strong>Apresentação</strong></td><td>{html_lib.escape(modo_coautoria)}</td></tr>
    <tr><td><strong>Total</strong></td><td>{total_geral} ({total_p} em periódicos, {total_c} em conferências)</td></tr>
    <tr><td><strong>Gerado em</strong></td><td>{gerado_em_coaut}</td></tr>
  </table>
</header>
{_secao_coaut("Periódicos", df_coaut_p)}
{_secao_coaut("Conferências", df_coaut_c)}
<footer>Sistema de Avaliação de Produtividade Acadêmica — {
  'papers distintos, unidos por DOI ou por título e ano quando assinados por mais de um docente do quadro.'
  if por_paper else
  'uma linha por docente: papers assinados por mais de um docente do quadro aparecem repetidos.'
}</footer>
<button class="noprint" onclick="window.print()">Imprimir / Salvar como PDF</button>
</body></html>"""

            st.download_button(
                "Baixar HTML",
                data=html_coautoria.encode("utf-8"),
                file_name=(
                    f"coautoria_discente_{rel_ano_inicio}_{rel_ano_fim}"
                    f"{'_por_paper' if por_paper else '_por_docente'}.html"
                ),
                mime="text/html",
                key="dl_coautoria_discente",
            )

# ------------------------------------------
# PÁGINA 9: COMPARATIVO ENTRE BASES
# ------------------------------------------
elif pagina_selecionada == PAGINA_COMPARATIVO:
    st.title("Comparativo entre Bases")
    st.markdown(
        "Módulo de auditoria comparativa: contraste lado a lado entre a base institucional "
        "vigente (**Base A**) e uma segunda base `.duckdb` de mesma arquitetura (**Base B**). "
        "As visualizações abaixo replicam os mesmos indicadores e gráficos já disponíveis nos "
        "demais módulos do sistema, espelhados para as duas bases."
    )

    # --------------------------------------------------
    # Escolha da Base B (antes vivia na barra lateral).
    # O cadastro/extração das bases geridas fica em Configurações; aqui só se
    # escolhe qual usar, ou se envia um .duckdb avulso.
    # --------------------------------------------------
    con_b = None
    nome_base_b = None

    bases_comparacao = listar_bases_comparacao_info()
    bases_com_duckdb = [l for l in bases_comparacao if l["_duckdb_existe"]]

    with st.expander("Base de Comparação (Base B)", expanded=True):
        fonte_base_b = st.radio(
            "Fonte da Base B",
            ["Base gerida pelo sistema", "Enviar arquivo .duckdb manualmente"],
            horizontal=True,
            key="fonte_base_b",
        )

        if fonte_base_b == "Base gerida pelo sistema":
            if not bases_com_duckdb:
                st.info(
                    "Nenhuma base gerida com banco gerado ainda. Cadastre a lista e rode "
                    "extração + reprocessamento na página de Configurações."
                )
                st.button(
                    "Abrir Configurações",
                    on_click=ir_para_pagina,
                    args=(PAGINA_CONFIGURACOES,),
                    key="btn_ir_config_comparativo",
                )
            else:
                nome_base_b_escolhida = st.selectbox(
                    "Escolha a base gerida",
                    [l["nome"] for l in bases_com_duckdb],
                    key="select_base_b_gerida",
                )
                linha_escolhida = next(l for l in bases_com_duckdb if l["nome"] == nome_base_b_escolhida)
                try:
                    mtime = os.path.getmtime(linha_escolhida["_duckdb_path"])
                    con_b = abrir_base_comparacao_gerida(linha_escolhida["_duckdb_path"], mtime)
                    nome_base_b = linha_escolhida["_duckdb_path"]
                    st.success(f"Base B carregada: {nome_base_b}")
                except Exception as e:
                    st.error(f"Falha ao abrir a base gerida: {e}")
                    con_b = None
        else:
            arquivo_base_b = st.file_uploader(
                "Envie um segundo arquivo .duckdb (mesma arquitetura de tabelas)",
                type=["duckdb", "db"],
                help="O arquivo deve conter as mesmas tabelas da base institucional: "
                     "tb_professores, tb_artigo_periodico, tb_artigo_conferencia e tb_orientacoes.",
                key="upload_base_b",
            )
            if arquivo_base_b is not None:
                try:
                    con_b = carregar_base_comparacao(arquivo_base_b.getvalue(), arquivo_base_b.name)
                    nome_base_b = arquivo_base_b.name
                    st.success(f"Base B carregada: {nome_base_b}")
                except Exception as e:
                    st.error(f"Falha ao abrir a base enviada: {e}")
                    con_b = None

    if con_b is None:
        st.info(
            "Selecione uma base gerida ou envie um arquivo **.duckdb** acima para habilitar "
            "este módulo. O arquivo precisa conter as tabelas tb_professores, "
            "tb_artigo_periodico, tb_artigo_conferencia e tb_orientacoes."
        )
    else:
        # --------------------------------------------------
        # Funções auxiliares — parametrizadas por conexão para
        # reaproveitar exatamente a mesma lógica das demais páginas
        # em qualquer uma das duas bases (A ou B).
        # --------------------------------------------------
        def comp_indicadores_gerais(conexao, ano_ini, ano_fim_, frag="", aplicar_recorte=True):
            res_docentes = conexao.execute("SELECT COUNT(id_lattes) FROM tb_professores").fetchone()
            res_p = conexao.execute(
                f"SELECT COUNT(id_artigo_periodico) FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?{frag}"
                f"{sql_recorte_docente('tb_artigo_periodico', 'ano_pub', aplicar=aplicar_recorte)}",
                [ano_ini, ano_fim_]
            ).fetchone()
            res_c = conexao.execute(
                f"SELECT COUNT(id_artigo_conferencia) FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?{frag}"
                f"{sql_recorte_docente('tb_artigo_conferencia', 'ano', aplicar=aplicar_recorte)}",
                [ano_ini, ano_fim_]
            ).fetchone()
            total_docentes = res_docentes[0] if res_docentes else 0
            total_p = res_p[0] if res_p else 0
            total_c = res_c[0] if res_c else 0
            return total_docentes, total_p, total_c

        def comp_serie_historica_periodico(conexao, frag="", aplicar_recorte=True):
            q = (
                f"SELECT CAST(ano_pub AS VARCHAR) AS Ano, COUNT(*) AS Quantidade FROM tb_artigo_periodico "
                f"WHERE ano_pub IS NOT NULL{frag}{sql_recorte_docente('tb_artigo_periodico', 'ano_pub', aplicar=aplicar_recorte)} "
                f"GROUP BY ano_pub ORDER BY ano_pub"
            )
            return conexao.execute(q).df()

        def comp_serie_historica_conferencia(conexao, frag="", aplicar_recorte=True):
            q = (
                f"SELECT CAST(ano AS VARCHAR) AS Ano, COUNT(*) AS Quantidade FROM tb_artigo_conferencia "
                f"WHERE ano IS NOT NULL{frag}{sql_recorte_docente('tb_artigo_conferencia', 'ano', aplicar=aplicar_recorte)} "
                f"GROUP BY ano ORDER BY ano"
            )
            return conexao.execute(q).df()

        def comp_ranking_docente(conexao, ano_ini, ano_fim_, frag_p="", frag_c="", aplicar_recorte=True):
            q = f"""
                SELECT
                    p.nome_completo AS Docente,
                    COUNT(DISTINCT a_p.id_artigo_periodico) AS Periodicos,
                    COUNT(DISTINCT a_c.id_artigo_conferencia) AS Conferencias,
                    (COUNT(DISTINCT a_p.id_artigo_periodico) + COUNT(DISTINCT a_c.id_artigo_conferencia)) AS Total
                FROM tb_professores p
                LEFT JOIN tb_artigo_periodico a_p ON p.id_lattes = a_p.id_lattes AND a_p.ano_pub BETWEEN {ano_ini} AND {ano_fim_}{frag_p}{sql_recorte_docente('a_p', 'ano_pub', aplicar=aplicar_recorte)}
                LEFT JOIN tb_artigo_conferencia a_c ON p.id_lattes = a_c.id_lattes AND a_c.ano BETWEEN {ano_ini} AND {ano_fim_}{frag_c}{sql_recorte_docente('a_c', 'ano', aplicar=aplicar_recorte)}
                GROUP BY p.nome_completo
                ORDER BY Total DESC
            """
            return conexao.execute(q).df()

        def comp_indice_quadrienal(conexao, ano_ini, ano_fim_, restrito, frag_p="", frag_c="", aplicar_recorte=True):
            """Replica a lógica das páginas de Avaliação Quadrienal (Geral ou Restrita A1-A4),
            retornando periódicos e conferências consolidados em um único score por docente."""
            ing_p = sql_recorte_docente('tb_artigo_periodico', 'ano_pub', aplicar=aplicar_recorte)
            ing_c = sql_recorte_docente('tb_artigo_conferencia', 'ano', aplicar=aplicar_recorte)
            if not restrito:
                query_p = f"""
                    WITH cte_classificacao AS (
                        SELECT id_lattes,
                            CASE
                                WHEN maior_percentil >= 87.5 THEN 1.000 WHEN maior_percentil >= 75.0 THEN 0.875
                                WHEN maior_percentil >= 62.5 THEN 0.750 WHEN maior_percentil >= 50.0 THEN 0.625
                                WHEN maior_percentil >= 37.5 THEN 0.500 WHEN maior_percentil >= 25.0 THEN 0.375
                                WHEN maior_percentil >= 12.5 THEN 0.250 ELSE 0.125
                            END AS pontos_artigo
                        FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?{frag_p}{ing_p}
                    )
                    SELECT id_lattes, COUNT(*) AS total_p, COALESCE(ROUND(SUM(pontos_artigo), 3), 0) AS score_p
                    FROM cte_classificacao GROUP BY id_lattes
                """
                query_c = f"""
                    WITH cte_classificacao AS (
                        SELECT id_lattes,
                            CASE
                                WHEN estrato = 'A1' THEN 1.000 WHEN estrato = 'A2' THEN 0.875
                                WHEN estrato = 'A3' THEN 0.750 WHEN estrato = 'A4' THEN 0.625
                                WHEN estrato = 'A5' THEN 0.500 WHEN estrato = 'A6' THEN 0.375
                                WHEN estrato = 'A7' THEN 0.250 ELSE 0.125
                            END AS pontos_artigo
                        FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?{frag_c}{ing_c}
                    )
                    SELECT id_lattes, COUNT(*) AS total_c, COALESCE(ROUND(SUM(pontos_artigo), 3), 0) AS score_c
                    FROM cte_classificacao GROUP BY id_lattes
                """
            else:
                query_p = f"""
                    WITH cte_classificacao AS (
                        SELECT id_lattes,
                            CASE
                                WHEN maior_percentil >= 87.5 THEN 1.000 WHEN maior_percentil >= 75.0 THEN 0.875
                                WHEN maior_percentil >= 62.5 THEN 0.750 WHEN maior_percentil >= 50.0 THEN 0.625
                                ELSE 0.000
                            END AS pontos_artigo
                        FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ? AND maior_percentil >= 50.0{frag_p}{ing_p}
                    )
                    SELECT id_lattes, COUNT(*) AS total_p, COALESCE(ROUND(SUM(pontos_artigo), 3), 0) AS score_p
                    FROM cte_classificacao GROUP BY id_lattes
                """
                query_c = f"""
                    WITH cte_classificacao AS (
                        SELECT id_lattes,
                            CASE
                                WHEN estrato = 'A1' THEN 1.000 WHEN estrato = 'A2' THEN 0.875
                                WHEN estrato = 'A3' THEN 0.750 WHEN estrato = 'A4' THEN 0.625
                                ELSE 0.000
                            END AS pontos_artigo
                        FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ? AND estrato IN ('A1', 'A2', 'A3', 'A4'){frag_c}{ing_c}
                    )
                    SELECT id_lattes, COUNT(*) AS total_c, COALESCE(ROUND(SUM(pontos_artigo), 3), 0) AS score_c
                    FROM cte_classificacao GROUP BY id_lattes
                """

            df_p = conexao.execute(query_p, [ano_ini, ano_fim_]).df()
            df_c = conexao.execute(query_c, [ano_ini, ano_fim_]).df()

            q_pessoas = "SELECT id_lattes, nome_completo AS Docente FROM tb_professores"
            df_pessoas = conexao.execute(q_pessoas).df()

            df = df_pessoas.merge(df_p, on='id_lattes', how='left').merge(df_c, on='id_lattes', how='left')
            for col in ['total_p', 'score_p', 'total_c', 'score_c']:
                df[col] = df[col].fillna(0)
            df['Score Total'] = df['score_p'] + df['score_c']
            return df.sort_values('Score Total', ascending=False)

        def comp_orientacoes(conexao, ano_ini, ano_fim_, aplicar_recorte=True):
            condicao = "ano_inicio <= ? AND COALESCE(ano_conclusao, 2026) >= ?" + sql_recorte_docente(
                'tb_orientacoes', 'ano_inicio', aplicar=aplicar_recorte
            )
            parametros = [ano_fim_, ano_ini]
            q = f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'Concluída' THEN 1 ELSE 0 END) AS concluidas,
                    SUM(CASE WHEN status = 'Em Andamento' THEN 1 ELSE 0 END) AS andamento
                FROM tb_orientacoes
                WHERE {condicao}
            """
            res = conexao.execute(q, parametros).fetchone()
            return (res[0] or 0), (res[1] or 0), (res[2] or 0)

        def comp_orientacoes_por_docente(conexao, ano_ini, ano_fim_, aplicar_recorte=True):
            """Orientações de cada docente cadastrado no mesmo recorte de
            `comp_orientacoes`, com 0 para quem não orientou. É a série que
            sustenta o desvio padrão e a mediana ao lado do per capita."""
            condicao = "o.ano_inicio <= ? AND COALESCE(o.ano_conclusao, 2026) >= ?" + sql_recorte_docente(
                'o', 'ano_inicio', aplicar=aplicar_recorte
            )
            q = f"""
                SELECT p.id_lattes, COUNT(o.id_orientacao) AS total
                FROM tb_professores p
                LEFT JOIN tb_orientacoes o ON p.id_lattes = o.id_lattes AND {condicao}
                GROUP BY p.id_lattes
            """
            return conexao.execute(q, [ano_fim_, ano_ini]).df()["total"]

        def comp_orientacoes_por_nivel(conexao, ano_ini, ano_fim_, aplicar_recorte=True):
            condicao = "ano_inicio <= ? AND COALESCE(ano_conclusao, 2026) >= ?" + sql_recorte_docente(
                'tb_orientacoes', 'ano_inicio', aplicar=aplicar_recorte
            )
            parametros = [ano_fim_, ano_ini]
            q = f"""
                SELECT nivel AS "Nível Acadêmico", COUNT(*) AS "Volume de Orientações"
                FROM tb_orientacoes
                WHERE nivel IS NOT NULL AND {condicao}
                GROUP BY nivel
                ORDER BY "Volume de Orientações" DESC
            """
            return conexao.execute(q, parametros).df()

        # --------------------------------------------------
        # Filtro de período compartilhado entre as duas bases
        # --------------------------------------------------
        ano_min_a, ano_max_a = get_year_bounds(con)
        ano_min_b, ano_max_b = get_year_bounds(con_b)
        comp_ano_min = min(ano_min_a, ano_min_b)
        comp_ano_max = max(ano_max_a, ano_max_b)

        st.subheader("Filtro de Período (aplicado simultaneamente às duas bases)")
        comp_ano_inicio, comp_ano_fim = renderizar_filtro_periodo(comp_ano_min, comp_ano_max, "comparacao")

        # Filtro de fonte aplicado às duas bases. A Base B enviada pode ter
        # arquitetura antiga (sem a coluna `fontes`); nesse caso o filtro
        # "Apenas Lattes" é aplicado somente à Base A.
        lattes_b_ok = tem_coluna(con_b, 'tb_artigo_periodico', 'fontes') and tem_coluna(con_b, 'tb_artigo_conferencia', 'fontes')
        if apenas_lattes and not lattes_b_ok:
            st.warning("A Base B enviada não possui a coluna `fontes`; o filtro 'Apenas cadastradas no Lattes' será aplicado somente à Base A.")
        _ativo_b = apenas_lattes and lattes_b_ok
        fA = sql_fonte()
        fB = sql_fonte(ativo=_ativo_b)
        fA_p, fA_c = sql_fonte('a_p.fontes'), sql_fonte('a_c.fontes')
        fB_p = sql_fonte('a_p.fontes', ativo=_ativo_b)
        fB_c = sql_fonte('a_c.fontes', ativo=_ativo_b)

        # Recorte temporal por docente aplicado às duas bases, no regime
        # escolhido na barra lateral. A Base B enviada pode não ter o insumo da
        # regra em vigor -- `data_ingresso` (arquitetura antiga, ou base gerada
        # só a partir do Lattes de outra instituição, sem `lista_pessoas.csv`)
        # ou `tb_credenciamento_anos` (base anterior à Seção 15 do notebook, ou
        # de um programa sem planilha de credenciamento). Nesses casos o corte
        # é aplicado somente à Base A, com o aviso correspondente.
        if regime_recorte == REGIME_VIGENCIA:
            recorte_b_ok = tabela_tem_linhas(con_b, 'tb_credenciamento_anos')
            aviso_recorte_b = (
                "A Base B enviada não possui `tb_credenciamento_anos` preenchida; o recorte "
                "por anos de credenciamento será aplicado somente à Base A."
            )
        else:
            recorte_b_ok = tem_coluna(con_b, 'tb_professores', 'data_ingresso')
            aviso_recorte_b = (
                "A Base B enviada não possui a coluna `data_ingresso`; o corte por data de "
                "ingresso será aplicado somente à Base A."
            )
        if not recorte_b_ok:
            st.warning(aviso_recorte_b)

        st.markdown(f"**Base A:** `{CAMINHO_BASE_INSTITUCIONAL}`  |  **Base B:** `{nome_base_b}`")
        st.markdown("---")

        aba_indic, aba_serie, aba_docente, aba_quadrienal, aba_orientacoes = st.tabs([
            "Indicadores Gerais",
            "Série Histórica",
            "Ranking por Docente",
            "Avaliação Quadrienal",
            "Orientações Acadêmicas",
        ])

        # ===== ABA 1: INDICADORES GERAIS (lado a lado) =====
        with aba_indic:
            st.subheader("Indicadores Institucionais — Base A vs. Base B")
            doc_a, p_a, c_a = comp_indicadores_gerais(con, comp_ano_inicio, comp_ano_fim, frag=fA, aplicar_recorte=True)
            doc_b, p_b, c_b = comp_indicadores_gerais(con_b, comp_ano_inicio, comp_ano_fim, frag=fB, aplicar_recorte=recorte_b_ok)

            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown("##### Base A")
                st.metric("Docentes Cadastrados", doc_a)
                st.metric("Artigos em Periódicos", p_a)
                st.metric("Trabalhos em Conferências", c_a)
                st.metric("Total de Produções", p_a + c_a)
            with col_b:
                st.markdown(f"##### Base B ({nome_base_b})")
                st.metric("Docentes Cadastrados", doc_b, delta=doc_b - doc_a)
                st.metric("Artigos em Periódicos", p_b, delta=p_b - p_a)
                st.metric("Trabalhos em Conferências", c_b, delta=c_b - c_a)
                st.metric("Total de Produções", p_b + c_b, delta=(p_b + c_b) - (p_a + c_a))

            st.markdown("#### Comparativo Direto de Volumes")
            df_comp_geral = pd.DataFrame({
                "Indicador": ["Docentes", "Periódicos", "Conferências", "Total"],
                "Base A": [doc_a, p_a, c_a, p_a + c_a],
                "Base B": [doc_b, p_b, c_b, p_b + c_b],
            }).set_index("Indicador")
            st.bar_chart(df_comp_geral, use_container_width=True)

        # ===== ABA 2: SÉRIE HISTÓRICA (lado a lado) =====
        with aba_serie:
            st.subheader("Série Histórica da Produção — Base A vs. Base B")
            sub_p, sub_c = st.tabs(["Periódicos", "Conferências"])

            with sub_p:
                col_a, col_b = st.columns(2)
                df_serie_p_a = comp_serie_historica_periodico(con, frag=fA, aplicar_recorte=True)
                df_serie_p_b = comp_serie_historica_periodico(con_b, frag=fB, aplicar_recorte=recorte_b_ok)
                with col_a:
                    st.markdown("##### Base A")
                    if not df_serie_p_a.empty:
                        st.bar_chart(data=df_serie_p_a, x="Ano", y="Quantidade", use_container_width=True)
                    else:
                        st.info("Sem registros de periódicos na Base A.")
                with col_b:
                    st.markdown(f"##### Base B")
                    if not df_serie_p_b.empty:
                        st.bar_chart(data=df_serie_p_b, x="Ano", y="Quantidade", use_container_width=True)
                    else:
                        st.info("Sem registros de periódicos na Base B.")

                # Visão sobreposta: as duas séries no mesmo gráfico, por ano
                if not df_serie_p_a.empty or not df_serie_p_b.empty:
                    st.markdown("#### Visão Sobreposta (mesmo gráfico)")
                    df_overlay_p = pd.merge(
                        df_serie_p_a.rename(columns={"Quantidade": "Base A"}),
                        df_serie_p_b.rename(columns={"Quantidade": "Base B"}),
                        on="Ano", how="outer"
                    ).fillna(0).sort_values("Ano")
                    st.bar_chart(df_overlay_p.set_index("Ano")[["Base A", "Base B"]], use_container_width=True)

            with sub_c:
                col_a, col_b = st.columns(2)
                df_serie_c_a = comp_serie_historica_conferencia(con, frag=fA, aplicar_recorte=True)
                df_serie_c_b = comp_serie_historica_conferencia(con_b, frag=fB, aplicar_recorte=recorte_b_ok)
                with col_a:
                    st.markdown("##### Base A")
                    if not df_serie_c_a.empty:
                        st.bar_chart(data=df_serie_c_a, x="Ano", y="Quantidade", use_container_width=True)
                    else:
                        st.info("Sem registros de conferências na Base A.")
                with col_b:
                    st.markdown(f"##### Base B")
                    if not df_serie_c_b.empty:
                        st.bar_chart(data=df_serie_c_b, x="Ano", y="Quantidade", use_container_width=True)
                    else:
                        st.info("Sem registros de conferências na Base B.")

                if not df_serie_c_a.empty or not df_serie_c_b.empty:
                    st.markdown("#### Visão Sobreposta (mesmo gráfico)")
                    df_overlay_c = pd.merge(
                        df_serie_c_a.rename(columns={"Quantidade": "Base A"}),
                        df_serie_c_b.rename(columns={"Quantidade": "Base B"}),
                        on="Ano", how="outer"
                    ).fillna(0).sort_values("Ano")
                    st.bar_chart(df_overlay_c.set_index("Ano")[["Base A", "Base B"]], use_container_width=True)

        # ===== ABA 3: RANKING POR DOCENTE (lado a lado) =====
        with aba_docente:
            st.subheader("Volume de Produção por Pesquisador — Base A vs. Base B")
            df_rank_a = comp_ranking_docente(con, comp_ano_inicio, comp_ano_fim, frag_p=fA_p, frag_c=fA_c, aplicar_recorte=True)
            df_rank_b = comp_ranking_docente(con_b, comp_ano_inicio, comp_ano_fim, frag_p=fB_p, frag_c=fB_c, aplicar_recorte=recorte_b_ok)

            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown("##### Base A")
                st.dataframe(df_rank_a, use_container_width=True, hide_index=True)
                if not df_rank_a.empty:
                    st.bar_chart(df_rank_a.set_index("Docente")["Total"], use_container_width=True)
            with col_b:
                st.markdown(f"##### Base B ({nome_base_b})")
                st.dataframe(df_rank_b, use_container_width=True, hide_index=True)
                if not df_rank_b.empty:
                    st.bar_chart(df_rank_b.set_index("Docente")["Total"], use_container_width=True)

            st.markdown("#### Comparativo de Totais por Docente (Base A vs. Base B)")
            st.caption("Une os pesquisadores presentes em qualquer uma das duas bases pelo nome completo.")
            df_rank_merge = pd.merge(
                df_rank_a[["Docente", "Total"]].rename(columns={"Total": "Base A"}),
                df_rank_b[["Docente", "Total"]].rename(columns={"Total": "Base B"}),
                on="Docente", how="outer"
            ).fillna(0).sort_values("Base A", ascending=False)
            st.bar_chart(df_rank_merge.set_index("Docente")[["Base A", "Base B"]], use_container_width=True)

        # ===== ABA 4: AVALIAÇÃO QUADRIENAL (lado a lado) =====
        with aba_quadrienal:
            st.subheader("Índice de Produtividade Intelectual — Base A vs. Base B")
            criterio = st.radio(
                "Critério de Apuração:",
                ["Pontuação Integral (A1-A8)", "Pontuação Restrita (A1-A4)"],
                horizontal=True, key="comp_criterio"
            )
            restrito = criterio == "Pontuação Restrita (A1-A4)"

            df_quad_a = comp_indice_quadrienal(con, comp_ano_inicio, comp_ano_fim, restrito, frag_p=fA, frag_c=fA, aplicar_recorte=True)
            df_quad_b = comp_indice_quadrienal(con_b, comp_ano_inicio, comp_ano_fim, restrito, frag_p=fB, frag_c=fB, aplicar_recorte=recorte_b_ok)

            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown("##### Base A")
                st.dataframe(
                    df_quad_a[["Docente", "total_p", "score_p", "total_c", "score_c", "Score Total"]]
                    .rename(columns={"total_p": "Periódicos", "score_p": "Score Periódicos",
                                      "total_c": "Conferências", "score_c": "Score Conferências"})
                    .style.background_gradient(subset=["Score Total"], cmap="Purples"),
                    use_container_width=True, hide_index=True
                )
            with col_b:
                st.markdown(f"##### Base B ({nome_base_b})")
                st.dataframe(
                    df_quad_b[["Docente", "total_p", "score_p", "total_c", "score_c", "Score Total"]]
                    .rename(columns={"total_p": "Periódicos", "score_p": "Score Periódicos",
                                      "total_c": "Conferências", "score_c": "Score Conferências"})
                    .style.background_gradient(subset=["Score Total"], cmap="Purples"),
                    use_container_width=True, hide_index=True
                )

            st.markdown("#### Comparativo do Score Institucional Consolidado por Docente")
            df_quad_merge = pd.merge(
                df_quad_a[["Docente", "Score Total"]].rename(columns={"Score Total": "Base A"}),
                df_quad_b[["Docente", "Score Total"]].rename(columns={"Score Total": "Base B"}),
                on="Docente", how="outer"
            ).fillna(0).sort_values("Base A", ascending=False)
            st.bar_chart(df_quad_merge.set_index("Docente")[["Base A", "Base B"]], use_container_width=True)

            st.markdown("#### Score Total Agregado da Instituição")
            df_score_total = pd.DataFrame({
                "Base": ["Base A", "Base B"],
                "Score Agregado": [df_quad_a["Score Total"].sum(), df_quad_b["Score Total"].sum()]
            }).set_index("Base")
            st.bar_chart(df_score_total, use_container_width=True)

            st.markdown("#### Índices Per Capita (Score ÷ Docentes Cadastrados)")
            st.caption(
                "Calculado para os dois critérios de apuração simultaneamente, "
                "independente do critério selecionado acima para o detalhamento por docente."
            )
            df_quad_geral_a = comp_indice_quadrienal(con, comp_ano_inicio, comp_ano_fim, False, frag_p=fA, frag_c=fA, aplicar_recorte=True)
            df_quad_geral_b = comp_indice_quadrienal(con_b, comp_ano_inicio, comp_ano_fim, False, frag_p=fB, frag_c=fB, aplicar_recorte=recorte_b_ok)
            df_quad_restrito_a = comp_indice_quadrienal(con, comp_ano_inicio, comp_ano_fim, True, frag_p=fA, frag_c=fA, aplicar_recorte=True)
            df_quad_restrito_b = comp_indice_quadrienal(con_b, comp_ano_inicio, comp_ano_fim, True, frag_p=fB, frag_c=fB, aplicar_recorte=recorte_b_ok)

            total_doc_a = len(df_quad_geral_a) or 1
            total_doc_b = len(df_quad_geral_b) or 1

            # O per capita não é mais calculado aqui: é a coluna Média das tabelas
            # abaixo, que o obtêm da mesma série por docente (`Score Total`, com
            # uma linha por docente cadastrado) de que saem desvio e mediana.
            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown("##### Base A")
                renderizar_tabela_dispersao(
                    [
                        ("Score Livre (A1-A8)", df_quad_geral_a["Score Total"]),
                        ("Score Restrito (A1-A4)", df_quad_restrito_a["Score Total"]),
                    ],
                    casas=3,
                )
            with col_b:
                st.markdown(f"##### Base B ({nome_base_b})")
                # A terceira posição de cada linha é a série da Base A: gera a
                # coluna de diferença que antes era o `delta` das métricas.
                renderizar_tabela_dispersao(
                    [
                        ("Score Livre (A1-A8)", df_quad_geral_b["Score Total"], df_quad_geral_a["Score Total"]),
                        ("Score Restrito (A1-A4)", df_quad_restrito_b["Score Total"], df_quad_restrito_a["Score Total"]),
                    ],
                    casas=3,
                    rotulo_delta="Δ Média vs. A",
                )

            renderizar_explicacao_calculos(
                descricao_x=(
                    "o score quadrienal daquele docente — a soma dos pesos por estrato "
                    "(A1 = 1,000; A2 = 0,875; …; A8 = 0,125) de todos os seus artigos no "
                    "recorte, periódicos e conferências juntos."
                ),
                recorte=(
                    f"Janela de {comp_ano_inicio} a {comp_ano_fim} e filtro de fonte da barra "
                    "lateral, aplicados igualmente às duas bases; no regime de contagem da "
                    f"barra lateral ({regime_recorte.lower()}), {frase_recorte_docente()} — "
                    "desde que a base traga o insumo dessa regra (a Base B pode não trazer). "
                    "Os divisores são os quadros de cada base: "
                    f"{total_doc_a} docentes na A e {total_doc_b} na B — daí as médias serem "
                    "comparáveis mesmo com programas de tamanhos diferentes. Livre soma os "
                    "oito estratos; Restrito, apenas A1-A4."
                ),
                observacoes=[
                    "A coluna Δ Média vs. A é a diferença entre a média da Base B e a da "
                    "Base A, em pontos de score por docente — o mesmo número que antes "
                    "aparecia como variação sob a métrica da Base B.",
                    OBS_DUPLA_CONTAGEM,
                ],
            )

        # ===== ABA 5: ORIENTAÇÕES ACADÊMICAS (lado a lado) =====
        with aba_orientacoes:
            st.subheader("Panorama de Orientações — Base A vs. Base B")
            try:
                tot_a, conc_a, and_a = comp_orientacoes(con, comp_ano_inicio, comp_ano_fim, aplicar_recorte=True)
                tot_b, conc_b, and_b = comp_orientacoes(con_b, comp_ano_inicio, comp_ano_fim, aplicar_recorte=recorte_b_ok)

                col_a, col_b = st.columns(2)
                with col_a:
                    st.markdown("##### Base A")
                    st.metric("Total de Orientações", tot_a)
                    st.metric("Concluídas", conc_a)
                    st.metric("Em Andamento", and_a)
                with col_b:
                    st.markdown(f"##### Base B ({nome_base_b})")
                    st.metric("Total de Orientações", tot_b, delta=tot_b - tot_a)
                    st.metric("Concluídas", conc_b, delta=conc_b - conc_a)
                    st.metric("Em Andamento", and_b, delta=and_b - and_a)

                st.markdown("#### Orientações Per Capita (÷ Docentes Cadastrados)")
                total_doc_ori_a = con.execute("SELECT COUNT(*) FROM tb_professores").fetchone()[0] or 1
                total_doc_ori_b = con_b.execute("SELECT COUNT(*) FROM tb_professores").fetchone()[0] or 1

                serie_ori_a = comp_orientacoes_por_docente(con, comp_ano_inicio, comp_ano_fim, aplicar_recorte=True)
                serie_ori_b = comp_orientacoes_por_docente(con_b, comp_ano_inicio, comp_ano_fim, aplicar_recorte=recorte_b_ok)

                col_a, col_b = st.columns(2)
                with col_a:
                    st.markdown("##### Base A")
                    renderizar_tabela_dispersao(
                        [("Orientações / Docente", serie_ori_a)], casas=2
                    )
                with col_b:
                    st.markdown(f"##### Base B ({nome_base_b})")
                    renderizar_tabela_dispersao(
                        [("Orientações / Docente", serie_ori_b, serie_ori_a)], casas=2,
                        rotulo_delta="Δ Média vs. A",
                    )

                renderizar_explicacao_calculos(
                    descricao_x=(
                        "o número de orientações daquele docente com vínculo ativo em algum "
                        "momento da janela."
                    ),
                    recorte=(
                        f"Orientações iniciadas até {comp_ano_fim} e concluídas a partir de "
                        f"{comp_ano_inicio} (ou ainda em andamento) — o vínculo precisa apenas "
                        "interceptar a janela, não caber inteiro nela. Os divisores são os "
                        f"quadros de cada base: {total_doc_ori_a} docentes na A e "
                        f"{total_doc_ori_b} na B."
                    ),
                    observacoes=[
                        "A coluna Δ Média vs. A é a diferença entre a média da Base B e a da "
                        "Base A, em orientações por docente.",
                        "Uma coorientação registrada nos dois currículos conta para cada um "
                        "dos orientadores do quadro.",
                    ],
                )

                st.markdown("#### Distribuição por Nível Acadêmico — Comparativo")
                df_nivel_a = comp_orientacoes_por_nivel(con, comp_ano_inicio, comp_ano_fim, aplicar_recorte=True)
                df_nivel_b = comp_orientacoes_por_nivel(con_b, comp_ano_inicio, comp_ano_fim, aplicar_recorte=recorte_b_ok)

                col_a, col_b = st.columns(2)
                with col_a:
                    st.markdown("##### Base A")
                    if not df_nivel_a.empty:
                        st.bar_chart(df_nivel_a.set_index("Nível Acadêmico"), use_container_width=True)
                    else:
                        st.info("Sem dados de orientação na Base A para o período.")
                with col_b:
                    st.markdown(f"##### Base B")
                    if not df_nivel_b.empty:
                        st.bar_chart(df_nivel_b.set_index("Nível Acadêmico"), use_container_width=True)
                    else:
                        st.info("Sem dados de orientação na Base B para o período.")

                if not df_nivel_a.empty or not df_nivel_b.empty:
                    st.markdown("#### Visão Sobreposta (mesmo gráfico)")
                    df_overlay_nivel = pd.merge(
                        df_nivel_a.rename(columns={"Volume de Orientações": "Base A"}),
                        df_nivel_b.rename(columns={"Volume de Orientações": "Base B"}),
                        on="Nível Acadêmico", how="outer"
                    ).fillna(0)
                    st.bar_chart(df_overlay_nivel.set_index("Nível Acadêmico")[["Base A", "Base B"]], use_container_width=True)
            except Exception as e:
                st.warning(f"Não foi possível comparar orientações: verifique se ambas as bases possuem a tabela tb_orientacoes. Detalhe: {e}")

# ------------------------------------------
# PÁGINA 10: CONFIGURAÇÕES (MANUTENÇÃO DE DADOS)
# ------------------------------------------
# Concentra tudo que antes lotava a barra lateral: os dois jobs assíncronos da
# base institucional (extração via scriptLattes + reprocessamento do notebook)
# e o ciclo de vida das bases de comparação. Os jobs são destacados do processo
# do Streamlit (sobrevivem a fechar a aba) e reportam estado em
# dados_brutos/status/*.json, lidos aqui por poll.
elif pagina_selecionada == PAGINA_CONFIGURACOES:
    st.title("Configurações")
    st.markdown(
        "Manutenção dos dados do sistema: atualização da base institucional e gestão das "
        "bases de outras instituições usadas no módulo Comparativo. As páginas analíticas "
        "leem sempre o último banco processado com sucesso."
    )

    # Resultado da última gravação de credenciamento. Fica FORA das abas de
    # propósito: `st.rerun()` devolve o `st.tabs` para a primeira aba, então uma
    # mensagem renderizada dentro da aba de credenciamento ficaria escondida
    # atrás de uma aba não selecionada -- e um erro de gravação passaria por
    # "não aconteceu nada".
    _resultado_cred = st.session_state.pop("_cred_resultado", None)
    if _resultado_cred:
        tipo, mensagens = _resultado_cred
        (st.success if tipo == "ok" else st.error)(mensagens[0])
        for extra in mensagens[1:]:
            st.caption(extra)

    aba_base, aba_credenciamento, aba_comparacao = st.tabs(
        ["Base institucional", "Anos de credenciamento", "Bases de comparação"]
    )

    # ===== ABA 1: BASE INSTITUCIONAL =====
    with aba_base:
        st.subheader("Base institucional")
        st.caption(f"Arquivo em uso: `{CAMINHO_BASE_INSTITUCIONAL}` (somente leitura)")

        algum_job_rodando = (
            status_extract.get("state") == "running" or status_process.get("state") == "running"
        )
        extracao_global_rodando = jobs.existe_extracao_rodando()

        st.markdown("#### Atualização de dados")
        st.markdown(
            "- **Re-extrair currículos**: roda o scriptLattes (Selenium) contra a Plataforma "
            "Lattes e, ao terminar, encadeia o reprocessamento automaticamente.\n"
            "- **Reprocessar dados**: roda `analyse_organizado.ipynb` sobre os JSONs já "
            "extraídos, sem bater na Lattes."
        )

        ignorar_cache_extracao = st.checkbox(
            "Ignorar cache (rebaixar todos os currículos)",
            value=False,
            disabled=extracao_global_rodando,
            key="ignorar_cache_extracao",
            help="Por padrão, 'Re-extrair' só busca quem ainda não foi baixado ou falhou — "
                 "reaproveita o cache do scriptLattes, então não pega CVs atualizados de quem "
                 "já está no cache. Marque isto pra apagar o cache antes e rebaixar todo mundo "
                 "de novo (mais lento, mais requisições à Lattes — maior risco de bloqueio).",
        )

        col_extrair, col_reprocessar = st.columns(2)

        if col_extrair.button(
            "Re-extrair currículos",
            disabled=extracao_global_rodando,
            help="Desabilitado enquanto qualquer extração (principal ou de comparação) "
                 "estiver em andamento: todas compartilham o mesmo cache e chromedriver.",
            use_container_width=True,
            key="btn_extrair_principal",
        ):
            jobs.write_status(jobs.EXTRACT_STATUS, state="running", started_at=jobs.now_iso())
            if ignorar_cache_extracao:
                jobs.launch("run_extract.py", "--limpar-cache")
            else:
                jobs.launch("run_extract.py")
            st.rerun()

        if col_reprocessar.button(
            "Reprocessar dados",
            disabled=algum_job_rodando,
            help="Roda analyse_organizado.ipynb sobre os JSONs já extraídos.",
            use_container_width=True,
            key="btn_reprocessar_principal",
        ):
            jobs.write_status(jobs.PROCESS_STATUS, state="running", started_at=jobs.now_iso())
            jobs.launch("run_process.py")
            st.rerun()

        st.markdown("#### Situação dos jobs")
        linha_status("Extração", status_extract)
        linha_status("Reprocessamento", status_process)

    # ===== ABA 2: ANOS DE CREDENCIAMENTO =====
    # Editor da vigência ano a ano. Grava SEMPRE no CSV
    # (`credenciamento_professores.csv`) e só depois reaplica a tabela no banco:
    # `run_process.py` regenera o `.duckdb` inteiro a partir do CSV, então uma
    # edição que fosse só para o banco desapareceria, sem aviso, no próximo
    # reprocessamento.
    with aba_credenciamento:
        st.subheader("Anos de credenciamento por docente")

        st.markdown(
            "Marque os anos em que cada docente esteve credenciado — são esses anos que "
            "definem quando a produção dele conta, no regime \"Anos de credenciamento\" da "
            "barra lateral. O primeiro ano disponível de cada docente é o seu ano de "
            "ingresso no programa: antes disso ele não fazia parte do quadro."
        )
        st.caption(
            f"Fonte da verdade: `{cred.CAMINHO_CSV_PADRAO}`. Salvar grava esse arquivo e, "
            "em seguida, recarrega `tb_credenciamento_anos` no banco — nessa ordem, para "
            "que um reprocessamento futuro reproduza exatamente o que foi editado aqui."
        )

        ANO_GRADE_FIM = max(ANO_MAX, datetime.now().year)

        def _estado_credenciamento():
            """Situação atual, por docente: cadastro (id, nome, ingresso) e o
            conjunto de anos credenciados de cada um.

            Lê do CSV, que é a fonte da verdade. Se ele ainda não existir, cai
            para a tabela do banco -- assim uma vigência que veio de um
            reprocessamento anterior não é apagada por uma primeira edição feita
            antes de o CSV existir."""
            docentes = con.execute(
                "SELECT id_lattes, nome_completo, data_ingresso FROM tb_professores "
                "ORDER BY nome_completo"
            ).df()

            anos_por_docente = {}
            if os.path.exists(cred.CAMINHO_CSV_PADRAO):
                ids = set(docentes["id_lattes"].astype(str))
                df_csv, _, _ = cred.ler_csv_credenciamento(cred.CAMINHO_CSV_PADRAO, ids)
                for id_lattes, grupo in df_csv.groupby("id_lattes"):
                    anos_por_docente[id_lattes] = {int(a) for a in grupo["ano"]}
            elif tem_tabela(con, "tb_credenciamento_anos"):
                for id_lattes, ano in con.execute(
                    "SELECT id_lattes, ano FROM tb_credenciamento_anos"
                ).fetchall():
                    anos_por_docente.setdefault(str(id_lattes), set()).add(int(ano))

            return docentes, anos_por_docente

        def _salvar_credenciamento(anos_por_docente, docentes, aviso_extra=None):
            """Grava o CSV, reaplica a tabela e recarrega a página.

            Nunca retorna: termina sempre em `st.rerun()`, com o resultado em
            `st.session_state['_cred_resultado']`. É de propósito -- a gravação
            fecha a conexão de leitura do app (o DuckDB não abre escrita
            enquanto houver leitura sobre o mesmo arquivo), então seguir
            renderizando a página com `con` morto daria erro. Recarregar
            reabre a conexão do zero, tanto no caminho de sucesso quanto no de
            falha.
            """
            mensagens = []
            registros = [
                {
                    "id_lattes": linha.id_lattes,
                    "nome_referencia": linha.nome_completo,
                    "anos": sorted(anos_por_docente.get(linha.id_lattes, set())),
                }
                for linha in docentes.itertuples()
            ]

            try:
                cred.escrever_csv_credenciamento(cred.CAMINHO_CSV_PADRAO, registros)
            except OSError as erro:
                st.session_state["_cred_resultado"] = (
                    "erro", [f"Falha ao gravar `{cred.CAMINHO_CSV_PADRAO}`: {erro}",
                             "Nada foi alterado no banco."]
                )
                st.rerun()

            # Aplica numa cópia e troca no fim (`aplicar_em_duckdb_por_copia`).
            # Escrever direto não funciona: este mesmo processo tem o banco
            # aberto para leitura, e fechar a conexão em cache não basta --
            # ela pode ter sido recriada, e a anterior segue viva segurando o
            # lock. Pelo caminho da cópia o banco original só é tocado no
            # `os.replace` final, então uma falha no meio não o corrompe.
            try:
                cred.aplicar_em_duckdb_por_copia(
                    CAMINHO_BASE_INSTITUCIONAL, cred.CAMINHO_CSV_PADRAO
                )
            except Exception as erro:
                st.session_state["_cred_resultado"] = ("erro", [
                    f"O CSV foi gravado, mas `tb_credenciamento_anos` não pôde ser "
                    f"recarregada no banco: {erro}",
                    "O CSV e o banco ficaram fora de sincronia. Rode "
                    f"`python credenciamento.py --db {CAMINHO_BASE_INSTITUCIONAL}` "
                    "com o app parado para alinhá-los.",
                ])
                st.rerun()

            # Só depois de a troca dar certo: descarta a conexão em cache, que
            # ainda aponta para o arquivo substituído.
            try:
                con.close()
            except Exception:
                pass
            get_db_connection.clear()

            total = sum(len(v) for v in anos_por_docente.values())
            mensagens.append(
                f"Anos de credenciamento salvos: {total} par(es) (docente, ano) gravados em "
                f"`{cred.CAMINHO_CSV_PADRAO}` e aplicados à base."
            )
            if aviso_extra:
                mensagens.append(aviso_extra)
            st.session_state["_cred_resultado"] = ("ok", mensagens)
            # As caixas do detalhe por docente têm key própria; sem limpar, elas
            # voltariam com o valor anterior à gravação em vez do recém-salvo.
            for chave in [k for k in st.session_state if str(k).startswith("cred_ano_")]:
                del st.session_state[chave]
            st.rerun()

        docentes_cred, anos_atuais = _estado_credenciamento()

        if docentes_cred.empty:
            st.warning("Nenhum docente cadastrado na base — nada para editar.")
        else:
            job_rodando = jobs.existe_algum_job_rodando()
            if job_rodando:
                st.warning(
                    "Há um job em execução (extração ou reprocessamento). A edição fica "
                    "bloqueada até ele terminar: os dois escrevem no mesmo banco."
                )

            # ---------- Grade geral ----------
            st.markdown("#### Grade geral")
            col_de, col_ate = st.columns(2)
            grade_ini = int(col_de.number_input(
                "Primeiro ano da grade", min_value=1950, max_value=ANO_GRADE_FIM,
                value=max(2010, ANO_GRADE_FIM - 9), step=1, key="cred_grade_ini",
            ))
            grade_fim = int(col_ate.number_input(
                "Último ano da grade", min_value=1950, max_value=ANO_GRADE_FIM,
                value=ANO_GRADE_FIM, step=1, key="cred_grade_fim",
            ))
            if grade_ini > grade_fim:
                grade_ini, grade_fim = grade_fim, grade_ini
            anos_grade = list(range(grade_ini, grade_fim + 1))

            st.caption(
                f"A grade cobre {anos_grade[0]}–{anos_grade[-1]}. Anos fora dessa faixa não "
                "são apagados ao salvar — ficam como estão. Marcações em anos anteriores ao "
                "ingresso do docente são ignoradas, e a gravação avisa quais foram."
            )

            linhas_grade = []
            for linha in docentes_cred.itertuples():
                marcados = anos_atuais.get(linha.id_lattes, set())
                registro = {
                    "Docente": linha.nome_completo,
                    "Ingresso": None if pd.isna(linha.data_ingresso) else int(linha.data_ingresso),
                }
                for ano in anos_grade:
                    registro[str(ano)] = ano in marcados
                linhas_grade.append(registro)
            df_grade = pd.DataFrame(linhas_grade)

            grade_editada = st.data_editor(
                df_grade,
                key="cred_grade_editor",
                use_container_width=True,
                hide_index=True,
                disabled=["Docente", "Ingresso"],
                column_config={
                    "Docente": st.column_config.TextColumn("Docente", width="medium"),
                    "Ingresso": st.column_config.NumberColumn(
                        "Ingresso", format="%d", width="small",
                        help="Ano de entrada no programa (`lista_pessoas.csv`). "
                             "Anos anteriores a ele não podem ser credenciados.",
                    ),
                    **{
                        str(ano): st.column_config.CheckboxColumn(str(ano), width="small")
                        for ano in anos_grade
                    },
                },
            )

            if st.button("Salvar grade", type="primary", disabled=job_rodando,
                         key="cred_salvar_grade"):
                # Parte do estado atual e sobrescreve apenas os anos visíveis na
                # grade; o que está fora da faixa exibida continua valendo.
                novo = {k: set(v) for k, v in anos_atuais.items()}
                ignorados = []
                for pos, linha_orig in enumerate(docentes_cred.itertuples()):
                    id_lattes = linha_orig.id_lattes
                    ingresso = (None if pd.isna(linha_orig.data_ingresso)
                                else int(linha_orig.data_ingresso))
                    atual = novo.setdefault(id_lattes, set())
                    for ano in anos_grade:
                        marcado = bool(grade_editada.iloc[pos][str(ano)])
                        if marcado and ingresso is not None and ano < ingresso:
                            ignorados.append(f"{linha_orig.nome_completo} ({ano})")
                            atual.discard(ano)
                        elif marcado:
                            atual.add(ano)
                        else:
                            atual.discard(ano)

                aviso = None
                if ignorados:
                    aviso = ("Ignorados por serem anteriores ao ingresso do docente: "
                             + "; ".join(ignorados))
                _salvar_credenciamento(novo, docentes_cred, aviso)

            # ---------- Detalhe por docente ----------
            st.markdown("---")
            st.markdown("#### Detalhe por docente")
            st.caption(
                "Mesma informação da grade, um docente por vez e começando no ano de "
                "ingresso dele — sem risco de marcar a linha errada."
            )

            nomes = list(docentes_cred["nome_completo"])
            escolhido = st.selectbox("Docente", nomes, key="cred_docente_detalhe")
            linha_doc = docentes_cred[docentes_cred["nome_completo"] == escolhido].iloc[0]
            id_escolhido = linha_doc["id_lattes"]
            ingresso_doc = (
                None if pd.isna(linha_doc["data_ingresso"]) else int(linha_doc["data_ingresso"])
            )
            marcados_doc = anos_atuais.get(id_escolhido, set())

            if ingresso_doc is None:
                inicio_doc = min(anos_grade[0], *(marcados_doc or {anos_grade[0]}))
                st.caption(
                    "Sem ano de ingresso em `lista_pessoas.csv` para este docente; a lista "
                    f"abaixo começa em {inicio_doc}."
                )
            else:
                inicio_doc = min(ingresso_doc, *(marcados_doc or {ingresso_doc}))
                st.caption(
                    f"Ingresso no programa: **{ingresso_doc}** · "
                    f"**{len(marcados_doc)}** ano(s) credenciados hoje"
                    + (f" ({min(marcados_doc)}–{max(marcados_doc)})." if marcados_doc else ".")
                )
            anos_doc = list(range(min(inicio_doc, ANO_GRADE_FIM), ANO_GRADE_FIM + 1))

            with st.form(f"form_cred_{id_escolhido}"):
                marcas = {}
                colunas = st.columns(6)
                for i, ano in enumerate(anos_doc):
                    marcas[ano] = colunas[i % 6].checkbox(
                        str(ano),
                        value=(ano in marcados_doc),
                        key=f"cred_ano_{id_escolhido}_{ano}",
                    )
                if st.form_submit_button("Salvar este docente", type="primary",
                                         disabled=job_rodando):
                    novo = {k: set(v) for k, v in anos_atuais.items()}
                    # Anos anteriores ao início da lista não aparecem no
                    # formulário; preserva-os em vez de apagá-los.
                    preservados = {a for a in novo.get(id_escolhido, set()) if a < anos_doc[0]}
                    novo[id_escolhido] = preservados | {a for a, v in marcas.items() if v}
                    _salvar_credenciamento(novo, docentes_cred)

    # ===== ABA 3: BASES DE COMPARAÇÃO =====
    with aba_comparacao:
        st.subheader("Bases de comparação")
        st.caption(
            "Cada base é uma lista `.list` de pessoas de outra instituição/programa, extraída "
            "e processada separadamente. A escolha de qual usar como Base B é feita na própria "
            "página Comparativo."
        )

        # Mensagem deixada por uma renomeação/exclusão que terminou com st.rerun().
        nivel_msg, texto_msg = st.session_state.pop("_msg_bases_comparacao", (None, None))
        if nivel_msg == "success":
            st.success(texto_msg)
        elif nivel_msg:
            st.error(texto_msg)

        arquivo_lista = st.file_uploader(
            "Enviar lista (.list) de uma nova instituição/programa",
            type=["list", "txt"],
            help="Mesmo formato usado pelo scriptLattes: uma linha por pessoa, "
                 "'id_lattes,Nome Completo'. O banco gerado é nomeado a partir do "
                 "nome deste arquivo.",
            key="upload_lista_comparacao",
        )
        if arquivo_lista is not None:
            conteudo = arquivo_lista.getvalue().decode("utf-8", errors="replace")
            linhas_validas = [l for l in conteudo.splitlines() if l.strip()]
            if not linhas_validas or any("," not in l for l in linhas_validas):
                st.error(
                    "Arquivo inválido: cada linha não vazia precisa ter o formato "
                    "'id_lattes,Nome Completo' (mesmo formato do scriptLattes)."
                )
            else:
                nome_comparacao = jobs.slugify(os.path.splitext(arquivo_lista.name)[0])
                # O nome do arquivo vira o nome da base, então um envio pode
                # cair em cima de uma base que já existe. Nesse caso exigimos
                # confirmação: trocar só a lista deixaria o banco e os
                # snapshots já extraídos descrevendo as pessoas antigas.
                colide = nome_comparacao in jobs.listar_nomes_comparacao()
                substituir = False
                if colide:
                    st.warning(
                        f"Já existe a base **{nome_comparacao}**. Substituir a lista **não** "
                        "atualiza o banco nem os snapshots já extraídos — eles continuarão "
                        "descrevendo as pessoas da lista anterior até você reextrair."
                    )
                    substituir = st.checkbox(
                        f"Substituir a lista da base '{nome_comparacao}'",
                        value=False,
                        key="confirma_substituir_lista_comparacao",
                    )
                if colide and not substituir:
                    st.info("Envio não aplicado. Confirme acima, ou renomeie o arquivo "
                            "para criar uma base nova.")
                else:
                    try:
                        nome_comparacao, sobrescreveu = jobs.salvar_lista_comparacao(
                            arquivo_lista.name, conteudo, sobrescrever=substituir)
                    except ValueError as erro:
                        st.error(str(erro))
                    else:
                        if sobrescreveu:
                            st.success(
                                f"Lista da base '{nome_comparacao}' substituída "
                                f"({len(linhas_validas)} pessoa(s)). Reextraia para "
                                "atualizar o banco."
                            )
                        else:
                            st.success(
                                f"Lista salva como '{nome_comparacao}' "
                                f"({len(linhas_validas)} pessoa(s))."
                            )

        bases_comparacao = listar_bases_comparacao_info()

        if not bases_comparacao:
            st.info("Nenhuma base de comparação cadastrada ainda — envie uma lista acima.")
        else:
            st.dataframe(
                [
                    {k: v for k, v in linha.items() if not k.startswith("_")}
                    for linha in bases_comparacao
                ],
                hide_index=True,
                use_container_width=True,
            )

            nome_selecionado = st.selectbox(
                "Base de comparação para operar",
                [linha["nome"] for linha in bases_comparacao],
                key="select_base_comparacao_operar",
            )

            status_extract_comp = jobs.read_status(jobs.comparacao_extract_status(nome_selecionado))
            status_process_comp = jobs.read_status(jobs.comparacao_process_status(nome_selecionado))
            algum_job_comp_rodando = (
                status_extract_comp.get("state") == "running"
                or status_process_comp.get("state") == "running"
            )

            ignorar_cache_comp = st.checkbox(
                "Ignorar cache nesta base de comparação (rebaixar tudo)",
                value=False,
                disabled=algum_job_comp_rodando,
                key="ignorar_cache_comparacao",
            )

            col_extrair_comp, col_reprocessar_comp = st.columns(2)

            if col_extrair_comp.button(
                "Re-extrair base de comparação",
                disabled=jobs.existe_extracao_rodando() or algum_job_comp_rodando,
                help="Roda o scriptLattes contra a lista desta base de comparação.",
                use_container_width=True,
                key="btn_extrair_comparacao",
            ):
                jobs.write_status(
                    jobs.comparacao_extract_status(nome_selecionado),
                    state="running", started_at=jobs.now_iso(),
                )
                if ignorar_cache_comp:
                    jobs.launch("run_extract_comparacao.py", "--nome", nome_selecionado, "--limpar-cache")
                else:
                    jobs.launch("run_extract_comparacao.py", "--nome", nome_selecionado)
                st.rerun()

            if col_reprocessar_comp.button(
                "Reprocessar base de comparação",
                disabled=algum_job_comp_rodando,
                help="Roda analyse_organizado_comparação.ipynb sobre os JSONs já extraídos desta base.",
                use_container_width=True,
                key="btn_reprocessar_comparacao",
            ):
                jobs.write_status(
                    jobs.comparacao_process_status(nome_selecionado),
                    state="running", started_at=jobs.now_iso(),
                )
                jobs.launch("run_process_comparacao.py", "--nome", nome_selecionado)
                st.rerun()

            linha_status(f"Extração ({nome_selecionado})", status_extract_comp)
            linha_status(f"Reprocessamento ({nome_selecionado})", status_process_comp)

            # --- Renomear / excluir -------------------------------------------------
            # Ficam num expander fechado porque excluir é irreversível e apaga
            # também o .duckdb e os snapshots extraídos — não é operação para
            # estar a um clique de distância dos botões de rotina.
            base_travada = algum_job_comp_rodando or jobs.comparacao_em_uso(nome_selecionado)

            with st.expander(f"Renomear ou excluir a base '{nome_selecionado}'"):
                if base_travada:
                    st.warning(
                        "Há um job em andamento nesta base. Espere terminar — renomear ou "
                        "excluir no meio da execução deixaria arquivos órfãos."
                    )

                artefatos = jobs.artefatos_comparacao(nome_selecionado)
                presentes = {c: p for c, p in artefatos.items() if os.path.lexists(p)}

                st.markdown("**Renomear**")
                st.caption(
                    "Move a lista, os snapshots extraídos, o banco gerado e os status. "
                    "O nome é normalizado igual ao do upload (minúsculo, sem acento nem espaço)."
                )
                col_nome, col_btn = st.columns([3, 1])
                novo_nome_base = col_nome.text_input(
                    "Novo nome",
                    value="",
                    placeholder=nome_selecionado,
                    label_visibility="collapsed",
                    disabled=base_travada,
                    key="input_novo_nome_comparacao",
                )
                if col_btn.button(
                    "Renomear",
                    disabled=base_travada or not novo_nome_base.strip(),
                    use_container_width=True,
                    key="btn_renomear_comparacao",
                ):
                    try:
                        novo = jobs.renomear_comparacao(nome_selecionado, novo_nome_base)
                    except ValueError as erro:
                        st.error(str(erro))
                    else:
                        # A conexão da Base B é cacheada por (caminho, mtime): o
                        # caminho antigo deixou de existir, então o cache precisa cair.
                        abrir_base_comparacao_gerida.clear()
                        for chave in (
                            "select_base_comparacao_operar",
                            "select_base_b_gerida",
                            "input_novo_nome_comparacao",
                            # sem limpar o uploader, o rerun re-salvaria o .list
                            # e ressuscitaria a base com o nome antigo
                            "upload_lista_comparacao",
                        ):
                            st.session_state.pop(chave, None)
                        st.session_state["_msg_bases_comparacao"] = (
                            "success", f"Base '{nome_selecionado}' renomeada para '{novo}'."
                        )
                        st.rerun()

                st.divider()

                st.markdown("**Excluir**")
                st.caption(
                    "Apaga a lista, os snapshots extraídos, o banco gerado e os status "
                    "desta base. **Irreversível.**"
                )
                st.markdown(
                    "Será apagado:\n"
                    + "\n".join(f"- `{caminho}`" for caminho in sorted(presentes.values()))
                )
                confirmou_exclusao = st.checkbox(
                    f"Confirmo que quero excluir a base '{nome_selecionado}' e todos os arquivos acima",
                    value=False,
                    disabled=base_travada,
                    key="confirma_exclusao_comparacao",
                )
                if st.button(
                    "Excluir base de comparação",
                    disabled=base_travada or not confirmou_exclusao,
                    type="primary",
                    key="btn_excluir_comparacao",
                ):
                    try:
                        jobs.excluir_comparacao(nome_selecionado)
                    except ValueError as erro:
                        st.error(str(erro))
                    else:
                        abrir_base_comparacao_gerida.clear()
                        for chave in (
                            "select_base_comparacao_operar",
                            "select_base_b_gerida",
                            "confirma_exclusao_comparacao",
                            "upload_lista_comparacao",
                        ):
                            st.session_state.pop(chave, None)
                        st.session_state["_msg_bases_comparacao"] = (
                            "success", f"Base '{nome_selecionado}' excluída."
                        )
                        st.rerun()

    # Poll só nesta página: enquanto algum job roda, recarrega pra atualizar os
    # status. As demais páginas não precisam ficar recarregando sozinhas.
    if jobs.existe_algum_job_rodando():
        time.sleep(2.5)
        st.rerun()
