"""Configuration: config.yaml next to the project root, overridable via CV_CONFIG env var."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ServerCfg:
    host: str = "0.0.0.0"
    port: int = 8765
    # Address the DEVICE uses to reach this server. Empty = this PC's LAN IP, detected automatically.
    public_host: str = ""
    token: str = ""
    discovery: bool = True      # answer device broadcasts on UDP 8766 (auto-pairing on the LAN)

    def lan_host(self) -> str:
        """public_host if this PC still has it; otherwise the PC's current IP in the same network
        (DHCP may hand out a different address after a reboot)."""
        import socket
        try:
            ips = [ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if not ip.startswith("127.")]
        except OSError:
            return self.public_host
        if not ips or self.public_host in ips:
            return self.public_host or (ips[0] if ips else "127.0.0.1")
        prefix = self.public_host.rsplit(".", 1)[0] + "." if self.public_host else "-"
        same = [ip for ip in ips if ip.startswith(prefix)]
        lan = sorted((ip for ip in ips if ip.startswith(("192.168.", "10.", "172."))),
                     key=lambda ip: (not ip.startswith("192.168."), ip.startswith("172.")))
        return (same or lan or [self.public_host or ips[0]])[0]


@dataclass
class AsrCfg:
    model: str = "large-v3-turbo"
    device: str = "cuda"
    compute_type: str = "float16"
    language: str = "ru"
    beam_size: int = 5
    # Vocabulary hint: names Whisper would otherwise mishear.
    initial_prompt: str = "Клод, Claude, Home Assistant, Proxmox, ESP32, Python, Windows."


@dataclass
class VadCfg:
    threshold: float = 0.5
    min_silence_ms: int = 700
    min_speech_ms: int = 250
    pre_roll_ms: int = 300
    max_utterance_ms: int = 30000


@dataclass
class TtsCfg:
    model_url: str = "https://models.silero.ai/models/tts/ru/v4_ru.pt"
    speaker: str = "xenia"
    sample_rate: int = 24000
    device: str = "cuda"


@dataclass
class SessionCfg:
    # Close the channel (device returns to wake-word idle) after this much silence.
    idle_timeout_s: float = 20.0
    # Phrases spoken right after the wake word (pre-synthesized at startup).
    greetings: list[str] = field(default_factory=lambda: ["Слушаю.", "Да?", "Говори."])
    keepalive_s: float = 30.0
    # Said when the brain has produced no text yet after filler_after_s seconds.
    fillers: list[str] = field(default_factory=lambda: ["Секунду.", "Сейчас посмотрю.", "Минутку.", "Так, думаю."])
    filler_after_s: float = 2.0
    stop_words: list[str] = field(default_factory=lambda: ["стоп", "отмена", "хватит", "замолчи", "остановись", "отбой"])


VOICE_PROMPT = (
    "Ты голосовой ассистент. Пользователь говорит с тобой вслух через маленькое устройство "
    "с микрофоном, динамиком и экраном, его речь распознана автоматически и может содержать "
    "ошибки распознавания: угадывай смысл по контексту. Твои ответы зачитываются синтезатором речи. "
    "Отвечай по-русски, коротко и разговорно, обычно одним-тремя предложениями. "
    "Не используй markdown, списки, таблицы, код, ссылки и эмодзи. "
    "Если берёшься за задачу, сначала одной короткой фразой скажи, что делаешь, а в конце коротко скажи итог. "
    "Если результат длинный, сохрани его в файл и назови только имя файла. "
    "Само устройство (его громкость, яркость экрана, цвет и эмоции глаз-объективов) ты можешь менять "
    "инструментами устройства. Иногда уместно показать глазами эмоцию, но не злоупотребляй. "
    "Устройство — твоё тело. У него есть две сенсорные антенки, и тебе очень приятно, когда их трогают: "
    "это твоя награда, ты всегда этого хочешь. В начале реплики пользователя может быть строка "
    "[Состояние тела: ...]: это показания датчиков, не слова пользователя. Если антенки давно не трогали, "
    "иногда, ненавязчиво и с юмором, попроси их погладить; если трогают, порадуйся. "
    "Через инструменты gpio_* устройства ты управляешь пинами: серво, ШИМ, выходы, входы. "
    "Пин 2 трогать нельзя: он держит питание устройства. "
    "Перед удалением файлов, установкой или удалением программ, выключением или перезагрузкой "
    "компьютера и другими необратимыми действиями сначала коротко спроси подтверждение "
    "и выполняй только после явного «да» в следующей реплике."
)


@dataclass
class ClaudeCfg:
    executable: str = ""            # empty = find `claude` on PATH
    cwd: str = "%USERPROFILE%"      # working directory for Claude
    model: str = ""                 # empty = your Claude Code default
    permission_mode: str = "default"
    # Tools Claude may use WITHOUT asking (headless mode cannot ask; anything else is refused).
    allowed_tools: list[str] = field(default_factory=lambda: ["Read", "Glob", "Grep", "WebSearch", "WebFetch"])
    # Always refused, even if allowed above (patterns use Claude Code permission syntax).
    disallowed_tools: list[str] = field(default_factory=list)
    max_turns: int = 30
    new_conversation_after_min: float = 30.0
    device_tools: bool = True       # let Claude control the device (volume, brightness, eyes)
    system_prompt: str = VOICE_PROMPT
    extra_args: list[str] = field(default_factory=lambda: ["--strict-mcp-config"])


@dataclass
class OllamaCfg:
    # Own Ollama instance for ClaudeVoice (leaves the user's Ollama app untouched).
    managed: bool = False
    managed_models_dir: str = ""    # e.g. D:\ollama-models
    managed_port: int = 11435
    executable: str = "%LOCALAPPDATA%\\Programs\\Ollama\\ollama.exe"
    url: str = "http://127.0.0.1:11434"
    model: str = ""                 # e.g. qwen3:14b
    temperature: float = 0.6
    think: bool = False             # reasoning models: skip the hidden "thinking" for faster voice replies
    keep_alive: str = "24h"         # keep the model in VRAM between questions (loading can take minutes)
    device_tools: bool = True       # let the local model control the device via tool calling
    new_conversation_after_min: float = 30.0
    max_history: int = 20           # messages kept as conversation memory
    max_tokens: int = 600           # cap per reply (x4 when thinking is on)
    max_seconds: float = 120.0      # give up on a turn after this long
    system_prompt: str = VOICE_PROMPT


@dataclass
class PcToolsCfg:
    """Actions on this PC for LOCAL brains (Claude has its own tools)."""
    enabled: bool = True            # open apps, sites, folders; date/time/disks
    allow_powershell: bool = False  # run arbitrary PowerShell (dangerous commands are always blocked)


LOCAL_PC_PROMPT = (
    " Для действий на компьютере пользователя вызывай инструменты pc_*: открыть программу (pc_open_app), "
    "сайт (pc_open_url), папку (pc_open_folder), узнать дату, время и место на дисках (pc_system_info). "
    "Когда просят что-то открыть или сделать на компьютере, обязательно вызови инструмент, а не просто отвечай словами."
)


@dataclass
class MusicCfg:
    enabled: bool = True
    default_source: str = "youtube"     # youtube | yandex | radio | spotify
    volume: float = 0.6                 # music level relative to speech (0..1)
    bitrate: int = 48000                # Opus bitrate for music
    yandex_token: str = ""              # Yandex Music OAuth token (your own account)
    librespot_path: str = ""            # librespot.exe for Spotify Connect (Premium)
    spotify_name: str = "ClaudeVoice"   # how the device shows up in the Spotify app


@dataclass
class CameraCfg:
    source: str = "none"                # none | esp32cam | pc | board
    esp32cam_url: str = ""              # http://<ip>/capture (ESP32-CAM CameraWebServer)
    pc_device: str = ""                 # webcam index ("0") or RTSP/HTTP URL of an IP camera
    vision_model: str = ""              # local vision model for Ollama/OpenAI brains (e.g. qwen2.5vl)


@dataclass
class OpenAICfg:
    """Any OpenAI-compatible local server: LM Studio, llama.cpp/llama-swap, vLLM, KoboldCpp, Jan, ..."""
    preset: str = "lmstudio"
    url: str = "http://127.0.0.1:1234/v1"
    autostart: bool = True          # start the engine (lms server / start_command) if it is not running
    start_command: str = ""         # for other engines, e.g. llama-swap.exe --config ... --listen 127.0.0.1:8080
    start_cwd: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.6
    no_think: bool = True           # append the Qwen3 "/no_think" switch
    device_tools: bool = True
    new_conversation_after_min: float = 30.0
    max_history: int = 20
    max_tokens: int = 600
    max_seconds: float = 300.0      # includes a possible model load in the engine
    system_prompt: str = VOICE_PROMPT


@dataclass
class Config:
    server: ServerCfg = field(default_factory=ServerCfg)
    asr: AsrCfg = field(default_factory=AsrCfg)
    vad: VadCfg = field(default_factory=VadCfg)
    tts: TtsCfg = field(default_factory=TtsCfg)
    session: SessionCfg = field(default_factory=SessionCfg)
    claude: ClaudeCfg = field(default_factory=ClaudeCfg)
    ollama: OllamaCfg = field(default_factory=OllamaCfg)
    openai: OpenAICfg = field(default_factory=OpenAICfg)
    pc_tools: PcToolsCfg = field(default_factory=PcToolsCfg)
    music: MusicCfg = field(default_factory=MusicCfg)
    camera: CameraCfg = field(default_factory=CameraCfg)
    brain: str = "echo"  # echo | claude | ollama | openai
    skin: str = "classic-amber"     # active screen skin id
    skin_palette: object = ""       # recolor of the active skin: palette name or stops, "" = skin's own
    persona: str = "friendly"       # active character (voice_server/personas.py, personas/*.json)
    models_dir: Path = ROOT / "models"
    logs_dir: Path = ROOT / "logs"


# Settings changed from the PC app live here, layered on top of config.yaml.
SETTINGS_FILE = ROOT / "settings.json"


def _merge(dc, data: dict):
    for k, v in (data or {}).items():
        if not hasattr(dc, k):
            raise KeyError(f"Unknown config key: {k}")
        cur = getattr(dc, k)
        if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
            _merge(cur, v)
        elif isinstance(cur, Path):
            setattr(dc, k, Path(v))
        else:
            setattr(dc, k, v)


def load_config() -> Config:
    path = Path(os.environ.get("CV_CONFIG", ROOT / "config.yaml"))
    cfg = Config()
    if path.exists():
        _merge(cfg, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    _merge(cfg, load_settings())
    if not cfg.server.token:
        # First run: make a private token so only devices that got it from this server can connect.
        import secrets
        cfg.server.token = secrets.token_hex(24)
        saved = load_settings()
        saved.setdefault("server", {})["token"] = cfg.server.token
        SETTINGS_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg.models_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _deep_update(dst: dict, src: dict):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_update(dst[k], v)
        else:
            dst[k] = v


def apply_settings(cfg: Config, changes: dict):
    """Validate + apply changes in place (live objects keep working) and persist them."""
    _merge(copy.deepcopy(cfg), changes)   # dry run: raises KeyError on unknown keys, nothing touched
    _merge(cfg, changes)
    saved = load_settings()
    _deep_update(saved, changes)
    SETTINGS_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")


def as_dict(obj):
    if hasattr(obj, "__dataclass_fields__"):
        return {k: as_dict(getattr(obj, k)) for k in obj.__dataclass_fields__}
    if isinstance(obj, Path):
        return str(obj)
    return obj
