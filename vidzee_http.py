"""VidZee without a browser (ported from the Akbots meowly resolvers: resolve_vidzee).

  1. GET core.vidzee.wtf/api-key                 -> base64 blob
  2. derive the AES key from it (fixed SHA-256 key, AES-GCM)
  3. GET player.vidzee.wtf/api/server?id=..&sr=0..13 (&ss=&ep= for episodes) -> encrypted link list
  4. decrypt each link (AES-CBC, key = derived key padded to 32 bytes) -> direct m3u8 / mp4

Returns (url, headers) for the first decrypted http(s) link, or None. Any failure returns None so the caller can fall
back to the browser sniff (meowly_dl.py).
"""
import base64
import logging

import requests
from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Util.Padding import unpad

logger = logging.getLogger(__name__)

PLAYER = "https://player.vidzee.wtf"
CORE = "https://core.vidzee.wtf"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
           "Referer": PLAYER, "Origin": PLAYER}
TIMEOUT = 15


def _derive_key(api_key_text: str) -> str:
    if not api_key_text:
        return ""
    try:
        s = api_key_text.replace(" ", "").replace("\n", "")
        raw = base64.b64decode(s + "=" * (-len(s) % 4))
        if len(raw) <= 28:
            return ""
        iv, tag, payload = raw[:12], raw[12:28], raw[28:]
        key_mat = SHA256.new(b"c4a8f1d7e2b9a6c3d0f5e8a1b7c4d9e2").digest()
        cipher = AES.new(key_mat, AES.MODE_GCM, nonce=iv)
        return cipher.decrypt_and_verify(payload, tag).decode("utf-8")
    except Exception:
        return ""


def _decrypt_link(encrypted: str, key: str) -> str:
    if not encrypted or not key:
        return ""
    try:
        decoded = base64.b64decode(encrypted).decode("utf-8")
        iv_str, cipher_str = decoded.split(":", 1)
        iv = base64.b64decode(iv_str)
        k = key.encode("utf-8").ljust(32, b"\0")[:32]
        ct = base64.b64decode(cipher_str)
        return unpad(AES.new(k, AES.MODE_CBC, iv=iv).decrypt(ct), 16).decode("utf-8")
    except Exception:
        return ""


def _get(session: requests.Session, url: str):
    try:
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        return r if r.status_code == 200 else None
    except requests.RequestException:
        return None


def resolve(tmdb_id, season=None, episode=None):
    """-> (url, headers) of a direct stream, or None."""
    session = requests.Session()
    r = _get(session, f"{CORE}/api-key")
    if not r:
        return None
    key = _derive_key(r.text)
    if not key:
        return None
    season_n, episode_n = season or 1, episode or 1
    for sr in range(14):
        url = f"{PLAYER}/api/server?id={tmdb_id}&sr={sr}"
        if season:
            url += f"&ss={season_n}&ep={episode_n}"
        resp = _get(session, url)
        if not resp:
            continue
        try:
            data = resp.json()
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("url"), list):
            continue
        for entry in data["url"]:
            if not isinstance(entry, dict) or not entry.get("link"):
                continue
            link = _decrypt_link(entry["link"], key)
            if link.startswith("http"):
                logger.info(f"vidzee_http: resolved {tmdb_id} via server sr={sr}")
                return link, dict(HEADERS)
    return None
