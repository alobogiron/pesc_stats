"""Regra única dos **anos de credenciamento** dos docentes.

`tb_professores.data_ingresso` responde "a partir de quando a produção deste
docente conta", o que só descreve bem quem entrou no programa e nunca mais
saiu. O credenciamento real é mais fino: cada docente tem um conjunto de anos
em que esteve credenciado, com descredenciamento, recredenciamento posterior e
lacunas no meio. Este módulo lê esse conjunto de
`dados_brutos/credenciamento_professores.csv` e o materializa em
`tb_credenciamento_anos(id_lattes, ano)` -- uma linha por par (docente, ano).

Vive em módulo, e não dentro do notebook, pelo mesmo motivo de
`dedup_publicacoes.py`: a regra é usada em dois lugares e não pode divergir
entre eles --

  * a Seção 15 de `analyse_organizado.ipynb`, no reprocessamento completo; e
  * a CLI aqui embaixo (`python credenciamento.py`), que aplica a tabela a um
    `.duckdb` já pronto quando só o CSV mudou e não faz sentido pagar as ~2h
    do pipeline inteiro.

O casamento com o cadastro é **sempre exato, pelo `id_lattes`** -- a coluna
`nome_referencia` do CSV existe só para leitura humana na hora de editar a
planilha à mão.
"""

import argparse

import duckdb
import pandas as pd

# Insumo padrão. Enquanto o registro administrativo oficial do programa não
# existir, é gerado com anos ALEATÓRIOS por `gerar_credenciamento_aleatorio.py`.
CAMINHO_CSV_PADRAO = "dados_brutos/credenciamento_professores.csv"

NOME_TABELA = "tb_credenciamento_anos"

# Separador dos anos dentro da célula `anos_credenciamento` ("2019;2020;2023").
SEPARADOR_ANOS = ";"

DDL_TABELA = f"""
    CREATE TABLE IF NOT EXISTS {NOME_TABELA} (
        id_lattes VARCHAR,
        ano INTEGER,
        PRIMARY KEY (id_lattes, ano),
        FOREIGN KEY (id_lattes) REFERENCES tb_professores(id_lattes)
    );
"""

# Uma linha por docente cadastrado (inclusive quem não tem nenhum ano vigente),
# para conferir a carga de relance.
SQL_RESUMO = f"""
    SELECT
        p.nome_completo AS docente,
        p.data_ingresso AS ingresso,
        COUNT(c.ano) AS anos_vigentes,
        MIN(c.ano) AS primeiro_ano,
        MAX(c.ano) AS ultimo_ano
    FROM tb_professores p
    LEFT JOIN {NOME_TABELA} c ON p.id_lattes = c.id_lattes
    GROUP BY p.nome_completo, p.data_ingresso
    ORDER BY anos_vigentes DESC, docente
"""


def ler_csv_credenciamento(caminho_csv, ids_professores):
    """Lê o CSV de credenciamento e devolve `(df, ids_desconhecidos, anos_invalidos)`.

    `df` tem as colunas `id_lattes` e `ano`, uma linha por par, ordenado e sem
    repetições -- o mesmo ano duas vezes para o mesmo docente é ruído de
    digitação, não um segundo credenciamento.

    `ids_professores` é o conjunto de `id_lattes` do cadastro. Como a tabela é
    filha de `tb_professores` (FOREIGN KEY), um id fora dele não pode entrar;
    ele volta em `ids_desconhecidos` para ser reportado, porque quase sempre é
    erro de digitação na planilha. `anos_invalidos` recolhe, do mesmo jeito, o
    que não pôde ser lido como ano.

    Um docente listado com a célula de anos vazia é legítimo (ainda não
    credenciado, ou já descredenciado antes da janela coberta) e simplesmente
    não gera linhas.
    """
    registros = []
    ids_desconhecidos = []
    anos_invalidos = []

    df_csv = pd.read_csv(caminho_csv, dtype=str)
    df_csv["id_lattes"] = df_csv["id_lattes"].astype(str).str.strip()

    for _, linha in df_csv.iterrows():
        id_lattes = linha["id_lattes"]
        if not id_lattes or id_lattes == "nan":
            continue
        if id_lattes not in ids_professores:
            ids_desconhecidos.append((id_lattes, linha.get("nome_referencia")))
            continue

        bruto = linha.get("anos_credenciamento")
        if pd.isna(bruto) or not str(bruto).strip():
            continue

        for pedaco in str(bruto).split(SEPARADOR_ANOS):
            pedaco = pedaco.strip()
            if not pedaco:
                continue
            try:
                ano = int(pedaco)
            except ValueError:
                anos_invalidos.append((id_lattes, pedaco))
                continue
            registros.append({"id_lattes": id_lattes, "ano": ano})

    df = pd.DataFrame(registros, columns=["id_lattes", "ano"])
    if not df.empty:
        df = (
            df.drop_duplicates(subset=["id_lattes", "ano"])
            .sort_values(["id_lattes", "ano"])
            .reset_index(drop=True)
        )
        df["ano"] = df["ano"].astype("Int64")

    return df, ids_desconhecidos, anos_invalidos


def formatar_avisos(ids_desconhecidos, anos_invalidos, caminho_csv=CAMINHO_CSV_PADRAO):
    """Traduz as anomalias devolvidas por `ler_csv_credenciamento` em linhas de
    texto. Existe para que notebook e CLI reclamem exatamente a mesma coisa."""
    avisos = []
    if ids_desconhecidos:
        avisos.append(
            f"AVISO: {len(ids_desconhecidos)} id_lattes de '{caminho_csv}' não existe(m) "
            "em tb_professores e ficaram de fora:"
        )
        avisos.extend(f"  - {id_lattes} ({nome})" for id_lattes, nome in ids_desconhecidos)
    if anos_invalidos:
        avisos.append(
            f"AVISO: {len(anos_invalidos)} ano(s) ilegível(is) em '{caminho_csv}', "
            f"descartado(s): {anos_invalidos}"
        )
    return avisos


def persistir_credenciamento(con, df_credenciamento_anos):
    """Cria (se preciso) e recarrega `tb_credenciamento_anos` numa conexão
    DuckDB aberta para escrita. A carga é sempre completa: o CSV é a verdade,
    então o conteúdo anterior sai inteiro antes da inserção."""
    con.execute(DDL_TABELA)
    con.execute(f"DELETE FROM {NOME_TABELA}")

    if not df_credenciamento_anos.empty:
        # O DataFrame é referenciado pelo nome na query (replacement scan do
        # DuckDB), então precisa estar visível como variável local.
        df_para_inserir = df_credenciamento_anos  # noqa: F841 -- usado pelo DuckDB
        con.execute(
            f"INSERT INTO {NOME_TABELA} (id_lattes, ano) "
            "SELECT id_lattes, ano FROM df_para_inserir"
        )


def aplicar_em_duckdb(caminho_duckdb, caminho_csv=CAMINHO_CSV_PADRAO):
    """Aplica o CSV a um `.duckdb` já existente, sem passar pelo pipeline
    inteiro. Devolve `(df_resumo, avisos)`."""
    con = duckdb.connect(caminho_duckdb)
    try:
        ids_professores = {
            str(linha[0])
            for linha in con.execute("SELECT id_lattes FROM tb_professores").fetchall()
        }
        df, ids_desconhecidos, anos_invalidos = ler_csv_credenciamento(
            caminho_csv, ids_professores
        )
        persistir_credenciamento(con, df)
        resumo = con.execute(SQL_RESUMO).df()
    finally:
        con.close()

    return resumo, formatar_avisos(ids_desconhecidos, anos_invalidos, caminho_csv)


def main():
    parser = argparse.ArgumentParser(
        description="Aplica os anos de credenciamento do CSV a um banco .duckdb já existente."
    )
    parser.add_argument("--db", required=True, help="caminho do arquivo .duckdb")
    parser.add_argument("--csv", default=CAMINHO_CSV_PADRAO,
                        help=f"CSV de credenciamento (padrão: {CAMINHO_CSV_PADRAO})")
    args = parser.parse_args()

    resumo, avisos = aplicar_em_duckdb(args.db, args.csv)
    for aviso in avisos:
        print(aviso)

    total_anos = int(resumo["anos_vigentes"].sum()) if not resumo.empty else 0
    com_vigencia = int((resumo["anos_vigentes"] > 0).sum()) if not resumo.empty else 0
    print(
        f"'{NOME_TABELA}' atualizada em '{args.db}': {total_anos} par(es) (docente, ano) "
        f"para {com_vigencia} de {len(resumo)} docente(s) cadastrado(s)."
    )
    print(resumo.to_string(index=False))


if __name__ == "__main__":
    main()
