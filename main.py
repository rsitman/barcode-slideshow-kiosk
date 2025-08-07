import configparser
import requests
import time
import subprocess
import logging
import os
import json
import shutil
import hashlib
from urllib.parse import urljoin
from evdev import InputDevice, ecodes, list_devices
import select
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import threading
from threading import Lock
from concurrent.futures import ThreadPoolExecutor

# --- KONFIGURACE A GLOBÁLNÍ STAV ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(level=logging.INFO, format='%(asctime)s.%(msecs)03d - %(levelname)s - %(funcName)s - %(message)s', datefmt='%H:%M:%S',
                    handlers=[logging.FileHandler(os.path.join(BASE_DIR, "app.log")), logging.StreamHandler()])
TEMP_IMAGE_DIR = os.path.join(BASE_DIR, "temp_images")
STATE = {"chromium_process": None, "barcode_reader_device": None, "accumulated_chars": [], "last_scan_time": 0, "last_barcode_data": "", "is_shift_pressed": False}
PROCESSING_LOCK = Lock()
DEVICE_FINGERPRINT = None
EVDEV_KEY_MAPS = {
    "normal": {'KEY_0':'0','KEY_1':'1','KEY_2':'2','KEY_3':'3','KEY_4':'4','KEY_5':'5','KEY_6':'6','KEY_7':'7','KEY_8':'8','KEY_9':'9','KEY_A':'a','KEY_B':'b','KEY_C':'c','KEY_D':'d','KEY_E':'e','KEY_F':'f','KEY_G':'g','KEY_H':'h','KEY_I':'i','KEY_J':'j','KEY_K':'k','KEY_L':'l','KEY_M':'m','KEY_N':'n','KEY_O':'o','KEY_P':'p','KEY_Q':'q','KEY_R':'r','KEY_S':'s','KEY_T':'t','KEY_U':'u','KEY_V':'v','KEY_W':'w','KEY_X':'x','KEY_Y':'y','KEY_Z':'z','KEY_MINUS':'-','KEY_EQUAL':'=','KEY_SPACE':' ','KEY_SLASH':'/','KEY_DOT':'.',},
    "shift": {'KEY_3':'#','KEY_A':'A','KEY_B':'B','KEY_C':'C','KEY_D':'D','KEY_E':'E','KEY_F':'F','KEY_G':'G','KEY_H':'H','KEY_I':'I','KEY_J':'J','KEY_K':'K','KEY_L':'L','KEY_M':'M','KEY_N':'N','KEY_O':'O','KEY_P':'P','KEY_Q':'Q','KEY_R':'R','KEY_S':'S','KEY_T':'T','KEY_U':'U','KEY_V':'V','KEY_W':'W','KEY_X':'X','KEY_Y':'Y','KEY_Z':'Z','KEY_MINUS':'_','KEY_EQUAL':'+',}
}

# --- FUNKCE ---

def load_config():
    config = configparser.ConfigParser(); config_path = os.path.join(BASE_DIR, 'config.ini')
    if not os.path.exists(config_path): logging.critical(f"Config soubor {config_path} nenalezen!"); raise FileNotFoundError()
    config.read(config_path); return config

def get_cpu_serial():
    try:
        with open('/proc/cpuinfo', 'r') as f:
            for line in f:
                if line.startswith('Serial'):
                    serial = line.split(':')[1].strip(); logging.info(f"Nalezeno sériové číslo CPU: {serial}"); return serial
    except Exception as e: logging.error(f"Chyba při čtení sériového čísla CPU: {e}")
    logging.error("Sériové číslo CPU nenalezeno v /proc/cpuinfo."); return None

def generate_device_fingerprint():
    serial_number = get_cpu_serial()
    if not serial_number: logging.critical("Nelze získat sériové číslo CPU."); return None
    PEPPER = "ChangeThisToYourOwnSecretPepperString_v1"
    data_to_hash = f"{serial_number}-{PEPPER}"
    fingerprint = hashlib.sha256(data_to_hash.encode('utf-8')).hexdigest()
    logging.info("Úspěšně vygenerován otisk prstu zařízení."); logging.debug(f"Fingerprint: {fingerprint}")
    return fingerprint

def run_web_server(config):
    port = config.getint('WebServer', 'Port', fallback=8000)
    server_address = ('localhost', port)
    class CustomHTTPRequestHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs): super().__init__(*args, directory=BASE_DIR, **kwargs)
        def log_message(self, format, *args): logging.debug(f"HTTP Server: {format % args}")
    httpd = ThreadingHTTPServer(server_address, CustomHTTPRequestHandler)
    logging.info(f"Spouštím lokální web server na http://{server_address[0]}:{server_address[1]}")
    httpd.serve_forever()

def update_image_list_json(slide_data, config, message=""):
    json_file_path = config.get('Slideshow', 'ImageListJsonFile', fallback=os.path.join(BASE_DIR, 'image_list.json'))
    delay_seconds = config.getint('Slideshow', 'DelaySeconds', fallback=5)
    data_to_write = {"slides": slide_data, "timestamp": int(time.time() * 1000), "message": message, "delaySeconds": delay_seconds}
    try:
        with open(json_file_path, 'w', encoding='utf-8') as f: json.dump(data_to_write, f, ensure_ascii=False, indent=4)
        logging.info(f"JSON soubor aktualizován s {len(slide_data)} položkami. Zpráva: '{message}'")
    except Exception as e: logging.error(f"Chyba při zápisu do JSON: {e}")

def launch_chromium_viewer(config):
    port = config.getint('WebServer', 'Port', fallback=8000)
    viewer_html_name = os.path.basename(config.get('Slideshow', 'ViewerHtmlFile', fallback='viewer.html'))
    viewer_html_uri = f"http://localhost:{port}/{viewer_html_name}"
    chromium_args = ["chromium-browser","--start-fullscreen","--kiosk","--noerrdialogs","--disable-infobars",
                     "--disable-features=TranslateUI","--disable-translate","--no-first-run","--fast-start",
                     "--disable-session-crashed-bubble","--window-position=0,0",viewer_html_uri]
    logging.info(f"Spouštím Chromium (X11 mode): {' '.join(chromium_args)}")
    try:
        env = os.environ.copy()
        if 'DISPLAY' not in env: logging.warning("Chybí DISPLAY, nastavuji na ':0'"); env['DISPLAY'] = ':0'
        STATE["chromium_process"] = subprocess.Popen(chromium_args, env=env)
        logging.info(f"Chromium spuštěno s PID: {STATE['chromium_process'].pid}")
    except Exception as e: logging.error(f"Chyba při spouštění Chromia: {e}", exc_info=True); STATE["chromium_process"] = None

def call_api(api_url, barcode, api_key=None):
    global DEVICE_FINGERPRINT
    config = load_config()
    headers = {'x-api-key': api_key} if api_key else {}
    fingerprint_header = config.get('API', 'FingerprintHeader', fallback='x-device-fingerprint')
    if DEVICE_FINGERPRINT and fingerprint_header:
        headers[fingerprint_header] = DEVICE_FINGERPRINT
    params = {'ean': barcode}
    logging.info(f"Volání API: {api_url}")
    try:
        response = requests.get(api_url, params=params, headers=headers, timeout=10)
        response.raise_for_status(); return response.json().get('attachments', [])
    except requests.exceptions.RequestException as e: logging.error(f"Chyba sítě API: {e}"); return None
    except ValueError: logging.error(f"Chyba parsování JSON z API."); return []

def initialize_barcode_reader(config):
    target_phys_path = config.get('Scanner', 'DevicePhysPath', fallback=None)
    target_name_keyword = config.get('Scanner', 'DeviceNameKeyword', fallback=None)
    if not target_phys_path or not target_name_keyword:
        logging.error("Chybí 'DevicePhysPath' nebo 'DeviceNameKeyword' v config.ini!")
        return False
    try:
        devices = [InputDevice(p) for p in list_devices()]
    except Exception as e:
        logging.error(f"Chyba výpisu zařízení: {e}"); return False

    found_device = None
    logging.info(f"Hledám čtečku s Phys: '{target_phys_path}'")
    for dev in devices:
        if dev.phys == target_phys_path:
            if target_name_keyword.lower() in dev.name.lower():
                found_device = dev
                logging.info(f"Nalezeno zařízení podle Phys: {dev.name}")
                break
            else:
                logging.warning(f"Zařízení na cestě '{target_phys_path}' má nesprávné jméno: '{dev.name}'.")
    
    if not found_device:
        logging.warning(f"Zařízení na cestě '{target_phys_path}' nenalezeno. Hledám podle jména '{target_name_keyword}'.")
        potential_devices = [dev for dev in devices if target_name_keyword.lower() in dev.name.lower()]
        if len(potential_devices) == 1:
            found_device = potential_devices[0]
            logging.info(f"Nalezeno 1 zařízení podle jména: {found_device.name}")
        elif len(potential_devices) > 1:
            logging.warning(f"Nalezeno více zařízení ({len(potential_devices)}) odpovídajících jménu. Používám první.")
            found_device = potential_devices[0]
        else:
            logging.error(f"Nenalezeno žádné zařízení odpovídající Phys ani jménu.")
            return False
            
    if found_device:
        try:
            found_device.grab(); STATE["barcode_reader_device"] = found_device
            logging.info(f"Zařízení '{found_device.name}' grabnuto."); return True
        except Exception as e:
            logging.warning(f"Nepodařilo se 'grabnout' {found_device.name}: {e}.")
            try:
                STATE["barcode_reader_device"] = InputDevice(found_device.path)
                logging.info(f"Zařízení '{found_device.name}' použito (bez grab)."); return True
            except Exception as e_open:
                logging.error(f"Nepodařilo se ani otevřít {found_device.name}: {e_open}")
    return False

def download_image(args):
    i, att, api_base_url, headers = args
    url, text = att.get('url'), att.get('text', '')
    if not url: logging.warning(f"Položka neobsahuje 'url': {att}"); return None
    abs_url = urljoin(api_base_url, url) if api_base_url and not url.startswith('http') else url
    try:
        img_res = requests.get(abs_url, headers=headers, timeout=20)
        img_res.raise_for_status()
        content_type = img_res.headers.get('Content-Type','image/jpeg')
        ext = content_type.split('/')[-1].split(';')[0].replace('jpeg', 'jpg')
        if ext == 'octet-stream': logging.warning("Server vrátil 'octet-stream', používám '.jpg'."); ext = 'jpg'
        fname = f"image_{i+1}.{ext.lower()}"
        fpath = os.path.join(TEMP_IMAGE_DIR, fname)
        with open(fpath, 'wb') as f: f.write(img_res.content)
        if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
            logging.info(f"Stažen obrázek: {fname}"); return {"url": f"temp_images/{fname}", "text": text}
        else: logging.error(f"Soubor {fpath} se nepodařilo uložit."); return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Chyba stahování obrázku {abs_url}: {e}"); return None

def download_and_prepare_slides(attachments, api_cfg):
    global DEVICE_FINGERPRINT
    headers = {'x-api-key': api_cfg.get('ApiKey', fallback=None)}
    fingerprint_header = api_cfg.get('FingerprintHeader', fallback='x-device-fingerprint')
    if DEVICE_FINGERPRINT: headers[fingerprint_header] = DEVICE_FINGERPRINT
    logging.info(f"Zahajuji paralelní stahování {len(attachments)} obrázků...")
    if os.path.exists(TEMP_IMAGE_DIR): shutil.rmtree(TEMP_IMAGE_DIR)
    os.makedirs(TEMP_IMAGE_DIR)
    api_base_url = api_cfg.get('BaseUrl', fallback=None)
    tasks = [(i, att, api_base_url, headers) for i, att in enumerate(attachments)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = executor.map(download_image, tasks)
    slides = [res for res in results if res is not None]
    msg = ""
    if not slides and attachments: msg = "Žádný z obrázků se nepodařilo stáhnout."
    return slides, msg

def handle_scan_in_background(barcode):
    if not PROCESSING_LOCK.acquire(blocking=False):
        logging.warning(f"Zpracování stále běží. Sken '{barcode}' bude ignorován."); return
    try:
        logging.info(f"Zahajuji zpracování na pozadí: {barcode}")
        config = load_config()
        update_image_list_json([], config, f"Načítám data pro kód: '{barcode}'...")
        api_cfg = config['API']
        attachments = call_api(api_cfg.get('Url'), barcode, api_cfg.get('ApiKey', fallback=None))
        slides, msg = [], ""
        if attachments is None: msg = "Chyba připojení k API."
        elif not attachments: msg = f"Pro kód '{barcode}' nebyly nalezeny žádné přílohy."
        else: slides, msg = download_and_prepare_slides(attachments, api_cfg)
        update_image_list_json(slides, config, msg)
    finally:
        PROCESSING_LOCK.release()
        logging.info(f"Zpracování na pozadí dokončeno: {barcode}")

def process_barcode_data(barcode):
    now = time.time()
    debounce_seconds = load_config().getfloat('Scanner', 'DebounceSeconds', fallback=2.0)
    if not barcode.strip(): return
    if barcode == STATE["last_barcode_data"] and (now - STATE["last_scan_time"]) < debounce_seconds:
        logging.info(f"Duplicitní sken '{barcode}', ignoruji."); return
    STATE["last_scan_time"], STATE["last_barcode_data"] = now, barcode
    thread = threading.Thread(target=handle_scan_in_background, args=(barcode,)); thread.start()

def read_from_barcode_reader_loop():
    if not STATE["barcode_reader_device"]: time.sleep(2); return
    try:
        r, w, x = select.select([STATE["barcode_reader_device"].fd], [], [], 0.1)
        if not r: return
        for event in STATE["barcode_reader_device"].read():
            if event.type == ecodes.EV_KEY:
                try: key_code_str = ecodes.KEY[event.code]
                except KeyError: continue
                if key_code_str in ['KEY_LEFTSHIFT', 'KEY_RIGHTSHIFT']:
                    STATE["is_shift_pressed"] = (event.value in [1, 2]); continue
                if event.value == 1:
                    terminator = f"KEY_{load_config().get('Scanner','TerminatorKey', fallback='enter').upper()}"
                    if key_code_str == terminator:
                        process_barcode_data("".join(STATE["accumulated_chars"])); STATE["accumulated_chars"] = []
                        STATE["is_shift_pressed"] = False
                    else:
                        map_to_use = EVDEV_KEY_MAPS["shift"] if STATE["is_shift_pressed"] else EVDEV_KEY_MAPS["normal"]
                        char = map_to_use.get(key_code_str)
                        if char: STATE["accumulated_chars"].append(char)
                        elif key_code_str not in ['KEY_RIGHTALT','KEY_LEFTALT','KEY_LEFTCTRL','KEY_RIGHTCTRL','KEY_CAPSLOCK']:
                            logging.warning(f"Neznámý keycode: {key_code_str}")
    except (BlockingIOError, InterruptedError): pass
    except OSError as e:
        logging.error(f"Chyba OS čtečky (odpojeno?): {e}")
        if STATE["barcode_reader_device"]: STATE["barcode_reader_device"].close()
        STATE["barcode_reader_device"] = None; STATE["accumulated_chars"] = []; STATE["is_shift_pressed"] = False

def main():
    global DEVICE_FINGERPRINT
    logging.info("Aplikace spuštěna.")
    try: config = load_config()
    except FileNotFoundError: return
    DEVICE_FINGERPRINT = generate_device_fingerprint()
    if not DEVICE_FINGERPRINT: logging.critical("Nepodařilo se vytvořit otisk prstu zařízení.")
    if not os.path.exists(TEMP_IMAGE_DIR): os.makedirs(TEMP_IMAGE_DIR)
    web_server_thread = threading.Thread(target=run_web_server, args=(config,), daemon=True); web_server_thread.start()
    time.sleep(1)
    update_image_list_json([], config, "Naskenujte čárový kód...")
    launch_chromium_viewer(config)
    if not initialize_barcode_reader(config): logging.warning("Inicializace čtečky selhala.")
    logging.info("Spouštím hlavní smyčku.")
    try:
        while True:
            if not STATE["barcode_reader_device"]:
                logging.info("Zkouším znovu inicializovat čtečku...")
                if not initialize_barcode_reader(config): time.sleep(5); continue
            read_from_barcode_reader_loop()
            time.sleep(0.02)
    except KeyboardInterrupt: logging.info("Přerušení (Ctrl+C).")
    finally:
        logging.info("Ukončuji aplikaci a čistím zdroje.")
        if os.path.exists(TEMP_IMAGE_DIR): shutil.rmtree(TEMP_IMAGE_DIR)
        if STATE["barcode_reader_device"]:
            try: STATE["barcode_reader_device"].ungrab(); STATE["barcode_reader_device"].close()
            except: pass
        if STATE["chromium_process"] and STATE["chromium_process"].poll() is None:
            logging.info("Zavírám Chromium."); STATE["chromium_process"].terminate()
            try: STATE["chromium_process"].wait(timeout=3)
            except subprocess.TimeoutExpired: STATE["chromium_process"].kill()

if __name__ == "__main__":
    try: main()
    except Exception as e: logging.critical(f"Kritická chyba v __main__: {e}", exc_info=True)
    finally: logging.info("Aplikace definitivně ukončena.")
