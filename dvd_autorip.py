import os
import subprocess
import time
import ctypes
import shutil
import threading
import re
import urllib.request
import json
import queue
from tqdm import tqdm

# --- KONFIGURATION ---
MAKE_MKV_PATH = r"C:\Program Files (x86)\MakeMKV\makemkvcon.exe"
HANDBRAKE_PATH = r"C:\Tools\HandBrakeCLI.exe"

# Der aktuelle MakeMKV Beta-Key
BETA_KEY = "T-O@rWpXbHvfXvW79b7uX8zXp9zXp9zXp9zXp9zXp9zXp9zXp9zXp9zXp9zXp9zXp9zX"

# Deine Laufwerksbuchstaben
DRIVES = ["D:", "E:"]

# Pfade (Lokaler Temp-Ordner & TrueNAS-Ziel)
OUTPUT_BASE = r"\\TRUENAS\Filme_und_Serien"
TEMP_BASE = r"C:\Temporaere_DVD_Kopie"
MIN_LENGTH = "15"
HANDBRAKE_PRESET = "Movie Standard"

# --- DISCORD CONFIG ---
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1525370900441202728/o1bLGo2UGEnfVdnUDrs7rmWRm-YkvHzg5nPyta9BIzC2cW4XIs7Z9L4r8W7sVwKW2nyk"

# ANSI-Farbcodes fuer die Konsole
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"

# Globale Sperren und Warteschlangen
print_lock = threading.Lock()
handbrake_queues = {drive[0].upper(): queue.Queue() for drive in DRIVES}

def log(drive, message, color=RESET):
    with print_lock:
        print(f"{color}[Laufwerk {drive[0].upper()}:] {message}{RESET}")

def send_discord_notification(disc_name, drive, event_type, error_msg=""):
    if not DISCORD_WEBHOOK_URL or "HIER_DEINE_WEBHOOK" in DISCORD_WEBHOOK_URL:
        return
    drive_letter = drive[0].upper()
    if event_type == "eject":
        title = "💿 DVD eingelesen & Ausgeworfen!"
        color = 3447003
        description = f"Die DVD **'{disc_name}'** wurde erfolgreich eingelesen und **ausgeworfen**. Du kannst eine neue DVD einlegen!\n\n*HandBrake beginnt jetzt im Hintergrund mit der Komprimierung.*"
    elif event_type == "success":
        title = "✅ Konvertierung abgeschlossen!"
        color = 3066993
        description = f"HandBrake hat die Komprimierung für **'{disc_name}'** beendet. Die Dateien sind nun auf dem TrueNAS verfügbar."
    else:
        title = "❌ Fehler aufgetreten!"
        color = 15158332
        description = f"Bei der Verarbeitung von **'{disc_name}'** gab es ein Problem.\n\n**Fehler:** {error_msg}"

    payload = {
        "embeds": [{
            "title": title, "description": description, "color": color,
            "fields": [
                {"name": "Laufwerk", "value": f"Laufwerk {drive_letter}:", "inline": True},
                {"name": "Zielordner", "value": f"`{OUTPUT_BASE}\\{disc_name}`", "inline": False}
            ],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }]
    }
    try:
        req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req): pass
    except Exception as e:
        log(drive, f"Discord-Benachrichtigung konnte nicht gesendet werden: {e}", YELLOW)

def register_makemkv():
    try:
        print("[-] Registriere MakeMKV Beta-Key...")
        subprocess.run([MAKE_MKV_PATH, "reg", BETA_KEY], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[!] Fehler bei der Registrierung: {e}")

def get_mkv_drive_index(drive_letter):
    if drive_letter.upper() == "D:": return "0"
    if drive_letter.upper() == "E:": return "1"
    return "0"

def get_dvd_name(drive):
    kernel32 = ctypes.windll.kernel32
    volume_name_buffer = ctypes.create_unicode_buffer(1024)
    file_system_name_buffer = ctypes.create_unicode_buffer(1024)
    
    success = kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(drive[0] + ":\\"), volume_name_buffer, ctypes.sizeof(volume_name_buffer),
        None, None, None, file_system_name_buffer, ctypes.sizeof(file_system_name_buffer)
    )
    if success and file_system_name_buffer.value in ["UDF", "CDFS"]:
        return volume_name_buffer.value.strip()
    return None

def get_dvd_total_bytes(drive_letter):
    try: free_bytes = ctypes.c_ulonglong(0); total_bytes = ctypes.c_ulonglong(0); total_free_bytes = ctypes.c_ulonglong(0); ctypes.windll.kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(f"{drive_letter}:\\"), ctypes.byref(free_bytes), ctypes.byref(total_bytes), ctypes.byref(total_free_bytes)); return total_bytes.value
    except: return 0

def get_folder_size(folder_path):
    total_size = 0
    if not os.path.exists(folder_path): return 0
    for dirpath, dirnames, filenames in os.walk(folder_path):
        for f in filenames:
            fp = os.path.join(dirpath, f); 
            if os.path.exists(fp): total_size += os.path.getsize(fp)
    return total_size

def eject_drive(drive):
    try:
        drive_letter = drive[0].upper()
        ctypes.windll.winmm.mciSendStringW(f"open {drive_letter}: type cdaudio alias drive{drive_letter}", None, 0, 0)
        ctypes.windll.winmm.mciSendStringW(f"set drive{drive_letter} door open", None, 0, 0)
        ctypes.windll.winmm.mciSendStringW(f"close drive{drive_letter}", None, 0, 0)
    except Exception as e: log(drive, f"Fehler beim Auswerfen: {e}", RED)

def run_makemkv_with_byte_progress(drive_letter, cmd, temp_folder):
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
    total_dvd_size = get_dvd_total_bytes(drive_letter)
    if total_dvd_size == 0: total_dvd_size = 8500000000
    total_mb = int(total_dvd_size / (1024 * 1024))
    
    with print_lock:
        pbar = tqdm(total=total_mb, desc=f"{CYAN}[Laufwerk {drive_letter}:] MakeMKV liest DVD aus{RESET}", unit="MB", bar_format="{desc}: |{bar}| {percentage:3.0f}% [{n_fmt}/{total_fmt} MB, {elapsed}<{remaining}]", leave=True)

    while process.poll() is None:
        time.sleep(1)
        current_bytes = get_folder_size(temp_folder)
        current_mb = int(current_bytes / (1024 * 1024))
        with print_lock:
            if current_mb > pbar.n:
                if current_mb >= total_mb: pbar.n = total_mb - 1
                else: pbar.n = current_mb
                pbar.refresh()
                
    process.wait()
    with print_lock: pbar.n = pbar.total; pbar.refresh(); pbar.close(); print("")
    if process.returncode != 0: raise subprocess.CalledProcessError(process.returncode, cmd)

def run_handbrake_with_progress(drive, cmd, description):
    """Startet HandBrake und fängt den Echtzeit-Prozentwert im Stream stabil ab."""
    # Wir leiten stdout weiter und erzwingen ungepuffertes Lesen (bufsize=0)
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
        text=True, encoding="utf-8", errors="ignore", bufsize=0
    )
    
    with print_lock:
        pbar = tqdm(
            total=100, 
            desc=f"{YELLOW}[Laufwerk {drive[0].upper()}: HB-Queue] {description}{RESET}", 
            bar_format="{desc}: |{bar}| {percentage:3.0f}% [{elapsed}<{remaining}]", 
            leave=True
        )

    last_val = 0
    full_output_log = []
    
    # HandBrake nutzt Carriage Returns (\r), um Zeilen im selben Platz zu aktualisieren.
    # Wir lesen den Datenstrom blockweise, um keine Updates zu verpassen.
    while True:
        # Lese bis zu 128 Zeichen auf einmal aus dem Stream
        chunk = process.stdout.read(64)
        if not chunk:
            break
            
        full_output_log.append(chunk)
        
        # Suche nach Prozentwerten (z.B. "25.40 %" oder " 5.12 %") im aktuellen Chunk
        matches = re.findall(r"(\d+[\.,]\d+)\s*%", chunk)
        if matches:
            try:
                # Nimm den aktuellsten gefundenen Prozentwert aus diesem Block
                pct_float = float(matches[-1].replace(",", "."))
                pct = int(pct_float)
                
                if pct > last_val and pct <= 100:
                    with print_lock:
                        pbar.n = pct
                        pbar.refresh()
                    last_val = pct
            except ValueError:
                pass

    process.wait()
    with print_lock:
        pbar.n = 100
        pbar.refresh()
        pbar.close()
        print("")

    if process.returncode != 0:
        with print_lock:
            print(f"{RED}[Laufwerk {drive[0].upper()}: !!! HANDBRAKE FEHLERLOG !!!]{RESET}")
            # Rekonstruiere den Log-Text für die Fehleranzeige
            log_text = "".join(full_output_log)
            for log_line in log_text.splitlines()[-12:]: 
                print(f"  -> {log_line}")
        raise subprocess.CalledProcessError(process.returncode, cmd)

def handbrake_worker(drive_letter):
    q = handbrake_queues[drive_letter]
    while True:
        task = q.get()
        temp_folder, final_folder, clean_name = task
        
        log(drive_letter, f"HandBrake-Warteschlange gestartet fuer '{clean_name}'...", YELLOW)
        try:
            files = os.listdir(temp_folder)
            for file in files:
                if file.endswith(".mkv"):
                    input_file = os.path.join(temp_folder, file)
                    output_file = os.path.join(final_folder, file)
                    
                    handbrake_cmd = [
                        HANDBRAKE_PATH, "--preset-import-gui",
                        "-i", input_file, "-o", output_file, "--preset", HANDBRAKE_PRESET
                    ]
                    run_handbrake_with_progress(drive_letter, handbrake_cmd, f"{file}")
            
            log(drive_letter, f"HandBrake fertig mit '{clean_name}'!", GREEN)
            send_discord_notification(clean_name, drive_letter, event_type="success")
            
        except subprocess.CalledProcessError as e:
            log(drive_letter, f"HandBrake-Fehler bei '{clean_name}' (Code {e.returncode})", RED)
            send_discord_notification(clean_name, drive_letter, event_type="error", error_msg=f"HandBrake-Fehler Code {e.returncode}")
        except Exception as e:
            log(drive_letter, f"Schwerer Fehler in HB-Warteschlange: {e}", RED)
            send_discord_notification(clean_name, drive_letter, event_type="error", error_msg=str(e))
        finally:
            if os.path.exists(temp_folder):
                shutil.rmtree(temp_folder)
            q.task_done()

def process_dvd(drive, disc_name):
    drive_letter = drive[0].upper()
    log(drive_letter, f"DVD erkannt: '{disc_name}'. Starte MakeMKV...", GREEN)
    
    mkv_index = get_mkv_drive_index(drive_letter + ":")
    clean_name = "".join(c for c in disc_name if c.isalnum() or c in (' ', '_', '-')).strip()
    if not clean_name: clean_name = f"Unbekannte_DVD_{drive_letter}"

    timestamp = time.strftime("%H%M%S")
    temp_folder = os.path.join(TEMP_BASE, f"{drive_letter}_{clean_name}_{timestamp}")
    final_folder = os.path.join(OUTPUT_BASE, clean_name)

    os.makedirs(temp_folder, exist_ok=True)
    os.makedirs(final_folder, exist_ok=True)

    try:
        makemkv_cmd = [MAKE_MKV_PATH, "mkv", f"disc:{mkv_index}", "all", temp_folder, f"--minlength={MIN_LENGTH}"]
        run_makemkv_with_byte_progress(drive_letter, makemkv_cmd, temp_folder)

        log(drive_letter, f"Auslesen beendet. Werfe Disc aus und sende Discord-Meldung...", GREEN)
        eject_drive(drive_letter)
        send_discord_notification(clean_name, drive_letter, event_type="eject")

        handbrake_queues[drive_letter].put((temp_folder, final_folder, clean_name))
        log(drive_letter, f"'{clean_name}' an Hintergrund-Warteschlange uebergeben. Laufwerk ist wieder BEREIT!", GREEN)
        
    except Exception as e:
        log(drive_letter, f"Fehler beim Einlesen: {e}", RED)
        send_discord_notification(clean_name, drive_letter, event_type="error", error_msg=f"MakeMKV-Fehler: {str(e)}")
        if os.path.exists(temp_folder): shutil.rmtree(temp_folder)
        eject_drive(drive_letter)

def drive_worker(drive):
    drive_letter = drive[0].upper()
    active_disc = None
    while True:
        disc_name = get_dvd_name(drive_letter)
        if disc_name and disc_name != active_disc:
            active_disc = disc_name
            process_dvd(drive_letter, disc_name)
        elif not disc_name:
            active_disc = None
        time.sleep(5)

def main():
    os.system('') # Aktiviert ANSI-Farben unter Windows
    print("==============================================")
    print("   Asynchroner DVD-Ripper mit Live-Balken v5 ")
    print("==============================================")
    
    register_makemkv()
    print(f"\nUeberwache {', '.join(DRIVES)} parallel...")
    print("Sowohl MakeMKV als auch HandBrake im Live-Modus.\n")

    for drive in DRIVES:
        dl = drive[0].upper()
        threading.Thread(target=handbrake_worker, args=(dl,), daemon=True).start()

    for drive in DRIVES:
        threading.Thread(target=drive_worker, args=(drive,), daemon=True).start()

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: print("\nProgramm beendet.")

if __name__ == "__main__":
    main()