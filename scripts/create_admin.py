#!/usr/bin/env python3
"""Crée (ou met à jour) le compte admin. Le mot de passe est lu depuis
la variable d'environnement OSC_PRA_ADMIN_PASSWORD, ou demandé de façon
interactive si elle n'est pas définie."""
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from passlib.hash import bcrypt

from app.db import get_connection, init_db


def main() -> None:
    username = os.environ.get("OSC_PRA_ADMIN_USER", "admin")
    password = os.environ.get("OSC_PRA_ADMIN_PASSWORD")
    if not password:
        password = getpass.getpass(f"Mot de passe pour le compte admin '{username}': ")
        confirm = getpass.getpass("Confirmation: ")
        if password != confirm:
            print("Les mots de passe ne correspondent pas.", file=sys.stderr)
            sys.exit(1)

    if len(password) < 8:
        print("Le mot de passe doit faire au moins 8 caractères.", file=sys.stderr)
        sys.exit(1)

    init_db()
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO users (username, password_hash, is_admin)
        VALUES (?, ?, 1)
        ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash
        """,
        (username, bcrypt.hash(password)),
    )
    conn.commit()
    conn.close()
    print(f"Compte admin '{username}' créé/mis à jour.")


if __name__ == "__main__":
    main()
