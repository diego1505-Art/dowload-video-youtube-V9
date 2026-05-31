import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_FOLDER = BASE_DIR / "downloads"

# FFmpeg configuration
FFMPEG_FALLBACK_DIRS = [
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft"
    / "WinGet"
    / "Packages"
    / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    / "ffmpeg-8.1.1-full_build"
    / "bin"
]

# Media settings
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".m4v", ".mov", ".avi"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".opus", ".ogg"}
ALLOWED_LOCAL_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
PREVIEWABLE_EXTENSIONS = {
    ".mp4", ".webm",          # Vidéo HTML5 native
    ".mp3", ".m4a", ".aac",   # Audio universel
    ".ogg", ".opus", ".wav",  # Audio Firefox/Chrome
}

# App settings
LOCAL_MEDIA_TTL_SECONDS = 3600
LOCAL_MEDIA_MAX_ENTRIES = 128

# Browser extractor settings
# Set DOWFLOW_BROWSER_VISIBLE=1 before starting the app if you need to debug the browser.
BROWSER_HEADLESS = os.environ.get("DOWFLOW_BROWSER_VISIBLE", "").strip().lower() not in {"1", "true", "yes", "on"}
BROWSER_PROFILE_DIR = BASE_DIR / "browser_profile"

# Proxy settings (Optional)
# Format: ["http://user:pass@host:port", "http://host2:port2"]
# Leave empty [] to not use proxies.
PROXIES = [
    "http://47.82.0.86:3128",
    "http://47.82.0.107:3128",
    "http://89.22.230.4:7890",
    "http://47.82.154.7:3128",
    "http://47.82.178.210:3128",
    "http://47.82.178.111:3128",
    "http://47.82.178.97:3128",
    "http://47.82.151.197:3128"
]

# Performance settings
MAX_DOWNLOADS_BEFORE_REFRESH = 10 # Nombre de téléchargements avant de forcer un nettoyage console/mémoire
