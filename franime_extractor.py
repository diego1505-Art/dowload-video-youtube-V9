"""
Extracteur franime.fr — supporte SIBNET, FILEMOON, SENDVID, VIDMOLY et HLS direct.
Lance Chrome en arrière-plan par défaut, intercepte les URLs des lecteurs, essaie chaque lecteur dans l'ordre.
"""
from __future__ import annotations
import re, os, shutil, tempfile, time, random, sys
from pathlib import Path
from typing import Any

from config import BROWSER_HEADLESS, BROWSER_PROFILE_DIR, PROXIES

FRANIME_PATTERN = re.compile(r"https?://(?:www\.)?franime\.fr/", re.IGNORECASE)

# ── Anti-Bot Settings ────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

def human_delay(min_ms=500, max_ms=1500):
    """Pause aléatoire pour simuler un comportement humain."""
    time.sleep(random.randint(min_ms, max_ms) / 1000.0)

def move_mouse_humanly(page):
    """Déplace la souris de manière moins linéaire."""
    try:
        width, height = 1280, 720
        for _ in range(random.randint(2, 5)):
            x, y = random.randint(0, width), random.randint(0, height)
            page.mouse.move(x, y, steps=random.randint(5, 15))
            human_delay(100, 300)
    except:
        pass

# ── Lecteurs connus et leurs patterns d'URL ──────────────────────────────────
# Chaque entrée : (nom, regex de détection, blacklist regex)
LECTEURS = [
    ("SIBNET",   re.compile(r"sibnet\.ru/shell\.php",          re.I), re.compile(r"sibnet\.ru/export/|sbcount|/time", re.I)),
    ("FILEMOON", re.compile(r"filemoon\.\w+/",                 re.I), None),
    ("SENDVID",  re.compile(r"sendvid\.com/",                  re.I), None),
    ("VIDMOLY",  re.compile(r"vidmoly\.\w+/",                  re.I), None),
    ("HLS",      re.compile(r"\.(m3u8|mpd)(\?|$)",            re.I), None),
    ("WATCH2",   re.compile(r"franime\.fr/watch2/",            re.I), None),
]

SKIP_HOSTS = {
    "google-analytics", "googletagmanager", "doubleclick", "facebook",
    "twitter", "sentry", "fonts.google", "gstatic", "clarity.ms",
    "hotjar", "segment.io", "amplitude", "adskeeper", "bidgear",
    "a-ads.com", "betweendigital", "yandex", "mail.ru",
}

# Priorité de téléchargement (yt-dlp supporte tous ces domaines)
LECTEUR_PRIORITY = ["HLS", "SIBNET", "SENDVID", "VIDMOLY", "FILEMOON", "WATCH2"]


def is_franime_url(url: str) -> bool:
    return bool(FRANIME_PATTERN.match(url))


def _skip(url: str) -> bool:
    return url.startswith("blob:") or any(h in url for h in SKIP_HOSTS)


def _detect_lecteur(url: str) -> str | None:
    """Retourne le nom du lecteur détecté pour une URL donnée."""
    for name, pattern, blacklist in LECTEURS:
        if blacklist and blacklist.search(url):
            continue
        if pattern.search(url):
            return name
    return None


def _candidate_filepaths_from_info(info: dict[str, Any], output_path: str) -> list[str]:
    candidates: list[str] = []

    requested_downloads = info.get("requested_downloads")
    if isinstance(requested_downloads, list):
        for item in requested_downloads:
            if isinstance(item, dict):
                filepath = item.get("filepath")
                if isinstance(filepath, str) and filepath:
                    candidates.append(filepath)

    for key in ("filepath", "_filename"):
        value = info.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)

    title = info.get("title")
    ext = info.get("ext")
    if isinstance(title, str) and title:
        if isinstance(ext, str) and ext:
            candidates.append(os.path.join(output_path, f"{title}.{ext}"))
        for known_ext in (".mp4", ".webm", ".mkv", ".m4v", ".mov", ".avi", ".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus"):
            candidates.append(os.path.join(output_path, f"{title}{known_ext}"))

    unique_candidates: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        normalized = os.path.normpath(path)
        if normalized not in seen:
            unique_candidates.append(normalized)
            seen.add(normalized)

    return unique_candidates


def _resolve_downloaded_file(info: dict[str, Any], output_path: str, before_mtime: dict[str, float]) -> str | None:
    ignored = {".part", ".ytdl", ".jpg", ".jpeg", ".png", ".webp", ".description"}

    for candidate in _candidate_filepaths_from_info(info, output_path):
        if os.path.isfile(candidate) and os.path.splitext(candidate)[1].lower() not in ignored:
            return candidate

    candidates = []
    for f in os.listdir(output_path):
        fp = os.path.join(output_path, f)
        mt = os.path.getmtime(fp)
        if f not in before_mtime or mt > before_mtime[f]:
            if os.path.splitext(f)[1].lower() not in ignored:
                candidates.append((mt, fp))

    candidates.sort(reverse=True)
    return candidates[0][1] if candidates else None


def clear_terminal():
    """Nettoie la console pour éviter la latence et les bugs d'affichage."""
    if sys.platform == "win32":
        os.system("cls")
    else:
        os.system("clear")
    print("\n" + "=" * 60)
    print("DowFlow — Console rafraîchie pour éviter la latence")
    print("=" * 60 + "\n")

def extract_stream_url(page_url: str, preferred_lecteur: str | None = None, proxy_url: str | None = None) -> tuple[str | None, str | None, dict[str, list[str]], dict[str, Any]]:
    """
    Retourne (url_stream, nom_lecteur, all_captured, diagnostics).
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        raise RuntimeError("Playwright non installé. Lance : pip install playwright && playwright install chromium")

    # dict lecteur → liste d'URLs trouvées
    captured: dict[str, list[str]] = {name: [] for name, *_ in LECTEURS}
    diagnostics: dict[str, Any] = {
        "main_status": None,
        "blocked": False,
        "blocked_url": None,
    }

    if proxy_url:
        print(f"  [franime] Utilisation du proxy : {proxy_url.split('@')[-1]}")

    def on_request(req):
        url = req.url
        if _skip(url): return
        name = _detect_lecteur(url)
        if name and url not in captured[name]:
            print(f"  [franime] ✓ [{name}] {url[:110]}")
            captured[name].append(url)

    def on_response(resp):
        url = resp.url
        if _skip(url): return
        # On ne bloque plus sur le 403 immédiatement, Cloudflare renvoie souvent 403/503 pendant le challenge
        if url.split("#", 1)[0] == page_url.split("#", 1)[0]:
            diagnostics["main_status"] = resp.status
            if resp.status in {401, 429}: # On garde 401 et 429 comme bloquants réels
                diagnostics["blocked"] = True
                diagnostics["blocked_url"] = url
        ct = resp.headers.get("content-type", "").lower()
        if any(t in ct for t in ["mpegurl", "dash+xml", "mp2t", "x-mpegurl"]):
            name = _detect_lecteur(url) or "HLS"
            if url not in captured.get(name, []):
                print(f"  [franime] ✓ [{name}] (content-type) {url[:100]}")
                captured.setdefault(name, []).append(url)
        if not any(e in url for e in [".js",".css",".png",".jpg",".svg",".woff",".ico",".gif",".webp",".wasm"]):
            print(f"  [franime]   {resp.status}  {url[:100]}")

    chrome_path   = _find_chrome()
    user_data_dir = _chrome_user_data_dir()
    
    # Utilisation du profil persistant du projet au lieu d'un temporaire
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    profile_path = str(BROWSER_PROFILE_DIR)

    print(f"  [franime] Chrome : {chrome_path or 'Chromium Playwright'}")
    print(f"  [franime] Profil : {profile_path}")

    with sync_playwright() as pw:
        # Arguments pour plus de discrétion (anti-bot)
        base_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-position=0,0",
        ]

        # User-Agent aléatoire
        user_agent = random.choice(USER_AGENTS)
        # Viewport aléatoire
        viewport_w = random.randint(1200, 1600)
        viewport_h = random.randint(700, 1000)

        # Proxy config for Playwright
        proxy_config = None
        if proxy_url:
            proxy_config = {"server": proxy_url}

        # Si Chrome est installé, on essaie de synchroniser les cookies initiaux une fois
        if chrome_path and user_data_dir and not (BROWSER_PROFILE_DIR / "Default").exists():
            print("  [franime] Synchronisation initiale du profil Chrome...")
            src_default = os.path.join(user_data_dir, "Default")
            dst_default = BROWSER_PROFILE_DIR / "Default"
            dst_default.mkdir(parents=True, exist_ok=True)
            for fname in ["Cookies", "Login Data", "Local State", "Preferences", "Web Data"]:
                src = os.path.join(src_default, fname)
                if os.path.isfile(src):
                    try: shutil.copy2(src, dst_default / fname)
                    except: pass
        
        # Lancement avec contexte PERSISTANT (maintient les sessions/cookies Franime)
        ctx = pw.chromium.launch_persistent_context(
            profile_path, 
            headless=BROWSER_HEADLESS, 
            executable_path=chrome_path,
            args=base_args, 
            no_viewport=False, 
            viewport={"width": viewport_w, "height": viewport_h},
            user_agent=user_agent,
            locale="fr-FR",
            timezone_id="Europe/Paris",
            proxy=proxy_config
        )
        page = ctx.new_page()

        # Injection de script pour masquer Playwright et simuler des caractéristiques réelles
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['fr-FR', 'fr', 'en-US', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            window.chrome = { runtime: {} };
        """)

        # --- PROTECTION LOCALE ---
        # Petite pause aléatoire avant de charger la page
        human_delay(2000, 6000)

        page.on("request",  on_request)
        page.on("response", on_response)

        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=45_000)
        except PwTimeout:
            print("  [franime] Timeout – on continue")

        # Détection Cloudflare
        content_lower = page.content().lower()
        if "challenge-platform" in content_lower or "cloudflare" in content_lower or "just a moment" in page.title().lower():
            print("  [franime] Challenge Cloudflare détecté, attente de résolution (jusqu'à 45s)...")
            human_delay(4000, 6000)
            try:
                # On essaie de cliquer sur la checkbox si elle apparaît (iframe Cloudflare)
                for i in range(15): # Augmenté à 15 tentatives
                    curr_content = page.content().lower()
                    if "challenge-platform" not in curr_content and "cloudflare" not in curr_content and "just a moment" not in page.title().lower():
                        break
                    
                    # On bouge la souris de manière aléatoire pour paraître humain
                    move_mouse_humanly(page)
                    
                    # Petit scroll aléatoire
                    if random.random() > 0.5:
                        page.mouse.wheel(0, random.randint(100, 400))
                    
                    # On cherche l'iframe du challenge
                    frames = page.frames
                    for f in frames:
                        if "cloudflare" in f.url or "turnstile" in f.url:
                            try:
                                checkbox = f.query_selector("input[type='checkbox'], #challenge-stage, .ctp-checkbox-container")
                                if checkbox:
                                    # On bouge d'abord sur l'élément avant de cliquer
                                    box = checkbox.bounding_box()
                                    if box:
                                        page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2, steps=10)
                                        human_delay(200, 500)
                                    checkbox.click()
                                    print(f"  [franime] ✓ Tentative de clic Cloudflare ({i+1})")
                            except: pass
                    human_delay(2000, 3500)

                # On attend le bouton final (on gère les traductions possibles)
                page.wait_for_selector("button:has-text('Regarder'), button:has-text('Watch'), .player-container", timeout=35_000)
                print("  [franime] ✓ Page accessible !")
            except Exception as e:
                print(f"  [franime] ⚠ Le challenge semble toujours présent ou erreur : {e}")
                page.wait_for_timeout(2_000)

        # Si un lecteur préféré est demandé, le sélectionner dans le dropdown
        if preferred_lecteur:
            _select_lecteur(page, preferred_lecteur)

        # Clic "Regarder l'épisode"
        page.wait_for_timeout(2_000)
        selectors = [
            "button:has-text('Regarder')", "a:has-text('Regarder')", 
            "button:has-text('Watch')", "a:has-text('Watch')",
            "button:has-text('épisode')", "button:has-text('episode')"
        ]
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=3_000)
                    print(f"  [franime] Clic 'Regarder' : {sel}")
                    page.wait_for_timeout(2_000)
                    break
            except Exception:
                pass

        # Attendre lecteur
        for sel in ["video", "iframe", "[class*='player']"]:
            try:
                page.wait_for_selector(sel, timeout=10_000)
                break
            except Exception:
                pass

        # Clic play
        for sel in ["video", ".play-button", "[class*='play']"]:
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=2_000)
                    break
            except Exception:
                pass

        # Attendre jusqu'à 35 secondes
        for i in range(35):
            page.wait_for_timeout(1_000)
            total = sum(len(v) for v in captured.values())
            if total >= 1 and i >= 3:
                break

        ctx.close()

    if tmp_profile:
        try: shutil.rmtree(tmp_profile, ignore_errors=True)
        except Exception: pass

    # Choisir le meilleur lecteur disponible
    order = ([preferred_lecteur] if preferred_lecteur else []) + LECTEUR_PRIORITY
    for name in order:
        urls = captured.get(name, [])
        if urls:
            print(f"  [franime] Lecteur choisi : {name} → {urls[0][:100]}")
            return urls[0], name, captured, diagnostics

    return None, None, captured, diagnostics


def _select_lecteur(page, lecteur_name: str):
    """Essaie de sélectionner un lecteur spécifique dans le dropdown franime."""
    try:
        # Ouvrir le dropdown
        for sel in ["select", "[class*='lecteur']", "[class*='player-select']", "button:has-text('Lecteur')"]:
            el = page.query_selector(sel)
            if el:
                el.click(timeout=2_000)
                page.wait_for_timeout(500)
                # Chercher l'option du lecteur voulu
                opt = page.query_selector(f"option:has-text('{lecteur_name}'), li:has-text('{lecteur_name}')")
                if opt:
                    opt.click(timeout=2_000)
                    print(f"  [franime] Lecteur sélectionné : {lecteur_name}")
                    page.wait_for_timeout(1_500)
                    return
    except Exception as e:
        print(f"  [franime] Impossible de changer de lecteur : {e}")


# Compteur global pour le rafraîchissement de la console
DOWNLOAD_COUNTER = 0

def download_franime(url: str, output_path: str = "downloads",
                     download_type: str = "video", quality: str = "best") -> dict[str, Any]:
    global DOWNLOAD_COUNTER
    os.makedirs(output_path, exist_ok=True)

    # Gestion de la latence console
    from config import MAX_DOWNLOADS_BEFORE_REFRESH
    DOWNLOAD_COUNTER += 1
    if DOWNLOAD_COUNTER >= MAX_DOWNLOADS_BEFORE_REFRESH:
        clear_terminal()
        DOWNLOAD_COUNTER = 0

    # --- ANTI-SPAM DELAY ---
    print(f"  [franime] Pause de sécurité anti-spam...")
    human_delay(3000, 8000)

    # Préparation des proxies (on ajoute None pour tester sans proxy aussi)
    shuffled_proxies = list(PROXIES)
    random.shuffle(shuffled_proxies)
    proxies_to_try = shuffled_proxies + [None]

    # 1. Tentative d'extraction avec rotation des proxies
    stream_url = None
    nom = None
    all_captured = {}
    
    for proxy in proxies_to_try:
        try:
            print(f"\n  [franime] Analyse de la page (Proxy: {proxy or 'Direct'}) : {url}")
            stream_url, nom, all_captured, diagnostics = extract_stream_url(url, proxy_url=proxy)
            
            if stream_url:
                break # On a trouvé un flux !
                
            if diagnostics.get("blocked") and diagnostics.get("main_status") in {401, 429}:
                print(f"  [franime] ⚠ Proxy bloqué ou erreur fatale ({diagnostics.get('main_status')}). Suivant...")
                continue
                
        except Exception as e:
            print(f"  [franime] ✗ Erreur avec le proxy {proxy} : {e}")
            continue

    if not stream_url and not all_captured:
        return {
            "success": False,
            "error": "Impossible d'extraire les flux, même après rotation des proxies.",
            "reason": "page_blocked"
        }

    # On garde trace des URLs déjà testées pour ne pas boucler inutilement
    tried_urls = set()

    # Fonction interne pour tenter un téléchargement et gérer les erreurs
    def try_download(s_url, s_name):
        if not s_url or s_url in tried_urls:
            return None
        tried_urls.add(s_url)
        
        # On essaie tous les proxies pour ce flux spécifique si nécessaire
        for proxy in proxies_to_try:
            print(f"  [franime] Tentative [{s_name}] (Proxy: {proxy or 'Direct'}) : {s_url[:100]}...")
            try:
                res = _download_stream(s_url, s_name, url, output_path, download_type, quality, proxy_url=proxy)
                if res.get("filename"):
                    return res
            except Exception as e:
                print(f"  [franime] ✗ Échec proxy {proxy} : {e}")
                continue
        return None

    # 2. On essaie d'abord le "meilleur" trouvé lors du premier passage (souvent HLS s'il est là)
    if stream_url:
        result = try_download(stream_url, nom)
        if result: return result

    # 3. On essaie toutes les autres URLs capturées lors du premier passage, par ordre de priorité
    for name in LECTEUR_PRIORITY:
        for s_url in all_captured.get(name, []):
            result = try_download(s_url, name)
            if result: return result

    # 4. Si toujours rien, on force le changement de lecteur dans le navigateur pour les lecteurs restants
    # (ceux qu'on n'a pas encore vus du tout lors du premier passage)
    for lecteur in LECTEUR_PRIORITY:
        # Si on a déjà capturé une URL pour ce lecteur, inutile de forcer (on l'a déjà testée au-dessus)
        if all_captured.get(lecteur):
            continue
        
        print(f"\n  [franime] Forçage du lecteur : {lecteur}")
        try:
            stream_url, nom, captured_now, diagnostics_now = extract_stream_url(url, preferred_lecteur=lecteur)
            # On ne bloque pas sur 403 ici non plus
            if diagnostics_now.get("blocked") and not stream_url and diagnostics_now.get("main_status") != 403:
                status = diagnostics_now.get("main_status")
                print(f"  [franime] ⚠ Bloqué ({status}).")
                continue
            # On tente l'URL principale retournée
            if stream_url:
                result = try_download(stream_url, nom)
                if result: return result
            
            # On tente aussi les autres capturées pendant ce forçage
            for name in LECTEUR_PRIORITY:
                for s_url in captured_now.get(name, []):
                    result = try_download(s_url, name)
                    if result: return result
        except Exception as e:
            print(f"  [franime] Erreur lors du forçage {lecteur} : {e}")

    print(f"  [franime] ⚠ Aucun lecteur n'a fonctionné pour {url}")
    return {
        "success": False,
        "filename": None,
        "filepath": None,
        "error": "Lecteur non capturé ou indisponible sur Franime.",
        "reason": "not_found_or_blocked"
    }


def _download_stream(stream_url: str, lecteur_name: str, page_url: str,
                     output_path: str, download_type: str, quality: str, proxy_url: str | None = None) -> dict[str, Any]:
    import yt_dlp
    import subprocess

    is_sibnet  = "sibnet.ru"   in stream_url
    is_filemoon= "filemoon"    in stream_url
    referer    = (
        "https://video.sibnet.ru/" if is_sibnet else
        "https://filemoon.sx/"     if is_filemoon else
        "https://franime.fr/"
    )

    slug   = re.search(r"/anime/([^?/]+)", page_url)
    title  = slug.group(1) if slug else "franime-video"
    params = dict(p.split("=",1) for p in page_url.split("?",1)[-1].split("&") if "=" in p)
    ep     = f"s{params.get('s','1')}-ep{params.get('ep','1')}-{params.get('lang','vo')}"

    ffmpeg_dir = _resolve_ffmpeg_dir()
    ffmpeg_exe = "ffmpeg"
    ffprobe_exe = "ffprobe"
    if ffmpeg_dir:
        ffmpeg_exe = os.path.join(ffmpeg_dir, "ffmpeg.exe")
        ffprobe_exe = os.path.join(ffmpeg_dir, "ffprobe.exe")

    ydl_opts: dict[str, Any] = {
        "format": "bestvideo+bestaudio/best" if download_type != "audio" else "bestaudio/best",
        "outtmpl": os.path.join(output_path, f"{title}-{ep}.%(ext)s"),
        "quiet": False,
        "no_warnings": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 16,
        "retries": 10,
        "fragment_retries": 10,
        "proxy": proxy_url, # Utilisation du proxy passé en argument
        "http_headers": {
            "Referer":    referer,
            "Origin":     referer.rstrip("/"),
            "User-Agent": random.choice(USER_AGENTS),
        },
    }

    if shutil.which("aria2c"):
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = [
            "--max-connection-per-server=16",
            "--split=16",
            "--min-split-size=1M",
            "--continue=true",
        ]
        print("  [aria2c] Téléchargeur rapide activé pour Franime")

    if ffmpeg_dir:
        ydl_opts["ffmpeg_location"] = ffmpeg_dir

    # Tentative de téléchargement
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(stream_url, download=True)
            filepath = _resolve_downloaded_file(info, output_path, {})
            
            if filepath and os.path.isfile(filepath):
                # --- AUTO-RÉPARATION ---
                print(f"  [franime] Vérification de l'intégrité : {os.path.basename(filepath)}")
                
                # 1. Vérifier si c'est une vraie vidéo lisible
                probe_cmd = [ffprobe_exe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filepath]
                probe_res = subprocess.run(probe_cmd, capture_output=True, text=True)
                
                is_valid = False
                try:
                    duration = float(probe_res.stdout.strip())
                    if duration > 10: # On considère que c'est valide si > 10s
                        is_valid = True
                except:
                    pass

                if not is_valid:
                    print(f"  [franime] ⚠ Vidéo corrompue ou illisible ({os.path.basename(filepath)}). Tentative de réparation...")
                    fixed_path = filepath.replace(".mp4", "_fixed.mp4")
                    # Tentative de remuxage pour réparer le container
                    fix_cmd = [ffmpeg_exe, "-y", "-i", filepath, "-c", "copy", "-map", "0:v", "-map", "0:a?", fixed_path]
                    fix_res = subprocess.run(fix_cmd, capture_output=True)
                    
                    if fix_res.returncode == 0 and os.path.exists(fixed_path) and os.path.getsize(fixed_path) > 1000000:
                        os.remove(filepath)
                        os.rename(fixed_path, filepath)
                        print(f"  [franime] ✓ Réparation réussie.")
                    else:
                        if os.path.exists(fixed_path): os.remove(fixed_path)
                        print(f"  [franime] ✗ Réparation échouée. Suppression pour retenter.")
                        os.remove(filepath)
                        raise Exception("Vidéo corrompue et non réparable.")

                return {
                    "success": True,
                    "filename": os.path.basename(filepath),
                    "filepath": filepath,
                    "folder": os.path.basename(output_path)
                }
        except Exception as e:
            print(f"  [franime] Erreur téléchargement/réparation : {e}")
            if 'filepath' in locals() and filepath and os.path.exists(filepath):
                os.remove(filepath)
            raise e

    return {"success": False, "error": "Échec final"}


def _find_chrome() -> str | None:
    import platform
    if platform.system() == "Windows":
        for base in ["PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"]:
            p = os.path.join(os.environ.get(base, ""), "Google", "Chrome", "Application", "chrome.exe")
            if os.path.isfile(p): return p
    elif platform.system() == "Darwin":
        p = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.isfile(p): return p
    else:
        for cmd in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            f = shutil.which(cmd); 
            if f: return f
    return None

def _chrome_user_data_dir() -> str | None:
    import platform
    if platform.system() == "Windows":
        return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    if platform.system() == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    return os.path.expanduser("~/.config/google-chrome")

def _resolve_ffmpeg_dir() -> str | None:
    if shutil.which("ffmpeg") and shutil.which("ffprobe"): return None
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages" / \
        "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe" / "ffmpeg-8.1.1-full_build" / "bin"
    return str(p) if (p / "ffmpeg.exe").exists() else None
