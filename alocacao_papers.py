"""Alocação ótima de papers entre docentes — regra única da página homônima.

O problema: dado um conjunto de docentes, uma cota de papers para cada um e a
janela de apuração, decidir **qual paper vai para qual docente** de modo a
maximizar a pontuação total do formulário. Um paper coassinado por dois
docentes do quadro pode ir para qualquer um dos dois, mas só para um: depois de
usado, sai da mesa.

Vive em módulo, e não dentro de `app.py`, pelo mesmo motivo de
`dedup_publicacoes.py` e `credenciamento.py`: a regra de pontuação e a regra de
alocação precisam ser testáveis sem subir o Streamlit, e a suíte de robustez as
exercita direto (`testes_robustez.py`, suíte A).

Nada aqui importa Streamlit nem abre banco: a entrada é um DataFrame com uma
linha por par (docente, paper), como as tabelas do projeto já entregam.


A pontuação
-----------
Reaproveita a tabela de pesos do score de credenciamento (`app.py`,
`montar_query_credenciamento`) e acrescenta o termo de citações:

    pontuação = peso_qualis x 1,25^(computação) x 1,5^(coautoria discente)
              + peso_citação x citações

O termo de citações é **somado**, não multiplicado: os bônus de área e de
coautoria discente não o amplificam. Com o peso padrão de 0,01, cerca de 87
citações valem um A1 inteiro — em janelas longas isso desloca bastante o
ranking, e é por isso que o peso é parâmetro e aparece na tela.

A pontuação é do **par** (docente, paper), não do paper: `coautoria_aluno` e
`maior_percentil` podem divergir entre as linhas de dois coautores do quadro
(a contaminação de metadados descrita em "Limitações conhecidas" no README).


A alocação
----------
É um problema de atribuição bipartida com cota (b-matching / transporte), não
uma busca heurística: a matriz de restrições é totalmente unimodular, então o
ótimo é inteiro e sai em tempo polinomial. `resolver` expande cada docente em
`cota` vagas e chama o húngaro do SciPy (`linear_sum_assignment`), que devolve
o **ótimo exato** — na base atual, 310x282 em ~1,3 ms. Não há aproximação nem
critério de parada a ajustar.

`resolver_guloso` implementa a alternativa óbvia (cada docente pega os seus
melhores papers ainda livres, do mais restrito para o menos restrito) e existe
só como linha de base: a diferença entre os dois é o que a página exibe como
"ganho da alocação ótima".
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from dedup_publicacoes import agrupar_papers_entre_docentes

# ---------------------------------------------------------------------------
# 1. Pontuação
# ---------------------------------------------------------------------------
# Mesma escala do score de credenciamento: oito estratos, de A1 (1,000) a A8
# (0,125), lidos do percentil Scopus do periódico. Mantida aqui como dado, e não
# como CASE WHEN em SQL, para que a página consiga decompor a pontuação de cada
# paper na tela (o usuário precisa entender por que aquele paper foi escolhido).
FAIXAS_PERCENTIL = [
    (87.5, 1.000, 'A1'),
    (75.0, 0.875, 'A2'),
    (62.5, 0.750, 'A3'),
    (50.0, 0.625, 'A4'),
    (37.5, 0.500, 'A5'),
    (25.0, 0.375, 'A6'),
    (12.5, 0.250, 'A7'),
]
PESO_SEM_PERCENTIL = 0.125
ESTRATO_SEM_PERCENTIL = 'A8'

BONUS_COMPUTACAO = 1.25
BONUS_COAUTORIA_DISCENTE = 1.5
PESO_CITACAO_PADRAO = 0.01

# Percentil mínimo do critério restrito (A1-A4), idêntico ao corte usado nas
# páginas Quadrienal Restrita e Credenciamento.
PERCENTIL_MINIMO_RESTRITO = 50.0

COLUNAS_ESPERADAS = [
    'id_lattes', 'docente', 'titulo_artigo', 'ano', 'doi',
    'maior_percentil', 'computation_area', 'coautoria_aluno', 'citacoes',
]


def _e_nulo(valor):
    """`pd.isna` de escalar devolve bool; de array-like devolve array, e usá-lo
    num `if` estoura. Mesmo padrão defensivo de `dedup_publicacoes`."""
    try:
        return bool(pd.isna(valor))
    except (TypeError, ValueError):
        return False


def classificar_percentil(percentil):
    """(peso, estrato) de um percentil Scopus. Percentil nulo cai em A8 — é o
    mesmo `ELSE 0.125` da query de credenciamento, e vale tanto para o que não
    casou com a planilha quanto para o que casou num estrato baixo."""
    if _e_nulo(percentil):
        return PESO_SEM_PERCENTIL, ESTRATO_SEM_PERCENTIL
    try:
        valor = float(percentil)
    except (TypeError, ValueError):
        return PESO_SEM_PERCENTIL, ESTRATO_SEM_PERCENTIL
    for minimo, peso, estrato in FAIXAS_PERCENTIL:
        if valor >= minimo:
            return peso, estrato
    return PESO_SEM_PERCENTIL, ESTRATO_SEM_PERCENTIL


def e_verdadeiro(valor):
    """Comparação explícita com True, como o resto do app faz com
    `coautoria_aluno`: banco antigo em que a coluna ficou nula conta como
    "não", em vez de quebrar.

    Pública porque a página também precisa dela para exibir sim/não: o booleano
    que o DuckDB devolve num DataFrame é `numpy.bool_`, e `valor is True` é
    **falso** para ele -- a comparação tem de ser por valor."""
    return valor is True or valor == 1 or valor == 'true' or valor == 'True'


def pontuar(df, peso_citacao=PESO_CITACAO_PADRAO, teto_citacoes=None):
    """Acrescenta ao DataFrame as colunas da pontuação de cada par
    (docente, paper), decompostas para exibição:

    `estrato`, `peso_qualis`, `fator_computacao`, `fator_discente`,
    `pontuacao_qualis` (o produto dos três), `citacoes_consideradas` e
    `pontuacao` (o total, já com o termo de citações).

    `teto_citacoes`, quando informado, limita quantas citações de um mesmo paper
    entram na conta — trava contra o outlier que domina uma janela longa."""
    resultado = df.copy()
    if resultado.empty:
        for coluna in ['estrato', 'peso_qualis', 'fator_computacao', 'fator_discente',
                       'pontuacao_qualis', 'citacoes_consideradas', 'pontuacao']:
            resultado[coluna] = pd.Series(dtype='float64' if coluna != 'estrato' else 'object')
        return resultado

    classificacao = [classificar_percentil(v) for v in resultado['maior_percentil']]
    resultado['peso_qualis'] = [peso for peso, _ in classificacao]
    resultado['estrato'] = [estrato for _, estrato in classificacao]
    resultado['fator_computacao'] = [
        BONUS_COMPUTACAO if e_verdadeiro(v) else 1.0 for v in resultado['computation_area']
    ]
    resultado['fator_discente'] = [
        BONUS_COAUTORIA_DISCENTE if e_verdadeiro(v) else 1.0 for v in resultado['coautoria_aluno']
    ]
    resultado['pontuacao_qualis'] = (
        resultado['peso_qualis'] * resultado['fator_computacao'] * resultado['fator_discente']
    )

    # Publicação sem correspondência na Scopus fica com `citacoes` nula, e aqui
    # ela conta como zero -- não como "desconhecido". É um viés conhecido contra
    # o que não está na Scopus, e a página exibe a cobertura junto do resultado.
    citacoes = pd.to_numeric(resultado['citacoes'], errors='coerce').fillna(0)
    citacoes = citacoes.clip(lower=0)
    if teto_citacoes is not None:
        citacoes = citacoes.clip(upper=float(teto_citacoes))
    resultado['citacoes_consideradas'] = citacoes
    resultado['pontuacao'] = resultado['pontuacao_qualis'] + peso_citacao * citacoes
    return resultado


def filtrar_restrito(df):
    """Deixa só os estratos A1-A4 (percentil >= 50), como fazem a Quadrienal
    Restrita e o Credenciamento restrito.

    O corte é por `WHERE`, e não por peso zero: com peso zero um A8 muito citado
    ainda pontuaria pelo termo de citações e poderia ser escolhido — exatamente
    o que o critério restrito exclui."""
    if df.empty:
        return df
    percentil = pd.to_numeric(df['maior_percentil'], errors='coerce')
    return df[percentil >= PERCENTIL_MINIMO_RESTRITO].copy()


# ---------------------------------------------------------------------------
# 2. Identidade do paper entre docentes
# ---------------------------------------------------------------------------
def agrupar(df):
    """Acrescenta a coluna `grupo`: o paper a que cada linha pertence,
    atravessando docentes (`dedup_publicacoes.agrupar_papers_entre_docentes`).

    É esta coluna que faz "cada paper é único": duas linhas com o mesmo grupo
    são o mesmo artigo, e a alocação só pode entregá-lo a um docente."""
    resultado = df.copy()
    resultado['grupo'] = agrupar_papers_entre_docentes(
        resultado, coluna_titulo='titulo_artigo', coluna_ano='ano', coluna_doi='doi')
    return resultado


# ---------------------------------------------------------------------------
# 3. Alocação
# ---------------------------------------------------------------------------
@dataclass
class Resultado:
    """Saída de `resolver`. `alocacao` tem uma linha por paper entregue (as
    colunas do DataFrame de entrada + a pontuação); `por_docente` fecha cota,
    entregues e score de cada um; `disputas` lista os papers que mais de um
    docente selecionado poderia usar."""
    alocacao: pd.DataFrame
    por_docente: pd.DataFrame
    disputas: pd.DataFrame
    total: float = 0.0
    total_guloso: float = 0.0
    vagas: int = 0
    papers_distintos: int = 0
    diagnostico: dict = field(default_factory=dict)

    @property
    def ganho_sobre_guloso(self):
        return self.total - self.total_guloso


def _melhor_linha_por_par(df):
    """Mapa (id_lattes, grupo) -> posição da linha de maior pontuação.

    Um docente só deveria ter uma linha por paper (a dedup da base garante
    isso), mas se duas linhas do mesmo docente caírem no mesmo grupo aqui, vale
    a de maior pontuação — nunca as duas, que dariam o paper duas vezes ao
    mesmo docente."""
    pontuacoes = list(df['pontuacao'])
    melhor = {}
    for posicao, (id_lattes, grupo) in enumerate(zip(df['id_lattes'], df['grupo'])):
        chave = (id_lattes, grupo)
        atual = melhor.get(chave)
        if atual is None or pontuacoes[posicao] > pontuacoes[atual]:
            melhor[chave] = posicao
    return melhor


def resolver(df, cotas, nomes=None):
    """Alocação **ótima** de papers a docentes.

    `df` traz uma linha por par (docente, paper) já pontuada por `pontuar` e
    agrupada por `agrupar`. `cotas` é um dicionário id_lattes -> quantos papers
    aquele docente precisa entregar. `nomes` (id_lattes -> nome) serve para que
    docente sem nenhum paper elegível ainda apareça pelo nome no fechamento.

    Maximiza a soma das pontuações sujeito a: cada docente recebe no máximo a
    sua cota, e cada paper é usado no máximo uma vez. Um docente com menos
    papers elegíveis do que a cota simplesmente fica com a cota incompleta — a
    restrição é "no máximo", e `por_docente` registra quantos faltaram."""
    vazio = pd.DataFrame(columns=list(df.columns) + ['pontuacao'])
    cotas = {d: int(c) for d, c in (cotas or {}).items() if int(c) > 0}
    nomes = dict(nomes or {})
    colunas_por_docente = ['id_lattes', 'docente', 'cota', 'elegiveis',
                           'alocados', 'score', 'faltando']
    colunas_disputas = ['grupo', 'titulo_artigo', 'ano', 'doi',
                        'candidatos', 'vencedor', 'delta']

    elegivel = df[df['id_lattes'].isin(cotas)].reset_index(drop=True) if not df.empty else df
    if elegivel.empty:
        # Sem nenhum paper elegível o fechamento ainda tem de listar os
        # docentes pedidos, com a cota inteira em aberto -- a página precisa
        # dizer "faltaram 5", não sumir com o docente.
        return Resultado(
            alocacao=vazio,
            por_docente=pd.DataFrame([
                {'id_lattes': d, 'docente': nomes.get(d, d), 'cota': c, 'elegiveis': 0,
                 'alocados': 0, 'score': 0.0, 'faltando': c}
                for d, c in sorted(cotas.items(), key=lambda kv: nomes.get(kv[0], kv[0]))
            ], columns=colunas_por_docente),
            disputas=pd.DataFrame(columns=colunas_disputas),
        )

    melhor = _melhor_linha_por_par(elegivel)
    papers_por_docente = {}
    for (id_lattes, _grupo) in melhor:
        papers_por_docente[id_lattes] = papers_por_docente.get(id_lattes, 0) + 1

    # Vaga a mais do que papers elegíveis é vaga que nunca poderia ser
    # preenchida: cortá-las mantém a matriz do tamanho dos dados, e não do
    # tamanho da cota pedida (uma cota de 500 não infla nada).
    vagas = []
    for id_lattes, cota in sorted(cotas.items()):
        for _ in range(min(cota, papers_por_docente.get(id_lattes, 0))):
            vagas.append(id_lattes)

    grupos = sorted({grupo for (_d, grupo) in melhor})
    indice_grupo = {grupo: i for i, grupo in enumerate(grupos)}
    linhas_por_docente = {}
    for posicao, id_lattes in enumerate(vagas):
        linhas_por_docente.setdefault(id_lattes, []).append(posicao)

    # Par inelegível vale 0; par elegível vale pontuação + EPSILON. O húngaro
    # preenche min(vagas, papers) células, e o epsilon garante que ele nunca
    # prefira uma célula inelegível a uma elegível de pontuação zero. As células
    # inelegíveis que sobrarem valem 0 e são descartadas logo abaixo, sem
    # alterar o total.
    EPSILON = 1e-9
    escolhidas = []
    if vagas:
        pontuacoes = list(elegivel['pontuacao'])
        matriz = np.zeros((len(vagas), len(grupos)), dtype='float64')
        for (id_lattes, grupo), posicao_linha in melhor.items():
            valor = float(pontuacoes[posicao_linha]) + EPSILON
            for vaga in linhas_por_docente[id_lattes]:
                matriz[vaga, indice_grupo[grupo]] = valor

        vagas_idx, grupos_idx = linear_sum_assignment(matriz, maximize=True)
        for vaga, coluna in zip(vagas_idx, grupos_idx):
            if matriz[vaga, coluna] <= 0:  # par inelegível, forçado pelo pareamento
                continue
            escolhidas.append(melhor[(vagas[vaga], grupos[coluna])])

    alocacao = elegivel.iloc[sorted(escolhidas)].copy() if escolhidas else vazio
    total = float(alocacao['pontuacao'].sum()) if not alocacao.empty else 0.0

    # --- fechamento por docente -------------------------------------------
    alocados = alocacao.groupby('id_lattes')['pontuacao'].agg(['count', 'sum']) \
        if not alocacao.empty else pd.DataFrame(columns=['count', 'sum'])
    nomes.update(dict(zip(elegivel['id_lattes'], elegivel['docente'])))
    por_docente = pd.DataFrame([
        {
            'id_lattes': id_lattes,
            'docente': nomes.get(id_lattes, id_lattes),
            'cota': cota,
            'elegiveis': papers_por_docente.get(id_lattes, 0),
            'alocados': int(alocados['count'].get(id_lattes, 0)) if not alocados.empty else 0,
            'score': float(alocados['sum'].get(id_lattes, 0.0)) if not alocados.empty else 0.0,
        }
        for id_lattes, cota in sorted(cotas.items(), key=lambda kv: nomes.get(kv[0], kv[0]))
    ], columns=[c for c in colunas_por_docente if c != 'faltando'])
    por_docente['faltando'] = por_docente['cota'] - por_docente['alocados']

    # --- papers disputados -------------------------------------------------
    # Só interessam os grupos com mais de um docente selecionado elegível: são
    # eles que a alocação teve de decidir, e é onde ela pode divergir do óbvio.
    candidatos_por_grupo = {}
    for (id_lattes, grupo), posicao_linha in melhor.items():
        candidatos_por_grupo.setdefault(grupo, []).append(
            (float(elegivel['pontuacao'].iloc[posicao_linha]), id_lattes))
    vencedor_por_grupo = dict(zip(alocacao['grupo'], alocacao['id_lattes'])) \
        if not alocacao.empty else {}

    disputas = []
    for grupo, candidatos in candidatos_por_grupo.items():
        if len(candidatos) < 2:
            continue
        candidatos.sort(reverse=True)
        linha = elegivel.iloc[melhor[(candidatos[0][1], grupo)]]
        vencedor = vencedor_por_grupo.get(grupo)
        disputas.append({
            'grupo': grupo,
            'titulo_artigo': linha['titulo_artigo'],
            'ano': linha['ano'],
            'doi': linha['doi'],
            'candidatos': ', '.join(nomes.get(d, d) for _p, d in candidatos),
            'vencedor': nomes.get(vencedor, '— (não usado)') if vencedor else '— (não usado)',
            'delta': candidatos[0][0] - candidatos[1][0],
        })
    disputas = pd.DataFrame(disputas, columns=colunas_disputas)
    if not disputas.empty:
        disputas = disputas.sort_values(['delta', 'titulo_artigo'], ascending=[False, True])

    return Resultado(
        alocacao=alocacao,
        por_docente=por_docente,
        disputas=disputas,
        total=total,
        total_guloso=resolver_guloso(elegivel, cotas, melhor=melhor),
        vagas=len(vagas),
        papers_distintos=len(grupos),
        diagnostico={
            'pares_elegiveis': len(melhor),
            'papers_disputados': int(sum(1 for c in candidatos_por_grupo.values() if len(c) > 1)),
        },
    )


def resolver_guloso(df, cotas, melhor=None):
    """Linha de base: cada docente pega os seus melhores papers ainda livres,
    começando pelo docente com menos opções (o mais restrito primeiro, que é a
    ordem gulosa que mais costuma acertar).

    Devolve só o total, que a página compara com o ótimo — a diferença é o que
    se ganha por resolver de verdade em vez de escolher na ordem óbvia."""
    cotas = {d: int(c) for d, c in (cotas or {}).items() if int(c) > 0}
    if df.empty or not cotas:
        return 0.0
    if melhor is None:
        df = df[df['id_lattes'].isin(cotas)].reset_index(drop=True)
        if df.empty:
            return 0.0
        melhor = _melhor_linha_por_par(df)

    candidatos_por_docente = {}
    for (id_lattes, grupo), posicao in melhor.items():
        candidatos_por_docente.setdefault(id_lattes, []).append(
            (float(df['pontuacao'].iloc[posicao]), grupo))

    ordem = sorted(cotas, key=lambda d: (len(candidatos_por_docente.get(d, [])), str(d)))
    usados, total = set(), 0.0
    for id_lattes in ordem:
        candidatos = sorted(candidatos_por_docente.get(id_lattes, []),
                            key=lambda item: (-item[0], item[1]))
        entregues = 0
        for pontuacao, grupo in candidatos:
            if entregues >= cotas[id_lattes]:
                break
            if grupo in usados:
                continue
            usados.add(grupo)
            total += pontuacao
            entregues += 1
    return total
