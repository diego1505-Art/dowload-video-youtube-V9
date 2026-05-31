import os
import shutil
import time
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

from config import (
    DOWNLOAD_FOLDER,
    LOCAL_MEDIA_TTL_SECONDS,
    LOCAL_MEDIA_MAX_ENTRIES,
    ALLOWED_LOCAL_EXTENSIONS,
    PREVIEWABLE_EXTENSIONS,
)
from utils import has_ffmpeg, is_http_url
from playlist import download_media

app = Flask(__name__)

DOWNLOAD_FOLDER.mkdir(exist_ok=True)
LOCAL_MEDIA_REGISTRY: dict[str, tuple[Path, float]] = {}


def is_allowed_local_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in ALLOWED_LOCAL_EXTENSIONS


def copy_local_media(source_path: Path) -> dict[str, str]:
    safe_name = secure_filename(source_path.name) or f"local-{uuid.uuid4().hex}{source_path.suffix.lower()}"
    destination = DOWNLOAD_FOLDER / f"{uuid.uuid4().hex[:8]}-{safe_name}"
    shutil.copy2(source_path, destination)
    return {
        "filename": destination.name,
        "filepath": str(destination),
        "file_url": f"/media/{destination.name}",
    }


def cleanup_local_media_registry() -> None:
    now = time.time()
    expired_tokens = [
        token
        for token, (_, created_at) in LOCAL_MEDIA_REGISTRY.items()
        if now - created_at > LOCAL_MEDIA_TTL_SECONDS
    ]
    for token in expired_tokens:
        LOCAL_MEDIA_REGISTRY.pop(token, None)

    if len(LOCAL_MEDIA_REGISTRY) <= LOCAL_MEDIA_MAX_ENTRIES:
        return

    tokens_by_age = sorted(
        LOCAL_MEDIA_REGISTRY.items(),
        key=lambda item: item[1][1],
    )
    overflow = len(LOCAL_MEDIA_REGISTRY) - LOCAL_MEDIA_MAX_ENTRIES
    for token, _ in tokens_by_age[:overflow]:
        LOCAL_MEDIA_REGISTRY.pop(token, None)


def register_local_media(source_path: Path) -> dict[str, str]:
    cleanup_local_media_registry()
    token = uuid.uuid4().hex
    LOCAL_MEDIA_REGISTRY[token] = (source_path.resolve(), time.time())
    return {
        "filename": source_path.name,
        "filepath": str(source_path),
        "file_url": f"/media-local/{token}",
    }


@app.route("/")
def index():
    return render_template("dowload.html", has_ffmpeg=has_ffmpeg())


@app.route("/check-ffmpeg")
def check_ffmpeg():
    return jsonify({"has_ffmpeg": has_ffmpeg()})


@app.route("/media/<path:filename>")
def serve_media(filename: str):
    return send_from_directory(DOWNLOAD_FOLDER, filename, as_attachment=False)


@app.route("/media-local/<token>")
def serve_local_media(token: str):
    cleanup_local_media_registry()
    entry = LOCAL_MEDIA_REGISTRY.get(token)
    if entry is None:
        abort(404)
    source_path, _ = entry
    if not source_path.is_file():
        LOCAL_MEDIA_REGISTRY.pop(token, None)
        abort(404)
    LOCAL_MEDIA_REGISTRY[token] = (source_path, time.time())
    return send_file(source_path, as_attachment=False)


@app.route("/open-folder")
def open_folder():
    try:
        os.startfile(str(DOWNLOAD_FOLDER))  # type: ignore[attr-defined]
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/upload-local", methods=["POST"])
def upload_local():
    try:
        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"success": False, "error": "Aucun fichier local recu."}), 400

        suffix = Path(file.filename).suffix.lower()
        if suffix not in ALLOWED_LOCAL_EXTENSIONS:
            return jsonify({"success": False, "error": "Format de fichier non supporte."}), 400

        safe_name = secure_filename(file.filename) or f"local-{uuid.uuid4().hex}{suffix}"
        stored_name = f"{uuid.uuid4().hex[:8]}-{safe_name}"
        destination = DOWNLOAD_FOLDER / stored_name
        file.save(destination)

        return jsonify(
            {
                "success": True,
                "filename": stored_name,
                "file_url": f"/media/{stored_name}",
                "source_type": "local",
                "source_label": file.filename,
                "message": "Fichier local charge avec succes.",
                "previewable": Path(stored_name).suffix.lower() in PREVIEWABLE_EXTENSIONS,
            }
        )
    except Exception as e:
        return jsonify({"success": False, "error": f"Upload local impossible: {e}"}), 500


@app.route("/fetch-episodes", methods=["POST"])
def fetch_episodes():
    try:
        data = request.get_json() or {}
        url = data.get("url", "").strip()
        if not url:
            return jsonify({"success": False, "error": "URL manquante."}), 400

        from urllib.parse import parse_qs, urlparse
        import json
        import re
        import requests

        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        anime_id = query.get("anime_id", [None])[0]
        lang = query.get("lang", ["vo"])[0]
        try:
            requested_season = max(1, int(query.get("s", ["1"])[0] or "1"))
        except ValueError:
            requested_season = 1
        
        # On extrait le "slug" de l'anime (le nom dans l'URL)
        path_parts = parsed.path.split('/')
        anime_slug = path_parts[-1] if path_parts[-1] else path_parts[-2]
        base_page_url = url.split('?')[0]

        # --- MÉTHODE 1 : MyAnimeList ---
        try:
            search_name = anime_slug.replace('-', ' ')
            # On cherche sur MAL pour avoir la structure globale
            search_res = requests.get(f"https://api.jikan.moe/v4/anime?q={search_name}&type=tv&order_by=start_date&sort=asc", timeout=5)

            if search_res.ok:
                results = search_res.json().get('data', [])
                keywords = [k for k in anime_slug.split('-') if len(k) > 3]
                main_results = [r for r in results if any(k in r['title'].lower() for k in keywords)]
                
                if not main_results:
                    main_results = results[:3]

                all_urls = []
                summary_info = []

                # Si l'utilisateur colle une URL avec s=2, il veut cette saison précise.
                # Pour s=1, on garde le comportement "tout trouver" historique.
                seasons_to_generate = list(enumerate(main_results, start=1))
                if requested_season > 1 or data.get("current_only"):
                    seasons_to_generate = [
                        item for item in seasons_to_generate if item[0] == requested_season
                    ] or [(requested_season, main_results[min(requested_season - 1, len(main_results) - 1)])]

                for season_num, anime_entry in seasons_to_generate:
                    # On récupère le nombre d'épisodes de MAL
                    ep_count = anime_entry.get('episodes')
                    
                    # Si MAL ne connaît pas encore le nombre (anime en cours) ou si c'est 0/None
                    # on met une valeur par défaut élevée (ex: 24 ou 25) pour ne rien rater
                    if not ep_count or ep_count == 0:
                        ep_count = 25 # Valeur par défaut pour les animes en cours
                        print(f"[DEBUG] Saison {season_num} : Nombre d'épisodes inconnu sur MAL, on tente {ep_count}")
                    
                    # Franime peut garder le même anime_id pour plusieurs saisons.
                    # La séparation fiable est donc le paramètre s=, pas l'ID.
                    season_best_id = anime_id
                    print(f"[DEBUG] Saison {season_num} -> anime_id conserve : {season_best_id}")
                    for ep_num in range(1, ep_count + 1):
                        all_urls.append(f"{base_page_url}?s={season_num}&ep={ep_num}&lang={lang}&anime_id={season_best_id}")
                    
                    summary_info.append(f"S{season_num} ({ep_count} eps)")

                if all_urls:
                    return jsonify({
                        "success": True,
                        "urls": all_urls,
                        "count": len(all_urls),
                        "anime_title": f"{main_results[0]['title']} - {' + '.join(summary_info)}"
                    })
        except Exception:
            pass

        # --- MÉTHODE 2 : Scraping Franime (Fallback) ---
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Referer": "https://franime.fr/",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.ok:
                match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', response.text, re.DOTALL)
                if match:
                    next_data = json.loads(match.group(1))
                    props = next_data.get("props", {}).get("pageProps", {})
                    anime_data = props.get("anime") or props.get("data")
                    if anime_data and "saisons" in anime_data:
                        return generate_episode_urls(
                            anime_data["saisons"],
                            base_page_url,
                            lang,
                            anime_id,
                            anime_data.get("title", "Anime"),
                            requested_season=requested_season if requested_season > 1 or data.get("current_only") else None,
                        )
        except Exception:
            pass

        return jsonify({
            "success": False, 
            "error": "Impossible de trouver la liste des épisodes automatiquement. Le site Franime bloque peut-être l'accès."
        }), 400

    except Exception as e:
        return jsonify({"success": False, "error": f"Erreur système: {str(e)}"}), 500

def generate_episode_urls(seasons, base_url, lang, anime_id, title, requested_season=None):
    all_urls = []
    for s_idx, season in enumerate(seasons):
        episodes = season.get("episodes", [])
        s_num = s_idx + 1
        if requested_season is not None and s_num != requested_season:
            continue
        for ep_idx, _ in enumerate(episodes):
            ep_num = ep_idx + 1
            ep_url = f"{base_url}?s={s_num}&ep={ep_num}&lang={lang}&anime_id={anime_id}"
            all_urls.append(ep_url)
    
    return jsonify({
        "success": True,
        "urls": all_urls,
        "count": len(all_urls),
        "anime_title": title
    })


@app.route("/download", methods=["POST"])
def download():
    try:
        data = request.get_json() or {}
        # Nettoyage agressif de l'URL pour supprimer les retours a la ligne accidentels
        source_value = data.get("url", "").strip().replace("\n", "").replace("\r", "")
        download_type = data.get("type", "video")
        quality = data.get("quality", "best")

        if not source_value:
            return jsonify({"success": False, "error": "Merci de renseigner une URL ou un chemin local."}), 400

        local_path = Path(source_value).expanduser()
        if not is_http_url(source_value) and is_allowed_local_file(local_path):
            registered = register_local_media(local_path)
            return jsonify(
                {
                    "success": True,
                    "message": "Fichier local pret sans copie.",
                    "filename": registered["filename"],
                    "file_url": registered["file_url"],
                    "source_type": "local",
                    "source_label": str(local_path),
                    "previewable": local_path.suffix.lower() in PREVIEWABLE_EXTENSIONS,
                }
            )

        if not is_http_url(source_value):
            return jsonify({"success": False, "error": "URL invalide ou chemin local introuvable."}), 400

        result = download_media(
            source_value,
            output_path=str(DOWNLOAD_FOLDER),
            download_type=download_type,
            quality=quality,
        )

        filename = result.get("filename")
        relative_path = result.get("relative_path") or filename
        
        if result.get("skipped"):
            reason = result.get("reason")
            if reason == "already_exists":
                reason_msg = "Déjà téléchargé."
            elif reason == "page_blocked":
                reason_msg = "Page bloquée par le serveur distant."
            else:
                reason_msg = "Introuvable ou indisponible."
            return jsonify({
                "success": True,
                "skipped": True,
                "reason": reason,
                "message": f"Episode sauté : {reason_msg}",
                "filename": filename,
                "source_label": source_value
            })

        if not filename:
            return jsonify({"success": False, "error": result.get("error", "Fichier telecharge mais introuvable.")}), 500

        from urllib.parse import urlparse

        domain = urlparse(source_value).netloc.replace("www.", "") or "remote"
        previewable = Path(filename).suffix.lower() in PREVIEWABLE_EXTENSIONS
        payload = {
            "success": True,
            "message": f"Telecharge depuis {domain} : {filename}",
            "filename": filename,
            "file_url": f"/media/{relative_path}",
            "source_type": domain,
            "source_label": source_value,
            "previewable": previewable,
            "storage_folder": result.get("folder"),
        }

        if download_type == "audio":
            payload["message"] = f"Audio telecharge : {filename}"
        elif not previewable:
            payload["message"] = f"Telecharge : {filename} (apercu non supporte par le navigateur)"

        return jsonify(payload)

    except Exception as e:
        error_msg = str(e)
        if "ffmpeg" in error_msg.lower() or "ffprobe" in error_msg.lower():
            return jsonify(
                {
                    "success": False,
                    "error": "FFmpeg est requis mais n'est pas installe. Installe FFmpeg pour continuer.",
                    "is_ffmpeg_error": True,
                }
            ), 500
        return jsonify({"success": False, "error": f"Echec du telechargement: {error_msg}"}), 500


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("DowFlow — Multi-Site Media Downloader")
    print("=" * 60)
    print("\nServeur démarré sur http://127.0.0.1:5001")
    print("Ctrl+C pour arrêter\n")
    app.run(debug=False, host="127.0.0.1", port=5001)
