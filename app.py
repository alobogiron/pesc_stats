import streamlit as st
import duckdb
import pandas as pd

# ==========================================
# 1. CONFIGURAÇÃO DA INTERFACE INSTITUCIONAL
# ==========================================
st.set_page_config(page_title="Sistema de Avaliação de Produtividade Acadêmica", layout="wide")

# ==========================================
# 2. CONEXÃO COM O BANCO DE DADOS
# ==========================================
@st.cache_resource
def get_db_connection():
    try:
        return duckdb.connect(database='pesquisadores.duckdb', read_only=True)
    except Exception as e:
        st.error(f"Falha na conexão com a base de dados institucional: {e}")
        st.stop()

con = get_db_connection()

def get_year_bounds():
    """Obtém os limites de anos disponíveis na base de dados."""
    try:
        res_p = con.execute("SELECT MIN(ano_pub), MAX(ano_pub) FROM tb_artigo_periodico").fetchone()
        res_c = con.execute("SELECT MIN(ano), MAX(ano) FROM tb_artigo_conferencia").fetchone()
        a_min = min([res_p[0] or 2000, res_c[0] or 2000])
        a_max = max([res_p[1] or 2026, res_c[1] or 2026])
        return int(a_min), int(a_max)
    except:
        return 2000, 2026

ANO_MIN, ANO_MAX = get_year_bounds()

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
     "Panorama de Orientações Acadêmicas"]
)

st.sidebar.divider()
st.sidebar.info("Plataforma integrada com indexadores bibliográficos Lattes, Scopus e Google Scholar.")

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
        f_ano_inicio = st.number_input("Ano de Início", min_value=ANO_MIN, max_value=ANO_MAX, value=ANO_MIN)
    with col_f2:
        f_ano_fim = st.number_input("Ano de Fim", min_value=ANO_MIN, max_value=ANO_MAX, value=ANO_MAX)
    
    res_docentes = con.execute("SELECT COUNT(id_lattes) FROM tb_professores").fetchone()
    
    # Métricas filtradas por ano
    query_p = "SELECT COUNT(id_artigo_periodico) FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?"
    res_periodicos = con.execute(query_p, [f_ano_inicio, f_ano_fim]).fetchone()
    
    query_c = "SELECT COUNT(id_artigo_conferencia) FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?"
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
        f_ano_inicio = st.number_input("Ano de Início", min_value=ANO_MIN, max_value=ANO_MAX, value=ANO_MIN)
    with col_f2:
        f_ano_fim = st.number_input("Ano de Fim", min_value=ANO_MIN, max_value=ANO_MAX, value=ANO_MAX)

    # Tabela Mestra Unificada
    query_mestra = f"""
        SELECT 
            p.nome_completo AS Docente, 
            COUNT(DISTINCT a_p.id_artigo_periodico) AS Periodicos, 
            COUNT(DISTINCT a_c.id_artigo_conferencia) AS Conferencias,
            (COUNT(DISTINCT a_p.id_artigo_periodico) + COUNT(DISTINCT a_c.id_artigo_conferencia)) AS Total
        FROM tb_professores p
        LEFT JOIN tb_artigo_periodico a_p ON p.id_lattes = a_p.id_lattes AND a_p.ano_pub BETWEEN {f_ano_inicio} AND {f_ano_fim}
        LEFT JOIN tb_artigo_conferencia a_c ON p.id_lattes = a_c.id_lattes AND a_c.ano BETWEEN {f_ano_inicio} AND {f_ano_fim}
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
        query_p = "SELECT CAST(ano_pub AS VARCHAR) AS Ano, COUNT(*) AS Quantidade FROM tb_artigo_periodico WHERE ano_pub IS NOT NULL GROUP BY ano_pub ORDER BY ano_pub"
        df_p = con.execute(query_p).df()
        if not df_p.empty: st.bar_chart(data=df_p, x='Ano', y='Quantidade', use_container_width=True)
        
    with aba_c:
        st.subheader("Histórico de Publicações em Conferências")
        query_c = "SELECT CAST(ano AS VARCHAR) AS Ano, COUNT(*) AS Quantidade FROM tb_artigo_conferencia WHERE ano IS NOT NULL GROUP BY ano ORDER BY ano"
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
        query_p = """
            SELECT p.nome_completo AS Docente, CAST(a.ano_pub AS VARCHAR) AS Ano, a.titulo_artigo AS Titulo, a.titulo_revista_lattes AS Revista, a.maior_percentil AS Percentil
            FROM tb_artigo_periodico a INNER JOIN tb_professores p ON a.id_lattes = p.id_lattes ORDER BY a.ano_pub DESC
        """
        df_p = con.execute(query_p).df()
        st.dataframe(df_p, use_container_width=True, hide_index=True)
        
    with aba_c:
        query_c = """
            SELECT p.nome_completo AS Docente, CAST(a.ano AS VARCHAR) AS Ano, a.titulo_artigo AS Titulo, a.titulo_evento_lattes AS Evento, a.estrato AS Estrato, a.tipo_match AS "Tipo Match"
            FROM tb_artigo_conferencia a INNER JOIN tb_professores p ON a.id_lattes = p.id_lattes ORDER BY a.ano DESC
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
        ano_inicio = st.number_input("Ano de Início", min_value=ANO_MIN, max_value=ANO_MAX, value=ANO_MAX-4)
    with col_f2:
        ano_fim = st.number_input("Ano de Fim", min_value=ANO_MIN, max_value=ANO_MAX, value=ANO_MAX)
    
    st.markdown(f"**Período sob análise regulamentar: {ano_inicio} a {ano_fim}**")
    
    aba_p, aba_c = st.tabs(["Indicadores de Periódicos", "Indicadores de Conferências"])
    
    with aba_p:
        st.subheader("Índice de Produção Bibliográfica em Periódicos")
        query_ranking_p = """
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
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?
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
        query_ranking_c = """
            WITH cte_classificacao AS (
                SELECT id_lattes, estrato,
                    CASE 
                        WHEN estrato = 'A1' THEN 1.000 WHEN estrato = 'A2' THEN 0.875
                        WHEN estrato = 'A3' THEN 0.750 WHEN estrato = 'A4' THEN 0.625
                        WHEN estrato = 'A5' THEN 0.500 WHEN estrato = 'A6' THEN 0.375
                        WHEN estrato = 'A7' THEN 0.250 ELSE 0.125 
                    END AS pontos_artigo
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?
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
        ano_inicio = st.number_input("Ano de Início", min_value=ANO_MIN, max_value=ANO_MAX, value=max(ANO_MIN, ANO_MAX-4))
    with col_f2:
        ano_fim = st.number_input("Ano de Fim", min_value=ANO_MIN, max_value=ANO_MAX, value=ANO_MAX)
        
    st.markdown(f"**Período sob análise regulamentar estrita: {ano_inicio} a {ano_fim}**")
    
    aba_p, aba_c = st.tabs(["Indicadores Restritos de Periódicos", "Indicadores Restritos de Conferências"])
    
    with aba_p:
        st.subheader("Índice Restrito em Periódicos (A1-A4)")
        query_ranking_restrito_p = """
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
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?
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
        query_ranking_restrito_c = """
            WITH cte_classificacao AS (
                SELECT id_lattes, estrato,
                    CASE 
                        WHEN estrato = 'A1' THEN 1.000 WHEN estrato = 'A2' THEN 0.875
                        WHEN estrato = 'A3' THEN 0.750 WHEN estrato = 'A4' THEN 0.625
                        ELSE 0.000 
                    END AS pontos_artigo
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ? AND estrato IN ('A1', 'A2', 'A3', 'A4')
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
        ano_inicio = st.number_input("Ano de Início", min_value=ANO_MIN, max_value=ANO_MAX, value=ANO_MAX-4)
    with col_f2:
        ano_fim = st.number_input("Ano de Fim", min_value=ANO_MIN, max_value=ANO_MAX, value=ANO_MAX)
        
    st.markdown(f"**Janela regulamentar consolidada ativa: {ano_inicio} a {ano_fim}**")
    
    filtro_tipo_avaliacao = st.radio("Selecione o Critério de Apuração institucional:", ["Pontuação Integral (A1-A8)", "Pontuação Restrita (A1-A4)"], horizontal=True)
    
    if filtro_tipo_avaliacao == "Pontuação Integral (A1-A8)":
        query_consolidada = """
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
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ?
                GROUP BY id_lattes
            ),
            cte_conferencias AS (
                SELECT id_lattes,
                    COUNT(*) AS total_c,
                    SUM(CASE WHEN estrato = 'A1' THEN 1.000 WHEN estrato = 'A2' THEN 0.875 WHEN estrato = 'A3' THEN 0.750 WHEN estrato = 'A4' THEN 0.625 WHEN estrato = 'A5' THEN 0.500 WHEN estrato = 'A6' THEN 0.375 WHEN estrato = 'A7' THEN 0.250 ELSE 0.125 END * CASE WHEN coautoria_aluno = TRUE THEN 1.5 ELSE 1.0 END) AS pontos_c
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ?
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
        query_consolidada = """
            WITH cte_p_class AS (
                SELECT id_lattes, computation_area,
                    CASE 
                        WHEN maior_percentil >= 87.5 THEN 1.000 WHEN maior_percentil >= 75.0 THEN 0.875
                        WHEN maior_percentil >= 62.5 THEN 0.750 WHEN maior_percentil >= 50.0 THEN 0.625
                        ELSE 0.000 
                    END AS peso_base
                FROM tb_artigo_periodico WHERE ano_pub BETWEEN ? AND ? AND maior_percentil >= 50.0
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
                FROM tb_artigo_conferencia WHERE ano BETWEEN ? AND ? AND estrato IN ('A1', 'A2', 'A3', 'A4')
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
