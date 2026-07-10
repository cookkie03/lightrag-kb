#!/usr/bin/env python3
"""CLI di amministrazione per obsidian-mcp-oauth-shim.

Uso:
  python3 manage.py list-clients
  python3 manage.py revoke-client <client_id>
  python3 manage.py rotate-jwt-secret
  python3 manage.py rotate-passcode
"""
import secrets
import sys

from dotenv import set_key

from app import config, db


def list_clients():
    clients = db.list_clients()
    if not clients:
        print("Nessun client registrato.")
        return
    for c in clients:
        print(f"- {c['client_id']}  ({c['client_name']})  redirect_uris={c['redirect_uris']}")


def revoke_client(client_id: str):
    if not db.get_client(client_id):
        print(f"Client {client_id} non trovato.")
        sys.exit(1)
    db.revoke_all_refresh_tokens_for_client(client_id)
    db.delete_client(client_id)
    print(f"Client {client_id} revocato (client + auth code + refresh token eliminati).")


def rotate_jwt_secret():
    new_secret = secrets.token_urlsafe(48)
    set_key(str(config.ENV_PATH), "JWT_SECRET", new_secret, quote_mode="never")
    print("JWT_SECRET rigenerato. Tutti gli access token in circolazione sono ora invalidi.")
    print("Riavvia il servizio: launchctl kickstart -k gui/$(id -u)/com.luca.obsidianmcpoauthshim")


def rotate_passcode():
    new_passcode = secrets.token_urlsafe(12)
    set_key(str(config.ENV_PATH), "CONSENT_PASSCODE", new_passcode, quote_mode="never")
    print(f"Nuovo passcode: {new_passcode}")
    print("Riavvia il servizio: launchctl kickstart -k gui/$(id -u)/com.luca.obsidianmcpoauthshim")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "list-clients":
        list_clients()
    elif cmd == "revoke-client":
        if len(sys.argv) < 3:
            print("Uso: manage.py revoke-client <client_id>")
            sys.exit(1)
        revoke_client(sys.argv[2])
    elif cmd == "rotate-jwt-secret":
        rotate_jwt_secret()
    elif cmd == "rotate-passcode":
        rotate_passcode()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    db.init_db()
    main()
