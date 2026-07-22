import streamlit as st
import duckdb
import pandas as pd
import tempfile
import os
import re
import html as html_lib
from datetime import datetime

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
CAMINHO_BASE_INSTITUCIONAL = 'pesquisadores_teste.duckdb'

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
    """Obtém os limites de anos disponíveis em uma conexão DuckDB qualquer."""
    try:
        res_p = conexao.execute("SELECT MIN(ano_pub), MAX(ano_pub) FROM tb_artigo_periodico").fetchone()
        res_c = conexao.execute("SELECT MIN(ano), MAX(ano) FROM tb_artigo_conferencia").fetchone()
        a_min = min([res_p[0] or 2000, res_c[0] or 2000])
        a_max = max([res_p[1] or 2026, res_c[1] or 2026])
        return int(a_min), int(a_max)
    except:
        return 2000, 2026

ANO_MIN, ANO_MAX = get_year_bounds(con)

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

# ==========================================
# 3. MENU DE NAVEGAÇÃO INTERNO
# ==========================================
st.sidebar.title("Módulos do Sistema")
st.sidebar.markdown("Selecione o painel analítico:")

pagina_selecionada = st.sidebar.radio(
    "",
    ["Indicadores Institucionais", 
     "Análise por Docente", 
     "Série Histórica da Produção", 
     "Repositório Geral de Artigos",
     "Avaliação Quadrienal Geral (A1-A8)",
     "Avaliação Quadrienal Restrita (A1-A4)",
     "Relatório de Credenciamento Consolidado",
     "Panorama de Orientações Acadêmicas",
     "Geração de Relatórios",
     "Comparativo entre Bases"]
)

st.sidebar.divider()

# ==========================================
# 3.0 FONTE DOS PAPERS (FILTRO GLOBAL)
# ==========================================
# Alterna, em todas as visualizações que envolvem papers, entre a base
# unificada (todas as fontes já deduplicadas) e apenas as publicações que
# constam do Lattes. "Apenas Lattes" filtra as tabelas unificadas por
# `fontes LIKE '%LATTES%'` (a coluna `fontes` registra em quais bases cada
# publicação foi encontrada). Vive na sidebar, com key própria, então o
# valor escolhido persiste ao trocar de página.
st.sidebar.subheader("Fonte dos Papers")
fonte_papers_opcao = st.sidebar.radio(
    "Considerar publicações de:",
    ["Base unificada (todas as fontes)", "Apenas cadastradas no Lattes"],
    key="fonte_papers",
    help="Aplica-se a todos os gráficos/indicadores de periódicos e conferências, "
         "inclusive o modo Comparativo. 'Apenas Lattes' considera somente publicações "
         "cujo campo `fontes` contém LATTES."
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


# Filtro de período compartilhado entre as páginas padrão: inicializado uma
# única vez para que o intervalo escolhido persista ao alternar de dataview.
if "filtro_ano_inicio" not in st.session_state:
    st.session_state["filtro_ano_inicio"] = max(ANO_MIN, ANO_MAX - 4)
if "filtro_ano_fim" not in st.session_state:
    st.session_state["filtro_ano_fim"] = ANO_MAX

st.sidebar.info("Plataforma integrada com indexadores bibliográficos Lattes, Scopus e Google Scholar.")

# ==========================================
# 3.1 UPLOAD DA BASE DE COMPARAÇÃO (BASE B)
# ==========================================
# A base principal ("Base A") permanece fixa em pesquisadores.duckdb.
# A "Base B" é opcional e só é solicitada quando o módulo Comparativo é usado,
# mas o uploader vive na sidebar para ficar disponível e persistente
# independentemente de qual página o usuário está visualizando.
con_b = None
nome_base_b = None

if pagina_selecionada == "Comparativo entre Bases":
    st.sidebar.divider()
    st.sidebar.subheader("Base de Comparação")
    arquivo_base_b = st.sidebar.file_uploader(
        "Envie um segundo arquivo .duckdb (mesma arquitetura de tabelas)",
        type=["duckdb", "db"],
        help="O arquivo deve conter as mesmas tabelas da base institucional: "
             "tb_professores, tb_artigo_periodico, tb_artigo_conferencia e tb_orientacoes."
    )
    if arquivo_base_b is not None:
        try:
            con_b = carregar_base_comparacao(arquivo_base_b.getvalue(), arquivo_base_b.name)
            nome_base_b = arquivo_base_b.name
            st.sidebar.success(f"Base B carregada: {nome_base_b}")
        except Exception as e:
            st.sidebar.error(f"Falha ao abrir a base enviada: {e}")
            con_b = None

# ==========================================
# 4. DESENVOLVIMENTO DOS MÓDULOS (DATAVIEWS)
# ==========================================

# ------------------------------------------
# PÁGINA 1: INDICADORES INSTITUCIONAIS
# ------------------------------------------
if pagina_selecionada == "Indicadores Institucionais":
    st.title("Indicadores Institucionais (Métricas Globais)")
    st.markdown("Consolidação estatística descritiva da base total de dados da instituição.")
    
    st.subheader("Filtro de Período")
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        f_ano_inicio = st.number_input("Ano de Início", min_value=ANO_MIN, max_value=ANO_MAX, key="filtro_ano_inicio")
    with col_f2:
        f_ano_fim = st.number_input("Ano de Fim", min_value=ANO_MIN, max_value=ANO_MAX, key="filtro_ano_fim")
    
    res_docentes = con.execute("SELECT COUNT(id_lattes) FROM tb_professores").fetchone()
    
    # Métricas filtradas por ano
    query_p = f"SELECT COUNT(id_artigo_periodico) FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?{sql_fonte()}"
    res_periodicos = con.execute(query_p, [f_ano_inicio, f_ano_fim]).fetchone()
    
    query_c = f"SELECT COUNT(id_artigo_conferencia) FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?{sql_fonte()}"
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
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        f_ano_inicio = st.number_input("Ano de Início", min_value=ANO_MIN, max_value=ANO_MAX, key="filtro_ano_inicio")
    with col_f2:
        f_ano_fim = st.number_input("Ano de Fim", min_value=ANO_MIN, max_value=ANO_MAX, key="filtro_ano_fim")

    # Tabela Mestra Unificada
    query_mestra = f"""
        SELECT 
            p.nome_completo AS Docente, 
            COUNT(DISTINCT a_p.id_artigo_periodico) AS Periodicos, 
            COUNT(DISTINCT a_c.id_artigo_conferencia) AS Conferencias,
            (COUNT(DISTINCT a_p.id_artigo_periodico) + COUNT(DISTINCT a_c.id_artigo_conferencia)) AS Total
        FROM tb_professores p
        LEFT JOIN tb_artigo_periodico a_p ON p.id_lattes = a_p.id_lattes AND a_p.ano_pub BETWEEN {f_ano_inicio} AND {f_ano_fim}{sql_fonte('a_p.fontes')}
        LEFT JOIN tb_artigo_conferencia a_c ON p.id_lattes = a_c.id_lattes AND a_c.ano BETWEEN {f_ano_inicio} AND {f_ano_fim}{sql_fonte('a_c.fontes')}
        GROUP BY p.nome_completo 
        ORDER BY Total DESC
    """
    df_mestra = con.execute(query_mestra).df()
    
    st.subheader("Volume de Produção por Pesquisador")
    st.dataframe(df_mestra, use_container_width=True, hide_index=True)
    
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
        query_p = f"SELECT CAST(ano_pub AS VARCHAR) AS Ano, COUNT(*) AS Quantidade FROM tb_artigo_periodico WHERE ano_pub IS NOT NULL{sql_fonte()} GROUP BY ano_pub ORDER BY ano_pub"
        df_p = con.execute(query_p).df()
        if not df_p.empty: st.bar_chart(data=df_p, x='Ano', y='Quantidade', use_container_width=True)
        
    with aba_c:
        st.subheader("Histórico de Publicações em Conferências")
        query_c = f"SELECT CAST(ano AS VARCHAR) AS Ano, COUNT(*) AS Quantidade FROM tb_artigo_conferencia WHERE ano IS NOT NULL{sql_fonte()} GROUP BY ano ORDER BY ano"
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
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        ano_inicio = st.number_input("Ano de Início", min_value=ANO_MIN, max_value=ANO_MAX, key="filtro_ano_inicio")
    with col_f2:
        ano_fim = st.number_input("Ano de Fim", min_value=ANO_MIN, max_value=ANO_MAX, key="filtro_ano_fim")
    
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
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ? AND ano_pub >= COALESCE((SELECT data_ingresso FROM tb_professores pr WHERE pr.id_lattes = tb_artigo_periodico.id_lattes), ano_pub){sql_fonte()}
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
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ? AND ano >= COALESCE((SELECT data_ingresso FROM tb_professores pr WHERE pr.id_lattes = tb_artigo_conferencia.id_lattes), ano){sql_fonte()}
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
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        ano_inicio = st.number_input("Ano de Início", min_value=ANO_MIN, max_value=ANO_MAX, key="filtro_ano_inicio")
    with col_f2:
        ano_fim = st.number_input("Ano de Fim", min_value=ANO_MIN, max_value=ANO_MAX, key="filtro_ano_fim")
        
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
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ? AND ano_pub >= COALESCE((SELECT data_ingresso FROM tb_professores pr WHERE pr.id_lattes = tb_artigo_periodico.id_lattes), ano_pub){sql_fonte()}
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
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ? AND estrato IN ('A1', 'A2', 'A3', 'A4') AND ano >= COALESCE((SELECT data_ingresso FROM tb_professores pr WHERE pr.id_lattes = tb_artigo_conferencia.id_lattes), ano){sql_fonte()}
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
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        ano_inicio = st.number_input("Ano de Início", min_value=ANO_MIN, max_value=ANO_MAX, key="filtro_ano_inicio")
    with col_f2:
        ano_fim = st.number_input("Ano de Fim", min_value=ANO_MIN, max_value=ANO_MAX, key="filtro_ano_fim")
        
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
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ? AND ano_pub >= COALESCE((SELECT data_ingresso FROM tb_professores pr WHERE pr.id_lattes = tb_artigo_periodico.id_lattes), ano_pub){sql_fonte()}
                GROUP BY id_lattes
            ),
            cte_conferencias AS (
                SELECT id_lattes,
                    COUNT(*) AS total_c,
                    SUM(CASE WHEN estrato = 'A1' THEN 1.000 WHEN estrato = 'A2' THEN 0.875 WHEN estrato = 'A3' THEN 0.750 WHEN estrato = 'A4' THEN 0.625 WHEN estrato = 'A5' THEN 0.500 WHEN estrato = 'A6' THEN 0.375 WHEN estrato = 'A7' THEN 0.250 ELSE 0.125 END * CASE WHEN coautoria_aluno = TRUE THEN 1.5 ELSE 1.0 END) AS pontos_c
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ? AND ano >= COALESCE((SELECT data_ingresso FROM tb_professores pr WHERE pr.id_lattes = tb_artigo_conferencia.id_lattes), ano){sql_fonte()}
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
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ? AND maior_percentil >= 50.0 AND ano_pub >= COALESCE((SELECT data_ingresso FROM tb_professores pr WHERE pr.id_lattes = tb_artigo_periodico.id_lattes), ano_pub){sql_fonte()}
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
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ? AND estrato IN ('A1', 'A2', 'A3', 'A4') AND ano >= COALESCE((SELECT data_ingresso FROM tb_professores pr WHERE pr.id_lattes = tb_artigo_conferencia.id_lattes), ano){sql_fonte()}
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
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        ano_inicio_filtro = st.number_input("Ano de Início", min_value=ano_min_ori, max_value=ano_max_ori, value=max(ano_min_ori, ano_max_ori-4))
    with col_f2:
        ano_fim_filtro = st.number_input("Ano de Fim", min_value=ano_min_ori, max_value=ano_max_ori, value=ano_max_ori)
    
    st.markdown(f"**Analisando vínculos ativos em qualquer momento entre {ano_inicio_filtro} e {ano_fim_filtro}**")
    
    condicao_intersecao = "ano_inicio <= ? AND COALESCE(ano_conclusao, 2026) >= ?"
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
    ]
    relatorio_selecionado = st.selectbox("Selecione o relatório:", RELATORIOS_DISPONIVEIS)
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

        def _dados_faltantes(id_lattes):
            df_p = con.execute(
                """
                SELECT titulo_artigo, ano_pub, doi, fontes
                FROM tb_artigo_periodico
                WHERE id_lattes = ? AND fontes IS NOT NULL AND fontes NOT LIKE '%LATTES%'
                ORDER BY ano_pub DESC NULLS LAST, titulo_artigo
                """, [id_lattes]).df()
            df_c = con.execute(
                """
                SELECT titulo_artigo, titulo_evento_lattes, ano, doi, fontes
                FROM tb_artigo_conferencia
                WHERE id_lattes = ? AND fontes IS NOT NULL AND fontes NOT LIKE '%LATTES%'
                ORDER BY ano DESC NULLS LAST, titulo_artigo
                """, [id_lattes]).df()
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
    <tr><td><strong>Gerado em</strong></td><td>{gerado_em}</td></tr>
  </table>
</header>
{secao("Periódicos", df_p, col_p)}
{secao("Conferências", df_c, col_c)}
<footer>Sistema de Avaliação de Produtividade Acadêmica — para incluir estes itens no Lattes,
localize cada publicação na base indicada na coluna "Rastreado em".</footer>
<button class="noprint" onclick="window.print()">Imprimir / Salvar como PDF</button>
</body></html>"""

        # Contagem de pendências por docente (subconsultas correlacionadas)
        df_profs = con.execute(
            """
            SELECT p.id_lattes, p.nome_completo,
                (SELECT COUNT(*) FROM tb_artigo_periodico ap
                   WHERE ap.id_lattes = p.id_lattes AND ap.fontes IS NOT NULL
                     AND ap.fontes NOT LIKE '%LATTES%') AS falt_p,
                (SELECT COUNT(*) FROM tb_artigo_conferencia ac
                   WHERE ac.id_lattes = p.id_lattes AND ac.fontes IS NOT NULL
                     AND ac.fontes NOT LIKE '%LATTES%') AS falt_c
            FROM tb_professores p
            ORDER BY p.nome_completo
            """
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
            st.success("Nenhum docente possui papers faltantes no Lattes nesta base.")

# ------------------------------------------
# PÁGINA 9: COMPARATIVO ENTRE BASES
# ------------------------------------------
elif pagina_selecionada == "Comparativo entre Bases":
    st.title("Comparativo entre Bases")
    st.markdown(
        "Módulo de auditoria comparativa: contraste lado a lado entre a base institucional "
        "vigente (**Base A**) e uma segunda base `.duckdb` de mesma arquitetura (**Base B**), "
        "enviada pela barra lateral. As visualizações abaixo replicam os mesmos indicadores "
        "e gráficos já disponíveis nos demais módulos do sistema, espelhados para as duas bases."
    )

    if con_b is None:
        st.info(
            "Envie um arquivo **.duckdb** na barra lateral (seção 'Base de Comparação') para "
            "habilitar este módulo. O arquivo precisa conter as tabelas tb_professores, "
            "tb_artigo_periodico, tb_artigo_conferencia e tb_orientacoes."
        )
    else:
        # --------------------------------------------------
        # Funções auxiliares — parametrizadas por conexão para
        # reaproveitar exatamente a mesma lógica das demais páginas
        # em qualquer uma das duas bases (A ou B).
        # --------------------------------------------------
        def comp_indicadores_gerais(conexao, ano_ini, ano_fim_, frag=""):
            res_docentes = conexao.execute("SELECT COUNT(id_lattes) FROM tb_professores").fetchone()
            res_p = conexao.execute(
                f"SELECT COUNT(id_artigo_periodico) FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?{frag}",
                [ano_ini, ano_fim_]
            ).fetchone()
            res_c = conexao.execute(
                f"SELECT COUNT(id_artigo_conferencia) FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?{frag}",
                [ano_ini, ano_fim_]
            ).fetchone()
            total_docentes = res_docentes[0] if res_docentes else 0
            total_p = res_p[0] if res_p else 0
            total_c = res_c[0] if res_c else 0
            return total_docentes, total_p, total_c

        def comp_serie_historica_periodico(conexao, frag=""):
            q = f"SELECT CAST(ano_pub AS VARCHAR) AS Ano, COUNT(*) AS Quantidade FROM tb_artigo_periodico WHERE ano_pub IS NOT NULL{frag} GROUP BY ano_pub ORDER BY ano_pub"
            return conexao.execute(q).df()

        def comp_serie_historica_conferencia(conexao, frag=""):
            q = f"SELECT CAST(ano AS VARCHAR) AS Ano, COUNT(*) AS Quantidade FROM tb_artigo_conferencia WHERE ano IS NOT NULL{frag} GROUP BY ano ORDER BY ano"
            return conexao.execute(q).df()

        def comp_ranking_docente(conexao, ano_ini, ano_fim_, frag_p="", frag_c=""):
            q = f"""
                SELECT 
                    p.nome_completo AS Docente, 
                    COUNT(DISTINCT a_p.id_artigo_periodico) AS Periodicos, 
                    COUNT(DISTINCT a_c.id_artigo_conferencia) AS Conferencias,
                    (COUNT(DISTINCT a_p.id_artigo_periodico) + COUNT(DISTINCT a_c.id_artigo_conferencia)) AS Total
                FROM tb_professores p
                LEFT JOIN tb_artigo_periodico a_p ON p.id_lattes = a_p.id_lattes AND a_p.ano_pub BETWEEN {ano_ini} AND {ano_fim_}{frag_p}
                LEFT JOIN tb_artigo_conferencia a_c ON p.id_lattes = a_c.id_lattes AND a_c.ano BETWEEN {ano_ini} AND {ano_fim_}{frag_c}
                GROUP BY p.nome_completo 
                ORDER BY Total DESC
            """
            return conexao.execute(q).df()

        def comp_indice_quadrienal(conexao, ano_ini, ano_fim_, restrito, frag_p="", frag_c=""):
            """Replica a lógica das páginas de Avaliação Quadrienal (Geral ou Restrita A1-A4),
            retornando periódicos e conferências consolidados em um único score por docente."""
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
                        FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?{frag_p}
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
                        FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?{frag_c}
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
                        FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ? AND maior_percentil >= 50.0{frag_p}
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
                        FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ? AND estrato IN ('A1', 'A2', 'A3', 'A4'){frag_c}
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

        def comp_orientacoes(conexao, ano_ini, ano_fim_):
            condicao = "ano_inicio <= ? AND COALESCE(ano_conclusao, 2026) >= ?"
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

        def comp_orientacoes_por_nivel(conexao, ano_ini, ano_fim_):
            condicao = "ano_inicio <= ? AND COALESCE(ano_conclusao, 2026) >= ?"
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
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            comp_ano_inicio = st.number_input(
                "Ano de Início", min_value=comp_ano_min, max_value=comp_ano_max,
                value=max(comp_ano_min, comp_ano_max - 4), key="comp_ano_inicio"
            )
        with col_f2:
            comp_ano_fim = st.number_input(
                "Ano de Fim", min_value=comp_ano_min, max_value=comp_ano_max,
                value=comp_ano_max, key="comp_ano_fim"
            )

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
            doc_a, p_a, c_a = comp_indicadores_gerais(con, comp_ano_inicio, comp_ano_fim, frag=fA)
            doc_b, p_b, c_b = comp_indicadores_gerais(con_b, comp_ano_inicio, comp_ano_fim, frag=fB)

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
                df_serie_p_a = comp_serie_historica_periodico(con, frag=fA)
                df_serie_p_b = comp_serie_historica_periodico(con_b, frag=fB)
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
                df_serie_c_a = comp_serie_historica_conferencia(con, frag=fA)
                df_serie_c_b = comp_serie_historica_conferencia(con_b, frag=fB)
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
            df_rank_a = comp_ranking_docente(con, comp_ano_inicio, comp_ano_fim, frag_p=fA_p, frag_c=fA_c)
            df_rank_b = comp_ranking_docente(con_b, comp_ano_inicio, comp_ano_fim, frag_p=fB_p, frag_c=fB_c)

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

            df_quad_a = comp_indice_quadrienal(con, comp_ano_inicio, comp_ano_fim, restrito, frag_p=fA, frag_c=fA)
            df_quad_b = comp_indice_quadrienal(con_b, comp_ano_inicio, comp_ano_fim, restrito, frag_p=fB, frag_c=fB)

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

        # ===== ABA 5: ORIENTAÇÕES ACADÊMICAS (lado a lado) =====
        with aba_orientacoes:
            st.subheader("Panorama de Orientações — Base A vs. Base B")
            try:
                tot_a, conc_a, and_a = comp_orientacoes(con, comp_ano_inicio, comp_ano_fim)
                tot_b, conc_b, and_b = comp_orientacoes(con_b, comp_ano_inicio, comp_ano_fim)

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

                st.markdown("#### Distribuição por Nível Acadêmico — Comparativo")
                df_nivel_a = comp_orientacoes_por_nivel(con, comp_ano_inicio, comp_ano_fim)
                df_nivel_b = comp_orientacoes_por_nivel(con_b, comp_ano_inicio, comp_ano_fim)

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
