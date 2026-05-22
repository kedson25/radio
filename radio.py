import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime

from flask import Flask, Response, abort, jsonify, send_file, send_from_directory
import requests
import yt_dlp


logging.getLogger("werkzeug").setLevel(logging.ERROR)


API_URL = "https://api.ecooy.com.br/1P-BO0xSfQqU3TlkPfZg7gQc8r9vbUIFlnD0obwKK9W4/musicas"

# Pasta onde este radio.py esta salvo
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Salva musicas, erros, cookies e estado em um caminho Linux.
# Por padrao usa a pasta do projeto; em systemd pode ser sobrescrito por RADIO_BASE_DIR.
BASE_DIR = os.environ.get("RADIO_BASE_DIR", SCRIPT_DIR)

PASTA_MUSICAS = os.path.join(BASE_DIR, "musicas")
PASTA_ERROS = os.path.join(BASE_DIR, "erros")
COOKIES_FILE = os.environ.get("RADIO_COOKIES_FILE", os.path.join(BASE_DIR, "cookies.txt"))
ESTADO_FILE = os.path.join(BASE_DIR, "radio_estado.json")

FFPROBE_EXE = shutil.which("ffprobe")
FFMPEG_EXE = shutil.which("ffmpeg")
NODE_EXE = shutil.which("node")
DENO_EXE = shutil.which("deno")
BUN_EXE = shutil.which("bun")

JS_RUNTIME = os.environ.get("RADIO_JS_RUNTIME", "").strip().lower()
JS_RUNTIME_PATH = os.environ.get("RADIO_JS_RUNTIME_PATH", "").strip()
if not JS_RUNTIME:
    if NODE_EXE:
        JS_RUNTIME = "node"
        JS_RUNTIME_PATH = NODE_EXE
    elif DENO_EXE:
        JS_RUNTIME = "deno"
        JS_RUNTIME_PATH = DENO_EXE
    elif BUN_EXE:
        JS_RUNTIME = "bun"
        JS_RUNTIME_PATH = BUN_EXE

INTERVALO_BUSCA = 10
INTERVALO_API = 3
PRE_DOWNLOAD_SEGUNDOS = 10
STREAM_CHUNK_SIZE = 64 * 1024
WEB_HOST = "0.0.0.0"
WEB_PORT = 8000

tocadas = set()
erros = set()
estado_lock = threading.Lock()
download_lock = threading.Lock()
pre_download_lock = threading.Lock()
skip_event = threading.Event()
ultima_lista_api = []
duracoes = {}
pre_baixando = set()
estado_persistido = {
    "dia_atual": None,
    "ultima_tocada": None,
    "tocadas": [],
    "erros": [],
}
estado_radio = {
    "playing": False,
    "id": None,
    "title": None,
    "requested_by": None,
    "path": None,
    "started_at": None,
    "duration": None,
}

app = Flask(__name__, static_folder="static", static_url_path="/static")


def log(texto):
    """
    Imprime mensagens no log do systemd.
    Se o terminal não aceitar algum caractere, ele troca por ?.
    """
    print(texto, flush=True)


def preparar_pastas():
    os.makedirs(PASTA_MUSICAS, exist_ok=True)
    os.makedirs(PASTA_ERROS, exist_ok=True)


def hoje_iso():
    return datetime.now().date().isoformat()


def obter_caminho_estado_para_leitura():
    if os.path.exists(ESTADO_FILE):
        return ESTADO_FILE

    return None


def parse_data_pedido(valor):
    texto = str(valor or "").strip()
    if not texto:
        return None

    texto = texto.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(texto).date()
    except ValueError:
        pass

    formatos = (
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%d/%m/%y %H:%M:%S",
        "%d/%m/%y %H:%M",
        "%d/%m/%y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    )
    for formato in formatos:
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue

    return None


def item_eh_de_hoje(item):
    data_pedido = parse_data_pedido(item.get("Carimbo de data/hora", ""))
    return data_pedido is None or data_pedido == datetime.now().date()


def filtrar_lista_do_dia(lista):
    return [item for item in lista if item_eh_de_hoje(item)]


def limpar_estado_para_hoje():
    tocadas.clear()
    erros.clear()
    duracoes.clear()
    with pre_download_lock:
        pre_baixando.clear()
    with estado_lock:
        ultima_lista_api.clear()

    estado_persistido.update(
        {
            "dia_atual": hoje_iso(),
            "ultima_tocada": None,
            "tocadas": [],
            "erros": [],
        }
    )


def garantir_estado_do_dia():
    hoje = hoje_iso()
    dia_salvo = estado_persistido.get("dia_atual")
    if dia_salvo == hoje:
        return

    if dia_salvo:
        log(f"[NOVO DIA] Limpando fila salva de {dia_salvo}. Aguardando pedidos de {hoje}.")
    limpar_estado_para_hoje()
    salvar_estado_persistido()


def carregar_estado_persistido():
    caminho_estado = obter_caminho_estado_para_leitura()
    if not caminho_estado:
        estado_persistido["dia_atual"] = hoje_iso()
        return

    try:
        with open(caminho_estado, "r", encoding="utf-8") as f:
            dados = json.load(f)
    except Exception as e:
        log(f"[AVISO] Nao consegui ler estado salvo: {e}")
        estado_persistido["dia_atual"] = hoje_iso()
        return

    hoje = hoje_iso()
    dia_salvo = dados.get("dia_atual")
    if not dia_salvo:
        ultima_salva = dados.get("ultima_tocada") or {}
        data_ultima = parse_data_pedido(ultima_salva.get("played_at"))
        dia_salvo = data_ultima.isoformat() if data_ultima else hoje

    if dia_salvo != hoje:
        log(f"[NOVO DIA] Estado salvo era de {dia_salvo}; iniciando limpo em {hoje}.")
        limpar_estado_para_hoje()
        return

    estado_persistido["dia_atual"] = hoje

    ids_tocadas = dados.get("tocadas", [])
    if isinstance(ids_tocadas, list):
        tocadas.update(str(id_musica) for id_musica in ids_tocadas)

    ids_erros = dados.get("erros", [])
    if isinstance(ids_erros, list):
        erros.update(str(id_musica) for id_musica in ids_erros)

    estado_persistido["ultima_tocada"] = dados.get("ultima_tocada")
    estado_persistido["tocadas"] = list(tocadas)
    estado_persistido["erros"] = list(erros)

    ultima = estado_persistido["ultima_tocada"] or {}
    if ultima.get("title"):
        log(f"[RETOMANDO] Ultima musica salva: {ultima['title']} em {ultima.get('played_at', '')}")


def salvar_estado_persistido():
    estado_persistido["dia_atual"] = hoje_iso()
    dados = {
        "dia_atual": estado_persistido["dia_atual"],
        "ultima_tocada": estado_persistido["ultima_tocada"],
        "tocadas": list(tocadas)[-1000:],
        "erros": list(erros)[-1000:],
    }
    temporario = f"{ESTADO_FILE}.tmp"

    try:
        with open(temporario, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        os.replace(temporario, ESTADO_FILE)
    except Exception as e:
        log(f"[AVISO] Nao consegui salvar estado da radio: {e}")


def salvar_musica_tocada(id_musica, titulo, nome_pessoa=""):
    tocadas.add(id_musica)
    estado_persistido["dia_atual"] = hoje_iso()
    estado_persistido["ultima_tocada"] = {
        "id": id_musica,
        "title": titulo,
        "requested_by": nome_pessoa,
        "played_at": datetime.now().isoformat(timespec="seconds"),
    }
    estado_persistido["tocadas"] = list(tocadas)[-1000:]
    estado_persistido["erros"] = list(erros)[-1000:]
    salvar_estado_persistido()


def marcar_anteriores_como_tocadas(lista):
    ultima = estado_persistido.get("ultima_tocada") or {}
    ultimo_id = ultima.get("id")
    if not ultimo_id:
        return

    ids = [gerar_id(item) for item in lista]
    if ultimo_id not in ids:
        log("[RETOMANDO] Ultima musica salva nao esta mais na API; mantendo historico salvo.")
        return

    for item in lista:
        id_musica = gerar_id(item)
        tocadas.add(id_musica)
        if id_musica == ultimo_id:
            break

    estado_persistido["tocadas"] = list(tocadas)[-1000:]


def normalizar_caminho_musica(caminho):
    caminho_real = os.path.realpath(caminho)
    pasta_real = os.path.realpath(PASTA_MUSICAS)

    if os.path.commonpath([caminho_real, pasta_real]) != pasta_real:
        return None

    return caminho_real


def atualizar_estado_tocando(id_musica, titulo, nome_pessoa, caminho, duracao=None):
    with estado_lock:
        estado_radio.update(
            {
                "playing": True,
                "id": id_musica,
                "title": titulo,
                "requested_by": nome_pessoa,
                "path": caminho,
                "started_at": time.time(),
                "duration": duracao,
            }
        )


def limpar_estado_tocando(id_musica):
    with estado_lock:
        if estado_radio["id"] == id_musica:
            estado_radio.update(
                {
                    "playing": False,
                    "id": None,
                    "title": None,
                    "requested_by": None,
                    "path": None,
                    "started_at": None,
                    "duration": None,
                }
            )


@app.get("/")
def pagina_inicial():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def api_status():
    with estado_lock:
        status = dict(estado_radio)

    status.pop("path", None)

    if status["playing"] and status["started_at"]:
        status["elapsed"] = max(0, time.time() - status["started_at"])
    else:
        status["elapsed"] = 0

    if status["duration"]:
        status["remaining"] = max(0, status["duration"] - status["elapsed"])
    else:
        status["remaining"] = None

    return jsonify(status)


@app.get("/api/queue")
def api_queue():
    garantir_estado_do_dia()

    with estado_lock:
        tocando_id = estado_radio["id"] if estado_radio["playing"] else None
        itens = filtrar_lista_do_dia(list(ultima_lista_api))

    fila = []
    for item in itens:
        musica = montar_item_fila(item)
        if not musica:
            continue

        id_musica = musica["id"]
        if id_musica == tocando_id:
            musica["status"] = "playing"
        elif id_musica in erros:
            musica["status"] = "error"
        elif id_musica in tocadas:
            musica["status"] = "played"
        else:
            musica["status"] = "pending"

        fila.append(musica)

    return jsonify({"items": fila})


@app.post("/api/skip")
def api_skip():
    with estado_lock:
        tocando = estado_radio["playing"]

    if tocando:
        skip_event.set()
        return jsonify({"ok": True, "message": "Pulando musica atual"})

    return jsonify({"ok": False, "message": "Nada tocando agora"}), 409


@app.post("/api/ended/<id_musica>")
def api_ended(id_musica):
    with estado_lock:
        atual = estado_radio["id"]

    if atual == id_musica:
        skip_event.set()
        return jsonify({"ok": True})

    return jsonify({"ok": False}), 409


@app.get("/audio/<id_musica>")
def audio_atual(id_musica):
    with estado_lock:
        caminho = estado_radio["path"] if estado_radio["id"] == id_musica else None

    if not caminho:
        abort(404)

    caminho = normalizar_caminho_musica(caminho)

    if not caminho or not os.path.exists(caminho):
        abort(404)

    return send_file(caminho, mimetype="audio/mpeg", conditional=True)


def estado_stream_atual():
    with estado_lock:
        estado = dict(estado_radio)

    caminho = estado.get("path")
    if not estado.get("playing") or not caminho:
        return None

    caminho = normalizar_caminho_musica(caminho)
    if not caminho or not os.path.exists(caminho):
        return None

    estado["path"] = caminho
    return estado


def mesmo_audio_tocando(id_musica, started_at):
    with estado_lock:
        return (
            estado_radio.get("playing")
            and estado_radio.get("id") == id_musica
            and estado_radio.get("started_at") == started_at
        )


def stream_arquivo_com_ffmpeg(caminho, id_musica, started_at, offset):
    comando = [
        FFMPEG_EXE,
        "-hide_banner",
        "-loglevel",
        "error",
        "-re",
    ]

    if offset > 1:
        comando.extend(["-ss", f"{offset:.3f}"])

    comando.extend(
        [
            "-i",
            caminho,
            "-vn",
            "-f",
            "mp3",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "pipe:1",
        ]
    )

    processo = subprocess.Popen(
        comando,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    try:
        while mesmo_audio_tocando(id_musica, started_at):
            bloco = processo.stdout.read(STREAM_CHUNK_SIZE)
            if not bloco:
                break
            yield bloco
    finally:
        if processo.poll() is None:
            processo.terminate()
            try:
                processo.wait(timeout=2)
            except subprocess.TimeoutExpired:
                processo.kill()


def stream_arquivo_direto(caminho, id_musica, started_at, offset, duracao):
    tamanho = os.path.getsize(caminho)
    inicio = 0

    if duracao and offset > 1:
        inicio = min(tamanho - 1, int((offset / duracao) * tamanho))

    bytes_por_segundo = tamanho / duracao if duracao else 16 * 1024
    atraso = STREAM_CHUNK_SIZE / max(bytes_por_segundo, 1)

    with open(caminho, "rb") as arquivo:
        if inicio > 0:
            arquivo.seek(inicio)

        while mesmo_audio_tocando(id_musica, started_at):
            bloco = arquivo.read(STREAM_CHUNK_SIZE)
            if not bloco:
                break
            yield bloco
            time.sleep(max(0.05, min(atraso, 1.0)))


def gerar_stream_radio():
    ultimo_started_at = None

    while True:
        estado = estado_stream_atual()
        if not estado:
            time.sleep(0.25)
            continue

        id_musica = estado.get("id")
        started_at = estado.get("started_at")
        if not id_musica or not started_at or started_at == ultimo_started_at:
            time.sleep(0.25)
            continue

        caminho = estado["path"]
        duracao = estado.get("duration")
        offset = max(0, time.time() - started_at)
        if duracao:
            offset = min(offset, max(0, duracao - 1))

        ultimo_started_at = started_at

        if FFMPEG_EXE:
            yield from stream_arquivo_com_ffmpeg(caminho, id_musica, started_at, offset)
        else:
            yield from stream_arquivo_direto(caminho, id_musica, started_at, offset, duracao)


@app.get("/stream")
def stream_radio():
    return Response(
        gerar_stream_radio(),
        mimetype="audio/mpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "X-Accel-Buffering": "no",
        },
        direct_passthrough=True,
    )


def iniciar_servidor_web():
    thread = threading.Thread(
        target=lambda: app.run(
            host=WEB_HOST,
            port=WEB_PORT,
            threaded=True,
            use_reloader=False,
        ),
        daemon=True,
    )
    thread.start()
    log(f"Frontend: http://localhost:{WEB_PORT}")


def iniciar_monitor_api():
    thread = threading.Thread(target=monitorar_api, daemon=True)
    thread.start()


def gerar_id(item):
    texto = (
        str(item.get("Carimbo de data/hora", ""))
        + str(item.get("Nome da musica", ""))
        + str(item.get(" Seu nome  ", ""))
    )

    return hashlib.md5(texto.encode("utf-8")).hexdigest()


def montar_item_fila(item):
    nome_musica = str(item.get("Nome da musica", "")).strip()
    nome_pessoa = str(item.get(" Seu nome  ", "")).strip()

    if not nome_musica:
        return None

    return {
        "id": gerar_id(item),
        "title": nome_musica,
        "requested_by": nome_pessoa,
        "timestamp": str(item.get("Carimbo de data/hora", "")).strip(),
    }


def item_tem_musica_pendente(item):
    musica = montar_item_fila(item)
    if not musica:
        return False

    id_musica = musica["id"]
    return id_musica not in tocadas and id_musica not in erros


def existe_musica_pendente():
    return any(item_tem_musica_pendente(item) for item in obter_lista_api_atual())


def buscar_api():
    garantir_estado_do_dia()

    try:
        r = requests.get(API_URL, timeout=15)
        r.raise_for_status()

        resposta = r.json()

        if isinstance(resposta, dict):
            lista = filtrar_lista_do_dia(resposta.get("dados", []))
            with estado_lock:
                ultima_lista_api[:] = lista
            return lista

        if isinstance(resposta, list):
            resposta = filtrar_lista_do_dia(resposta)
            with estado_lock:
                ultima_lista_api[:] = resposta
            return resposta

        return []

    except Exception as e:
        log(f"[ERRO] Erro na API: {e}")
        return []


def obter_lista_api_atual():
    garantir_estado_do_dia()

    with estado_lock:
        return list(ultima_lista_api)


def monitorar_api():
    while True:
        buscar_api()
        time.sleep(INTERVALO_API)


def obter_duracao_arquivo(caminho):
    if not FFPROBE_EXE or not os.path.exists(caminho):
        return None

    try:
        resultado = subprocess.run(
            [
                FFPROBE_EXE,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                caminho,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if resultado.returncode == 0:
            return float(resultado.stdout.strip())
    except Exception:
        return None

    return None


def limpar_download_incompleto(id_musica):
    removidos = []
    prefixo = f"{id_musica}."

    for nome_arquivo in os.listdir(PASTA_MUSICAS):
        if not nome_arquivo.startswith(prefixo):
            continue

        caminho = os.path.join(PASTA_MUSICAS, nome_arquivo)
        if nome_arquivo.endswith(".mp3"):
            continue

        try:
            os.remove(caminho)
            removidos.append(caminho)
        except OSError as e:
            log(f"[AVISO] Nao consegui remover download incompleto {caminho}: {e}")

    for caminho in removidos:
        log(f"[LIMPO] Download incompleto removido: {caminho}")


def mensagem_erro_download(erro):
    texto = str(erro)

    if "No space left on device" in texto or "Errno 28" in texto:
        return (
            "Sem espaco livre para baixar a musica. "
            "Libere espaco no disco ou ajuste RADIO_BASE_DIR."
        )

    return texto


def baixar_musica(nome_musica, id_musica):
    with download_lock:
        return baixar_musica_sem_lock(nome_musica, id_musica)


def baixar_musica_sem_lock(nome_musica, id_musica):
    arquivo_final = os.path.join(PASTA_MUSICAS, f"{id_musica}.mp3")
    limpar_download_incompleto(id_musica)

    if os.path.exists(arquivo_final):
        log(f"[OK] Já baixada: {arquivo_final}")
        duracao = duracoes.get(id_musica) or obter_duracao_arquivo(arquivo_final)
        duracoes[id_musica] = duracao
        return arquivo_final, duracao

    log(f"[DOWNLOAD] Baixando música: {nome_musica}")

    opcoes = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(PASTA_MUSICAS, f"{id_musica}.%(ext)s"),
        "default_search": "ytsearch1",
        "noplaylist": True,
        "quiet": False,
        "ignoreerrors": False,
        "retries": 10,
        "fragment_retries": 10,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    }

    if JS_RUNTIME:
        runtime_config = {}
        if JS_RUNTIME_PATH:
            runtime_config["path"] = JS_RUNTIME_PATH
        opcoes["js_runtimes"] = {JS_RUNTIME: runtime_config}
        log(f"[EJS] Usando JavaScript runtime: {JS_RUNTIME}")
    else:
        log("[AVISO] Nenhum JavaScript runtime encontrado para o yt-dlp.")

    if os.path.exists(COOKIES_FILE):
        opcoes["cookiefile"] = COOKIES_FILE
        log(f"[COOKIES] Usando cookies: {COOKIES_FILE}")

    try:
        with yt_dlp.YoutubeDL(opcoes) as ydl:
            info = ydl.extract_info(nome_musica, download=True)
    except Exception as e:
        limpar_download_incompleto(id_musica)
        raise Exception(f"Erro ao baixar com yt-dlp: {mensagem_erro_download(e)}")

    duracao = None
    if isinstance(info, dict):
        if info.get("entries"):
            entradas = [entrada for entrada in info.get("entries", []) if entrada]
            if entradas:
                duracao = entradas[0].get("duration")
        else:
            duracao = info.get("duration")

    if os.path.exists(arquivo_final):
        log(f"[OK] Download concluído: {arquivo_final}")
        duracao = duracao or obter_duracao_arquivo(arquivo_final)
        duracoes[id_musica] = duracao
        return arquivo_final, duracao

    log("[ERRO] O MP3 não foi gerado.")
    limpar_download_incompleto(id_musica)
    return None, None


def proxima_musica_pendente(id_atual):
    lista = obter_lista_api_atual()
    passou_atual = False

    for item in lista:
        musica = montar_item_fila(item)
        if not musica:
            continue

        id_musica = musica["id"]
        if id_musica == id_atual:
            passou_atual = True
            continue

        if not passou_atual:
            continue

        if id_musica in tocadas or id_musica in erros:
            continue

        return item

    return None


def pre_baixar_musica(item):
    musica = montar_item_fila(item)
    if not musica:
        return

    id_musica = musica["id"]
    with pre_download_lock:
        if id_musica in pre_baixando or id_musica in tocadas or id_musica in erros:
            return
        pre_baixando.add(id_musica)

    try:
        log(f"[PRE-DOWNLOAD] Preparando proxima: {musica['title']}")
        arquivo, _ = baixar_musica(musica["title"], id_musica)
        if arquivo:
            log(f"[PRE-DOWNLOAD] Pronta para tocar: {musica['title']}")
        else:
            salvar_erro(id_musica, musica["title"], "Pre-download nao gerou MP3")
    except Exception as e:
        salvar_erro(id_musica, musica["title"], str(e))
    finally:
        with pre_download_lock:
            pre_baixando.discard(id_musica)


def iniciar_pre_download_proxima(id_atual):
    item = proxima_musica_pendente(id_atual)
    if not item:
        return False

    thread = threading.Thread(target=pre_baixar_musica, args=(item,), daemon=True)
    thread.start()
    return True


def tocar(caminho, titulo, id_musica=None, nome_pessoa="", duracao=None):
    if not caminho or not os.path.exists(caminho):
        log(f"[ERRO] Arquivo não encontrado: {caminho}")
        return

    log(f"[AO VIVO NO SITE] {titulo}")
    if id_musica:
        skip_event.clear()
        atualizar_estado_tocando(id_musica, titulo, nome_pessoa, caminho, duracao)

    try:
        inicio = time.time()
        pre_download_disparado = False
        ultima_tentativa_pre_download = 0
        while True:
            if skip_event.wait(timeout=0.5):
                log(f"[SKIP] Pulou: {titulo}")
                return

            decorrido = time.time() - inicio
            agora = time.time()
            if (
                id_musica
                and duracao
                and not pre_download_disparado
                and duracao - decorrido <= PRE_DOWNLOAD_SEGUNDOS
                and agora - ultima_tentativa_pre_download >= 2
            ):
                ultima_tentativa_pre_download = agora
                pre_download_disparado = iniciar_pre_download_proxima(id_musica)

            if duracao and time.time() - inicio >= duracao:
                log(f"[OK] Finalizou: {titulo}")
                return
    finally:
        if id_musica:
            salvar_musica_tocada(id_musica, titulo, nome_pessoa)
            limpar_estado_tocando(id_musica)


def tocar_pausa():
    log("[AGUARDANDO] Nenhuma música nova...")
    time.sleep(INTERVALO_BUSCA)


def salvar_erro(id_musica, nome_musica, erro):
    erros.add(id_musica)
    estado_persistido["erros"] = list(erros)[-1000:]

    caminho = os.path.join(PASTA_ERROS, f"{id_musica}.txt")

    with open(caminho, "w", encoding="utf-8") as f:
        f.write(f"Música: {nome_musica}\n")
        f.write(f"Erro: {erro}\n")

    log(f"[ERRO SALVO] {caminho}")
    salvar_estado_persistido()


def iniciar():
    preparar_pastas()
    carregar_estado_persistido()
    iniciar_servidor_web()
    lista_inicial = buscar_api()
    marcar_anteriores_como_tocadas(lista_inicial)
    salvar_estado_persistido()
    iniciar_monitor_api()

    log("================================")
    log("RADIO INICIADA NO LINUX")
    log("Lendo API")
    log("Modo: ao vivo no site, sem tocar audio no PC")
    log(f"Pasta músicas: {PASTA_MUSICAS}")
    log(f"Pasta erros: {PASTA_ERROS}")
    log(f"Estado: {ESTADO_FILE}")
    log(f"Dia da fila: {estado_persistido.get('dia_atual') or hoje_iso()}")
    if os.path.exists(COOKIES_FILE):
        log(f"Cookies: {COOKIES_FILE}")
    else:
        log(f"Cookies: nao encontrado ({COOKIES_FILE})")
    log("================================")

    while True:
        lista = obter_lista_api_atual()
        achou_nova = False

        for item in lista:
            nome_musica = str(item.get("Nome da musica", "")).strip()
            nome_pessoa = str(item.get(" Seu nome  ", "")).strip()

            if not nome_musica:
                continue

            id_musica = gerar_id(item)

            if id_musica in tocadas:
                continue

            if id_musica in erros:
                continue

            achou_nova = True

            log("--------------------------------")
            log(f"Pedido: {nome_musica}")
            log(f"Pedido por: {nome_pessoa}")

            try:
                arquivo, duracao = baixar_musica(nome_musica, id_musica)

                if arquivo:
                    tocar(arquivo, nome_musica, id_musica, nome_pessoa, duracao)
                else:
                    salvar_erro(id_musica, nome_musica, "Download não gerou MP3")

            except Exception as e:
                salvar_erro(id_musica, nome_musica, str(e))

        if not achou_nova:
            tocar_pausa()


if __name__ == "__main__":
    iniciar()
