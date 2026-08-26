import sys; sys.path.insert(0, "/app")
from streamlit.testing.v1 import AppTest

PAGINAS = [
    "Indicadores Institucionais", "Análise por Docente", "Série Histórica da Produção",
    "Repositório Geral de Artigos", "Avaliação Quadrienal Geral (A1-A8)",
    "Avaliação Quadrienal Restrita (A1-A4)", "Relatório de Credenciamento Consolidado",
    "Credenciamento por Vigência",
    "Panorama de Orientações Acadêmicas", "Geração de Relatórios",
    "Comparativo entre Bases", "Configurações",
]

# Cada página é exercitada nos dois regimes de contagem, já que o recorte por
# docente (`sql_recorte_docente`) atravessa quase todas as consultas. Num banco
# sem `tb_credenciamento_anos` a segunda passada simplesmente repete a primeira:
# o seletor não existe e o app cai no regime de ingresso.
REGIMES = ["Data de ingresso (atual)", "Anos de credenciamento"]

for regime in REGIMES:
    for p in PAGINAS:
        at = AppTest.from_file("app.py", default_timeout=180)
        at.session_state["pagina_atual"] = p
        at.session_state["regime_recorte"] = regime
        at.run()
        status = "ERRO" if at.exception else "ok"
        print(f"{status:5} | {regime:24} | {p}")
        for e in at.exception:
            print("   ", e.value)
