import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates

STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _static_version() -> str:
    """Hash du CSS pour invalider le cache navigateur à chaque déploiement qui le modifie."""
    css = STATIC_DIR / "style.css"
    if not css.exists():
        return "0"
    return hashlib.sha256(css.read_bytes()).hexdigest()[:10]


templates.env.globals["asset_version"] = _static_version()
