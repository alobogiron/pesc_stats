#!/usr/bin/python
# encoding: utf-8

import argparse
import time
import os
import urllib.request, urllib.parse, urllib.error
import platform
import warnings
import re

try:
    import bs4
except ImportError:
    bs4 = None

try:
    from selenium import webdriver
    from selenium.common.exceptions import InvalidArgumentException, TimeoutException, WebDriverException
    from selenium.webdriver.chrome.service import Service
except ImportError:
    webdriver = None
    InvalidArgumentException = None
    TimeoutException = None
    WebDriverException = None
    Service = None

import platform
import warnings
warnings.filterwarnings("ignore")

RESULTS_DIR = os.environ.get('DATA_DIR', 'htmls')
URL = 'http://buscatextual.cnpq.br/buscatextual/preview.do?metodo=apresentar&id={0}'
URL_LATTES_ID10 = 'http://buscatextual.cnpq.br/buscatextual/visualizacv.do?id={0}'
URL_LATTES_ID16 = 'http://lattes.cnpq.br/{0}'


def resolver_chromedriver(padrao=None):
    """[modificação local] Caminho do chromedriver a usar.

    `CHROMEDRIVER_PATH` tem precedência -- é como a imagem Docker aponta para o
    driver do sistema (pareado com o chromium do mesmo pacote Debian). Sem a
    variável, mantém o comportamento antigo: `padrao` se informado, senão
    ./chromedriver relativo ao diretório de trabalho.
    """
    do_ambiente = os.environ.get('CHROMEDRIVER_PATH')
    if do_ambiente:
        return do_ambiente
    if padrao:
        return padrao
    nome = "chromedriver.exe" if platform.system() == 'Windows' else "chromedriver"
    return os.path.abspath(nome)


class LattesRobot:
    def __init__(self, driver_path, results_dir):
        #logging.getLogger('selenium').setLevel(logging.WARNING)
        # [modificação local] resolve pelo ambiente antes de cair no argumento,
        # senão a checagem em initialize() barraria a execução em container
        # (onde não existe ./chromedriver) antes mesmo de create_driver rodar.
        self.driver_path = resolver_chromedriver(driver_path)
        self.results_dir = results_dir
        self.driver = None
        #self.ua = UserAgent()
        self.identifiers = set()
        self.downloaded_identifiers = set()
        self.sleep_time = 4
        self.lid_type = -1
        self.initialize()

    def initialize(self):
        if not os.path.exists(self.driver_path):
            # [modificação local] antes era um exit(1) mudo, que terminava o
            # processo sem dizer o motivo -- na UI virava "falhou" sem log.
            raise RuntimeError(
                f"chromedriver não encontrado em '{self.driver_path}'. "
                "Rode 'make setup-chromedriver' (execução local) ou defina "
                "CHROMEDRIVER_PATH apontando para o binário."
            )

        if not os.path.exists(self.results_dir):
            os.makedirs(self.results_dir)

    def load_codes(self, id_lattes):
        self.identifiers.add(id_lattes)
        self._set_lid_type()

    def check_downloaded_cvs(self):
        self.downloaded_identifiers = {h for h in os.listdir(self.results_dir) if len(h) == self.lid_type}


    def create_driver(self):
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_argument("start-maximized")
        chrome_options.add_argument('--blink-settings=imagesEnabled=false')
        chrome_options.add_argument("headless")

        # [modificação local] Flags extras vindas do ambiente, separadas por
        # espaço. Dentro de container são obrigatórias (--no-sandbox, porque
        # não há usuário privilegiado nem namespaces pro sandbox do Chrome, e
        # --disable-dev-shm-usage, porque /dev/shm do Docker é pequeno demais e
        # o Chrome estoura com "session not created"). Fora do container a
        # variável não existe e nada muda.
        for arg in os.environ.get('CHROME_EXTRA_ARGS', '').split():
            chrome_options.add_argument(arg)

        # [modificação local] Binário do navegador. O chromedriver procura o
        # google-chrome nos caminhos padrão; na imagem Docker o navegador é o
        # chromium do sistema, então o caminho vem explícito por CHROME_BIN.
        chrome_binary = os.environ.get('CHROME_BIN')
        if chrome_binary:
            chrome_options.binary_location = chrome_binary

        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_experimental_option('prefs', {'download.default_directory': self.results_dir})

        # [modificação local] CHROMEDRIVER_PATH tem precedência (na imagem
        # Docker o driver vem do pacote do sistema, pareado com o chromium).
        # Sem a variável, mantém o comportamento antigo: ./chromedriver
        # relativo ao diretório de trabalho.
        chrome_driver_path = resolver_chromedriver(self.driver_path)

        if not os.path.exists(chrome_driver_path):
            raise RuntimeError(
                f"chromedriver não encontrado em '{chrome_driver_path}'. "
                "Rode 'make setup-chromedriver' (execução local) ou defina "
                "CHROMEDRIVER_PATH apontando para o binário."
            )

        service = Service(chrome_driver_path)

        try:
            self.driver = webdriver.Chrome(service=service, options=chrome_options)
        except Exception as e:
            # [modificação local] Antes só imprimia e seguia, deixando
            # self.driver indefinido -- o erro real só aparecia depois, como um
            # AttributeError sem relação aparente. Falhar aqui faz a mensagem
            # chegar inteira ao status do job exibido na UI.
            raise RuntimeError(
                f"Erro ao inicializar o Chrome/chromedriver "
                f"(driver='{chrome_driver_path}', binário='{chrome_binary or 'padrão'}'): {e}"
            ) from e


    def collect_html_cvs(self, start, end):
        total_lids = len(list(self.identifiers)[start:end])

        for identifier in sorted(self.identifiers)[start:end]:
            lids = self._get_lids_10_16(identifier)

            if lids[10]:
                if lids[self.lid_type] not in self.downloaded_identifiers:
                    self._execute_js(lids)


    def store_html(self, lid, page):
        with open(os.path.join(self.results_dir, lid), 'wb') as fout:
            try:
                #data = page.encode('iso-8859-1', 'replace').strip()
                data = page.encode('utf-8', 'replace').strip()
            except UnicodeEncodeError:
                data = page.encode('utf-8').strip()

            if data:
                fout.write(data)
        

    def _execute_js(self, lids):
        self.driver.get(URL.format(lids[10]))
        time.sleep(self.sleep_time)

        cmd_open_cv = 'abreCV()'
        self.driver.execute_script(cmd_open_cv)
        time.sleep(self.sleep_time)

        self.driver.switch_to.window(self.driver.window_handles[-1])

        if not lids[16]:
            lids[16] = self._extract_lid16(self.driver.page_source)

            if self.lid_type == 16 and len(lids[16]) != 16:
                #logging.error('It was not possible to obtain Lattes identifier with 16 chars for %s' % str(lids))
                return

        self.store_html(lids[self.lid_type], self.driver.page_source)



    def _get_lids_10_16(self, lid):
        lids = {10: '', 16: ''}

        if len(lid) == 10:
            lids[10] = lid

        if len(lid) == 16:
            lids[16] = lid

            self.driver.get(URL_LATTES_ID16.format(lid))
            lid10 = urllib.parse.parse_qs(urllib.parse.urlparse(self.driver.current_url.encode()).query)[b'id'][0].decode('utf-8')

            if len(lid10) == 10:
                lids[10] = lid10

        return lids

    def _extract_lid16(self, page_source):
        if bs4:
            soup = bs4.BeautifulSoup(page_source, 'html.parser')
            lid16 = soup.find('span', attrs={'style': 'font-weight: bold; color: #326C99;'}).text.encode()
        else:
            match = re.search(r'<span style="font-weight: bold; color: #326C99;">(.*?)</span>', page_source)
            if match:
                lid16 = match.group(1).encode()
            else:
                return None

        if len(lid16) == 16 and lid16.isdigit():
            return lid16

    def _set_lid_type(self):
        if len(self.identifiers) > 0:
            ld = len(list(self.identifiers)[0])
            self.lid_type = ld


def __get_data(id_lattes, diretorio):
    rob = LattesRobot(driver_path="./chromedriver", results_dir=diretorio)
    print(f"Baixando CV Lattes: {id_lattes}. Este processo pode demorar alguns segundos.")
    rob.load_codes(id_lattes)
    rob.check_downloaded_cvs()
    rob.create_driver()

    try:
        #logging.info('Collecting cvs (there are %d cvs to be collected)...' % len(rob.identifiers))
        rob.collect_html_cvs(0, None)
    #except KeyboardInterrupt:
    #    logging.info('Execution was interrupted')
    finally:
        # [modificação local] se create_driver falhou, self.driver é None --
        # chamar .quit() aqui trocaria o erro real por um AttributeError.
        if rob.driver is not None:
            rob.driver.quit()


def baixaCVLattes(id_lattes, diretorio ):
    # caso nao for baixado, tenta novamente ate 5 vezes
    count = 5
    while count>0:
        __get_data(id_lattes, diretorio)

        if os.path.exists ( diretorio+"/"+id_lattes ):
            break
        else:
            count = count - 1

    #raise Exception("Nao foi possivel baixar o CV Lattes em 5 tentativas")
    
def baixaCVLattes(id_lattes, diretorio):
    max_tentativas = 5
    tentativas = 0

    while tentativas < max_tentativas:
        # se passou sem lançar exception, verifica se o arquivo foi salvo
        destino = os.path.join(diretorio, id_lattes)
        if os.path.exists(destino):
            return

        try:
            __get_data(id_lattes, diretorio)
        except WebDriverException as e:
            if 'ERR_CONNECTION_REFUSED' in str(e):
                print(f"Connection refused ao baixar {id_lattes}: dormindo 5 minutos antes de tentar novamente...")
                time.sleep(300)  # 5 minutos
                print(f"Retomando tentativa de download do CV Lattes: {id_lattes}")
                # não conta essa como tentativa falha, volta ao início do loop
                continue
            else:
                # qualquer outro erro, relança
                raise

        # se passou sem lançar exception, verifica se o arquivo foi salvo
        destino = os.path.join(diretorio, id_lattes)
        if os.path.exists(destino):
            return
        else:
            tentativas += 1
            print(f"Tentativa #{tentativas} falhou para {id_lattes}. Restam {max_tentativas - tentativas} tentativas.")

    # se esgotou as tentativas sem sucesso
    raise Exception(f"Não foi possível baixar o CV Lattes de {id_lattes} após {max_tentativas} tentativas.")




