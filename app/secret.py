import secrets
from pathlib import Path

SECRET_PATH = Path(__file__).resolve().parent.parent / "data" / "session.key"


def load_or_create_secret() -> str:
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SECRET_PATH.exists():
        return SECRET_PATH.read_text().strip()
    key = secrets.token_hex(32)
    SECRET_PATH.write_text(key)
    SECRET_PATH.chmod(0o600)
    return key
