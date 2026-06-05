import asyncio
import os
import sys

import twitchio
from dotenv import load_dotenv

PLACEHOLDERS = {"", "...", "deine_client_id", "dein_client_secret"}


def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value in PLACEHOLDERS:
        raise SystemExit(
            f"Fehlt/ungültig: {name}. Trage den Wert in deine .env ein "
            f"oder setze die Umgebungsvariable."
        )
    return value


def get_logins() -> list[str]:
    # Nutzung: python get_twitch_ids.py streamer_login bot_login
    logins = [arg.strip().lower() for arg in sys.argv[1:] if arg.strip()]

    if not logins:
        channel = os.getenv("TWITCH_CHANNEL", "").strip().lower()
        if channel:
            logins.append(channel)

        print("Twitch-Logins eingeben, nicht Display-Namen.")
        if not channel:
            streamer = input("Streamer/Kanal-Login: ").strip().lower()
            if streamer:
                logins.append(streamer)

        bot = input("Bot-Account-Login: ").strip().lower()
        if bot:
            logins.append(bot)

    # Duplikate entfernen, Reihenfolge behalten
    return list(dict.fromkeys(logins))


async def main() -> None:
    load_dotenv()

    client_id = get_required_env("TWITCH_CLIENT_ID")
    client_secret = get_required_env("TWITCH_CLIENT_SECRET")
    logins = get_logins()

    if not logins:
        raise SystemExit("Keine Twitch-Logins angegeben.")

    async with twitchio.Client(client_id=client_id, client_secret=client_secret) as client:
        await client.login()
        users = await client.fetch_users(logins=logins)

    found = {user.name.lower(): user for user in users if user.name is not None}
    for login in logins:
        user = found.get(login)
        if user is None or user.name is None:
            print(f"Nicht gefunden: {login}")
            continue
        print(f"User: {user.name} - ID: {user.id}")


if __name__ == "__main__":
    asyncio.run(main())
