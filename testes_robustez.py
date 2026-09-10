"""Suíte de robustez — entradas hostis, estados degenerados e casos de borda.

Não é teste de "caminho feliz": cada caso aqui existe para tentar **quebrar** o
sistema. Rode com:

    python testes_robustez.py            # tudo
    python testes_robustez.py A B        # só as suítes escolhidas

Suítes:
  A  dedup_publicacoes  — entradas patológicas nas funções puras
  B  jobs               — renomear/excluir/upload sob estados de arquivo hostis
  C  app                — 11 páginas sob bases vazias, corrompidas ou de outra
                          arquitetura
  D  invariantes        — propriedades que o resultado real do pipeline tem de
                          satisfazer (unicidade, integridade referencial, faixas)
  E  UI                 — fluxo real de renomear/excluir, dirigido por AppTest

B roda inteira num sandbox temporário. C cria bancos derivados em /tmp. E é a
única que escreve no projeto: usa uma base descartável com prefixo `zz_` e, no
fim, confere que as bases pré-existentes seguem intactas — sem nome fixo no
código, para não quebrar quando alguém renomear uma base.

O banco de referência de C e D vem de PESC_DUCKDB_REFERENCIA (padrão:
`pesquisadores_teste.duckdb`).
"""

import os
import shutil
import signal
import sys
import tempfile
import time
import traceback

RAIZ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, RAIZ)

RESULTADOS = []


def caso(suite, nome, severidade="média"):
    """Decorator que registra o resultado de um caso de teste.
    `severidade` é o impacto se o caso falhar, não a chance de falhar."""
    def wrap(fn):
        try:
            detalhe = fn()
            RESULTADOS.append((suite, nome, "PASSOU", detalhe or "", severidade))
        except AssertionError as e:
            RESULTADOS.append((suite, nome, "FALHOU", str(e), severidade))
        except Exception:
            RESULTADOS.append((suite, nome, "ERRO", traceback.format_exc(limit=3), severidade))
        return fn
    return wrap


class Prazo:
    """Aborta um trecho que demore demais — usado para caçar explosão
    combinatória (ReDoS, laço quadrático) em vez de esperar para sempre."""
    def __init__(self, segundos):
        self.segundos = segundos

    def __enter__(self):
        def estourou(signum, frame):
            raise TimeoutError(f"excedeu {self.segundos}s")
        signal.signal(signal.SIGALRM, estourou)
        signal.alarm(self.segundos)

    def __exit__(self, *a):
        signal.alarm(0)
        return False


# =============================================================================
# SUÍTE A — dedup_publicacoes: entradas patológicas
# =============================================================================
def suite_a():
    import pandas as pd
    import dedup_publicacoes as dp

    @caso("A", "normalizar_doi não estoura com tipos inesperados", "alta")
    def _():
        entradas = [None, float("nan"), 123, 10.5, b"10.1007/x", [], {}, True,
                    "", "   ", "\n\t", "doi:", "https://doi.org/", "10.", "10.1007/"]
        saidas = []
        for e in entradas:
            saidas.append(dp.normalizar_doi(e))  # não pode levantar
        assert all(pd.isna(s) or isinstance(s, str) for s in saidas), saidas
        return f"{len(entradas)} tipos aceitos sem exceção"

    @caso("A", "normalizar_doi resiste a string gigante (ReDoS/backtracking)", "alta")
    def _():
        # prefixo de resolvedor repetido 20 mil vezes: exercita o laço `while`
        # e o quantificador (https?://)* ao mesmo tempo
        hostil = "http://dx.doi.org/" * 20000 + "10.1007/x"
        inicio = time.time()
        with Prazo(10):
            r = dp.normalizar_doi(hostil)
        dur = time.time() - inicio
        assert r == "10.1007/x", f"resultado inesperado: {r!r}"
        assert dur < 5, f"lento demais: {dur:.1f}s"
        return f"20k prefixos em {dur:.2f}s"

    @caso("A", "normalizar_doi resiste a lixo longo sem prefixo válido", "alta")
    def _():
        hostil = "http://" * 50000
        with Prazo(10):
            inicio = time.time()
            r = dp.normalizar_doi(hostil)
            dur = time.time() - inicio
        assert pd.isna(r), f"deveria recusar, devolveu {r!r}"
        return f"350 KB de lixo em {dur:.2f}s"

    @caso("A", "normalizar_titulo_dedup com título só de pontuação vira ''", "baixa")
    def _():
        for t in ["...", "!!!", "   ", "—–-", None, float("nan")]:
            assert dp.normalizar_titulo_dedup(t) == "", repr(t)
        return "vazio como esperado (linha não casa por título)"

    @caso("A", "calcular_chave_dedup com DataFrame vazio", "média")
    def _():
        vazio = pd.DataFrame(columns=["id_lattes", "titulo_artigo", "doi", "fonte"])
        r = dp.calcular_chave_dedup(vazio)
        assert len(r) == 0, r
        return "devolve Series vazia"

    @caso("A", "calcular_chave_dedup com índice NÃO único", "alta")
    def _():
        df = pd.DataFrame(
            {"id_lattes": ["1", "1"], "titulo_artigo": ["A", "B"],
             "doi": [None, None], "fonte": ["LATTES", "LATTES"]},
            index=[0, 0],  # índice duplicado
        )
        chaves = dp.calcular_chave_dedup(df)
        assert len(chaves) == 2, chaves
        # o uso real é `df['chave_dedup'] = calcular_chave_dedup(df)`
        df["chave_dedup"] = chaves
        return "aceita índice duplicado na atribuição"

    @caso("A", "calcular_chave_dedup sem a coluna 'fonte'", "alta")
    def _():
        df = pd.DataFrame({"id_lattes": ["1"], "titulo_artigo": ["A"], "doi": ["10.1/a"]})
        try:
            dp.calcular_chave_dedup(df)
        except KeyError:
            return "KeyError explícito (contrato exige 'fonte')"
        raise AssertionError("passou sem 'fonte' — contrato silenciosamente violável")

    @caso("A", "linhas sem DOI e sem título não se fundem entre si", "alta")
    def _():
        df = pd.DataFrame({
            "id_lattes": ["1", "1", "1"],
            "titulo_artigo": [None, "", "   "],
            "doi": [None, None, None],
            "fonte": ["LATTES"] * 3,
        })
        chaves = list(dp.calcular_chave_dedup(df))
        assert len(set(chaves)) == 3, f"fundiu linhas vazias: {chaves}"
        return "3 chaves LINHA: distintas"

    @caso("A", "coautoria entre professores nunca funde publicações", "alta")
    def _():
        df = pd.DataFrame({
            "id_lattes": ["prof_a", "prof_b"],
            "titulo_artigo": ["Mesmo Artigo", "Mesmo Artigo"],
            "doi": ["10.1/x", "10.1/x"],
            "fonte": ["LATTES", "LATTES"],
        })
        chaves = list(dp.calcular_chave_dedup(df))
        assert chaves[0] != chaves[1], chaves
        assert all(c.startswith(p) for c, p in zip(chaves, ["prof_a", "prof_b"])), chaves
        return "cada professor mantém a sua linha"

    @caso("A", "dois grupos jamais retêm o mesmo DOI (voltariam a se fundir)", "alta")
    def _():
        # DOI contestado com DOIS títulos corroborados por fontes diferentes
        df = pd.DataFrame({
            "id_lattes": ["p"] * 6,
            "titulo_artigo": ["T1", "T2", "T3", "T1", "T2", "T3"],
            "doi": ["10.1/x"] * 6,
            "fonte": ["LATTES", "LATTES", "LATTES", "SCOPUS", "ORCID", "ORCID"],
        })
        chaves = list(dp.calcular_chave_dedup(df))
        com_doi = [c for c in chaves if "|DOI:" in c]
        assert len(set(com_doi)) <= 1, f"mais de um grupo reteve DOI: {set(com_doi)}"
        return f"{len(set(chaves))} grupos, no máximo 1 com DOI"

    @caso("A", "desempenho: professor com 20 mil publicações", "média")
    def _():
        n = 20000
        df = pd.DataFrame({
            "id_lattes": ["p"] * n,
            "titulo_artigo": [f"T{i}" for i in range(n)],
            "doi": [f"10.1/{i}" for i in range(n)],
            "fonte": ["LATTES"] * n,
        })
        inicio = time.time()
        with Prazo(60):
            dp.calcular_chave_dedup(df)
        dur = time.time() - inicio
        assert dur < 30, f"lento: {dur:.1f}s"
        return f"{n} linhas em {dur:.2f}s"

    @caso("A", "desempenho: DOI contestado em massa (laço aninhado)", "alta")
    def _():
        # 4 mil linhas, 2 mil DOIs contestados -> exercita o for-dentro-de-for
        n = 4000
        df = pd.DataFrame({
            "id_lattes": ["p"] * n,
            "titulo_artigo": [f"T{i}" for i in range(n)],
            "doi": [f"10.1/{i // 2}" for i in range(n)],  # cada DOI em 2 títulos
            "fonte": ["LATTES"] * n,
        })
        inicio = time.time()
        with Prazo(90):
            dp.calcular_chave_dedup(df)
        dur = time.time() - inicio
        return f"{n} linhas / {n // 2} DOIs contestados em {dur:.2f}s"

    @caso("A", "sanear_doi_gravado não altera contagem de linhas", "alta")
    def _():
        df = pd.DataFrame({
            "id_lattes": ["p"] * 4,
            "titulo_artigo": ["A", "B", "C", "D"],
            "doi": ["10.1/x", "10.1/x", "lixo", None],
            "ano": [2020, 2021, 2022, None],
            "fonte": ["LATTES"] * 4,
        })
        df["chave_dedup"] = dp.calcular_chave_dedup(df)
        saneado, rel = dp.sanear_doi_gravado(df, "congresso", "ano")
        assert len(saneado) == len(df), "perdeu ou criou linha"
        assert list(saneado.columns) == list(df.columns), "mudou o schema"
        return f"{len(df)} linhas preservadas, {len(rel)} descarte(s) relatado(s)"

    @caso("A", "sanear_doi_gravado com coluna de ano inexistente", "média")
    def _():
        df = pd.DataFrame({"id_lattes": ["p"], "titulo_artigo": ["A"],
                           "doi": ["10.1/x"], "fonte": ["LATTES"]})
        try:
            dp.sanear_doi_gravado(df, "congresso", "ano_que_nao_existe")
        except KeyError:
            return "KeyError explícito"
        raise AssertionError("aceitou coluna inexistente silenciosamente")

    @caso("A", "unificar_com_dedup com uma única fonte e coluna toda nula", "média")
    def _():
        df = pd.DataFrame({
            "id_lattes": ["p", "p"], "titulo_artigo": ["A", "A"],
            "doi": [None, None], "estrato": [None, None],
            "fonte": ["LATTES", "LATTES"], "chave_dedup": ["p|TIT:A", "p|TIT:A"],
        })
        r = dp.unificar_com_dedup(df)
        assert len(r) == 1, r
        assert r["fontes"].iloc[0] == "LATTES"
        return "colapsa para 1 linha, fontes='LATTES'"

    # -------------------------------------------------------------------------
    # alocacao_papers: a pontuação e a alocação ótima, sem Streamlit no caminho
    # -------------------------------------------------------------------------
    import itertools
    import random

    import alocacao_papers as ap

    def _instancia(linhas):
        """Atalho: dicionários -> DataFrame pontuado e agrupado."""
        colunas = {c: None for c in ap.COLUNAS_ESPERADAS}
        return ap.agrupar(ap.pontuar(pd.DataFrame([{**colunas, **linha} for linha in linhas])))

    @caso("A", "pontuar reproduz a tabela de pesos do credenciamento", "alta")
    def _():
        df = ap.pontuar(pd.DataFrame([
            {"maior_percentil": 90, "computation_area": True, "coautoria_aluno": True,
             "citacoes": 10},
            {"maior_percentil": 51, "computation_area": False, "coautoria_aluno": False,
             "citacoes": None},
            {"maior_percentil": None, "computation_area": None, "coautoria_aluno": None,
             "citacoes": "lixo"},
        ]))
        assert abs(df["pontuacao"].iloc[0] - (1.0 * 1.25 * 1.5 + 0.10)) < 1e-9, df.iloc[0]
        assert abs(df["pontuacao"].iloc[1] - 0.625) < 1e-9, df.iloc[1]
        assert abs(df["pontuacao"].iloc[2] - 0.125) < 1e-9, df.iloc[2]
        assert list(df["estrato"]) == ["A1", "A4", "A8"], list(df["estrato"])
        return "A1 c/ bônus = 1,975; percentil nulo cai em A8; citação não numérica vira 0"

    @caso("A", "pontuar com citações negativas e teto", "média")
    def _():
        df = ap.pontuar(pd.DataFrame([
            {"maior_percentil": 90, "computation_area": False, "coautoria_aluno": False,
             "citacoes": -5},
            {"maior_percentil": 90, "computation_area": False, "coautoria_aluno": False,
             "citacoes": 5000},
        ]), teto_citacoes=100)
        assert df["citacoes_consideradas"].tolist() == [0.0, 100.0], df["citacoes_consideradas"]
        return "citação negativa vira 0; teto corta o outlier"

    @caso("A", "alocação é o ótimo exato (conferido por força bruta)", "crítica")
    def _():
        rng = random.Random(20260910)
        for _ in range(120):
            docentes = [f"d{i}" for i in range(rng.randint(1, 4))]
            linhas = []
            for p in range(rng.randint(1, 5)):
                for d in rng.sample(docentes, rng.randint(1, len(docentes))):
                    linhas.append({
                        "id_lattes": d, "docente": d, "titulo_artigo": f"T{p}", "ano": 2020,
                        "doi": f"10.1/{p}" if rng.random() < 0.7 else None,
                        "maior_percentil": rng.choice([None, 10, 55, 95]),
                        "computation_area": rng.random() < 0.5,
                        "coautoria_aluno": rng.random() < 0.5,
                        "citacoes": rng.choice([None, 0, 3, 40]),
                    })
            df = _instancia(linhas)
            cotas = {d: rng.randint(0, 3) for d in docentes}
            resultado = ap.resolver(df, cotas)

            grupos = sorted(df["grupo"].unique())
            candidatos = {g: sorted(set(df[df["grupo"] == g]["id_lattes"])) for g in grupos}
            melhor = 0.0
            for combinacao in itertools.product(*[[None] + candidatos[g] for g in grupos]):
                uso, total, viavel = {}, 0.0, True
                for grupo, docente in zip(grupos, combinacao):
                    if docente is None:
                        continue
                    uso[docente] = uso.get(docente, 0) + 1
                    if uso[docente] > cotas.get(docente, 0):
                        viavel = False
                        break
                    total += df[(df["grupo"] == grupo)
                                & (df["id_lattes"] == docente)]["pontuacao"].max()
                if viavel:
                    melhor = max(melhor, total)
            assert abs(melhor - resultado.total) < 1e-6, (melhor, resultado.total, cotas, linhas)
        return "120 instâncias aleatórias: húngaro == força bruta"

    @caso("A", "invariantes da alocação: paper único e cota como teto", "crítica")
    def _():
        linhas = [
            {"id_lattes": "a", "docente": "A", "titulo_artigo": "Compartilhado", "ano": 2020,
             "doi": "10.1/x", "maior_percentil": 95, "computation_area": True,
             "coautoria_aluno": True, "citacoes": 3},
            {"id_lattes": "b", "docente": "B", "titulo_artigo": "COMPARTILHADO!", "ano": 2020,
             "doi": None, "maior_percentil": 10, "computation_area": False,
             "coautoria_aluno": False, "citacoes": 0},
        ]
        df = _instancia(linhas)
        assert df["grupo"].nunique() == 1, "título normalizado igual no mesmo ano não uniu"
        resultado = ap.resolver(df, {"a": 5, "b": 5})
        assert len(resultado.alocacao) == 1, resultado.alocacao
        assert resultado.alocacao["id_lattes"].iloc[0] == "a", "foi para quem pontua menos"
        assert list(resultado.por_docente["faltando"]) == [4, 5], resultado.por_docente
        assert len(resultado.disputas) == 1, resultado.disputas
        return "paper coassinado vai para um só; cota não preenchida vira 'faltando'"

    @caso("A", "alocação com entradas degeneradas", "alta")
    def _():
        vazio = ap.agrupar(ap.pontuar(pd.DataFrame(columns=ap.COLUNAS_ESPERADAS)))
        sem_papers = ap.resolver(vazio, {"x": 3}, nomes={"x": "Xis"})
        assert sem_papers.total == 0.0
        # Base sem nenhum paper ainda lista o docente pedido, com a cota inteira
        # em aberto -- a página precisa dizer "faltaram 3", não sumir com ele.
        assert list(sem_papers.por_docente["faltando"]) == [3], sem_papers.por_docente
        assert list(sem_papers.por_docente["docente"]) == ["Xis"], sem_papers.por_docente
        assert ap.resolver(vazio, {}).total == 0.0
        assert ap.resolver(vazio, None).total == 0.0
        df = _instancia([
            {"id_lattes": "a", "docente": "A", "titulo_artigo": "T", "ano": 2020,
             "doi": None, "maior_percentil": 95, "computation_area": False,
             "coautoria_aluno": False, "citacoes": 1},
        ])
        assert ap.resolver(df, {"a": 0}).total == 0.0, "cota zero alocou algo"
        assert ap.resolver(df, {"z": 9}).total == 0.0, "alocou paper de quem não o assina"
        assert len(ap.resolver(df, {"a": 99}).alocacao) == 1, "duplicou o único paper"
        return "df vazio, cota 0, docente sem papers e cota maior que o acervo"

    @caso("A", "alocação não degenera com muitos pares", "média")
    def _():
        rng = random.Random(7)
        linhas = [{
            "id_lattes": f"d{i % 40}", "docente": f"D{i % 40}",
            "titulo_artigo": f"T{i}", "ano": 2020, "doi": f"10.1/{i}",
            "maior_percentil": rng.choice([None, 30, 60, 90]),
            "computation_area": rng.random() < 0.5, "coautoria_aluno": rng.random() < 0.5,
            "citacoes": rng.randint(0, 50),
        } for i in range(4000)]
        df = _instancia(linhas)
        with Prazo(30):
            resultado = ap.resolver(df, {f"d{i}": 20 for i in range(40)})
        assert len(resultado.alocacao) == 40 * 20, len(resultado.alocacao)
        return "4.000 pares x 800 vagas resolvidos em menos de 30s"


# =============================================================================
# SUÍTE B — jobs: renomear/excluir sob estados hostis
# =============================================================================
def suite_b():
    import jobs

    projeto = os.getcwd()
    sandbox = tempfile.mkdtemp(prefix="pesc_rob_")
    os.chdir(sandbox)

    def criar(nome):
        a = jobs.artefatos_comparacao(nome)
        os.makedirs(jobs.COMPARACAO_LISTS_DIR, exist_ok=True)
        os.makedirs(jobs.STATUS_DIR, exist_ok=True)
        open(a["lista"], "w").write("123,Fulano\n")
        return a

    try:
        @caso("B", "path traversal no nome novo é neutralizado", "crítica")
        def _():
            hostis = ["../../../etc/passwd", "..", ".", "/etc/shadow",
                      "a/../../b", "....//....//x", "\\..\\..\\win",
                      "%2e%2e%2fetc", "x\x00/etc/passwd", "con", "..;/etc"]
            for i, h in enumerate(hostis):
                origem = f"alvo{i}"
                criar(origem)
                novo = jobs.renomear_comparacao(origem, h)
                caminho = jobs.artefatos_comparacao(novo)["lista"]
                real = os.path.realpath(caminho)
                assert real.startswith(os.path.realpath(sandbox)), \
                    f"escapou do sandbox: {h!r} -> {real}"
                assert "/" not in novo and "\\" not in novo and ".." not in novo, \
                    f"slug perigoso: {novo!r}"
                assert os.path.exists(caminho), f"arquivo não foi para o slug: {novo!r}"
                jobs.excluir_comparacao(novo)
            return f"{len(hostis)} payloads contidos, nenhum escapou do sandbox"

        @caso("B", "excluir com nome forjado é recusado", "crítica")
        def _():
            criar("real")
            for forjado in ["../../etc", "real/../real", "REAL", "real "]:
                try:
                    jobs.excluir_comparacao(forjado)
                except ValueError:
                    continue
                raise AssertionError(f"aceitou nome forjado: {forjado!r}")
            assert "real" in jobs.listar_nomes_comparacao(), "apagou a base legítima"
            jobs.excluir_comparacao("real")
            return "4 nomes forjados recusados; base legítima intacta"

        @caso("B", "dois nomes diferentes que colidem no mesmo slug", "alta")
        def _():
            criar("origem")
            jobs.renomear_comparacao("origem", "CEFET/RJ")
            assert "cefet_rj" in jobs.listar_nomes_comparacao()
            criar("outra")
            try:
                jobs.renomear_comparacao("outra", "cefet-rj")  # mesmo slug
            except ValueError as e:
                jobs.excluir_comparacao("cefet_rj")
                jobs.excluir_comparacao("outra")
                return f"colisão detectada: {e}"
            raise AssertionError("permitiu duas bases com o mesmo slug")

        @caso("B", "nomes exóticos (emoji, só símbolos) caem no fallback 'base'", "média")
        def _():
            criar("x1")
            n1 = jobs.renomear_comparacao("x1", "🎓📚")
            assert n1 == "base", n1
            criar("x2")
            try:
                jobs.renomear_comparacao("x2", "###")  # também vira 'base'
            except ValueError:
                jobs.excluir_comparacao("base")
                jobs.excluir_comparacao("x2")
                return "fallback 'base' colide e é recusado na segunda vez"
            raise AssertionError("duas bases distintas viraram 'base'")

        @caso("B", "upload sobrescreve base existente sem avisar", "alta")
        def _():
            """O uploader salva em `<slug>.list` direto. Se o slug bater com uma
            base já cadastrada, a lista é trocada por baixo dos panos enquanto o
            .duckdb e os snapshots continuam sendo os da lista antiga."""
            a = criar("instituicao_x")
            open(a["lista"], "w").write("111,Pessoa Antiga\n")
            os.makedirs(a["raw"], exist_ok=True)
            open(a["duckdb"], "w").write("banco da lista antiga")

            novo_conteudo = "999,Pessoa Nova\n"
            # nome de arquivo diferente, mesmo slug
            try:
                jobs.salvar_lista_comparacao("Instituição X.list", novo_conteudo)
            except ValueError as e:
                recusou = "Já existe" in str(e)
            else:
                recusou = False
            intacta = open(a["lista"]).read() == "111,Pessoa Antiga\n"

            # com confirmação explícita, grava e sinaliza que sobrescreveu
            slug, sobrescreveu = jobs.salvar_lista_comparacao(
                "Instituição X.list", novo_conteudo, sobrescrever=True)
            trocou = open(a["lista"]).read() == novo_conteudo

            jobs.excluir_comparacao("instituicao_x")
            assert recusou, "gravou por cima de base existente sem confirmação"
            assert intacta, "alterou a lista mesmo tendo recusado"
            assert slug == "instituicao_x" and sobrescreveu and trocou, \
                "confirmação explícita não gravou"
            return "recusa sem confirmação; grava e sinaliza com confirmação"

        @caso("B", "salvar_lista_comparacao é recusado com job em andamento", "alta")
        def _():
            criar("ocupada")
            jobs.acquire_lock(jobs.comparacao_extract_lock("ocupada"))
            try:
                jobs.salvar_lista_comparacao("ocupada.list", "1,X\n", sobrescrever=True)
            except ValueError as e:
                jobs.release_lock(jobs.comparacao_extract_lock("ocupada"))
                jobs.excluir_comparacao("ocupada")
                return f"recusado: {str(e)[:40]}"
            raise AssertionError("substituiu a lista durante um job")

        @caso("B", "nome de arquivo com caminho não escapa do diretório de listas", "crítica")
        def _():
            slug, _ = jobs.salvar_lista_comparacao("../../../../tmp/evil.list", "1,X\n")
            caminho = jobs.artefatos_comparacao(slug)["lista"]
            real = os.path.realpath(caminho)
            assert real.startswith(os.path.realpath(sandbox)), f"escapou: {real}"
            jobs.excluir_comparacao(slug)
            return f"'../../../../tmp/evil.list' -> slug '{slug}', contido"

        @caso("B", "nome longo é recusado como ValueError, não OSError", "alta")
        def _():
            """A UI trata ValueError; um OSError cru sobe como traceback na tela."""
            a = criar("longo")
            for tamanho in (5000, 300, jobs.MAX_NOME_COMPARACAO + 1):
                try:
                    novo = jobs.renomear_comparacao("longo", "a" * tamanho)
                except ValueError:
                    continue
                except OSError as e:
                    raise AssertionError(
                        f"OSError vazou para a UI com {tamanho} chars: {e}")
                jobs.excluir_comparacao(novo)
                raise AssertionError(f"aceitou nome de {tamanho} chars")
            assert os.path.exists(a["lista"]), "base ficou partida após a recusa"
            assert "longo" in jobs.listar_nomes_comparacao(), "base sumiu"
            jobs.excluir_comparacao("longo")
            return "3 tamanhos recusados como ValueError; base intacta"

        @caso("B", "renomeação recusada não deixa a base partida entre dois nomes", "crítica")
        def _():
            """A renomeação move artefato por artefato: uma falha no meio deixaria
            metade sob o nome antigo e metade sob o novo. A validação de nome
            acontece antes de qualquer movimento, então nada se move."""
            a = criar("integra")
            os.makedirs(a["raw"], exist_ok=True)
            open(a["duckdb"], "w").write("x")
            jobs.write_status(a["process_status"], state="done")
            antes = {c: os.path.lexists(p) for c, p in a.items()}
            try:
                jobs.renomear_comparacao("integra", "b" * 400)
            except ValueError:
                pass
            depois = {c: os.path.lexists(p) for c, p in a.items()}
            assert antes == depois, f"artefatos se moveram: {antes} -> {depois}"
            novo = jobs.artefatos_comparacao("b" * 400)
            assert not any(os.path.lexists(p) for p in novo.values()), \
                "criou artefato sob o nome recusado"
            jobs.excluir_comparacao("integra")
            return "nenhum artefato se moveu; nada criado sob o nome recusado"

        @caso("B", "renomear com symlink 'current' quebrado", "média")
        def _():
            a = criar("quebrada")
            os.makedirs(a["raw"], exist_ok=True)
            os.symlink("snapshot_que_nao_existe", os.path.join(a["raw"], "current"))
            novo = jobs.renomear_comparacao("quebrada", "consertada")
            destino = jobs.artefatos_comparacao(novo)["raw"]
            assert os.path.lexists(os.path.join(destino, "current")), "perdeu o symlink"
            jobs.excluir_comparacao(novo)
            return "symlink pendurado movido sem erro"

        @caso("B", "excluir apaga symlink pendurado sem seguir o alvo", "alta")
        def _():
            fora = os.path.join(sandbox, "arquivo_externo.txt")
            open(fora, "w").write("não me apague")
            a = criar("perigosa")
            os.makedirs(a["raw"], exist_ok=True)
            os.symlink(fora, os.path.join(a["raw"], "current"))
            jobs.excluir_comparacao("perigosa")
            assert os.path.exists(fora), "seguiu o symlink e apagou arquivo externo!"
            return "alvo externo preservado"

        @caso("B", "duckdb da base é um diretório (estado corrompido)", "média")
        def _():
            a = criar("esquisita")
            os.makedirs(a["duckdb"], exist_ok=True)
            open(os.path.join(a["duckdb"], "dentro.txt"), "w").write("x")
            jobs.excluir_comparacao("esquisita")
            assert not os.path.exists(a["duckdb"]), "não removeu o diretório"
            return "diretório no lugar do banco removido"

        @caso("B", "renomear é recusado com lock de processamento", "alta")
        def _():
            criar("travada")
            jobs.acquire_lock(jobs.comparacao_process_lock("travada"))
            try:
                jobs.renomear_comparacao("travada", "nova")
            except ValueError:
                jobs.release_lock(jobs.comparacao_process_lock("travada"))
                jobs.excluir_comparacao("travada")
                return "lock de processamento respeitado"
            raise AssertionError("renomeou com job em andamento")

        @caso("B", "excluir base cujo .list já sumiu do disco", "média")
        def _():
            a = criar("sumida")
            os.remove(a["lista"])
            try:
                jobs.excluir_comparacao("sumida")
            except ValueError:
                return "recusa coerente: sem .list, a base não existe"
            raise AssertionError("aceitou excluir base inexistente")

        @caso("B", "artefatos_comparacao cobre tudo que os run_* criam", "crítica")
        def _():
            chaves = set(jobs.artefatos_comparacao("n").keys())
            esperadas = {"lista", "config", "saida_scriptlattes", "raw", "duckdb",
                         "extract_status", "process_status", "extract_lock",
                         "process_lock", "log_notebook"}
            assert chaves == esperadas, f"divergência: {chaves ^ esperadas}"
            # e os caminhos têm de bater com os helpers usados pelos scripts
            a = jobs.artefatos_comparacao("n")
            assert a["duckdb"] == jobs.caminho_duckdb_comparacao("n")
            assert a["extract_status"] == jobs.comparacao_extract_status("n")
            assert a["process_lock"] == jobs.comparacao_process_lock("n")
            return f"{len(chaves)} artefatos, caminhos coerentes com os helpers"
    finally:
        os.chdir(projeto)
        shutil.rmtree(sandbox, ignore_errors=True)


# =============================================================================
# SUÍTE C — app sob dados degenerados
# =============================================================================
# Requer um .duckdb institucional de referência; passe o caminho em
# PESC_DUCKDB_REFERENCIA (padrão: o banco de produção do projeto).
def suite_c():
    import duckdb
    from streamlit.testing.v1 import AppTest

    referencia = os.environ.get("PESC_DUCKDB_REFERENCIA",
                                os.path.join(RAIZ, "pesquisadores_teste.duckdb"))
    if not os.path.exists(referencia):
        RESULTADOS.append(("C", "banco de referência disponível", "FALHOU",
                           f"não encontrado: {referencia}", "alta"))
        return

    sandbox = tempfile.mkdtemp(prefix="pesc_rob_c_")
    PAGINAS = ["Indicadores Institucionais", "Análise por Docente",
               "Série Histórica da Produção", "Repositório Geral de Artigos",
               "Avaliação Quadrienal Geral (A1-A8)", "Avaliação Quadrienal Restrita (A1-A4)",
               "Relatório de Credenciamento Consolidado", "Alocação Ótima de Papers",
               "Panorama de Orientações Acadêmicas",
               "Geração de Relatórios", "Comparativo entre Bases", "Configurações"]

    def rodar_paginas(data_dir):
        anterior = os.environ.get("PESC_DATA_DIR")
        os.environ["PESC_DATA_DIR"] = data_dir
        try:
            quebradas = []
            for p in PAGINAS:
                at = AppTest.from_file(os.path.join(RAIZ, "app.py"), default_timeout=180)
                at.session_state["pagina_atual"] = p
                at.run()
                if at.exception:
                    quebradas.append((p, str(at.exception[0].value)[:120]))
            return quebradas
        finally:
            if anterior is None:
                os.environ.pop("PESC_DATA_DIR", None)
            else:
                os.environ["PESC_DATA_DIR"] = anterior

    try:
        @caso("C", "app com base institucional COMPLETAMENTE VAZIA", "crítica")
        def _():
            d = os.path.join(sandbox, "vazio")
            os.makedirs(d, exist_ok=True)
            alvo = os.path.join(d, "pesquisadores_teste.duckdb")
            shutil.copy2(referencia, alvo)
            con = duckdb.connect(alvo)
            tabelas = [r[0] for r in con.sql("show tables").fetchall()]
            for t in tabelas:  # filhas primeiro; FK impede a ordem inversa
                if t not in ("tb_professores", "tb_alunos"):
                    con.execute(f"DELETE FROM {t}")
            con.execute("DELETE FROM tb_alunos")
            con.execute("DELETE FROM tb_professores")
            con.close()
            quebradas = rodar_paginas(d)
            assert not quebradas, f"páginas com exceção: {quebradas}"
            return f"{len(PAGINAS)} páginas sem exceção com 0 registros"

        @caso("C", "app com docente sem nenhuma publicação", "alta")
        def _():
            d = os.path.join(sandbox, "sem_pub")
            os.makedirs(d, exist_ok=True)
            alvo = os.path.join(d, "pesquisadores_teste.duckdb")
            shutil.copy2(referencia, alvo)
            con = duckdb.connect(alvo)
            con.execute("DELETE FROM tb_artigo_periodico")
            con.execute("DELETE FROM tb_artigo_conferencia")
            con.close()
            quebradas = rodar_paginas(d)
            assert not quebradas, f"páginas com exceção: {quebradas}"
            return "31 docentes, 0 publicações — nenhuma divisão por zero"

        @caso("C", "filtro de período com intervalo invertido não quebra a página", "alta")
        def _():
            # Digitar um Ano de Fim menor que o Ano de Início levantava
            # StreamlitAPIException em qualquer página: `renderizar_filtro_periodo`
            # corrigia a inversão escrevendo em st.session_state DEPOIS de o widget
            # de mesma chave existir. A correção passou a acontecer no on_change.
            quebradas, sem_troca = [], []
            for pagina, chave in [("Avaliação Quadrienal Geral (A1-A8)", "quadrienal_geral"),
                                  ("Alocação Ótima de Papers", "alocacao"),
                                  ("Análise por Docente", "docente")]:
                at = AppTest.from_file(os.path.join(RAIZ, "app.py"), default_timeout=180)
                at.session_state["pagina_atual"] = pagina
                at.run()
                at.number_input(key=f"{chave}_ano_inicio").set_value(2020).run()
                at.number_input(key=f"{chave}_ano_fim").set_value(2010).run()
                if at.exception:
                    quebradas.append((pagina, str(at.exception[0].value)[:120]))
                    continue
                valores = (at.number_input(key=f"{chave}_ano_inicio").value,
                           at.number_input(key=f"{chave}_ano_fim").value)
                if valores != (2010, 2020):
                    sem_troca.append((pagina, valores))
            assert not quebradas, f"páginas com exceção: {quebradas}"
            assert not sem_troca, f"páginas que não trocaram os anos: {sem_troca}"
            return "3 páginas: intervalo invertido é trocado, com aviso e sem exceção"

        @caso("C", "Base B: arquivo aleatório é recusado com erro tratável", "alta")
        def _():
            lixo = os.path.join(sandbox, "lixo.duckdb")
            with open(lixo, "wb") as f:
                f.write(os.urandom(4096))
            try:
                con = duckdb.connect(database=lixo, read_only=True)
                con.execute("SELECT 1 FROM tb_professores LIMIT 1")
            except Exception as e:
                return f"recusado: {type(e).__name__}"
            raise AssertionError("arquivo aleatório passou como base válida")

        @caso("C", "Base B: DuckDB válido sem as tabelas obrigatórias", "alta")
        def _():
            outro = os.path.join(sandbox, "outra_arquitetura.duckdb")
            con = duckdb.connect(outro)
            con.execute("CREATE TABLE qualquer_coisa (x INTEGER)")
            con.close()
            obrigatorias = ["tb_professores", "tb_artigo_periodico",
                            "tb_artigo_conferencia", "tb_orientacoes"]
            con = duckdb.connect(database=outro, read_only=True)
            faltando = []
            for t in obrigatorias:
                try:
                    con.execute(f"SELECT 1 FROM {t} LIMIT 1")
                except Exception:
                    faltando.append(t)
            con.close()
            assert faltando == obrigatorias, faltando
            return "as 4 tabelas obrigatórias são detectadas como ausentes"
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


# =============================================================================
# SUÍTE D — invariantes sobre o resultado real do pipeline
# =============================================================================
def suite_d():
    import duckdb
    import pandas as pd
    import dedup_publicacoes as dp

    referencia = os.environ.get("PESC_DUCKDB_REFERENCIA",
                                os.path.join(RAIZ, "pesquisadores_teste.duckdb"))
    if not os.path.exists(referencia):
        RESULTADOS.append(("D", "banco de referência disponível", "FALHOU",
                           f"não encontrado: {referencia}", "alta"))
        return
    con = duckdb.connect(referencia, read_only=True)

    try:
        @caso("D", "nenhuma chave_dedup é compartilhada entre professores", "crítica")
        def _():
            total = 0
            for t in ("tb_artigo_periodico", "tb_artigo_conferencia"):
                n = con.execute(f"""
                    SELECT COUNT(*) FROM (
                      SELECT chave_dedup FROM {t}
                      GROUP BY chave_dedup HAVING COUNT(DISTINCT id_lattes) > 1)
                """).fetchone()[0]
                total += n
            assert total == 0, f"{total} chave(s) cruzando professores"
            return "coautoria interna não funde ninguém"

        @caso("D", "nenhum professor tem o mesmo DOI em duas publicações", "crítica")
        def _():
            problemas = []
            for t in ("tb_artigo_periodico", "tb_artigo_conferencia"):
                df = con.execute(f"SELECT id_lattes, doi FROM {t} WHERE doi IS NOT NULL").df()
                df["d"] = df["doi"].map(dp.normalizar_doi)
                df = df[df["d"].notna()]
                dup = df.groupby(["id_lattes", "d"]).size()
                problemas += [(t, k, int(v)) for k, v in dup[dup > 1].items()]
            assert not problemas, f"{problemas[:5]}"
            return "DOI é único por professor nas duas tabelas"

        @caso("D", "nenhum professor tem o mesmo título em duas publicações", "alta")
        def _():
            problemas = []
            for t in ("tb_artigo_periodico", "tb_artigo_conferencia"):
                df = con.execute(f"SELECT id_lattes, titulo_artigo FROM {t}").df()
                df["t"] = df["titulo_artigo"].map(dp.normalizar_titulo_dedup)
                df = df[df["t"] != ""]
                dup = df.groupby(["id_lattes", "t"]).size()
                problemas += [(t, k[0], k[1][:40], int(v)) for k, v in dup[dup > 1].items()]
            assert not problemas, f"{problemas[:5]}"
            return "título é único por professor nas duas tabelas"

        @caso("D", "coluna `fontes` nunca é nula nem vazia", "alta")
        def _():
            for t in ("tb_artigo_periodico", "tb_artigo_conferencia"):
                n = con.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE fontes IS NULL OR TRIM(fontes) = ''"
                ).fetchone()[0]
                assert n == 0, f"{t}: {n} linha(s) sem fontes"
            return "toda publicação declara de onde veio"

        @caso("D", "todo DOI gravado está na forma canônica", "média")
        def _():
            for t in ("tb_artigo_periodico", "tb_artigo_conferencia"):
                df = con.execute(f"SELECT doi FROM {t} WHERE doi IS NOT NULL").df()
                ruins = [d for d in df["doi"] if not dp.RE_DOI_CANONICO.match(str(d))]
                assert not ruins, f"{t}: {ruins[:3]}"
            return "nenhuma URL ou lixo sobrou na coluna doi"

        @caso("D", "toda publicação aponta para um professor existente (FK)", "crítica")
        def _():
            for t in ("tb_artigo_periodico", "tb_artigo_conferencia",
                      "tb_orientacoes", "tb_dois_descartados"):
                n = con.execute(f"""
                    SELECT COUNT(*) FROM {t} f
                    LEFT JOIN tb_professores p ON p.id_lattes = f.id_lattes
                    WHERE f.id_lattes IS NOT NULL AND p.id_lattes IS NULL
                """).fetchone()[0]
                assert n == 0, f"{t}: {n} órfã(s)"
            return "nenhuma linha órfã em 4 tabelas filhas"

        @caso("D", "tabelas por fonte não têm linha repetida por (fonte, chave)", "alta")
        def _():
            for tipo in ("periodico", "conferencia"):
                for fonte in ("lattes", "orcid", "scopus"):
                    t = f"tb_artigo_{tipo}_{fonte}"
                    n = con.execute(f"""
                        SELECT COUNT(*) FROM (
                          SELECT chave_dedup FROM {t}
                          GROUP BY chave_dedup HAVING COUNT(*) > 1)
                    """).fetchone()[0]
                    assert n == 0, f"{t}: {n} chave(s) repetida(s)"
            return "6 tabelas por fonte sem repetição interna"

        @caso("D", "toda linha da base unificada existe em alguma tabela por fonte", "alta")
        def _():
            for tipo, unificada in (("periodico", "tb_artigo_periodico"),
                                    ("conferencia", "tb_artigo_conferencia")):
                uniao = " UNION ALL ".join(
                    f"SELECT chave_dedup FROM tb_artigo_{tipo}_{f}"
                    for f in ("lattes", "orcid", "scopus"))
                n = con.execute(f"""
                    SELECT COUNT(*) FROM {unificada} u
                    WHERE u.chave_dedup NOT IN (SELECT chave_dedup FROM ({uniao}))
                """).fetchone()[0]
                assert n == 0, f"{unificada}: {n} linha(s) sem origem"
            return "nenhuma publicação surgiu do nada na unificação"

        @caso("D", "anos de publicação estão em faixa plausível", "média")
        def _():
            suspeitos = []
            for t, col in (("tb_artigo_periodico", "ano_pub"), ("tb_artigo_conferencia", "ano")):
                df = con.execute(
                    f"SELECT {col} AS a, COUNT(*) n FROM {t} "
                    f"WHERE {col} IS NOT NULL AND ({col} < 1950 OR {col} > 2035) "
                    f"GROUP BY 1"
                ).df()
                suspeitos += [(t, int(r.a), int(r.n)) for r in df.itertuples()]
            assert not suspeitos, f"{suspeitos}"
            return "nenhum ano fora de 1950–2035"
    finally:
        con.close()


# =============================================================================
# SUÍTE E — fluxo real da UI de renomear/excluir (escreve no projeto)
# =============================================================================
# Usa uma base descartável com prefixo `zz_` e confere, ao final, que as bases
# pré-existentes (quaisquer que sejam — o nome NÃO é fixo no teste) continuam
# intactas.
def suite_e():
    from streamlit.testing.v1 import AppTest
    import jobs

    NOME = "zz_robustez"
    RENOMEADA = "zz_robustez_2"

    def instantaneo():
        """Nome -> artefatos existentes, para as bases que já estavam aqui."""
        return {
            n: {c: os.path.lexists(p)
                for c, p in jobs.artefatos_comparacao(n).items()}
            for n in jobs.listar_nomes_comparacao()
            if not n.startswith("zz_")
        }

    def limpar():
        for n in (NOME, RENOMEADA):
            try:
                if n in jobs.listar_nomes_comparacao():
                    jobs.excluir_comparacao(n)
            except Exception:
                pass

    def widget(at, colecao, chave):
        return next((w for w in getattr(at, colecao) if w.key == chave), None)

    antes = instantaneo()
    limpar()
    try:
        @caso("E", "renomear pela UI move todos os artefatos", "alta")
        def _():
            a = jobs.artefatos_comparacao(NOME)
            os.makedirs(jobs.COMPARACAO_LISTS_DIR, exist_ok=True)
            open(a["lista"], "w").write("123,Fulano\n")
            os.makedirs(os.path.join(a["raw"], "snap"), exist_ok=True)
            open(a["duckdb"], "w").write("x")

            at = AppTest.from_file(os.path.join(RAIZ, "app.py"), default_timeout=180)
            at.session_state["pagina_atual"] = "Configurações"
            at.run()
            widget(at, "selectbox", "select_base_comparacao_operar").select(NOME).run()
            widget(at, "text_input", "input_novo_nome_comparacao").set_value("ZZ Robustez 2").run()
            widget(at, "button", "btn_renomear_comparacao").click().run()

            assert not at.exception, str(at.exception[0].value)[:200]
            assert NOME not in jobs.listar_nomes_comparacao(), "nome antigo sobreviveu"
            assert RENOMEADA in jobs.listar_nomes_comparacao(), "nome novo não apareceu"
            novo = jobs.artefatos_comparacao(RENOMEADA)
            assert os.path.exists(novo["duckdb"]), "banco não foi junto"
            assert os.path.isdir(novo["raw"]), "snapshots não foram junto"
            at.run()  # o uploader não pode ressuscitar o nome antigo
            assert NOME not in jobs.listar_nomes_comparacao(), "nome antigo ressuscitou no rerun"
            return "lista, banco e snapshots movidos; sem ressurreição"

        @caso("E", "excluir exige confirmação e apaga tudo", "alta")
        def _():
            at = AppTest.from_file(os.path.join(RAIZ, "app.py"), default_timeout=180)
            at.session_state["pagina_atual"] = "Configurações"
            at.run()
            widget(at, "selectbox", "select_base_comparacao_operar").select(RENOMEADA).run()
            botao = widget(at, "button", "btn_excluir_comparacao")
            assert botao.disabled, "botão de excluir nasce habilitado (sem confirmação)"
            widget(at, "checkbox", "confirma_exclusao_comparacao").check().run()
            botao = widget(at, "button", "btn_excluir_comparacao")
            assert not botao.disabled, "confirmação não habilitou o botão"
            botao.click().run()
            assert not at.exception, str(at.exception[0].value)[:200]
            assert RENOMEADA not in jobs.listar_nomes_comparacao()
            sobrou = [p for p in jobs.artefatos_comparacao(RENOMEADA).values()
                      if os.path.lexists(p)]
            assert not sobrou, f"sobraram artefatos: {sobrou}"
            return "confirmação obrigatória; nenhum artefato sobrou"

        @caso("E", "bases pré-existentes ficam intactas", "crítica")
        def _():
            depois = instantaneo()
            assert set(antes) == set(depois), \
                f"conjunto de bases mudou: {set(antes) ^ set(depois)}"
            for nome, artefatos in antes.items():
                assert depois[nome] == artefatos, f"artefatos de '{nome}' mudaram"
            return f"{len(antes)} base(s) intacta(s): {sorted(antes)}"
    finally:
        limpar()


if __name__ == "__main__":
    escolhidas = [s.upper() for s in sys.argv[1:]] or ["A", "B", "C", "D", "E"]
    if "A" in escolhidas:
        suite_a()
    if "B" in escolhidas:
        suite_b()
    if "C" in escolhidas:
        suite_c()
    if "D" in escolhidas:
        suite_d()
    if "E" in escolhidas:
        suite_e()

    largura = max(len(n) for _, n, _, _, _ in RESULTADOS) + 2
    print()
    for suite, nome, status, detalhe, sev in RESULTADOS:
        marca = {"PASSOU": "ok  ", "FALHOU": "FALHA", "ERRO": "ERRO "}[status]
        print(f"{marca} [{suite}] {nome:<{largura}} {detalhe.splitlines()[0][:80] if detalhe else ''}")
    ruins = [r for r in RESULTADOS if r[2] != "PASSOU"]
    print(f"\n{len(RESULTADOS) - len(ruins)}/{len(RESULTADOS)} passaram")
    for suite, nome, status, detalhe, sev in ruins:
        print(f"\n--- {status} [{suite}] {nome}  (severidade: {sev})\n{detalhe}")
