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
import configparser
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

OUTPUT_FILE = "favorites.json"

LIBREWOLF_PROFILE_PATHS = [
    "~/.config/librewolf/librewolf",
    "~/.librewolf",
    "~/snap/librewolf/common/.librewolf",
]

# ytmusicapi requires cookies from these domains
YTMUSIC_COOKIE_DOMAINS = [
    ".youtube.com",
    "music.youtube.com",
    ".google.com",
    "accounts.google.com",
]


# ---------------------------------------------------------------------------
# LibreWolf cookie extraction
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


def _extract_cookies_from_db(cookies_db: Path, domains: list[str]) -> dict[str, str]:
    """Read cookies from cookies.sqlite for the given domains.

    Firefox/LibreWolf stores cookie values in plaintext in the 'value' column.
    """
    # Copy the database to a temp file to avoid locking issues with the browser
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    shutil.copy2(cookies_db, tmp_path)

    cookies = {}
    try:
        conn = sqlite3.connect(str(tmp_path))

        # Build WHERE clause for all domains
        conditions = []
        params = []
        for domain in domains:
            conditions.append("host LIKE ?")
            params.append(f"%{domain}%")

        where_clause = " OR ".join(conditions)

        rows = conn.execute(
            f"SELECT name, value, host FROM moz_cookies WHERE {where_clause}",
            params
        ).fetchall()

        for name, value, host in rows:
            if value:
                cookies[name] = value
    finally:
        conn.close()
        tmp_path.unlink(missing_ok=True)

    return cookies


def _grab_librewolf_cookies(domains: list[str]) -> dict[str, str]:
    """Extract cookies from LibreWolf for the given domains."""
    profile = _find_librewolf_profile()
    if not profile:
        raise FileNotFoundError("Could not find LibreWolf profile directory")

    cookies_db = profile / "cookies.sqlite"
    if not cookies_db.exists():
        raise FileNotFoundError(f"No cookies.sqlite in {profile}")

    return _extract_cookies_from_db(cookies_db, domains)


# ---------------------------------------------------------------------------
# YouTube Music
# ---------------------------------------------------------------------------

def setup_from_browser():
    """Tries to grab cookies from LibreWolf and setup browser.json automatically."""
    try:
        from ytmusicapi import setup as ytm_setup
    except ImportError:
        print("ERROR: ytmusicapi not installed.")
        return False

    print("Attempting to grab YouTube Music cookies from LibreWolf...")

    try:
        cookies = _grab_librewolf_cookies(YTMUSIC_COOKIE_DOMAINS)
    except Exception as e:
        print(f"❌ Failed to access LibreWolf cookies: {e}")
        return False

    if not cookies:
        print("❌ No cookies found for YouTube/Google domains in LibreWolf.")
        print("   Make sure you've logged into YouTube Music in LibreWolf recently.")
        return False

    # Build the cookie string
    cookie_str = "; ".join(f"{name}={value}" for name, value in cookies.items())

    if not cookie_str:
        print("❌ Cookie string is empty.")
        return False

    # ytmusicapi requires: cookie, x-goog-authuser, AND authorization (SAPISIDHASH)
    # SAPISIDHASH is computed from __Secure-3PAPISID cookie + origin + timestamp
    # determine_auth_type() checks for "SAPISIDHASH" in authorization to identify browser auth
    # Without it, ytmusicapi thinks the file is OAuth and crashes

    # Compute SAPISIDHASH
    sapisid = cookies.get("__Secure-3PAPISID", "")
    if not sapisid:
        print("❌ Missing __Secure-3PAPISID cookie. Are you logged into YouTube Music?")
        return False

    import hashlib
    import time as _time
    origin = "https://music.youtube.com"
    unix_timestamp = str(int(_time.time()))
    sha_1 = hashlib.sha1()
    sha_1.update(f"{unix_timestamp} {sapisid} {origin}".encode("utf-8"))
    sapisidhash = f"SAPISIDHASH {unix_timestamp}_{sha_1.hexdigest()}"

    authuser = "0"

    # Build headers_raw in the format ytmusicapi.setup_browser expects
    headers_raw = "\n".join([
        f"cookie: {cookie_str}",
        f"x-goog-authuser: {authuser}",
        f"authorization: {sapisidhash}",
    ])

    try:
        ytm_setup(filepath="browser.json", headers_raw=headers_raw)
        print(f"✅ browser.json generated from LibreWolf cookies ({len(cookies)} cookies)!")
        return True
    except Exception as e:
        print(f"❌ ytmusicapi setup failed: {e}")
        return False


def export_ytmusic() -> list[dict]:
    """Export liked songs from YouTube Music."""
    from ytmusicapi import YTMusic
    from ytmusicapi.exceptions import YTMusicUserError

    auth_file = Path("browser.json")
    if not auth_file.exists():
        print("browser.json not found. Attempting automatic setup...")
        if not setup_from_browser():
            print("\nERROR: Automatic setup failed.")
            print("Please perform manual setup: uv run ytmusicapi setup --file browser.json")
            return []

    try:
        yt = YTMusic(str(auth_file))
    except YTMusicUserError as e:
        if "oauth" in str(e).lower():
            # browser.json is malformed (e.g. missing SAPISIDHASH) — regenerate it
            print(f"browser.json is invalid ({e}). Regenerating from LibreWolf...")
            auth_file.unlink(missing_ok=True)
            if not setup_from_browser():
                print("ERROR: Automatic setup failed.")
                return []
            yt = YTMusic(str(auth_file))
        else:
            raise
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
