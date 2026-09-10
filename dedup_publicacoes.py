"""Deduplicação de publicações — regra única, compartilhada pelos dois pipelines.

Este módulo é a definição *única* de "duas linhas são a mesma publicação" e de
"este DOI vale". Ele é importado tanto por `analyse_organizado.ipynb` (base
institucional, Lattes + ORCID + Scopus) quanto por
`analyse_organizado_comparação.ipynb` (base de comparação, só Lattes).

Antes, cada notebook tinha a sua própria cópia destas funções. Como as duas
bases existem para serem comparadas entre si, qualquer correção aplicada em só
um dos lados fazia justamente o que a comparação deveria detectar: os números
divergirem por causa do código, e não por causa dos dados. Manter uma cópia só
é o que garante que uma diferença entre as bases signifique diferença real de
produção.
"""

import re
import unicodedata

import pandas as pd

try:  # dentro de um notebook, mostra DataFrame formatado
    from IPython.display import display
except ImportError:  # fora dele, degrada para print sem quebrar
    display = print


# ---------------------------------------------------------------------------
# 1. Normalização de DOI e título
# ---------------------------------------------------------------------------
# Forma canônica de um DOI: prefixo "10.<registrante>" e um sufixo sem espaços.
# Serve de porteiro em `normalizar_doi`: o que não tem essa cara não é DOI e não
# pode ser usado como identificador de publicação.
RE_DOI_CANONICO = re.compile(r'^10\.\d{4,9}/\S+$')

# Prefixos de resolvedor que o Lattes guarda colados ao DOI. Removidos em laço
# porque vários currículos trazem o prefixo repetido, como em
# "http://dx.doi.org/http://dx.doi.org/10.1109/MEDHOCNET.2010.5546860" e
# "http://dx.doi.org/http://doi.ieeecomputersociety.org/10.1109/ICGSE.2010.20".
RE_PREFIXO_RESOLVEDOR = re.compile(
    r'^(https?://)*(dx\.)?(doi\.org|doi\.ieeecomputersociety\.org)/'
)


def _e_nulo(valor):
    """`pd.isna` de um escalar devolve bool, mas de algo array-like devolve um
    array — e usá-lo num `if` levanta ValueError.

    Isso importa porque o campo `doi` das três fontes é texto livre vindo de
    JSON/API: basta uma célula chegar como lista (um registro com dois
    identificadores externos, por exemplo) para o `.map(normalizar_doi)`
    estourar e abortar o reprocessamento inteiro. Aqui esse valor só deixa de
    ser tratado como nulo e segue para a validação de formato, que o recusa.
    Mesmo padrão defensivo dos formatadores de `app.py`."""
    try:
        return bool(pd.isna(valor))
    except (TypeError, ValueError):
        return False


def normalizar_doi(doi):
    """Reduz um DOI à sua forma canônica para comparação EXATA: minúsculo, sem
    espaços, sem prefixo textual ("doi:") nem de resolvedor
    (https://doi.org/, http://dx.doi.org/) e sem pontuação solta na borda
    (várias fontes trazem o DOI seguido de ponto final).

    Devolve <NA> — ou seja, a linha não participa do casamento por DOI — em
    dois casos: quando não sobra nada de útil, e quando o que sobra **não tem
    formato de DOI**. Essa segunda checagem existe porque o campo `doi` do
    Lattes é texto livre e frequentemente guarda outra coisa: link direto para
    o PDF do artigo, ou a URL "citado por" da Scopus. Essa URL carrega o DOI
    real num parâmetro `doi=`, que é aproveitado quando existe, mas vale
    literalmente `null` quando a Scopus não conhecia o DOI — e aí é a *mesma
    string* em publicações sem nenhuma relação (uma delas aparece em 11
    professores). Tratada como DOI, ela funde publicações distintas do mesmo
    professor numa só.
    """
    if _e_nulo(doi):
        return pd.NA
    texto = str(doi).strip().lower()

    # URL "citado por" da Scopus: o DOI verdadeiro vem no parâmetro `doi=`.
    if 'scopus.com' in texto and 'doi=' in texto:
        capturado = re.search(r'[?&]doi=([^&]*)', texto)
        texto = capturado.group(1) if capturado else ''

    texto = re.sub(r'^doi\s*:\s*', '', texto)
    anterior = None
    while anterior != texto:  # desfaz prefixos de resolvedor repetidos
        anterior = texto
        texto = RE_PREFIXO_RESOLVEDOR.sub('', texto)
    texto = re.sub(r'\s+', '', texto).strip(' .,;')

    if texto in ('', 'nan', 'none', 'null', '<na>'):
        return pd.NA
    if not RE_DOI_CANONICO.match(texto):
        return pd.NA
    return texto


def normalizar_titulo_dedup(titulo):
    """Reduz um título à sua forma canônica para comparação EXATA:
    maiúsculas, sem acentuação, sem pontuação e com espaços colapsados.
    Assim "Título: Algo Novo." (Lattes) e "TITULO ALGO NOVO" (Scopus) viram a
    mesma string. Retorna '' quando não há título — nesse caso a linha não
    participa do casamento por título."""
    if _e_nulo(titulo):
        return ''
    texto = str(titulo).upper().strip()
    texto = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('ascii')
    texto = re.sub(r'[^A-Z0-9 ]+', ' ', texto)
    return re.sub(r'\s+', ' ', texto).strip()


# ---------------------------------------------------------------------------
# 2. Chave de deduplicação — SEMPRE dentro de um único professor
# ---------------------------------------------------------------------------
def _neutralizar_dois_contestados(dois, titulos, fontes, posicoes):
    """Marca como <NA> o DOI das linhas em que ele não pode servir de
    identificador — alterando `dois` no lugar, apenas nas posições do professor
    recebido. A coluna `doi` gravada não é tocada aqui: o que muda é só o
    critério de casamento.

    Um DOI identifica uma única publicação. Quando **uma mesma fonte** lista o
    mesmo DOI em dois títulos diferentes do mesmo professor, ele está errado em
    pelo menos uma delas — erro de digitação no currículo, ou metadado herdado
    de um coautor pela fusão de duplicatas do scriptLattes. Deixar como está
    funde publicações distintas numa linha só, e a sobrevivente ainda passa a
    alegar, em `fontes`, uma cobertura que não tem.

    A checagem é **dentro de uma mesma fonte**, e isso é essencial: fontes
    diferentes escrevem o mesmo título de formas diferentes (o Lattes em
    português, a Scopus em inglês), e casar essas variações é justamente o
    trabalho do DOI. Comparar títulos entre fontes marcaria como conflito o
    caso normal, e partiria ao meio publicações corretamente unificadas.

    Quando um DOI é contestado, o desempate é a corroboração: se algum título
    aparece com aquele DOI em mais de uma fonte, é ele que responde pelo DOI, e
    as demais linhas caem no casamento por título. Se nenhum título é
    corroborado, só as linhas da(s) fonte(s) em conflito perdem o DOI — as
    fontes que o usam de forma consistente continuam com ele.

    Em qualquer caso, no máximo um título por professor continua respondendo
    por um dado DOI. Isso é obrigatório: a chave final de um grupo que tenha
    DOI é `id_lattes|DOI:<doi>`, então dois grupos que guardassem o mesmo DOI
    receberiam a mesma chave e voltariam a se fundir na redução.
    """
    titulos_por_fonte_doi = {}
    fontes_por_doi_titulo = {}
    for posicao in posicoes:
        doi = dois[posicao]
        if pd.isna(doi):
            continue
        titulos_por_fonte_doi.setdefault((fontes[posicao], doi), set()).add(titulos[posicao])
        fontes_por_doi_titulo.setdefault((doi, titulos[posicao]), set()).add(fontes[posicao])

    contestados = {
        doi for (_, doi), titulos_vistos in titulos_por_fonte_doi.items()
        if len(titulos_vistos) > 1
    }
    if not contestados:
        return

    for doi in contestados:
        corroborados = sorted(
            titulo for (doi_visto, titulo), fontes_vistas in fontes_por_doi_titulo.items()
            if doi_visto == doi and len(fontes_vistas) > 1
        )
        # `sorted` só para o desempate ser determinístico quando, excepcional-
        # mente, mais de um título tiver corroboração para o mesmo DOI.
        vencedor = corroborados[0] if corroborados else None
        fontes_em_conflito = {
            fonte for (fonte, doi_visto), titulos_vistos in titulos_por_fonte_doi.items()
            if doi_visto == doi and len(titulos_vistos) > 1
        }
        for posicao in posicoes:
            if pd.isna(dois[posicao]) or dois[posicao] != doi:
                continue
            if vencedor is not None:
                if titulos[posicao] != vencedor:
                    dois[posicao] = pd.NA
            elif fontes[posicao] in fontes_em_conflito:
                dois[posicao] = pd.NA


def _agrupar_posicoes_por_professor(professores):
    """Posições das linhas agrupadas por professor. Tanto a deduplicação
    quanto o saneamento do DOI acontecem sempre dentro de um único professor."""
    posicoes_por_professor = {}
    for posicao, professor in enumerate(professores):
        posicoes_por_professor.setdefault(professor, []).append(posicao)
    return posicoes_por_professor


def _dois_confiaveis(df):
    """DOI normalizado de cada linha, com <NA> onde ele não pode identificar a
    publicação — seja porque não tem forma de DOI (`normalizar_doi`), seja
    porque a própria fonte o usa em mais de uma publicação do professor
    (`_neutralizar_dois_contestados`).

    É a única fonte de verdade sobre "este DOI vale?": a chave de deduplicação
    e o saneamento da coluna `doi` gravada saem daqui, então os dois nunca
    discordam sobre a mesma linha."""
    dois = df['doi'].map(normalizar_doi).tolist()
    titulos = df['titulo_artigo'].map(normalizar_titulo_dedup).tolist()
    fontes = df['fonte'].astype(str).tolist()
    professores = df['id_lattes'].astype(str).tolist()
    for posicoes in _agrupar_posicoes_por_professor(professores).values():
        _neutralizar_dois_contestados(dois, titulos, fontes, posicoes)
    return dois


def calcular_chave_dedup(df):
    """Atribui a cada linha a chave da publicação a que ela pertence.

    Regra fundamental: a deduplicação acontece **dentro da lista de cada
    professor**, nunca entre professores. As linhas são agrupadas por
    `id_lattes` e o casamento (por DOI ou por título) só é procurado dentro
    do grupo; a chave gerada carrega o `id_lattes` como prefixo, então duas
    linhas de professores diferentes nunca podem cair na mesma chave. Se dois
    professores do quadro coassinaram o mesmo artigo, cada um mantém a sua
    própria linha daquele artigo — o que se elimina é apenas a repetição
    dentro da lista de um mesmo professor.

    Dentro do grupo de um professor, duas linhas são a mesma publicação se:
      - têm o mesmo DOI normalizado (comparação exata), OU
      - têm o mesmo título normalizado (comparação exata).
    Nenhum limiar de similaridade é usado em nenhum dos dois critérios.

    Antes de casar, `_neutralizar_dois_contestados` retira do jogo os DOIs que
    uma mesma fonte atribui a mais de um título deste professor — DOI errado no
    currículo não pode fundir publicações distintas.

    Os dois critérios são combinados por união (componentes conexos) porque
    fontes diferentes preenchem campos diferentes: se o Lattes trouxe o
    título sem DOI e o Scopus trouxe o mesmo título com DOI, é o título que
    liga as duas linhas; e se uma terceira linha do ORCID só tem o DOI, é o
    DOI que a liga à do Scopus. As três acabam no mesmo grupo.

    A chave final de cada grupo é `id_lattes|DOI:<doi>` quando alguma linha do
    grupo tem DOI (é o identificador mais forte) e `id_lattes|TIT:<titulo>`
    caso contrário. Linhas sem DOI e sem título não casam com nada e recebem
    uma chave própria (`id_lattes|LINHA:<n>`), para não serem fundidas entre si.
    """
    total = len(df)
    if total == 0:
        return pd.Series([], dtype='object', index=df.index)

    # Já vem com <NA> nos DOIs que não identificam a publicação.
    dois = _dois_confiaveis(df)
    titulos = df['titulo_artigo'].map(normalizar_titulo_dedup).tolist()
    professores = df['id_lattes'].astype(str).tolist()

    # Cada professor é deduplicado isoladamente, sem enxergar as linhas dos demais.
    posicoes_por_professor = _agrupar_posicoes_por_professor(professores)

    chaves = [None] * total

    for professor, posicoes in posicoes_por_professor.items():
        pai = {posicao: posicao for posicao in posicoes}

        def encontrar(x):
            while pai[x] != x:
                pai[x] = pai[pai[x]]
                x = pai[x]
            return x

        def unir(a, b):
            raiz_a, raiz_b = encontrar(a), encontrar(b)
            if raiz_a != raiz_b:
                pai[raiz_b] = raiz_a

        # Uma passada só: cada linha se une à primeira linha do professor que
        # tenha o mesmo DOI e à primeira que tenha o mesmo título.
        primeira_com_doi = {}
        primeira_com_titulo = {}
        for posicao in posicoes:
            doi = dois[posicao]
            if not pd.isna(doi):
                unir(primeira_com_doi.setdefault(doi, posicao), posicao)
            titulo = titulos[posicao]
            if titulo:
                unir(primeira_com_titulo.setdefault(titulo, posicao), posicao)

        # DOI representante de cada grupo (o menor, para ser determinístico
        # caso um grupo tenha sido ligado por título e traga dois DOIs).
        doi_do_grupo = {}
        for posicao in posicoes:
            doi = dois[posicao]
            if pd.isna(doi):
                continue
            raiz = encontrar(posicao)
            if raiz not in doi_do_grupo or doi < doi_do_grupo[raiz]:
                doi_do_grupo[raiz] = doi

        for posicao in posicoes:
            raiz = encontrar(posicao)
            if raiz in doi_do_grupo:
                identificador = f'DOI:{doi_do_grupo[raiz]}'
            elif titulos[raiz]:
                identificador = f'TIT:{titulos[raiz]}'
            else:
                identificador = f'LINHA:{raiz}'
            chaves[posicao] = f'{professor}|{identificador}'

    return pd.Series(chaves, index=df.index, dtype='object')


# ---------------------------------------------------------------------------
# 3. Saneamento da coluna `doi` gravada
# ---------------------------------------------------------------------------
COLUNAS_DOI_DESCARTADO = [
    'tipo', 'id_lattes', 'fonte', 'titulo_artigo', 'ano', 'doi_descartado', 'motivo',
]
MOTIVO_SEM_FORMATO = 'não tem forma de DOI'
MOTIVO_CONTESTADO = 'a mesma fonte usa este DOI em mais de uma publicação do professor'


def sanear_doi_gravado(df, rotulo, coluna_ano):
    """Reescreve a coluna `doi` e devolve `(df saneado, relatório dos descartes)`.

    Duas coisas acontecem aqui:

    1. **Descarte.** O que não identifica aquela publicação vira <NA>. Desfazer
       o vínculo na deduplicação não bastaria: o valor errado continuaria
       gravado, apareceria como "DOI" nos relatórios e na página do docente, e
       faria a auditoria acusar "mesmo DOI para o mesmo professor" —
       corretamente, já que o campo de fato repetiria.

    2. **Canonização.** O que sobrevive é gravado na forma normalizada. O campo
       vindo do Lattes é texto livre e chega em formatos muito diferentes para
       o mesmo DOI ("http://dx.doi.org/10.1007/x", "doi: 10.1007/X",
       "http://www.scopus.com/...&doi=10.1007/x&md5=..."). Como a coluna é
       exibida ao usuário, gravar sempre "10.1007/x" deixa a base legível e
       consistente — e igual ao que a deduplicação de fato usou para casar.

    O valor original não se perde: vai para o relatório, que é o material
    acionável — é com ele que o docente localiza e corrige a entrada no próprio
    currículo.
    """
    if df.empty:
        return df, pd.DataFrame(columns=COLUNAS_DOI_DESCARTADO)

    normalizados = df['doi'].map(normalizar_doi).tolist()
    confiaveis = _dois_confiaveis(df)
    originais = df['doi'].tolist()
    identificadores = df['id_lattes'].astype(str).tolist()
    fontes = df['fonte'].astype(str).tolist()
    titulos = df['titulo_artigo'].tolist()
    anos = df[coluna_ano].tolist()

    descartes = []
    saneados = []
    for posicao in range(len(df)):
        if not pd.isna(confiaveis[posicao]):
            saneados.append(confiaveis[posicao])  # forma canônica
            continue
        saneados.append(pd.NA)
        original = originais[posicao]
        if _e_nulo(original) or not str(original).strip():
            continue  # já era vazio: não há nada a relatar
        descartes.append({
            'tipo': rotulo,
            'id_lattes': identificadores[posicao],
            'fonte': fontes[posicao],
            'titulo_artigo': titulos[posicao],
            'ano': anos[posicao],
            'doi_descartado': original,
            'motivo': MOTIVO_SEM_FORMATO if pd.isna(normalizados[posicao]) else MOTIVO_CONTESTADO,
        })

    df_saneado = df.copy()
    df_saneado['doi'] = saneados
    df_relatorio = (
        pd.DataFrame(descartes)[COLUNAS_DOI_DESCARTADO] if descartes
        else pd.DataFrame(columns=COLUNAS_DOI_DESCARTADO)
    )
    return df_saneado, df_relatorio


# ---------------------------------------------------------------------------
# 4. Redução: uma linha por publicação por fonte, e uma linha por publicação
# ---------------------------------------------------------------------------
# Lattes é a fonte curada manualmente pelo próprio professor; Scopus tem
# metadados bibliográficos mais limpos que o resumo público do ORCID. A base de
# comparação só tem LATTES, e as demais entradas simplesmente não são usadas lá.
ORDEM_PRIORIDADE_FONTE = {'LATTES': 0, 'SCOPUS': 1, 'ORCID': 2}


def deduplicar_bruto_por_fonte(df, chave='chave_dedup'):
    """Garante uma única linha por (publicação, fonte) na base bruta — que é
    a base propagada de volta para as tabelas por fonte. Sem isso, uma
    repetição vinda da própria extração (o mesmo artigo listado duas vezes
    no currículo Lattes, por exemplo) sobreviveria até as tabelas usadas nos
    relatórios. Mantém a primeira ocorrência, respeitando a ordem de
    prioridade das fontes."""
    if df.empty:
        return df
    df_ordenado = df.copy()
    df_ordenado['_prioridade'] = df_ordenado['fonte'].map(ORDEM_PRIORIDADE_FONTE).fillna(9)
    df_ordenado = df_ordenado.sort_values('_prioridade', kind='stable')
    return (
        df_ordenado.drop_duplicates(subset=['fonte', chave], keep='first')
        .drop(columns=['_prioridade'])
        .reset_index(drop=True)
    )


def unificar_com_dedup(df, chave='chave_dedup'):
    """Reduz a base bruta (uma linha por publicação por fonte) à base final
    (uma linha por publicação de cada professor). Cada coluna recebe o
    primeiro valor não nulo encontrado no grupo, na ordem de prioridade das
    fontes, e a coluna `fontes` registra todas as bases em que aquela
    publicação foi encontrada — é ela que permite, depois, apontar em quais
    bases o artigo está faltando."""
    if df.empty:
        return pd.DataFrame(columns=[c for c in df.columns if c != 'fonte'] + ['fontes'])

    df_ordenado = df.copy()
    df_ordenado['_prioridade'] = df_ordenado['fonte'].map(ORDEM_PRIORIDADE_FONTE).fillna(9)
    df_ordenado = df_ordenado.sort_values('_prioridade', kind='stable')

    colunas_dado = [c for c in df.columns if c not in (chave, 'fonte', '_prioridade')]
    linhas_unicas = df_ordenado.drop_duplicates(subset=[chave], keep='first')[[chave] + colunas_dado].copy()

    for coluna in colunas_dado:
        if not linhas_unicas[coluna].isna().any():
            continue
        preenchimento = (
            df_ordenado.dropna(subset=[coluna])
            .drop_duplicates(subset=[chave], keep='first')[[chave, coluna]]
            .rename(columns={coluna: '_preenchimento'})
        )
        linhas_unicas = linhas_unicas.merge(preenchimento, on=chave, how='left')
        linhas_unicas[coluna] = linhas_unicas[coluna].fillna(linhas_unicas['_preenchimento'])
        linhas_unicas = linhas_unicas.drop(columns=['_preenchimento'])

    fontes_por_publicacao = (
        df.groupby(chave)['fonte']
        .apply(lambda serie: ','.join(sorted(set(serie.dropna()))))
        .rename('fontes')
        .reset_index()
    )
    return linhas_unicas.merge(fontes_por_publicacao, on=chave, how='left').reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5. Segunda passada: percorre as listas de novo procurando deslizes
# ---------------------------------------------------------------------------
def auditar_duplicatas(df_unificado, df_bruto, rotulo):
    """Confere (sem alterar nada) se o resultado bate com os requisitos:

    1. nenhum professor tem o mesmo DOI repetido na base unificada;
    2. nenhum professor tem o mesmo título repetido na base unificada;
    3. nenhuma chave se repete na base unificada;
    4. nenhuma linha se repete dentro da mesma fonte na base bruta (é ela que
       vira as tabelas por fonte);
    5. nenhuma chave é compartilhada por dois professores diferentes — ou
       seja, a coautoria entre professores do quadro não fundiu ninguém.
    """
    if df_unificado.empty:
        print(f"  OK ({rotulo}): nenhuma publicação nesta base -- nada para auditar.")
        return

    doi_norm = df_unificado['doi'].map(normalizar_doi)
    titulo_norm = df_unificado['titulo_artigo'].map(normalizar_titulo_dedup)
    professor = df_unificado['id_lattes'].astype(str)

    dup_doi = pd.concat([professor, doi_norm], axis=1).duplicated(keep=False) & doi_norm.notna()
    dup_titulo = (professor + '|' + titulo_norm).duplicated(keep=False) & (titulo_norm != '')
    dup_chave = df_unificado['chave_dedup'].duplicated(keep=False)
    dup_fonte = df_bruto.duplicated(subset=['fonte', 'chave_dedup'], keep=False)
    chaves_multiprofessor = (
        df_unificado.groupby('chave_dedup')['id_lattes'].nunique().gt(1).sum()
    )

    total_problemas = (
        int(dup_doi.sum()) + int(dup_titulo.sum()) + int(dup_chave.sum())
        + int(dup_fonte.sum()) + int(chaves_multiprofessor)
    )
    if total_problemas == 0:
        print(f"  OK ({rotulo}): nenhum professor tem artigo repetido, nem na base "
              f"unificada nem nas bases por fonte; nenhuma chave cruzou professores.")
        return

    if dup_doi.any():
        print(f"  AVISO ({rotulo}): {int(dup_doi.sum())} linha(s) com o mesmo DOI para o mesmo professor:")
        display(df_unificado[dup_doi][['id_lattes', 'titulo_artigo', 'doi', 'chave_dedup', 'fontes']]
                .sort_values(['id_lattes', 'doi']))
    if dup_titulo.any():
        print(f"  AVISO ({rotulo}): {int(dup_titulo.sum())} linha(s) com o mesmo título para o mesmo professor:")
        display(df_unificado[dup_titulo][['id_lattes', 'titulo_artigo', 'doi', 'chave_dedup', 'fontes']]
                .sort_values(['id_lattes', 'titulo_artigo']))
    if dup_chave.any():
        print(f"  AVISO ({rotulo}): {int(dup_chave.sum())} linha(s) com chave_dedup repetida na base unificada.")
    if dup_fonte.any():
        print(f"  AVISO ({rotulo}): {int(dup_fonte.sum())} linha(s) repetida(s) dentro da mesma fonte (base bruta):")
        display(df_bruto[dup_fonte][['id_lattes', 'fonte', 'titulo_artigo', 'doi', 'chave_dedup']]
                .sort_values(['id_lattes', 'fonte', 'titulo_artigo']))
    if chaves_multiprofessor:
        print(f"  AVISO ({rotulo}): {int(chaves_multiprofessor)} chave(s) compartilhada(s) por professores diferentes.")


# ---------------------------------------------------------------------------
# 6. A exceção: agrupar o mesmo paper ENTRE docentes
# ---------------------------------------------------------------------------
# Tudo acima deduplica dentro de um único professor -- `calcular_chave_dedup`
# prefixa a chave com o `id_lattes` justamente para nunca fundir currículos, e
# é isso que mantém respondível a pergunta "quais artigos este professor tem?".
#
# Estas duas funções fazem o contrário, e existem para as perguntas em que a
# unidade é o paper e não o par (professor, paper):
#
#   * "quantos papers distintos o programa produziu com discentes?"
#     (relatório de coautoria discente); e
#   * "qual paper vai para qual docente?" (página de Alocação Ótima), em que um
#     paper coassinado por dois docentes do quadro só pode ser usado uma vez.
#
# O casamento continua sendo **exato**, nunca por similaridade, e reaproveita as
# mesmas normalizações canônicas: mesmo DOI normalizado OU mesmo título
# normalizado no mesmo ano. Os dois critérios são combinados por componentes
# conexos porque o mesmo paper costuma vir com DOI no registro de um docente e
# sem DOI no de outro -- sem a união, essas linhas não se encontrariam.
#
# Fundir por DOI é seguro aqui porque `sanear_doi_gravado` já apagou da coluna
# os DOIs que uma fonte usa em mais de uma publicação do mesmo docente; o que
# sobrou identifica publicação.
def agrupar_papers_entre_docentes(df, coluna_titulo='titulo_artigo',
                                  coluna_ano='ano', coluna_doi='doi'):
    """Rotula cada linha com o paper a que ela pertence, atravessando docentes.

    Devolve uma Series alinhada ao índice de `df` com um código inteiro por
    paper distinto: duas linhas com o mesmo código são o mesmo paper, ainda que
    sejam de professores diferentes."""
    if df.empty:
        return pd.Series([], dtype='int64', index=df.index)

    # A coluna de DOI pode faltar (bases antigas): sem ela sobra o casamento por
    # título+ano, que é o que o resto do módulo já faz quando o DOI é nulo.
    if coluna_doi in df.columns:
        dois = [normalizar_doi(v) for v in df[coluna_doi]]
    else:
        dois = [pd.NA] * len(df)
    titulos = [normalizar_titulo_dedup(v) for v in df[coluna_titulo]]
    anos = list(df[coluna_ano])

    pai = list(range(len(df)))

    def raiz(x):
        while pai[x] != x:
            pai[x] = pai[pai[x]]
            x = pai[x]
        return x

    def unir(a, b):
        ra, rb = raiz(a), raiz(b)
        if ra != rb:
            pai[rb] = ra

    primeiro_doi, primeiro_titulo = {}, {}
    for i in range(len(df)):
        if not _e_nulo(dois[i]):
            unir(primeiro_doi.setdefault(dois[i], i), i)
        if titulos[i]:
            unir(primeiro_titulo.setdefault((titulos[i], anos[i]), i), i)

    return pd.Series([raiz(i) for i in range(len(df))], index=df.index, dtype='int64')


def escolher_representantes(df, grupos, coluna_doi='doi'):
    """Posições (para `df.iloc`) de uma linha por paper, em ordem crescente.

    Prefere como representante de cada grupo a primeira linha que tem DOI — é o
    registro mais completo —, e cai na primeira linha do grupo quando nenhuma
    tem."""
    if df.empty:
        return []

    if coluna_doi in df.columns:
        tem_doi = [not _e_nulo(normalizar_doi(v)) for v in df[coluna_doi]]
    else:
        tem_doi = [False] * len(df)
    codigos = list(grupos)

    representante = {}
    for i in range(len(df)):
        atual = representante.get(codigos[i])
        if atual is None or (not tem_doi[atual] and tem_doi[i]):
            representante[codigos[i]] = i
    return sorted(representante.values())
