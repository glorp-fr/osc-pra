"""Version de l'application et vérification de mise à jour disponible — voir
CLAUDE.MD, section « Versioning et mises à jour ».

La version courante est lue depuis le fichier VERSION à la racine du dépôt
(mis à jour par update.sh lors d'un `git checkout` vers un tag de release).
La dernière release GitHub est interrogée au plus une fois toutes les
CHECK_INTERVAL_SECONDS (résultat mis en cache en mémoire) pour ne pas
solliciter l'API GitHub à chaque affichage de page ; un échec (offline,
GitHub injoignable, dépôt privé...) est silencieux — l'absence de message de
mise à jour ne doit jamais être interprétée comme "à jour"."""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
GITHUB_REPO = "glorp-fr/osc-pra"
CHECK_INTERVAL_SECONDS = 6 * 60 * 60
CHECK_TIMEOUT_SECONDS = 3

_cache = {"checked_at": 0.0, "latest": None}


def current_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except FileNotFoundError:
        return "0.0.0"


def _parse_version(version: str) -> tuple:
    """Tolérant aux tags non strictement SemVer (ex. suffixe -rc1) : les
    composants non numériques arrêtent le découpage plutôt que de lever une
    exception, pour rester comparable a minima sur major.minor[.patch]."""
    parsed = []
    for part in version.lstrip("v").split("."):
        try:
            parsed.append(int(part))
        except ValueError:
            break
    return tuple(parsed)


def _fetch_latest_release() -> str | None:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())
        tag = data.get("tag_name") or ""
        return tag.lstrip("v") or None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def available_update() -> str | None:
    """Retourne le numéro de la dernière release GitHub si elle est plus
    récente que la version installée, sinon None."""
    now = time.monotonic()
    if now - _cache["checked_at"] >= CHECK_INTERVAL_SECONDS:
        _cache["latest"] = _fetch_latest_release()
        _cache["checked_at"] = now

    latest = _cache["latest"]
    if latest and _parse_version(latest) > _parse_version(current_version()):
        return latest
    return None
