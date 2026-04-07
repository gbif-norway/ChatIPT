import os
from typing import Optional, Dict, Any

import requests


def get_developer_user_id() -> str:
    # New generic key, with backward compatibility for earlier rollout naming.
    return (
        os.getenv("DISCORD_DEVELOPER_USER_ID", "").strip()
        or os.getenv("DISCORD_RUKAYA_USER_ID", "").strip()
    )


def get_developer_mention() -> str:
    developer_user_id = get_developer_user_id()
    if developer_user_id:
        return f"<@{developer_user_id}>"

    fallback_handle = os.getenv("DISCORD_DEVELOPER_HANDLE", "@_rkian").strip()
    return fallback_handle or "@_rkian"


def send_discord_message(message: str, allowed_mentions: Optional[Dict[str, Any]] = None):
    print(message)
    payload: Dict[str, Any] = {"content": message}
    if allowed_mentions is not None:
        payload["allowed_mentions"] = allowed_mentions

    response = requests.post(os.getenv('DISCORD_WEBHOOK'), json=payload)

    if response.status_code == 204:
        print("Message sent successfully.")
    else:
        print(f"Failed to send message. Status code: {response.status_code}")
