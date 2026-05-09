import streamlit as st
import duckdb
import pandas as pd

# ==========================================
# 1. CONFIGURAÇÃO DA PÁGINA
# ==========================================
st.set_page_config(page_title="Dashboard Acadêmico", page_icon="🎓", layout="wide")

# ==========================================
# 2. CONEXÃO COM O BANCO DE DADOS
# ==========================================
@st.cache_resource
def get_db_connection():
    return duckdb.connect(database='pesquisadores.duckdb', read_only=True)

con = get_db_connection()

# ==========================================
# 3. MENU DE NAVEGAÇÃO (SIDEBAR)
# ==========================================
st.sidebar.title("Navegação")
st.sidebar.markdown("Escolha o Dataview desejado:")

# Criação das opções de páginas
pagina_selecionada = st.sidebar.radio(
    "",
    ["📊 Resumo Geral", 
     "👨‍🏫 Produção por Docente", 
     "📈 Produção Histórica (Ano)", 
     "🔍 Explorador de Artigos"]
)

st.sidebar.divider()
st.sidebar.info("Dashboard alimentado por dados do Currículo Lattes.")

# ==========================================
# 4. CONSTRUÇÃO DAS PÁGINAS (DATAVIEWS)
# ==========================================

# ------------------------------------------
# PÁGINA 1: RESUMO GERAL
# ------------------------------------------
if pagina_selecionada == "📊 Resumo Geral":
    st.title("📊 Resumo Geral")
    
    # Consultas rápidas para as métricas (KPIs)
    total_docentes = con.execute("SELECT COUNT(id_lattes) FROM tb_pessoas").fetchone()[0]
    total_artigos = con.execute("SELECT COUNT(*) FROM tb_artigos").fetchone()[0]
    
    col1, col2, col3 = st.columns(3)
    col1.metric("Total de Docentes Cadastrados", total_docentes)
    col2.metric("Total de Artigos Publicados", total_artigos)
    
    # Média de artigos por docente
    media = round(total_artigos / total_docentes, 1) if total_docentes > 0 else 0
    col3.metric("Média de Artigos por Docente", media)

# ------------------------------------------
# PÁGINA 2: PRODUÇÃO POR DOCENTE
# ------------------------------------------
elif pagina_selecionada == "👨‍🏫 Produção por Docente":
    st.title("👨‍🏫 Produção por Docente")
    st.markdown("Visão consolidada da quantidade de artigos por pesquisador.")
    
    query = """
        SELECT 
            p.nome_completo AS Docente,
            COUNT(a.id_lattes) AS Total_Artigos
        FROM tb_pessoas p
        LEFT JOIN tb_artigos a ON p.id_lattes = a.id_lattes
        GROUP BY p.nome_completo
        ORDER BY Total_Artigos DESC
    """
    df_prod_docente = con.execute(query).df()
    
    col1, col2 = st.columns([1, 2]) # A segunda coluna é mais larga que a primeira
    with col1:
        st.dataframe(df_prod_docente, use_container_width=True, hide_index=True)
    with col2:
        # Gráfico de barras horizontal para leitura dos nomes
        st.bar_chart(df_prod_docente.set_index('Docente'), use_container_width=True)

# ------------------------------------------
# PÁGINA 3: PRODUÇÃO HISTÓRICA
# ------------------------------------------
elif pagina_selecionada == "📈 Produção Histórica (Ano)":
    st.title("📈 Evolução da Produção por Ano")
    st.markdown("Volume total de artigos publicados ano a ano.")
    
    query = """
        SELECT 
            ano AS Ano,
            COUNT(*) AS Quantidade
        FROM tb_artigos
        WHERE ano IS NOT NULL
        GROUP BY ano
        ORDER BY ano
    """
    df_prod_ano = con.execute(query).df()
    df_prod_ano['Ano'] = df_prod_ano['Ano'].astype(str) # Evita "2,020" no gráfico
    
    st.bar_chart(data=df_prod_ano, x='Ano', y='Quantidade', use_container_width=True)

# ------------------------------------------
# PÁGINA 4: EXPLORADOR DE ARTIGOS COM FILTROS
# ------------------------------------------
elif pagina_selecionada == "🔍 Explorador de Artigos":
    st.title("🔍 Explorador Geral de Artigos")
    st.markdown("Filtre e busque por artigos específicos na base de dados.")
    
    # 1. Carrega todos os dados combinados
    query_artigos = """
        SELECT 
            p.nome_completo AS Docente,
            a.ano AS Ano,
            a.titulo AS Titulo,
            a.revista AS Revista,
            a.doi AS DOI
        FROM tb_artigos a
        INNER JOIN tb_pessoas p ON a.id_lattes = p.id_lattes
        ORDER BY a.ano DESC
    """
    df_geral = con.execute(query_artigos).df()
    df_geral['Ano'] = df_geral['Ano'].astype(str).replace('<NA>', 'Sem Ano')
    
    # 2. Criação da barra de filtros na interface
    st.subheader("Filtros")
    col_filtro1, col_filtro2 = st.columns(2)
    
    # Prepara os valores únicos para os filtros
    lista_docentes = ["Todos"] + df_geral['Docente'].unique().tolist()
    lista_anos = ["Todos"] + sorted(df_geral['Ano'].unique().tolist(), reverse=True)
    
    with col_filtro1:
        filtro_docente = st.selectbox("Filtrar por Docente:", lista_docentes)
    with col_filtro2:
        filtro_ano = st.selectbox("Filtrar por Ano:", lista_anos)
        
    # 3. Lógica de aplicação dos filtros
    df_filtrado = df_geral.copy()
    
    if filtro_docente != "Todos":
        df_filtrado = df_filtrado[df_filtrado['Docente'] == filtro_docente]
        
    if filtro_ano != "Todos":
        df_filtrado = df_filtrado[df_filtrado['Ano'] == filtro_ano]
        
    # 4. Exibição da tabela final filtrada
    st.markdown(f"**Mostrando {len(df_filtrado)} artigo(s)**")
    
    # O st.dataframe interativo do Streamlit permite ordenar as colunas clicando no cabeçalho
    st.dataframe(df_filtrado, use_container_width=True, hide_index=True)