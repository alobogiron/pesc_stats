import sys; sys.path.insert(0, "/app")
from streamlit.testing.v1 import AppTest

PAGINAS = [
    "Indicadores Institucionais", "Análise por Docente", "Série Histórica da Produção",
    "Repositório Geral de Artigos", "Avaliação Quadrienal Geral (A1-A8)",
    "Avaliação Quadrienal Restrita (A1-A4)", "Relatório de Credenciamento Consolidado",
    "Panorama de Orientações Acadêmicas", "Geração de Relatórios",
    "Comparativo entre Bases", "Configurações",
]
for p in PAGINAS:
    at = AppTest.from_file("app.py", default_timeout=180)
    at.session_state["pagina_atual"] = p
    at.run()
    status = "ERRO" if at.exception else "ok"
    print(f"{status:5} | {p}")
    for e in at.exception:
        print("   ", e.value)
