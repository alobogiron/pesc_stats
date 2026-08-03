# syntax=docker/dockerfile:1
#
# Imagem única para todo o pipeline: Streamlit (apresentação), scriptLattes +
# Selenium/Chromium (extração) e papermill (reprocessamento dos notebooks).
#
# Por que um container só: o app dispara a extração e o reprocessamento com
# subprocess.Popen (jobs.py) dentro do próprio processo/filesystem. Separar em
# dois containers exigiria trocar isso por fila/API — mudança de arquitetura,
# não de empacotamento. O mesmo image serve para rodar os jobs avulsos:
#   docker compose run --rm app python run_extract.py
#
# Python 3.13 = mesma versão do venv usado no host (requirements.txt tem
# pandas 3 / numpy 2.4 pinados, que só têm wheel para cp313 em diante).
FROM python:3.13-slim-trixie

# Chromium e chromedriver vêm do repositório do Debian de propósito: os dois
# pacotes são versionados juntos, então nunca dá o clássico "This version of
# ChromeDriver only supports Chrome version X". Nada de baixar driver em build
# nem de depender do binário local scriptlattes/chromedriver (que é pareado com
# o Chrome do host e é ignorado dentro da imagem via CHROMEDRIVER_PATH).
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        fonts-liberation \
        ca-certificates \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Consumido por scriptlattes/scriptLattes/baixaLattes.py:create_driver
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    # --no-sandbox: sem isso o Chrome não sobe em container (não há como criar
    #   o sandbox de processos); --disable-dev-shm-usage: /dev/shm padrão do
    #   Docker tem 64 MB e o Chrome trava ao renderizar páginas grandes (o
    #   compose ainda aumenta o shm_size, mas a flag protege quem rodar
    #   `docker run` na mão); --disable-gpu/--window-size: headless estável.
    CHROME_EXTRA_ARGS="--no-sandbox --disable-dev-shm-usage --disable-gpu --window-size=1920,1080"

WORKDIR /app

# Dependências primeiro, em camada própria: mudar código não reinstala nada.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Usuário não-root com UID/GID configuráveis. Importante com bind mount: os
# arquivos que o container cria em dados_brutos/ e os .duckdb ficam com o dono
# certo no host (padrão 1000, que é o usuário comum em desktop Linux).
ARG UID=1000
ARG GID=1000
RUN groupadd -g "${GID}" app 2>/dev/null || true \
    && useradd -u "${UID}" -g "${GID}" -m -s /bin/bash app 2>/dev/null || true \
    && mkdir -p /home/app /app \
    && chown -R "${UID}:${GID}" /home/app /app

# Código da aplicação. Com o bind mount do docker-compose isto fica coberto
# pelo diretório do host; sem o mount, a imagem roda sozinha (aí use
# PESC_DATA_DIR + volumes para os dados persistirem).
COPY --chown=${UID}:${GID} . /app

# HOME precisa ser gravável: pybliometrics (Scopus) grava config/cache em
# ~/.config e ~/.cache na primeira inicialização, e o Chromium usa ~/.cache.
ENV HOME=/home/app
USER ${UID}:${GID}

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

# tini como PID 1: o app cria processos filhos (scriptLattes, papermill,
# chromedriver/chromium) e sem um init de verdade os zumbis se acumulam.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
