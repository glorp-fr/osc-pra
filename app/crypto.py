from pathlib import Path

from cryptography.fernet import Fernet

KEY_PATH = Path(__file__).resolve().parent.parent / "data" / "encryption.key"


def _load_or_create_key() -> bytes:
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes()
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    KEY_PATH.chmod(0o600)
    return key


def encrypt(value: str) -> str:
    if not value:
        return ""
    return Fernet(_load_or_create_key()).encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value:
        return ""
    return Fernet(_load_or_create_key()).decrypt(value.encode()).decode()
