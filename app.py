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

@st.cache_resource
def get_db_connection():
    try:
        return duckdb.connect(database=CAMINHO_BASE_INSTITUCIONAL, read_only=True)
    except Exception as e:
        st.error(
            f"Falha na conexão com a base de dados institucional "
            f"('{CAMINHO_BASE_INSTITUCIONAL}'): {e}. "
            "Verifique se o arquivo existe — ele é gerado ao executar o notebook "
            "`analyse_organizado.ipynb`."
        )
        st.stop()

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

def renderizar_filtro_periodo(ano_min, ano_max, chave_pagina, titulo_extra=""):
    """Renderiza o par de campos 'Ano de Início'/'Ano de Fim'.

    A intenção do usuário é compartilhada globalmente entre todas as páginas
    via st.session_state['filtro_ano_inicio'/'filtro_ano_fim'] -- mudar o
    período em qualquer página propaga para as demais. Cada página, porém,
    usa uma chave de widget própria (`chave_pagina`) e recorta (clampa) o
    valor herdado para os seus próprios limites válidos (ano_min/ano_max),
    já que páginas diferentes podem ter intervalos de dados diferentes (ex.:
    a base de comparação enviada pelo usuário). Valida ao final que início
    <= fim, corrigindo automaticamente -- nunca deixa passar um intervalo
    invertido/negativo para as consultas.
    """
    def _clamp(valor, padrao):
        if valor is None:
            valor = padrao
        return min(max(int(valor), ano_min), ano_max)

    chave_inicio = f"{chave_pagina}_ano_inicio"
    chave_fim = f"{chave_pagina}_ano_fim"

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
            f"Ano de Início{titulo_extra}", min_value=ano_min, max_value=ano_max, key=chave_inicio
        )
    with col2:
        ano_fim = st.number_input(
            f"Ano de Fim{titulo_extra}", min_value=ano_min, max_value=ano_max, key=chave_fim
        )

    if ano_inicio > ano_fim:
        st.error(
            f"Ano de Início ({ano_inicio}) não pode ser maior que Ano de Fim ({ano_fim}); "
            "os valores foram trocados automaticamente."
        )
        ano_inicio, ano_fim = ano_fim, ano_inicio
        st.session_state[chave_inicio] = ano_inicio
        st.session_state[chave_fim] = ano_fim

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


def sql_ingresso(alias_tabela, coluna_ano, conector="AND", aplicar=True):
    """Fragmento SQL que restringe `coluna_ano` de `alias_tabela` (nome de
    tabela ou alias de JOIN) à produção/orientação posterior à data de
    ingresso do professor no programa (`tb_professores.data_ingresso`) —
    currículos Lattes trazem a vida acadêmica inteira, mas a apresentação
    institucional só deve considerar o período em que o docente já fazia
    parte do quadro. `COALESCE` faz o corte virar no-op quando
    `data_ingresso` é nulo (docente sem essa informação, ou Base B do
    Comparativo, que não tem `lista_pessoas.csv`). `aplicar=False` também
    devolve '' -- usado quando a base não tem a coluna `data_ingresso`
    (verificar antes com `tem_coluna`)."""
    if not aplicar:
        return ""
    return (
        f" {conector} {alias_tabela}.{coluna_ano} >= COALESCE("
        f"(SELECT data_ingresso FROM tb_professores pr_ing WHERE pr_ing.id_lattes = {alias_tabela}.id_lattes), "
        f"{alias_tabela}.{coluna_ano})"
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
    query_p = f"SELECT COUNT(id_artigo_periodico) FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?{sql_fonte()}{sql_ingresso('tb_artigo_periodico', 'ano_pub')}"
    res_periodicos = con.execute(query_p, [f_ano_inicio, f_ano_fim]).fetchone()

    query_c = f"SELECT COUNT(id_artigo_conferencia) FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?{sql_fonte()}{sql_ingresso('tb_artigo_conferencia', 'ano')}"
    res_conferencias = con.execute(query_c, [f_ano_inicio, f_ano_fim]).fetchone()
    
    total_docentes = res_docentes[0] if res_docentes else 0
    total_periodicos = res_periodicos[0] if res_periodicos else 0
    total_conferencias = res_conferencias[0] if res_conferencias else 0
    total_producoes = total_periodicos + total_conferencias
    
    col1, col2, col3 = st.columns(3)
    col1.metric("Docentes Cadastrados", total_docentes)
    col2.metric("Total de Produções Bibliográficas", total_producoes)
    
    media = round(total_producoes / total_docentes, 1) if total_docentes > 0 else 0
    col3.metric("Média de Produções / Docente", media)
    
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

    # Tabela Mestra Unificada
    query_mestra = f"""
        SELECT
            p.nome_completo AS Docente,
            p.data_ingresso AS "Ano de Ingresso",
            COUNT(DISTINCT a_p.id_artigo_periodico) AS Periodicos,
            COUNT(DISTINCT a_c.id_artigo_conferencia) AS Conferencias,
            (COUNT(DISTINCT a_p.id_artigo_periodico) + COUNT(DISTINCT a_c.id_artigo_conferencia)) AS Total
        FROM tb_professores p
        LEFT JOIN tb_artigo_periodico a_p ON p.id_lattes = a_p.id_lattes AND a_p.ano_pub BETWEEN {f_ano_inicio} AND {f_ano_fim}{sql_fonte('a_p.fontes')}{sql_ingresso('a_p', 'ano_pub')}
        LEFT JOIN tb_artigo_conferencia a_c ON p.id_lattes = a_c.id_lattes AND a_c.ano BETWEEN {f_ano_inicio} AND {f_ano_fim}{sql_fonte('a_c.fontes')}{sql_ingresso('a_c', 'ano')}
        GROUP BY p.nome_completo, p.data_ingresso
        ORDER BY Total DESC
    """
    df_mestra = con.execute(query_mestra).df()

    def _destacar_fora_do_periodo(linha):
        """Sinaliza (linha inteira) o pesquisador cujo ano de ingresso é
        posterior ao fim da janela selecionada -- ele ainda não fazia parte
        do programa durante todo o período em análise, então a produção
        exibida (sempre recortada por `sql_ingresso`) fica zerada aqui."""
        fora = pd.notna(linha["Ano de Ingresso"]) and linha["Ano de Ingresso"] > f_ano_fim
        cor = "background-color: rgba(255, 75, 75, 0.25)" if fora else ""
        return [cor] * len(linha)

    tabela_mestra_estilizada = (
        df_mestra.style
        .format({"Ano de Ingresso": lambda v: "—" if pd.isna(v) else str(int(v))})
        .apply(_destacar_fora_do_periodo, axis=1)
    )

    st.subheader("Volume de Produção por Pesquisador")
    st.dataframe(tabela_mestra_estilizada, use_container_width=True, hide_index=True)
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
        query_p = f"SELECT CAST(ano_pub AS VARCHAR) AS Ano, COUNT(*) AS Quantidade FROM tb_artigo_periodico WHERE ano_pub IS NOT NULL{sql_fonte()}{sql_ingresso('tb_artigo_periodico', 'ano_pub')} GROUP BY ano_pub ORDER BY ano_pub"
        df_p = con.execute(query_p).df()
        if not df_p.empty: st.bar_chart(data=df_p, x='Ano', y='Quantidade', use_container_width=True)

    with aba_c:
        st.subheader("Histórico de Publicações em Conferências")
        query_c = f"SELECT CAST(ano AS VARCHAR) AS Ano, COUNT(*) AS Quantidade FROM tb_artigo_conferencia WHERE ano IS NOT NULL{sql_fonte()}{sql_ingresso('tb_artigo_conferencia', 'ano')} GROUP BY ano ORDER BY ano"
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
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?{sql_fonte()}{sql_ingresso('tb_artigo_periodico', 'ano_pub')}
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
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?{sql_fonte()}{sql_ingresso('tb_artigo_conferencia', 'ano')}
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
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?{sql_fonte()}{sql_ingresso('tb_artigo_periodico', 'ano_pub')}
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
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ? AND estrato IN ('A1', 'A2', 'A3', 'A4'){sql_fonte()}{sql_ingresso('tb_artigo_conferencia', 'ano')}
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
    
    if filtro_tipo_avaliacao == "Pontuação Integral (A1-A8)":
        query_consolidada = f"""
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
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?{sql_fonte()}{sql_ingresso('tb_artigo_periodico', 'ano_pub')}
                GROUP BY id_lattes
            ),
            cte_conferencias AS (
                SELECT id_lattes,
                    COUNT(*) AS total_c,
                    SUM(CASE WHEN estrato = 'A1' THEN 1.000 WHEN estrato = 'A2' THEN 0.875 WHEN estrato = 'A3' THEN 0.750 WHEN estrato = 'A4' THEN 0.625 WHEN estrato = 'A5' THEN 0.500 WHEN estrato = 'A6' THEN 0.375 WHEN estrato = 'A7' THEN 0.250 ELSE 0.125 END * CASE WHEN coautoria_aluno = TRUE THEN 1.5 ELSE 1.0 END) AS pontos_c
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?{sql_fonte()}{sql_ingresso('tb_artigo_conferencia', 'ano')}
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
        query_consolidada = f"""
            WITH cte_p_class AS (
                SELECT id_lattes, computation_area, coautoria_aluno, -- << CORRIGIDO AQUI (Inclusão da coluna na CTE)
                    CASE 
                        WHEN maior_percentil >= 87.5 THEN 1.000 WHEN maior_percentil >= 75.0 THEN 0.875
                        WHEN maior_percentil >= 62.5 THEN 0.750 WHEN maior_percentil >= 50.0 THEN 0.625
                        ELSE 0.000 
                    END AS peso_base
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ? AND maior_percentil >= 50.0{sql_fonte()}{sql_ingresso('tb_artigo_periodico', 'ano_pub')}
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
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ? AND estrato IN ('A1', 'A2', 'A3', 'A4'){sql_fonte()}{sql_ingresso('tb_artigo_conferencia', 'ano')}
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
        f"{sql_ingresso('tb_orientacoes', 'ano_inicio')}"
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
        "DOIs inconsistentes no currículo Lattes",
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

    elif relatorio_selecionado == "DOIs inconsistentes no currículo Lattes":
        st.markdown(
            "Lista, para cada docente, as publicações cujo **campo DOI do Lattes não pôde ser "
            "usado** — para que ele corrija a entrada no próprio currículo. O campo é texto "
            "livre, e o pipeline recusa o valor em dois casos (`analyse_organizado.ipynb`, "
            "Seção 8.2.1):\n"
            "- **não tem forma de DOI** — o campo guarda outra coisa, tipicamente o link do PDF "
            "ou a URL *citado por* da Scopus;\n"
            "- **o mesmo DOI aparece em mais de uma publicação** do docente na mesma fonte — está "
            "errado em pelo menos uma delas, e mantê-lo faria duas publicações distintas "
            "colapsarem numa só.\n\n"
            "Nos dois casos a publicação **não é descartada**: ela passa a ser identificada pelo "
            "título. O que a correção no Lattes recupera é a precisão do casamento com ORCID e "
            "Scopus. A coluna *DOI registrado no Lattes* traz o valor exatamente como está no "
            "currículo — é por ele que a entrada é localizada."
        )

        # Bases geradas antes desta seção existir não têm a tabela; o relatório
        # avisa em vez de estourar (mesmo tratamento dado a tb_situacao_orientandos).
        tabela_descartes_ok = True
        try:
            con.execute("SELECT 1 FROM tb_dois_descartados LIMIT 1")
        except Exception:
            tabela_descartes_ok = False

        if not tabela_descartes_ok:
            st.info(
                "Esta base não possui a tabela `tb_dois_descartados` — ela passou a ser gerada "
                "por `analyse_organizado.ipynb` (Seção 8.2.1). Rode o reprocessamento da base "
                "para que o relatório fique disponível."
            )
        else:
            # Registros sem ano entram sempre: são justamente inconsistências de
            # preenchimento, e escondê-las por causa do recorte de período seria
            # contraproducente num relatório de qualidade de dado.
            pred_descarte = _pred_ano("d.ano", True)

            def _dados_descartes(id_lattes):
                return con.execute(
                    f"""
                    SELECT d.tipo, d.titulo_artigo, d.ano, d.doi_descartado, d.motivo
                    FROM tb_dois_descartados d
                    WHERE d.id_lattes = ? AND {pred_descarte}
                    ORDER BY d.tipo, d.ano DESC NULLS LAST, d.titulo_artigo
                    """, [id_lattes, rel_ano_inicio, rel_ano_fim]).df()

            def _html_descartes(nome, id_lattes, df_d):
                gerado_em = datetime.now().strftime("%d/%m/%Y %H:%M")
                colunas = [
                    ("Tipo", "tipo", _fmt_txt), ("Título", "titulo_artigo", _fmt_txt),
                    ("Ano", "ano", _fmt_ano),
                    ("DOI registrado no Lattes", "doi_descartado", _fmt_txt),
                    ("Por que não foi usado", "motivo", _fmt_txt),
                ]
                corpo = (
                    "<p class='vazio'>Nenhuma inconsistência encontrada.</p>"
                    if df_d.empty else _tabela_html(df_d, colunas)
                )
                return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>DOIs inconsistentes no Lattes — {html_lib.escape(str(nome))}</title>
<style>{_CSS_RELATORIO}</style></head>
<body>
<header>
  <h1>DOIs inconsistentes no currículo Lattes</h1>
  <p class="sub">Publicações cujo campo DOI não pôde ser usado para identificar a publicação.</p>
  <table class="meta">
    <tr><td><strong>Docente</strong></td><td>{html_lib.escape(str(nome))}</td></tr>
    <tr><td><strong>ID Lattes</strong></td><td>{html_lib.escape(str(id_lattes))}</td></tr>
    <tr><td><strong>Período</strong></td><td>{rel_ano_inicio} a {rel_ano_fim} (inclui itens sem ano informado)</td></tr>
    <tr><td><strong>Gerado em</strong></td><td>{gerado_em}</td></tr>
  </table>
</header>
<h2>Inconsistências <span class='cont'>({len(df_d)})</span></h2>{corpo}
<footer>Sistema de Avaliação de Produtividade Acadêmica — localize cada publicação no currículo
Lattes pelo valor da coluna "DOI registrado no Lattes" e substitua-o pelo DOI correto (ou apague-o,
se a publicação não tiver DOI).</footer>
<button class="noprint" onclick="window.print()">Imprimir / Salvar como PDF</button>
</body></html>"""

            df_profs_doi = con.execute(
                f"""
                SELECT p.id_lattes, p.nome_completo,
                    (SELECT COUNT(*) FROM tb_dois_descartados d
                       WHERE d.id_lattes = p.id_lattes AND {pred_descarte}) AS descartes
                FROM tb_professores p
                ORDER BY p.nome_completo
                """, [rel_ano_inicio, rel_ano_fim]
            ).df()

            total_descartes = int(df_profs_doi["descartes"].sum())
            if total_descartes == 0:
                st.success(
                    "Nenhum DOI inconsistente nesta base: todo DOI preenchido identifica "
                    "uma única publicação."
                )
            else:
                st.caption(
                    f"{total_descartes} inconsistência(s) em "
                    f"{int((df_profs_doi['descartes'] > 0).sum())} docente(s)."
                )
                mostrar_todos_doi = st.checkbox(
                    "Mostrar também docentes sem inconsistências", value=False, key="todos_doi")

                st.markdown("#### Docentes")
                h1, h2, h3 = st.columns([5, 1, 2])
                h1.markdown("**Docente**")
                h2.markdown("**Inconsistências**")
                h3.markdown("**Relatório**")

                for _, prof in df_profs_doi.iterrows():
                    total = int(prof["descartes"])
                    if total == 0 and not mostrar_todos_doi:
                        continue
                    c1, c2, c3 = st.columns([5, 1, 2])
                    c1.write(prof["nome_completo"])
                    c2.write(total)
                    if total == 0:
                        c3.caption("Sem inconsistências")
                    else:
                        df_d = _dados_descartes(prof["id_lattes"])
                        doc_html = _html_descartes(prof["nome_completo"], prof["id_lattes"], df_d)
                        slug = re.sub(r"[^A-Za-z0-9]+", "_", str(prof["nome_completo"])).strip("_")
                        c3.download_button(
                            "Baixar HTML",
                            data=doc_html.encode("utf-8"),
                            file_name=f"dois_inconsistentes_{slug}.html",
                            mime="text/html",
                            key=f"dl_doi_{prof['id_lattes']}",
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
        def comp_indicadores_gerais(conexao, ano_ini, ano_fim_, frag="", aplicar_ingresso=True):
            res_docentes = conexao.execute("SELECT COUNT(id_lattes) FROM tb_professores").fetchone()
            res_p = conexao.execute(
                f"SELECT COUNT(id_artigo_periodico) FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?{frag}"
                f"{sql_ingresso('tb_artigo_periodico', 'ano_pub', aplicar=aplicar_ingresso)}",
                [ano_ini, ano_fim_]
            ).fetchone()
            res_c = conexao.execute(
                f"SELECT COUNT(id_artigo_conferencia) FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?{frag}"
                f"{sql_ingresso('tb_artigo_conferencia', 'ano', aplicar=aplicar_ingresso)}",
                [ano_ini, ano_fim_]
            ).fetchone()
            total_docentes = res_docentes[0] if res_docentes else 0
            total_p = res_p[0] if res_p else 0
            total_c = res_c[0] if res_c else 0
            return total_docentes, total_p, total_c

        def comp_serie_historica_periodico(conexao, frag="", aplicar_ingresso=True):
            q = (
                f"SELECT CAST(ano_pub AS VARCHAR) AS Ano, COUNT(*) AS Quantidade FROM tb_artigo_periodico "
                f"WHERE ano_pub IS NOT NULL{frag}{sql_ingresso('tb_artigo_periodico', 'ano_pub', aplicar=aplicar_ingresso)} "
                f"GROUP BY ano_pub ORDER BY ano_pub"
            )
            return conexao.execute(q).df()

        def comp_serie_historica_conferencia(conexao, frag="", aplicar_ingresso=True):
            q = (
                f"SELECT CAST(ano AS VARCHAR) AS Ano, COUNT(*) AS Quantidade FROM tb_artigo_conferencia "
                f"WHERE ano IS NOT NULL{frag}{sql_ingresso('tb_artigo_conferencia', 'ano', aplicar=aplicar_ingresso)} "
                f"GROUP BY ano ORDER BY ano"
            )
            return conexao.execute(q).df()

        def comp_ranking_docente(conexao, ano_ini, ano_fim_, frag_p="", frag_c="", aplicar_ingresso=True):
            q = f"""
                SELECT
                    p.nome_completo AS Docente,
                    COUNT(DISTINCT a_p.id_artigo_periodico) AS Periodicos,
                    COUNT(DISTINCT a_c.id_artigo_conferencia) AS Conferencias,
                    (COUNT(DISTINCT a_p.id_artigo_periodico) + COUNT(DISTINCT a_c.id_artigo_conferencia)) AS Total
                FROM tb_professores p
                LEFT JOIN tb_artigo_periodico a_p ON p.id_lattes = a_p.id_lattes AND a_p.ano_pub BETWEEN {ano_ini} AND {ano_fim_}{frag_p}{sql_ingresso('a_p', 'ano_pub', aplicar=aplicar_ingresso)}
                LEFT JOIN tb_artigo_conferencia a_c ON p.id_lattes = a_c.id_lattes AND a_c.ano BETWEEN {ano_ini} AND {ano_fim_}{frag_c}{sql_ingresso('a_c', 'ano', aplicar=aplicar_ingresso)}
                GROUP BY p.nome_completo
                ORDER BY Total DESC
            """
            return conexao.execute(q).df()

        def comp_indice_quadrienal(conexao, ano_ini, ano_fim_, restrito, frag_p="", frag_c="", aplicar_ingresso=True):
            """Replica a lógica das páginas de Avaliação Quadrienal (Geral ou Restrita A1-A4),
            retornando periódicos e conferências consolidados em um único score por docente."""
            ing_p = sql_ingresso('tb_artigo_periodico', 'ano_pub', aplicar=aplicar_ingresso)
            ing_c = sql_ingresso('tb_artigo_conferencia', 'ano', aplicar=aplicar_ingresso)
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

        def comp_orientacoes(conexao, ano_ini, ano_fim_, aplicar_ingresso=True):
            condicao = "ano_inicio <= ? AND COALESCE(ano_conclusao, 2026) >= ?" + sql_ingresso(
                'tb_orientacoes', 'ano_inicio', aplicar=aplicar_ingresso
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

        def comp_orientacoes_por_nivel(conexao, ano_ini, ano_fim_, aplicar_ingresso=True):
            condicao = "ano_inicio <= ? AND COALESCE(ano_conclusao, 2026) >= ?" + sql_ingresso(
                'tb_orientacoes', 'ano_inicio', aplicar=aplicar_ingresso
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

        # Corte por data de ingresso aplicado às duas bases (produção e
        # orientações só contam a partir do ano em que o docente ingressou no
        # programa). A Base B enviada pode não ter a coluna `data_ingresso`
        # (arquitetura antiga, ou base gerada só a partir do Lattes de outra
        # instituição, sem `lista_pessoas.csv`); nesse caso o corte é
        # aplicado somente à Base A.
        ingresso_b_ok = tem_coluna(con_b, 'tb_professores', 'data_ingresso')
        if not ingresso_b_ok:
            st.warning("A Base B enviada não possui a coluna `data_ingresso`; o corte por data de ingresso será aplicado somente à Base A.")

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
            doc_a, p_a, c_a = comp_indicadores_gerais(con, comp_ano_inicio, comp_ano_fim, frag=fA, aplicar_ingresso=True)
            doc_b, p_b, c_b = comp_indicadores_gerais(con_b, comp_ano_inicio, comp_ano_fim, frag=fB, aplicar_ingresso=ingresso_b_ok)

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
                df_serie_p_a = comp_serie_historica_periodico(con, frag=fA, aplicar_ingresso=True)
                df_serie_p_b = comp_serie_historica_periodico(con_b, frag=fB, aplicar_ingresso=ingresso_b_ok)
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
                df_serie_c_a = comp_serie_historica_conferencia(con, frag=fA, aplicar_ingresso=True)
                df_serie_c_b = comp_serie_historica_conferencia(con_b, frag=fB, aplicar_ingresso=ingresso_b_ok)
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
            df_rank_a = comp_ranking_docente(con, comp_ano_inicio, comp_ano_fim, frag_p=fA_p, frag_c=fA_c, aplicar_ingresso=True)
            df_rank_b = comp_ranking_docente(con_b, comp_ano_inicio, comp_ano_fim, frag_p=fB_p, frag_c=fB_c, aplicar_ingresso=ingresso_b_ok)

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

            df_quad_a = comp_indice_quadrienal(con, comp_ano_inicio, comp_ano_fim, restrito, frag_p=fA, frag_c=fA, aplicar_ingresso=True)
            df_quad_b = comp_indice_quadrienal(con_b, comp_ano_inicio, comp_ano_fim, restrito, frag_p=fB, frag_c=fB, aplicar_ingresso=ingresso_b_ok)

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
            df_quad_geral_a = comp_indice_quadrienal(con, comp_ano_inicio, comp_ano_fim, False, frag_p=fA, frag_c=fA, aplicar_ingresso=True)
            df_quad_geral_b = comp_indice_quadrienal(con_b, comp_ano_inicio, comp_ano_fim, False, frag_p=fB, frag_c=fB, aplicar_ingresso=ingresso_b_ok)
            df_quad_restrito_a = comp_indice_quadrienal(con, comp_ano_inicio, comp_ano_fim, True, frag_p=fA, frag_c=fA, aplicar_ingresso=True)
            df_quad_restrito_b = comp_indice_quadrienal(con_b, comp_ano_inicio, comp_ano_fim, True, frag_p=fB, frag_c=fB, aplicar_ingresso=ingresso_b_ok)

            total_doc_a = len(df_quad_geral_a) or 1
            total_doc_b = len(df_quad_geral_b) or 1

            percapita_livre_a = df_quad_geral_a["Score Total"].sum() / total_doc_a
            percapita_livre_b = df_quad_geral_b["Score Total"].sum() / total_doc_b
            percapita_restrito_a = df_quad_restrito_a["Score Total"].sum() / total_doc_a
            percapita_restrito_b = df_quad_restrito_b["Score Total"].sum() / total_doc_b

            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown("##### Base A")
                st.metric("Per Capita — Livre (A1-A8)", f"{percapita_livre_a:.3f}")
                st.metric("Per Capita — Restrito (A1-A4)", f"{percapita_restrito_a:.3f}")
            with col_b:
                st.markdown(f"##### Base B ({nome_base_b})")
                st.metric(
                    "Per Capita — Livre (A1-A8)", f"{percapita_livre_b:.3f}",
                    delta=round(percapita_livre_b - percapita_livre_a, 3)
                )
                st.metric(
                    "Per Capita — Restrito (A1-A4)", f"{percapita_restrito_b:.3f}",
                    delta=round(percapita_restrito_b - percapita_restrito_a, 3)
                )

        # ===== ABA 5: ORIENTAÇÕES ACADÊMICAS (lado a lado) =====
        with aba_orientacoes:
            st.subheader("Panorama de Orientações — Base A vs. Base B")
            try:
                tot_a, conc_a, and_a = comp_orientacoes(con, comp_ano_inicio, comp_ano_fim, aplicar_ingresso=True)
                tot_b, conc_b, and_b = comp_orientacoes(con_b, comp_ano_inicio, comp_ano_fim, aplicar_ingresso=ingresso_b_ok)

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
                percapita_ori_a = tot_a / total_doc_ori_a
                percapita_ori_b = tot_b / total_doc_ori_b

                col_a, col_b = st.columns(2)
                with col_a:
                    st.markdown("##### Base A")
                    st.metric("Orientações Per Capita", f"{percapita_ori_a:.2f}")
                with col_b:
                    st.markdown(f"##### Base B ({nome_base_b})")
                    st.metric(
                        "Orientações Per Capita", f"{percapita_ori_b:.2f}",
                        delta=round(percapita_ori_b - percapita_ori_a, 2)
                    )

                st.markdown("#### Distribuição por Nível Acadêmico — Comparativo")
                df_nivel_a = comp_orientacoes_por_nivel(con, comp_ano_inicio, comp_ano_fim, aplicar_ingresso=True)
                df_nivel_b = comp_orientacoes_por_nivel(con_b, comp_ano_inicio, comp_ano_fim, aplicar_ingresso=ingresso_b_ok)

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

    aba_base, aba_comparacao = st.tabs(["Base institucional", "Bases de comparação"])

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

    # ===== ABA 2: BASES DE COMPARAÇÃO =====
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
