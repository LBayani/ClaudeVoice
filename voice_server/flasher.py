"""First-setup wizard backend: find a board on USB, identify it, back up its flash, build/flash firmware.

Everything runs esptool as a subprocess (the venv's own esptool), one job at a time, with progress
parsed from its output so the settings app can show a progress bar.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import ROOT

log = logging.getLogger("wizard")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FW_DIR = ROOT / "firmware"
SRC = FW_DIR / "xiaozhi-esp32"
IMAGES = FW_DIR / "images"          # images/<board>/flash_args + bins, made by build jobs
BACKUPS = ROOT / "backups"
CLAUDEVOICE_BOARDS = {"zhengchen-1.54tft-wifi"}   # boards with the full ClaudeVoice body (eyes, antennae, profile)
ESP_VIDS = {0x303A: "ESP32 USB (родной)", 0x1A86: "CH340/CH343", 0x10C4: "CP210x", 0x0403: "FTDI"}


def _python() -> str:
    exe = Path(sys.executable)
    py = exe.with_name("python.exe")   # pythonw has no stdout pipes problems, but prefer python
    return str(py if py.exists() else exe)


def esptool(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run([_python(), "-m", "esptool", *args], capture_output=True, text=True, timeout=timeout,
                          creationflags=NO_WINDOW, encoding="utf-8", errors="replace")


# ---------------- discovery of boards ----------------
def list_ports() -> list[dict]:
    from serial.tools import list_ports as lp
    out = []
    for p in lp.comports():
        if p.vid is None or "bluetooth" in (p.description or "").lower():
            continue
        out.append({"port": p.device, "description": p.description, "vid": p.vid, "pid": p.pid,
                    "kind": ESP_VIDS.get(p.vid, "USB-UART"), "likely_esp": p.vid in ESP_VIDS})
    return out


def _app_desc(blob: bytes) -> dict:
    d = blob[0x20:0x20 + 256]
    if len(d) < 144 or struct.unpack("<I", d[:4])[0] != 0xABCD5432:
        return {}
    s = lambda a, b: d[a:b].split(b"\0")[0].decode("ascii", "replace")
    return {"version": s(16, 48), "project": s(48, 80), "built": f"{s(96, 112)} {s(80, 96)}", "idf": s(112, 144)}


def _partitions(blob: bytes) -> list[dict]:
    parts = []
    for i in range(0, len(blob), 32):
        e = blob[i:i + 32]
        if e[:2] != b"\xaa\x50":
            break
        off, size = struct.unpack("<II", e[4:12])
        parts.append({"name": e[12:28].split(b"\0")[0].decode("ascii", "replace"), "type": e[2], "subtype": e[3],
                      "offset": off, "size": size})
    return parts


def _esptool_retry(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    """Native-USB boards re-enumerate the COM port after each reset: give it a moment and retry."""
    r = None
    for attempt in range(3):
        r = esptool(*args, timeout=timeout)
        if r.returncode == 0:
            return r
        time.sleep(1.5)
    return r


def probe(port: str) -> dict:
    """Chip, flash, MAC and what firmware runs now (read-only). Stays in the bootloader between the
    reads and resets the board back into its firmware only at the end."""
    r = _esptool_retry("--port", port, "--after", "no-reset", "flash-id")
    out = r.stdout + r.stderr
    if "Chip type" not in out and "Detecting chip type" not in out:
        raise RuntimeError("плата не отвечает: зажмите BOOT и переподключите USB, затем повторите" +
                           (f" ({out.strip().splitlines()[-1][:120]})" if out.strip() else ""))
    g = lambda pat: (m.group(1).strip() if (m := re.search(pat, out)) else "")
    chip_line = g(r"Chip type:\s*(.+)") or g(r"Detecting chip type\.\.\.\s*(\S+)")
    info = {"port": port, "chip": chip_line, "target": chip_line.split()[0].lower().replace("-", ""),
            "features": g(r"Features:\s*(.+)"), "mac": g(r"MAC:\s*([0-9a-f:]{17})"),
            "flash": g(r"Detected flash size:\s*(\S+)"), "firmware": {}}
    info["psram"] = bool(re.search(r"PSRAM", info["features"]))
    # what is installed now: partition table -> first app partition header
    tmp = BACKUPS / "_probe.bin"
    BACKUPS.mkdir(exist_ok=True)
    try:
        if _esptool_retry("--port", port, "--before", "no-reset", "--after", "no-reset", "read-flash", "0x8000", "0xC00",
                          str(tmp)).returncode == 0:
            parts = _partitions(tmp.read_bytes())
            app = next((p for p in parts if p["type"] == 0), None)
            info["partitions"] = [p["name"] for p in parts]
            info["_parts"] = parts
            if app and _esptool_retry("--port", port, "--before", "no-reset", "--after", "no-reset", "read-flash",
                                      hex(app["offset"]), "0x200", str(tmp)).returncode == 0:
                info["firmware"] = _app_desc(tmp.read_bytes())
        try:
            info["detected"] = detect_board(port, info)
        except Exception as e:
            info["detected"] = {"board": "", "hints": [], "error": str(e)}
    finally:
        tmp.unlink(missing_ok=True)
        info.pop("_parts", None)
        esptool("--port", port, "--before", "no-reset", "--after", "hard-reset", "chip-id", timeout=30)   # back to its firmware
    fw = info["firmware"]
    info["is_xiaozhi"] = fw.get("project") == "xiaozhi"
    info["is_claudevoice"] = info["is_xiaozhi"] and _ver(fw.get("version", "0")) >= (2, 4)
    info["variant"] = pick_variant(info)
    return info


def _ver(v: str) -> tuple[int, ...]:
    """'2.10.1-dirty' -> (2, 10, 1): compare versions as numbers, not strings ("2.10" > "2.4")."""
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


# ---------------- board catalogue ----------------
def boards(target: str = "") -> list[dict]:
    """Boards the firmware source knows, optionally only for one chip (esp32s3, esp32c3, ...)."""
    out = []
    if not SRC.exists():
        return [{"name": b, "target": "esp32s3", "claudevoice": True, "image": (IMAGES / b).exists()}
                for b in sorted(CLAUDEVOICE_BOARDS)]
    for cfg in sorted((SRC / "main" / "boards").glob("*/config.json")):
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except ValueError:
            continue
        t = data.get("target", "")
        if target and t != target:
            continue
        for b in data.get("builds", []):
            name = b.get("name") or cfg.parent.name
            out.append({"name": name, "dir": cfg.parent.name, "target": t,
                        "claudevoice": cfg.parent.name in CLAUDEVOICE_BOARDS,
                        "image": (IMAGES / name / "flash_args").exists()})
    out.sort(key=lambda b: (not b["claudevoice"], not b["image"], b["name"]))
    return out


# ---------------- jobs (one at a time) ----------------
class Job:
    def __init__(self, kind: str):
        self.kind, self.state, self.progress, self.message = kind, "running", 0.0, ""
        self.log: list[str] = []
        self.started = time.time()
        self.result: dict = {}

    def line(self, s: str):
        s = s.rstrip()
        if not s:
            return
        self.log.append(s)
        del self.log[:-200]
        if m := re.search(r"\(\s*(\d+(?:\.\d+)?)\s*%\)|\]\s*(\d+(?:\.\d+)?)%", s):
            self.progress = float(m.group(1) or m.group(2))

    def as_dict(self):
        return {"kind": self.kind, "state": self.state, "progress": self.progress, "message": self.message,
                "log": self.log[-40:], "result": self.result, "seconds": round(time.time() - self.started)}


_job: Job | None = None
_lock = threading.Lock()


def current_job() -> dict | None:
    return _job.as_dict() if _job else None


def _run(job: Job, steps: list[tuple[str, list[str]]]):
    """steps: (title, argv) run in order; stops on the first failure."""
    try:
        for title, argv in steps:
            job.message, job.progress = title, 0.0
            job.line(f"== {title}")
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                 creationflags=NO_WINDOW, encoding="utf-8", errors="replace", cwd=str(FW_DIR))
            buf = ""
            while True:
                ch = p.stdout.read(1)
                if not ch:
                    break
                if ch in "\r\n":
                    job.line(buf)
                    buf = ""
                else:
                    buf += ch
            job.line(buf)
            if p.wait() != 0:
                raise RuntimeError(f"{title}: ошибка (код {p.returncode})")
        job.state, job.progress = "done", 100.0
    except Exception as e:
        job.state, job.message = "failed", str(e)
        log.warning("wizard job %s failed: %s", job.kind, e)


def _start(kind: str, steps, result: dict | None = None) -> dict:
    global _job
    with _lock:
        if _job and _job.state == "running":
            raise RuntimeError("уже идёт другая операция: дождитесь её окончания")
        _job = Job(kind)
        _job.result = result or {}
        threading.Thread(target=_run, args=(_job, steps), daemon=True).start()
        return _job.as_dict()


def start_backup(port: str, mac: str, flash: str) -> dict:
    BACKUPS.mkdir(exist_ok=True)
    name = f"backup_{(mac or 'board').replace(':', '')}_{time.strftime('%Y%m%d_%H%M%S')}.bin"
    path = BACKUPS / name
    return _start("backup", [("Сохраняю копию всей памяти платы", [_python(), "-m", "esptool", "--port", port,
                  "--baud", "460800", "read-flash", "0", "ALL", str(path)])], {"file": str(path)})


def start_build(board: str) -> dict:
    # board goes into a shell command inside the container: only names the source really has
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", board or "") or \
            board not in {b["name"] for b in boards()} | {b.get("dir", "") for b in boards()}:
        raise ValueError(f"неизвестная плата: {board!r}")
    if not shutil.which("docker"):
        raise RuntimeError("для сборки нужен Docker Desktop")
    if not SRC.exists():
        raise RuntimeError("нет исходников прошивки: запустите firmware\\build.ps1 один раз")
    src = str(SRC)
    cmd = ("git config --global --add safe.directory '*' && "
           f"idf.py -B build_{board} -DBOARD_NAME={board} -DBOARD_TYPE={board} build")
    return _start("build", [(f"Собираю прошивку для {board} (5–15 минут)",
                             ["docker", "run", "--rm", "-v", f"{src}:/project", "-w", "/project",
                              "espressif/idf:v5.5.2", "bash", "-lc", cmd])], {"board": board})


def collect_image(board: str) -> Path:
    """Copy a finished build into images/<board>/ (flash_args + files it lists)."""
    build = SRC / f"build_{board}"
    if not (build / "flash_args").exists() and board in CLAUDEVOICE_BOARDS:
        build = SRC / "build"
    args = (build / "flash_args").read_text(encoding="utf-8").split()
    dst = IMAGES / board
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for tok in args:
        if not tok.startswith(("-", "0x")) and (build / tok).exists():
            (dst / tok).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(build / tok, dst / tok)
    shutil.copy2(build / "flash_args", dst / "flash_args")
    return dst


def start_flash(port: str, board: str, erase: bool) -> dict:
    img = IMAGES / board
    if not (img / "flash_args").exists():
        try:
            img = collect_image(board)
        except (OSError, ValueError) as e:
            raise RuntimeError(f"нет готовой прошивки для {board}: сначала соберите её") from e
    # flash_args: first line = options ("--flash_mode dio ..."), then "<offset> <file>" per line
    opts, files = [], []
    for line in (img / "flash_args").read_text(encoding="utf-8").splitlines():
        tok = line.split()
        if not tok:
            continue
        if tok[0].startswith("--"):
            opts += [t.replace("_", "-") if t.startswith("--") else t for t in tok]
        elif tok[0].startswith("0x") and len(tok) == 2:
            files += [tok[0], str(img / tok[1])]
    base = [_python(), "-m", "esptool", "--port", port, "--baud", "460800"]
    steps = []
    if erase:
        steps.append(("Стираю память платы", base + ["erase-flash"]))
    steps.append(("Записываю прошивку", base + ["--after", "hard-reset", "write-flash"] + opts + files))
    return _start("flash", steps, {"board": board})


# ---------------- universal firmware: variants, images, settings partition ----------------
VARIANTS = {
    "claudevoice-s3-16m": {"target": "esp32s3", "flash": "16MB", "psram": "octal",
                           "label": "ESP32-S3 · 16 МБ флеш · PSRAM octal (N16R8)"},
    "claudevoice-s3-8m": {"target": "esp32s3", "flash": "8MB", "psram": "octal",
                          "label": "ESP32-S3 · 8 МБ флеш · PSRAM octal (N8R8)"},
    "claudevoice-s3-8m-quad": {"target": "esp32s3", "flash": "8MB", "psram": "quad",
                               "label": "ESP32-S3 · 8 МБ флеш · PSRAM quad / без PSRAM (N8R2, N8)"},
    "claudevoice-c3-4m": {"target": "esp32c3", "flash": "4MB", "psram": "none",
                          "label": "ESP32-C3 · 4 МБ флеш и больше"},
}
RELEASE_URL = "https://github.com/LBayani/ClaudeVoice/releases/latest/download/{name}.bin"
SUMS_URL = "https://github.com/LBayani/ClaudeVoice/releases/latest/download/SHA256SUMS.txt"
CHIP_IDS = {"esp32s3": 9, "esp32c3": 5}   # esp_image_header_t.chip_id
NVS_OFFSET, NVS_SIZE = 0x9000, 0x4000


def pick_variant(info: dict) -> str:
    """Best universal image for a probed board."""
    t, flash, feats = info.get("target", ""), info.get("flash", ""), info.get("features", "")
    if t == "esp32c3":
        return "claudevoice-c3-4m"
    if t == "esp32s3":
        if "PSRAM 2MB" in feats:
            return "claudevoice-s3-8m-quad"
        if flash.startswith(("16", "32")):
            return "claudevoice-s3-16m"
        return "claudevoice-s3-8m"
    return ""


def image_path(variant: str) -> Path:
    return IMAGES / f"{variant}.bin"


def collect_release_images() -> list[str]:
    """Copy merged images produced by scripts/release.py (releases/v*_<variant>.zip) into images/."""
    import zipfile
    got = []
    rel = SRC / "releases"
    if not rel.exists():
        return got
    IMAGES.mkdir(parents=True, exist_ok=True)
    for z in sorted(rel.glob("v*_claudevoice-*.zip"), key=lambda p: p.stat().st_mtime):
        variant = z.stem.split("_", 1)[1]
        with zipfile.ZipFile(z) as zf:
            image_path(variant).write_bytes(zf.read("merged-binary.bin"))
        got.append(variant)
    return got


def ensure_image(variant: str, job: "Job | None" = None) -> Path:
    """Local image, or download it from the project's GitHub release."""
    p = image_path(variant)
    if p.exists():
        return p
    collect_release_images()
    if p.exists():
        return p
    import hashlib
    import urllib.request
    if variant not in VARIANTS:
        raise ValueError(f"неизвестный вариант прошивки: {variant}")
    url = RELEASE_URL.format(name=variant)
    if job:
        job.line(f"Скачиваю прошивку {variant} с GitHub…")
    with urllib.request.urlopen(SUMS_URL, timeout=30) as r:
        sums = r.read(65536).decode("utf-8", "replace")
    want = next((ln.split()[0].lower() for ln in sums.splitlines()
                 if len(ln.split()) == 2 and ln.split()[1].lstrip("*") == f"{variant}.bin"), "")
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        raise RuntimeError(f"в релизе нет контрольной суммы для {variant}.bin — прошивка не скачана")
    IMAGES.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".part")
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(65536):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if job and total:
                    job.progress = done * 100 / total
        if h.hexdigest() != want:
            raise RuntimeError("скачанная прошивка повреждена или подменена (SHA256 не совпал)")
        check_image(tmp, VARIANTS[variant]["target"], merged=True)
        tmp.replace(p)
    finally:
        tmp.unlink(missing_ok=True)
    if job:
        job.line(f"SHA256 совпал: {want[:16]}…")
    return p


def check_image(path: Path, target: str = "", merged: bool = False):
    """ESP-IDF image sanity: header magic, chip, app descriptor of a xiaozhi build. ValueError if not."""
    data = Path(path).read_bytes()
    if merged:          # bootloader first; the app follows at the ota_0 / factory offset
        if len(data) < 0x20000 or data[0] != 0xE9:
            raise ValueError("это не полный образ прошивки ESP32")
        chip = int.from_bytes(data[12:14], "little")
    else:
        if len(data) < 0x100 or len(data) > 0x800000 or data[0] != 0xE9:
            raise ValueError("это не файл прошивки ESP32")
        chip = int.from_bytes(data[12:14], "little")
        if int.from_bytes(data[0x20:0x24], "little") != 0xABCD5432:
            raise ValueError("в файле нет описания приложения ESP-IDF")
        project = data[0x50:0x70].split(b"\0", 1)[0].decode("ascii", "replace")
        if project != "xiaozhi":
            raise ValueError(f"это прошивка «{project}», а не xiaozhi/ClaudeVoice")
    if chip not in CHIP_IDS.values():
        raise ValueError(f"прошивка для неподдерживаемого чипа (id {chip})")
    if target and CHIP_IDS.get(target) != chip:
        raise ValueError(f"прошивка собрана для другого чипа (нужен {target})")


def build_settings_partition(profile: dict, ssid: str, password: str, ota_url: str, server_id: str) -> Path:
    """NVS image with the hardware profile, Wi-Fi and this server, so the board works on first boot."""
    import csv
    from .hardware import to_device
    BACKUPS.mkdir(exist_ok=True)
    csv_path, bin_path = BACKUPS / "_settings.csv", BACKUPS / "_settings.bin"
    rows = [("key", "type", "encoding", "value"),
            ("cvhw", "namespace", "", ""),
            ("json", "data", "string", json.dumps(to_device(profile), separators=(",", ":"), ensure_ascii=False)),
            ("wifi", "namespace", "", ""),
            ("ota_url", "data", "string", ota_url),
            ("cv_srv_id", "data", "string", server_id)]
    if ssid:
        rows += [("ssid", "data", "string", ssid), ("password", "data", "string", password)]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    r = subprocess.run([_python(), "-m", "esp_idf_nvs_partition_gen", "generate", str(csv_path), str(bin_path),
                        hex(NVS_SIZE)], capture_output=True, text=True, creationflags=NO_WINDOW)
    csv_path.unlink(missing_ok=True)   # it holds the Wi-Fi password
    if r.returncode != 0 or not bin_path.exists():
        raise RuntimeError("не удалось собрать раздел настроек: " + (r.stderr or r.stdout)[-200:])
    return bin_path


def start_install(port: str, variant: str, profile: dict, ssid: str, password: str, ota_url: str,
                  server_id: str, erase: bool, backup: bool, mac: str) -> dict:
    """Backup (optional) -> erase (optional) -> universal image -> settings partition, as one job."""
    global _job
    with _lock:
        if _job and _job.state == "running":
            raise RuntimeError("уже идёт другая операция: дождитесь её окончания")
        job = Job("install")
        job.result = {"variant": variant}
        _job = job

    def work():
        try:
            job.message = "Готовлю прошивку"
            img = ensure_image(variant, job)
            nvs = build_settings_partition(profile, ssid, password, ota_url, server_id)
            base = [_python(), "-m", "esptool", "--port", port, "--baud", "460800"]
            steps = []
            if backup:
                BACKUPS.mkdir(exist_ok=True)
                bpath = BACKUPS / f"backup_{(mac or 'board').replace(':', '')}_{time.strftime('%Y%m%d_%H%M%S')}.bin"
                job.result["backup"] = str(bpath)
                steps.append(("Сохраняю копию всей памяти платы", base + ["--after", "no-reset", "read-flash", "0", "ALL", str(bpath)]))
            if erase:
                steps.append(("Стираю память платы", base + ["--before", "default-reset", "--after", "no-reset", "erase-flash"]))
            steps.append(("Записываю прошивку ClaudeVoice", base + ["--after", "no-reset", "write-flash", "0x0", str(img)]))
            steps.append(("Записываю настройки (плата, Wi-Fi, адрес ПК)",
                          base + ["--after", "hard-reset", "write-flash", hex(NVS_OFFSET), str(nvs)]))
            _run(job, steps)
        except Exception as e:
            job.state, job.message = "failed", str(e)
        finally:
            (BACKUPS / "_settings.bin").unlink(missing_ok=True)

    threading.Thread(target=work, daemon=True).start()
    return job.as_dict()


def detect_board(port: str, info: dict) -> dict:
    """Guess the board: xiaozhi firmware carries its board name; other firmware gives hints (display
    libraries in its strings)."""
    from .hardware import board_presets
    presets = board_presets(info.get("target", ""))
    guess = {"board": "", "hints": []}
    app = next((p for p in info.get("_parts", []) if p["type"] == 0), None)
    if not app:
        return guess
    tmp = BACKUPS / "_scan.bin"
    try:
        size = min(app["size"], 0x180000)
        if _esptool_retry("--port", port, "--before", "no-reset", "--after", "no-reset", "read-flash",
                          hex(app["offset"]), hex(size), str(tmp), timeout=120).returncode != 0:
            return guess
        blob = tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)
    for name in sorted(presets, key=len, reverse=True):          # longest first: xmini-c3-v3 before xmini-c3
        if name.encode() in blob:
            guess["board"] = name
            break
    for hint, pat in (("ST7789", b"ST7789"), ("GC9A01", b"GC9A01"), ("ILI9341", b"ILI9341"), ("SSD1306", b"SSD1306"),
                      ("SH1106", b"SH1106"), ("ES8311", b"ES8311"), ("I2S", b"I2S"), ("TFT_eSPI", b"TFT_eSPI"),
                      ("LovyanGFX", b"LovyanGFX"), ("Adafruit GFX", b"Adafruit_GFX"), ("U8g2", b"U8g2")):
        if pat in blob:
            guess["hints"].append(hint)
    return guess
