"""Gera `dados_brutos/credenciamento_professores.csv` com anos de credenciamento
ALEATÓRIOS para cada professor de `lista_pessoas.csv`.

Provisório por natureza: o arquivo de destino é o insumo real do pipeline (lido
pela Seção 15 de `analyse_organizado.ipynb`), e a intenção é que um dia ele seja
mantido à mão / exportado do registro administrativo do programa. Enquanto esse
registro não existe, este script produz um arquivo com a forma certa e valores
plausíveis, para que a regra de vigência possa ser desenvolvida e conferida.

Como os anos são sorteados, rodar de novo com a mesma `--seed` devolve
exatamente o mesmo arquivo -- os números do app não mudam sozinhos entre duas
execuções. Trocar a seed (ou os limites) reembaralha tudo.

Uso:
    python gerar_credenciamento_aleatorio.py                  # sobrescreve o CSV
    python gerar_credenciamento_aleatorio.py --seed 7
    python gerar_credenciamento_aleatorio.py --piso 2013 --teto 2026
"""

import argparse
import csv
import random

import credenciamento as cred

ARQUIVO_LISTA_PESSOAS = "lista_pessoas.csv"
ARQUIVO_SAIDA = "dados_brutos/credenciamento_professores.csv"

# Ano mais antigo que um credenciamento sorteado pode alcançar. Existe porque
# vários docentes ingressaram nos anos 70/90 e uma vigência que começasse lá
# geraria dezenas de milhares de linhas sem nenhum uso analítico -- as janelas
# olhadas no app são sempre recentes.
PISO_PADRAO = 2010
# Ano mais recente. Acompanha o último ano com publicação na base.
TETO_PADRAO = 2027

SEED_PADRAO = 20260825

# Probabilidades dos desvios em relação ao caso comum ("credenciado desde o
# ingresso e ainda vigente"). Servem só para que o CSV sintético exercite os
# casos que a regra de vigência precisa saber tratar.
PROB_ENTRADA_TARDIA = 0.25   # credenciou-se alguns anos depois de ingressar
PROB_DESCREDENCIADO = 0.20   # saiu do quadro antes do fim da janela
PROB_LACUNA = 0.25           # descredenciou e recredenciou no meio do caminho


def sortear_anos(rng, data_ingresso, piso, teto):
    """Sorteia a lista de anos de credenciamento de um docente.

    `data_ingresso` pode ser nulo/ilegível (o CSV é preenchido à mão); nesse
    caso o piso é usado como início possível. O retorno é sempre ordenado e
    sem repetições, e pode ser vazio -- um docente que ingressou depois do
    teto simplesmente não tem nenhum ano vigente na janela coberta.
    """
    try:
        ingresso = int(data_ingresso)
    except (TypeError, ValueError):
        ingresso = piso

    inicio = max(ingresso, piso)
    if inicio > teto:
        return []

    if rng.random() < PROB_ENTRADA_TARDIA:
        inicio = min(inicio + rng.randint(1, 4), teto)

    fim = teto
    if rng.random() < PROB_DESCREDENCIADO and fim - inicio >= 3:
        fim = rng.randint(inicio + 1, teto - 1)

    anos = set(range(inicio, fim + 1))

    # A lacuna só é sorteada quando sobra intervalo suficiente para que ela
    # fique estritamente no meio -- tirar anos da borda seria apenas mudar
    # início ou fim, que os sorteios acima já cobrem.
    if rng.random() < PROB_LACUNA and fim - inicio >= 4:
        tamanho = rng.randint(1, min(3, fim - inicio - 2))
        comeco_lacuna = rng.randint(inicio + 1, fim - tamanho)
        anos -= set(range(comeco_lacuna, comeco_lacuna + tamanho))

    return sorted(anos)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=SEED_PADRAO,
                        help=f"semente do sorteio (padrão: {SEED_PADRAO})")
    parser.add_argument("--piso", type=int, default=PISO_PADRAO,
                        help=f"ano mais antigo possível (padrão: {PISO_PADRAO})")
    parser.add_argument("--teto", type=int, default=TETO_PADRAO,
                        help=f"ano mais recente possível (padrão: {TETO_PADRAO})")
    parser.add_argument("--entrada", default=ARQUIVO_LISTA_PESSOAS,
                        help=f"CSV de professores (padrão: {ARQUIVO_LISTA_PESSOAS})")
    parser.add_argument("--saida", default=ARQUIVO_SAIDA,
                        help=f"CSV de destino (padrão: {ARQUIVO_SAIDA})")
    args = parser.parse_args()

    if args.piso > args.teto:
        parser.error(f"--piso ({args.piso}) não pode ser maior que --teto ({args.teto}).")

    with open(args.entrada, newline="", encoding="utf-8") as f:
        pessoas = list(csv.DictReader(f))

    rng = random.Random(args.seed)
    # Ordem estável (por id_lattes) antes de sortear: assim reordenar ou
    # acrescentar linhas em lista_pessoas.csv não reembaralha quem já estava lá.
    pessoas.sort(key=lambda p: str(p.get("id_lattes", "")).strip())

    linhas = []
    for pessoa in pessoas:
        id_lattes = str(pessoa.get("id_lattes", "")).strip()
        if not id_lattes:
            continue
        anos = sortear_anos(rng, pessoa.get("data_ingresso"), args.piso, args.teto)
        linhas.append({
            "id_lattes": id_lattes,
            "nome_referencia": str(pessoa.get("nome_referencia", "")).strip(),
            "anos": anos,
        })

    # A escrita passa por `credenciamento.py`, dono do formato do arquivo, para
    # que este gerador não seja um segundo lugar onde o layout do CSV é decidido.
    cred.escrever_csv_credenciamento(args.saida, linhas)

    total_anos = sum(len(l["anos"]) for l in linhas)
    print(f"{args.saida}: {len(linhas)} docentes, {total_anos} anos de credenciamento "
          f"(seed={args.seed}, janela {args.piso}-{args.teto}).")


if __name__ == "__main__":
    main()
