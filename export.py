#!/usr/bin/env python3
"""
Music Favorites Exporter
=======================
Export your liked songs from YouTube Music and Deezer to JSON.
Then use SpotiFLAC-Next or deemix to download in FLAC.

Usage:
    python3 export.py --ytmusic
    python3 export.py --deezer --deezer-user-id 12345678
    python3 export.py --deezer --deezer-arl YOUR_ARL_TOKEN
    python3 export.py --ytmusic --deezer --deezer-user-id 12345678
"""

import argparse
import json
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path

OUTPUT_FILE = "favorites.json"

LIBREWOLF_PROFILE_PATHS = [
    "~/.config/librewolf/librewolf",
    "~/.librewolf",
    "~/snap/librewolf/common/.librewolf",
]


# ---------------------------------------------------------------------------
# LibreWolf cookie extraction (no third-party cookie libs needed)
# ---------------------------------------------------------------------------

def _find_librewolf_profile() -> Path | None:
    """Find the default LibreWolf profile directory."""
    for base in LIBREWOLF_PROFILE_PATHS:
        expanded = Path(base).expanduser()
        if not expanded.is_dir():
            continue
        # Look for profiles.ini to find the default profile
        profiles_ini = list(expanded.glob("**/profiles.ini"))
        if profiles_ini:
            import configparser
            cfg = configparser.ConfigParser()
            cfg.read(profiles_ini[0], encoding="utf-8")
            # Prefer Install* sections, then Default=1
            profile_path = None
            for section in cfg.sections():
                if section.startswith("Install"):
                    profile_path = cfg[section].get("Default")
                    break
                if cfg[section].get("Default") == "1" and not profile_path:
                    profile_path = cfg[section].get("Path")

            if profile_path:
                # Check if absolute or relative
                for section in cfg.sections():
                    if cfg[section].get("Path") == profile_path:
                        if cfg[section].get("IsRelative") == "0":
                            return Path(profile_path)
                        else:
                            return Path(profiles_ini[0]).parent / profile_path

        # Fallback: find any profile with cookies.sqlite
        for cookie_file in expanded.glob("**/cookies.sqlite"):
            return cookie_file.parent

    return None


def _decrypt_aes256_gcm(key: bytes, iv: bytes, ciphertext: bytes, tag: bytes) -> bytes:
    """Decrypt AES-256-GCM (Firefox 80+ cookie encryption)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aesgcm = AESGCM(key)
    # AES-GCM: tag is appended to ciphertext
    return aesgcm.decrypt(iv, ciphertext + tag, None)


def _decrypt_3des(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """Decrypt 3DES-CBC (older Firefox cookie encryption)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_padding
    cipher = Cipher(algorithms.TripleDES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    # Remove PKCS7 padding
    unpadder = sym_padding.PKCS7(64).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _derive_key_pbkdf2(password: bytes, salt: bytes, iterations: int, length: int = 32) -> bytes:
    """Derive key using PBKDF2-SHA256 (Firefox key derivation)."""
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password, salt, iterations, dklen=length)


def _decrypt_firefox_key(key4db_path: Path) -> bytes | None:
    """Extract and decrypt the master key from key4.db.

    LibreWolf/Firefox on Linux with no master password uses an empty password.
    """
    if not key4db_path.exists():
        return None

    conn = sqlite3.connect(f"file:{key4db_path}?mode=ro", uri=True)
    try:
        # Get global salt from metaData
        row = conn.execute(
            "SELECT item1, item2 FROM metaData WHERE id = 'password-check'"
        ).fetchone()
        if not row:
            return None

        global_salt = row[0]  # item1 is the global salt
        password_check = row[2] if len(row) > 2 else row[1]

        # Get the encrypted key from nssPrivate
        row = conn.execute(
            "SELECT a11, a102 FROM nssPrivate WHERE a11 IS NOT NULL"
        ).fetchone()
        if not row:
            return None

        entry_salt = row[0]
        encrypted_key = row[1]

        if not encrypted_key:
            return None

        # Parse the ASN.1 structure of the encrypted key
        # Format: SEQUENCE { SEQUENCE { OID, ... }, OCTET STRING }
        # The encrypted key data starts after the ASN.1 headers

        # Try to find the encrypted data offset
        # Mozilla uses a BER-encoded structure, we need to extract the raw encrypted bytes
        # The structure is: 30 82 ... (SEQUENCE) containing algorithm info + encrypted data

        # Simple approach: find the pattern that indicates the start of encrypted data
        # The last OCTET STRING in the SEQUENCE contains the encrypted key

        # Actually, let's parse it properly
        # The nssPrivate.a102 field contains: SEQUENCE { algorithmId, encryptedData }
        # We need to extract both parts

        data = encrypted_key
        if isinstance(data, str):
            data = bytes.fromhex(data)

        # Parse ASN.1 to extract salt and encrypted value
        # Mozilla's format for key4.db:
        # - Global salt (from metaData.item1)
        # - Entry salt (from nssPrivate.a11)
        # - Password: empty string (no master password)
        # Derive key using PBKDF2(empty_password, global_salt, iterations)

        # For AES-256-GCM (modern Firefox):
        # The a102 blob structure:
        # 30 <len> - SEQUENCE
        #   30 <len> - SEQUENCE (algorithm)
        #     06 <len> <oid> - OID
        #     04 <len> <salt> - OCTET STRING (entry salt)
        #   04 <len> <encrypted> - OCTET STRING (encrypted key)

        # Parse the ASN.1 structure
        def _read_asn1_len(data, offset):
            """Read ASN.1 length field."""
            b = data[offset]
            if b < 0x80:
                return b, offset + 1
            num_bytes = b & 0x7F
            length = 0
            for i in range(num_bytes):
                length = (length << 8) | data[offset + 1 + i]
            return length, offset + 1 + num_bytes

        def _find_encrypted_key(data):
            """Extract the encrypted key bytes and entry salt from ASN.1 structure."""
            # Quick and dirty: find the pattern
            # Look for the OCTET STRING containing the encrypted data
            # It's the last significant blob in the SEQUENCE
            offset = 0
            if data[offset] != 0x30:
                return None, None
            _, offset = _read_asn1_len(data, offset + 1)

            # First inner SEQUENCE (algorithm identifier + salt)
            if data[offset] != 0x30:
                return None, None
            inner_len, inner_offset = _read_asn1_len(data, offset + 1)
            inner_end = inner_offset + inner_len

            # Skip OID
            if data[inner_offset] == 0x06:
                oid_len, inner_offset = _read_asn1_len(data, inner_offset + 1)
                inner_offset += oid_len

            # Read entry salt (OCTET STRING)
            entry_salt = None
            if data[inner_offset] == 0x04:
                salt_len, salt_offset = _read_asn1_len(data, inner_offset + 1)
                entry_salt = data[salt_offset:salt_offset + salt_len]
                inner_offset = salt_offset + salt_len

            offset = inner_end

            # The encrypted key (OCTET STRING)
            encrypted = None
            if data[offset] == 0x04:
                enc_len, enc_offset = _read_asn1_len(data, offset + 1)
                encrypted = data[enc_offset:enc_offset + enc_len]

            return entry_salt, encrypted

        entry_salt_from_asn1, encrypted_data = _find_encrypted_key(data)
        if entry_salt_from_asn1:
            entry_salt = entry_salt_from_asn1

        if not encrypted_data:
            return None

        # Derive the key using PBKDF2 with empty password
        iterations = 1  # Firefox default for no master password
        derived = _derive_key_pbkdf2(b"", global_salt, iterations, 32)

        # Try AES-256-GCM first (modern Firefox/LibreWolf)
        # The encrypted_data format: iv (12 bytes) + ciphertext + tag (16 bytes)
        if len(encrypted_data) > 28:  # At least iv + some ciphertext + tag
            try:
                iv = encrypted_data[:12]
                ciphertext_and_tag = encrypted_data[12:]
                decrypted = _decrypt_aes256_gcm(
                    derived[:32], iv,
                    ciphertext_and_tag[:-16],  # ciphertext
                    ciphertext_and_tag[-16:],  # tag
                )
                return decrypted
            except Exception:
                pass

        # Try 3DES-CBC (older format)
        try:
            iv = encrypted_data[:8]
            ciphertext = encrypted_data[8:]
            decrypted = _decrypt_3des(derived[:24], iv, ciphertext)
            return decrypted
        except Exception:
            pass

        return None
    except Exception:
        return None
    finally:
        conn.close()


def _extract_cookies_from_db(cookies_db: Path, domain: str, master_key: bytes | None = None) -> dict[str, str]:
    """Read cookies from cookies.sqlite, decrypting if necessary."""
    # Copy the database to a temp file to avoid locking issues
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    import shutil
    shutil.copy2(cookies_db, tmp_path)

    cookies = {}
    try:
        conn = sqlite3.connect(str(tmp_path))
        rows = conn.execute(
            "SELECT name, encrypted_value, value, host, path, isSecure, isHttpOnly "
            "FROM moz_cookies WHERE host LIKE ? OR host LIKE ?",
            (f"%{domain}", f".{domain}")
        ).fetchall()

        for name, encrypted_value, plain_value, host, path, is_secure, is_httponly in rows:
            # If there's a plain value, use it directly
            if plain_value:
                cookies[name] = plain_value
                continue

            # Decrypt the encrypted value
            if encrypted_value and master_key:
                try:
                    decrypted = _decrypt_cookie_value(encrypted_value, master_key)
                    if decrypted:
                        cookies[name] = decrypted
                except Exception:
                    pass
    finally:
        conn.close()
        tmp_path.unlink(missing_ok=True)

    return cookies


def _decrypt_cookie_value(encrypted_value: bytes, master_key: bytes) -> str | None:
    """Decrypt a single cookie value using the master key."""
    if not encrypted_value:
        return None

    # Modern Firefox/LibreWolf: prefix b"v11" or b"v10" + AES-256-GCM
    # v10/v11 = AES-256-GCM, iv is bytes 1-13, ciphertext+tag is the rest
    if encrypted_value[:3] in (b"v10", b"v11"):
        iv = encrypted_value[3:15]  # 12 bytes
        ciphertext_and_tag = encrypted_value[15:]
        if len(ciphertext_and_tag) > 16:
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                aesgcm = AESGCM(master_key[:32])
                decrypted = aesgcm.decrypt(iv, ciphertext_and_tag, None)
                return decrypted.decode("utf-8", errors="replace")
            except Exception:
                pass

    return None


def _grab_librewolf_cookies(domain: str) -> dict[str, str]:
    """Extract cookies from LibreWolf for the given domain."""
    profile = _find_librewolf_profile()
    if not profile:
        raise FileNotFoundError("Could not find LibreWolf profile directory")

    cookies_db = profile / "cookies.sqlite"
    key4db = profile / "key4.db"

    if not cookies_db.exists():
        raise FileNotFoundError(f"No cookies.sqlite in {profile}")

    # Try to get the master key for decryption
    master_key = None
    if key4db.exists():
        master_key = _decrypt_firefox_key(key4db)

    return _extract_cookies_from_db(cookies_db, domain, master_key)


# ---------------------------------------------------------------------------
# YouTube Music
# ---------------------------------------------------------------------------

def setup_from_browser():
    """Tries to grab cookies from LibreWolf and setup browser.json automatically."""
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        print("ERROR: ytmusicapi not installed.")
        return False

    domain = "music.youtube.com"
    print("Attempting to grab YouTube Music cookies from LibreWolf...")

    try:
        cookies = _grab_librewolf_cookies(domain)
    except Exception as e:
        print(f"❌ Failed to access LibreWolf cookies: {e}")
        return False

    if not cookies:
        print("❌ No cookies found for music.youtube.com in LibreWolf.")
        print("   Make sure you've logged into YouTube Music in LibreWolf recently.")
        return False

    # ytmusicapi needs these specific cookies
    cookie_str = "; ".join(f"{name}={value}" for name, value in cookies.items())

    if not cookie_str:
        print("❌ Cookie string is empty.")
        return False

    # Construct raw headers string for ytmusicapi
    headers_raw = (
        f"Cookie: {cookie_str}\n"
        f"User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
    )

    YTMusic.setup(filepath="browser.json", headers_raw=headers_raw)
    print(f"✅ browser.json generated from LibreWolf cookies ({len(cookies)} cookies)!")
    return True


def export_ytmusic() -> list[dict]:
    """Export liked songs from YouTube Music."""
    from ytmusicapi import YTMusic

    auth_file = Path("browser.json")
    if not auth_file.exists():
        print("browser.json not found. Attempting automatic setup...")
        if not setup_from_browser():
            print("\nERROR: Automatic setup failed.")
            print("Please perform manual setup: uv run ytmusicapi setup --file browser.json")
            return []

    yt = YTMusic(str(auth_file))
    tracks = []

    print("Fetching YouTube Music liked songs...")
    try:
        liked = yt.get_liked_songs(limit=10000)
    except Exception as e:
        # If unauthorized, maybe cookies expired? Try re-setup
        if "401" in str(e) or "Unauthorized" in str(e):
            print("Auth error. Retrying setup from LibreWolf...")
            if setup_from_browser():
                yt = YTMusic(str(auth_file))
                liked = yt.get_liked_songs(limit=10000)
            else:
                return []
        else:
            print(f"ERROR: Failed to fetch liked songs: {e}")
            return []

    for t in liked.get("tracks", []):
        artists = ", ".join(a.get("name", "") for a in t.get("artists", []))
        album = t.get("album", {})
        album_name = album.get("name", "") if isinstance(album, dict) else str(album)
        tracks.append({
            "artist": artists,
            "title": t.get("title", ""),
            "album": album_name,
            "source": "ytmusic",
        })

    print(f"  Found {len(tracks)} liked tracks from YouTube Music")
    return tracks


# ---------------------------------------------------------------------------
# Deezer (public API)
# ---------------------------------------------------------------------------

def export_deezer_public(user_id: int) -> list[dict]:
    """Export favorites from Deezer using the public API."""
    import requests

    tracks = []
    url = f"https://api.deezer.com/user/{user_id}/tracks"
    page = 0

    print(f"Fetching Deezer favorites for user {user_id}...")
    while url:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            print(f"  ERROR: Deezer API returned: {data['error']}")
            break

        for t in data.get("data", []):
            tracks.append({
                "artist": t.get("artist", {}).get("name", ""),
                "title": t.get("title", ""),
                "album": t.get("album", {}).get("title", ""),
                "source": "deezer",
            })

        # Pagination
        url = data.get("next")
        page += 1
        if url and page % 5 == 0:
            print(f"  ... fetched {len(tracks)} tracks so far")
            time.sleep(0.5)  # Rate limit courtesy

    print(f"  Found {len(tracks)} tracks from Deezer")
    return tracks


# ---------------------------------------------------------------------------
# Deezer (ARL token — private favorites)
# ---------------------------------------------------------------------------

def export_deezer_arl(arl_token: str) -> list[dict]:
    """Export favorites from Deezer using ARL token (for private libraries)."""
    import requests

    session = requests.Session()
    session.cookies.set("arl", arl_token, domain=".deezer.com")

    # Get user info
    resp = session.get("https://api.deezer.com/user/me", timeout=30)
    resp.raise_for_status()
    me = resp.json()

    if "error" in me:
        print(f"  ERROR: Invalid ARL token: {me['error']}")
        return []

    user_id = me.get("id")
    print(f"  Authenticated as {me.get('name', 'unknown')} (ID: {user_id})")

    # Fall back to public API with the user ID
    return export_deezer_public(user_id)


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def format_plain(tracks: list[dict]) -> str:
    """One track per line: Artist - Title"""
    return "\n".join(f"{t['artist']} - {t['title']}" for t in tracks)


def format_json(tracks: list[dict]) -> str:
    """JSON array"""
    return json.dumps(tracks, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Export music favorites from YouTube Music and Deezer"
    )
    parser.add_argument("--ytmusic", action="store_true", help="Export YouTube Music liked songs")
    parser.add_argument("--deezer", action="store_true", help="Export Deezer favorites")
    parser.add_argument("--deezer-user-id", type=int, help="Deezer user ID (for public favorites)")
    parser.add_argument("--deezer-arl", type=str, help="Deezer ARL token (for private favorites)")
    parser.add_argument("--format", choices=["json", "plain"], default="json",
                        help="Output format (default: json)")
    parser.add_argument("--output", "-o", type=str, default=OUTPUT_FILE,
                        help=f"Output file (default: {OUTPUT_FILE})")
    parser.add_argument("--setup", action="store_true", help="Force automatic setup from LibreWolf")

    args = parser.parse_args()

    if args.setup:
        setup_from_browser()
        sys.exit(0)

    if not args.ytmusic and not args.deezer:
        parser.print_help()
        sys.exit(1)

    all_tracks = []

    if args.ytmusic:
        all_tracks.extend(export_ytmusic())

    if args.deezer:
        if args.deezer_arl:
            all_tracks.extend(export_deezer_arl(args.deezer_arl))
        elif args.deezer_user_id:
            all_tracks.extend(export_deezer_public(args.deezer_user_id))
        else:
            print("ERROR: --deezer requires --deezer-user-id or --deezer-arl")
            sys.exit(1)

    if not all_tracks:
        print("No tracks exported.")
        sys.exit(1)

    # Write output
    output_path = Path(args.output)
    if args.format == "plain":
        content = format_plain(all_tracks)
    else:
        content = format_json(all_tracks)

    output_path.write_text(content, encoding="utf-8")
    print(f"\n✓ Exported {len(all_tracks)} tracks to {output_path}")

    # Deduplicate hint
    unique = len({(t["artist"], t["title"]) for t in all_tracks})
    if unique < len(all_tracks):
        print(f"  ({len(all_tracks) - unique} duplicates found across sources)")


if __name__ == "__main__":
    main()
