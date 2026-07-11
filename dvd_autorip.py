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
DRIVES = ["D:", "E"]

# Pfade (Lokaler Temp-Ordner & TrueNAS-Ziel)
OUTPUT_BASE = r"\\TRUENAS\Filme_und_Serien"
TEMP_BASE = r"C:\Temporaere_DVD_Kopie"
MIN_LENGTH = "15"
HANDBRAKE_PRESET = "Movie Standard"

# --- DISCORD CONFIG ---
# FUEGE HIER DEINE KOPIERTE WEBHOOK-URL EIN! (Wenn leer "", wird keine Nachricht gesendet)
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1525370900441202728/o1bLGo2UGEnfVdnUDrs7rmWRm-YkvHzg5nPyta9BIzC2cW4XIs7Z9L4r8W7sVwKW2nyk"

# ANSI-Farbcodes fuer die Konsole
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"

# Globale Sperren und Warteschlangen
print_lock = threading.Lock()
# Jedes Laufwerk bekommt seine eigene HandBrake-Warteschlange
handbrake_queues = {drive[0].upper(): queue.Queue() for drive in DRIVES}

def log(drive, message, color=RESET):
    """Hilfsfunktion fuer saubere Log-Ausgaben pro Laufwerk mit Thread-Sperre."""
    with print_lock:
        print(f"{color}[Laufwerk {drive[0].upper()}:] {message}{RESET}")

def send_discord_notification(disc_name, drive, event_type, error_msg=""):
    """Sendet Benachrichtigungen fuer verschiedene Ereignisse (Auswurf / Fertigstellung / Fehler)."""
    if not DISCORD_WEBHOOK_URL or "HIER_DEINE_WEBHOOK" in DISCORD_WEBHOOK_URL:
        return

    drive_letter = drive[0].upper()
    
    if event_type == "eject":
        title = "💿 DVD eingelesen & Ausgeworfen!"
        color = 3447003  # Blau
        description = f"Die DVD **'{disc_name}'** wurde erfolgreich eingelesen und **ausgeworfen**. Du kannst eine neue DVD einlegen!\n\n*HandBrake beginnt jetzt im Hintergrund mit der Komprimierung.*"
    elif event_type == "success":
        title = "✅ Konvertierung abgeschlossen!"
        color = 3066993  # Gruen
        description = f"HandBrake hat die Komprimierung für **'{disc_name}'** beendet. Die Dateien sind nun auf dem TrueNAS verfügbar."
    else:
        title = "❌ Fehler aufgetreten!"
        color = 15158332  # Rot
        description = f"Bei der Verarbeitung von **'{disc_name}'** gab es ein Problem.\n\n**Fehler:** {error_msg}"

    payload = {
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "fields": [
                {"name": "Laufwerk", "value": f"Laufwerk {drive_letter}:", "inline": True},
                {"name": "Zielordner", "value": f"`{OUTPUT_BASE}\\{disc_name}`", "inline": False}
            ],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }]
    }

    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        )
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
        ctypes.c_wchar_p(drive[0] + ":\\"),
        volume_name_buffer,
        ctypes.sizeof(volume_name_buffer),
        None, None, None,
        file_system_name_buffer,
        ctypes.sizeof(file_system_name_buffer)
    )
    if success and file_system_name_buffer.value in ["UDF", "CDFS"]:
        return volume_name_buffer.value.strip()
    return None

def eject_drive(drive):
    try:
        drive_letter = drive[0].upper()
        ctypes.windll.winmm.mciSendStringW(f"open {drive_letter}: type cdaudio alias drive{drive_letter}", None, 0, 0)
        ctypes.windll.winmm.mciSendStringW(f"set drive{drive_letter} door open", None, 0, 0)
        ctypes.windll.winmm.mciSendStringW(f"close drive{drive_letter}", None, 0, 0)
    except Exception as e:
        log(drive, f"Fehler beim Auswerfen: {e}", RED)

def run_makemkv_with_progress(drive, cmd, description):
    cmd_with_robot = cmd + ["-r"]
    process = subprocess.Popen(cmd_with_robot, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
    
    with print_lock:
        pbar = tqdm(total=100, desc=f"{CYAN}[Laufwerk {drive[0].upper()}:] {description}{RESET}", bar_format="{desc}: {elapsed} vergangen (Warte auf Sektoren-Scan...)", leave=True)

    last_val = 0
    has_procent_mode = False
    
    def ticker():
        while not has_procent_mode and process.poll() is None:
            time.sleep(1)
            if not has_procent_mode:
                with print_lock: pbar.update(0)

    threading.Thread(target=ticker, daemon=True).start()

    for line in iter(process.stdout.readline, ""):
        if "PRGV" in line:
            match = re.search(r"PRGV:(\d+),(\d+)", line)
            if match:
                try:
                    current = int(match.group(1))
                    total = int(match.group(2))
                    if total > 0:
                        if not has_procent_mode:
                            has_procent_mode = True
                            with print_lock: pbar.bar_format = "{desc}: |{bar}| {percentage:3.0f}% [{elapsed}<{remaining}]"
                        pct = int((current / total) * 100)
                        if pct > last_val and pct <= 100:
                            with print_lock: pbar.update(pct - last_val)
                            last_val = pct
                except ValueError: pass
    process.wait()
    with print_lock:
        pbar.close()
        print("")
    if process.returncode != 0: raise subprocess.CalledProcessError(process.returncode, cmd)

def run_handbrake_with_progress(drive, cmd, description):
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore", bufsize=0)
    
    with print_lock:
        pbar = tqdm(total=100, desc=f"{YELLOW}[Laufwerk {drive[0].upper()}: HB-Queue] {description}{RESET}", bar_format="{desc}: {elapsed} vergangen (Warte auf Start...) ", leave=True)

    last_val = 0
    current_line = ""
    has_procent_mode = False
    full_output_log = []
    
    while True:
        char = process.stdout.read(1)
        if not char: break
        if char in ("\r", "\n"):
            if current_line.strip(): full_output_log.append(current_line.strip())
            match = re.search(r"(\d+[\.,]\d+)\s*%", current_line)
            if match:
                try:
                    if not has_procent_mode:
                        has_procent_mode = True
                        with print_lock: pbar.bar_format = "{desc}: |{bar}| {percentage:3.0f}% [{elapsed}<{remaining}]"
                    pct = int(float(match.group(1).replace(",", ".")))
                    if pct > last_val and pct <= 100:
                        with print_lock: pbar.update(pct - last_val)
                        last_val = pct
                except ValueError: pass
            current_line = ""
        else: current_line += char

    process.wait()
    with print_lock: pbar.close()
    if process.returncode != 0:
        with print_lock:
            print(f"{RED}[Laufwerk {drive[0].upper()}: !!! HANDBRAKE FEHLERLOG !!!]{RESET}")
            for log_line in full_output_log[-10:]: print(f"  -> {log_line}")
        raise subprocess.CalledProcessError(process.returncode, cmd)

def handbrake_worker(drive_letter):
    """Arbeitet dauerhaft im Hintergrund die HandBrake-Aufgaben fuer ein Laufwerk ab."""
    q = handbrake_queues[drive_letter]
    while True:
        # Wartet, bis MakeMKV eine neue Aufgabe in die Queue legt
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
                    run_handbrake_with_progress(drive_letter, handbrake_cmd, f"Komprimiere: {file}")
            
            log(drive_letter, f"HandBrake fertig mit '{clean_name}'!", GREEN)
            send_discord_notification(clean_name, drive_letter, event_type="success")
            
        except subprocess.CalledProcessError as e:
            log(drive_letter, f"HandBrake-Fehler bei '{clean_name}' (Code {e.returncode})", RED)
            send_discord_notification(clean_name, drive_letter, event_type="error", error_msg=f"HandBrake-Fehler Code {e.returncode}")
        except Exception as e:
            log(drive_letter, f"Schwerer Fehler in HB-Warteschlange: {e}", RED)
            send_discord_notification(clean_name, drive_letter, event_type="error", error_msg=str(e))
        finally:
            # Nach der Komprimierung den Temp-Ordner loeschen
            if os.path.exists(temp_folder):
                shutil.rmtree(temp_folder)
            q.task_done()

def process_dvd(drive, disc_name):
    """Liest die DVD aus und reicht sie direkt an die HandBrake-Queue weiter."""
    drive_letter = drive[0].upper()
    log(drive_letter, f"DVD erkannt: '{disc_name}'. Starte MakeMKV...", GREEN)
    
    mkv_index = get_mkv_drive_index(drive_letter + ":")
    clean_name = "".join(c for c in disc_name if c.isalnum() or c in (' ', '_', '-')).strip()
    if not clean_name: clean_name = f"Unbekannte_DVD_{drive_letter}"

    # Eindeutigen Zeitstempel nutzen, falls dieselbe DVD mehrfach nacheinander eingelegt wird
    timestamp = time.strftime("%H%M%S")
    temp_folder = os.path.join(TEMP_BASE, f"{drive_letter}_{clean_name}_{timestamp}")
    final_folder = os.path.join(OUTPUT_BASE, clean_name)

    os.makedirs(temp_folder, exist_ok=True)
    os.makedirs(final_folder, exist_ok=True)

    try:
        # 1. MakeMKV: Kopiert die Daten auf die Festplatte
        makemkv_cmd = [MAKE_MKV_PATH, "mkv", f"disc:{mkv_index}", "all", temp_folder, f"--minlength={MIN_LENGTH}"]
        run_makemkv_with_progress(drive_letter, makemkv_cmd, "MakeMKV liest DVD aus")

        # 2. Sofortiger Auswurf & Discord Ping!
        log(drive_letter, f"Auslesen beendet. Werfe Disc aus und sende Discord-Meldung...", GREEN)
        eject_drive(drive_letter)
        send_discord_notification(clean_name, drive_letter, event_type="eject")

        # 3. An HandBrake-Warteschlange uebergeben (Skript laeuft hier sofort weiter)
        handbrake_queues[drive_letter].put((temp_folder, final_folder, clean_name))
        log(drive_letter, f"'{clean_name}' an Hintergrund-Warteschlange uebergeben. Laufwerk ist wieder BEREIT!", GREEN)
        
    except Exception as e:
        log(drive_letter, f"Fehler beim Einlesen: {e}", RED)
        send_discord_notification(clean_name, drive_letter, event_type="error", error_msg=f"MakeMKV-Fehler: {str(e)}")
        if os.path.exists(temp_folder): shutil.rmtree(temp_folder)
        eject_drive(drive_letter)

def drive_worker(drive):
    """Ueberwacht das DVD-Laufwerk auf neue Discs."""
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
    print("   Asynchroner DVD-Ripper mit HB-Queue v3")
    print("==============================================")
    
    register_makemkv()
    print(f"\nUeberwache {', '.join(DRIVES)} parallel...")
    print("HandBrake-Warteschlangen im Hintergrund aktiv.\n")

    # Starte die HandBrake Hintergrund-Threads (einer pro Laufwerk)
    for drive in DRIVES:
        dl = drive[0].upper()
        threading.Thread(target=handbrake_worker, args=(dl,), daemon=True).start()

    # Starte die Laufwerks-Ueberwachungen
    for drive in DRIVES:
        threading.Thread(target=drive_worker, args=(drive,), daemon=True).start()

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: print("\nProgramm beendet.")

if __name__ == "__main__":
    main()