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
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

try:
    from ytmusicapi import YTMusic
except ImportError:
    print("❌ Critical: ytmusicapi not found. Install it with: pip install ytmusicapi")
    sys.exit(1)

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
    With Total Cookie Protection (dFPI), cookies are partitioned by top-level site.
    We prefer cookies partitioned under music.youtube.com, then fall back to
    unpartitioned cookies.

    IMPORTANT: When the same cookie name exists on multiple domains (e.g.
    HSID on both .youtube.com and .google.com), we must use the value from
    the domain that matches the target site. A request to music.youtube.com
    only sends .youtube.com cookies — NOT .google.com cookies.
    """
    # Copy the database to a temp file to avoid locking issues with the browser
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    shutil.copy2(cookies_db, tmp_path)

    # Partition key for music.youtube.com (URL-encoded in originAttributes)
    ytm_partition = "%28https%2Cmusic.youtube.com%29"

    # We only want cookies from .youtube.com and music.youtube.com
    # The browser never sends .google.com cookies to music.youtube.com
    ytm_domains = {".youtube.com", "music.youtube.com", "youtube.com"}

    cookies = {}
    try:
        conn = sqlite3.connect(str(tmp_path))

        # Build WHERE clause for all domains (to find candidates)
        conditions = []
        params = []
        for domain in domains:
            conditions.append("host LIKE ?")
            params.append(f"%{domain}%")

        where_clause = " OR ".join(conditions)

        # Try to read with originAttributes column (Total Cookie Protection)
        try:
            rows = conn.execute(
                f"SELECT name, value, host, originAttributes FROM moz_cookies WHERE {where_clause}",
                params
            ).fetchall()

            # Pass 1: prefer cookies partitioned under music.youtube.com
            for name, value, host, origin_attrs in rows:
                if not value:
                    continue
                if ytm_partition in (origin_attrs or ""):
                    cookies[name] = value

            # Pass 2: fill in missing from .youtube.com unpartitioned cookies
            for name, value, host, origin_attrs in rows:
                if not value:
                    continue
                if name not in cookies and host in ytm_domains and not origin_attrs:
                    cookies[name] = value

            # Pass 3: last resort — any .youtube.com cookie
            for name, value, host, origin_attrs in rows:
                if not value:
                    continue
                if name not in cookies and host in ytm_domains:
                    cookies[name] = value

        except sqlite3.OperationalError:
            # Older Firefox without originAttributes column
            rows = conn.execute(
                f"SELECT name, value, host FROM moz_cookies WHERE {where_clause}",
                params
            ).fetchall()

            for name, value, host in rows:
                if value and host in ytm_domains:
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

    # ytmusicapi only needs specific auth cookies, not all 200+
    # Sending everything causes HTTP 413 (Request Entity Too Large)
    AUTH_COOKIE_NAMES = {
        "HSID", "SSID", "APISID", "SAPISID", "SID", "SIDCC",
        "__Secure-1PSID", "__Secure-3PSID", "__Secure-3PAPISID",
        "__Secure-1PSIDTS", "__Secure-3PSIDTS",
        "__Secure-1PSIDCC", "__Secure-3PSIDCC",
        "__Secure-1PAPISID",
        "LOGIN_INFO", "NID",
    }
    auth_cookies = {k: v for k, v in cookies.items() if k in AUTH_COOKIE_NAMES}
    cookie_str = "; ".join(f"{name}={value}" for name, value in auth_cookies.items())

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


def export_ytmusic():
    """Export liked songs from YouTube Music using local LibreWolf cookies."""
    auth_file = Path("browser.json")
    if not auth_file.exists():
        print("browser.json not found. Attempting automatic setup...")
        cookies = get_librewolf_cookies()
        if cookies:
            with open(auth_file, 'w') as f:
                json.dump(cookies, f, indent=2)
            print("✅ browser.json created from LibreWolf cookies.")
        else:
            print("❌ Failed to extract cookies from LibreWolf.")
            print("Please perform manual setup: uv run ytmusicapi setup --file browser.json")
            return []

    try:
        yt = YTMusic(str(auth_file))
        print("Fetching YouTube Music liked songs...")
        playlists = yt.get_library_playlists(limit=1)
        if not playlists:
            print("❌ No playlists found or not logged in.")
            return []

        liked_id = next((p['playlistId'] for p in playlists if p['title'].lower() == 'liked songs'), None)
        if not liked_id:
            print("❌ 'Liked Songs' playlist not found.")
            return []

        songs = yt.get_playlist(liked_id)
        if not songs or 'tracks' not in songs:
            print("❌ No songs found in liked playlist.")
            return []

        print(f"✅ Found {len(songs['tracks'])} liked songs.")
        return songs['tracks']
    except Exception as e:
        print(f"❌ Failed to fetch liked songs: {e}")
        return []
def get_librewolf_cookies():
    """Silently extract YouTube Music auth cookies from LibreWolf profile."""
    profile_base = Path('/home/fulgidus/.config/librewolf/librewolf/')
    if not profile_base.exists():
        # Fallback to general home if not hardcoded
        profile_base = Path('~/.config/librewolf/librewolf/').expanduser()
    
    if not profile_base.exists():
        return None

    # Find the correct profile folder
    profiles = [p for p in profile_base.iterdir() if p.is_dir() and ('.default' in p.name or 'default-release' in p.name)]
    if not profiles:
        return None
    
    # Sort to get the most likely one (default-default)
    profile = sorted(profiles, key=lambda p: 'default-default' in p.name, reverse=True)[0]
    db_path = profile / 'cookies.sqlite'
    if not db_path.exists():
        return None

    # Copy to bypass potential LibreWolf lock
    tmp_db = Path('/tmp/ytmusic_cookies.sqlite')
    import shutil, sqlite3, json, time, hashlib, os
    shutil.copy2(db_path, tmp_db)
    
    try:
        conn = sqlite3.connect(tmp_db)
        c = conn.cursor()
        targets = ('SID', 'HSID', 'SSID', 'APISID', 'SAPISID', '__Secure-3PAPISID', '__Secure-3PSID', 'LOGIN_INFO')
        query = f"SELECT name, value FROM moz_cookies WHERE host LIKE '%youtube.com%' AND name IN {targets}"
        c.execute(query)
        rows = dict(c.fetchall())
        conn.close()
    except sqlite3.OperationalError as e:
        print(f"❌ SQLite error: {e}")
        return None
    finally:
        if tmp_db.exists():
            os.remove(tmp_db)

    if not rows:
        return None

    cookie_str = '; '.join([f'{k}={v}' for k, v in rows.items()])
    
    # Generate SAPISIDHASH (Required for ytmusicapi browser auth)
    sapisid = rows.get('SAPISID') or rows.get('__Secure-3PAPISID')
    auth_header = ""
    if sapisid:
        timestamp = str(int(time.time()))
        origin = "https://music.youtube.com"
        payload = f"{timestamp} {sapisid} {origin}"
        sapisid_hash = hashlib.sha1(payload.encode()).hexdigest()
        auth_header = f"SAPISIDHASH {timestamp}_{sapisid_hash}"

    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
        "Cookie": cookie_str,
        "X-Goog-AuthUser": "0",
        "Authorization": auth_header
    }

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
        error_str = str(e)
        # Debug: try a simple request to see what YouTube returns
        if "Expecting value" in error_str:
            print("ERROR: YouTube returned an empty/non-JSON response.")
            print("  This usually means the cookies are invalid or expired.")
            # Try a raw request to see what we get
            try:
                import requests as _req
                with open(auth_file) as f:
                    headers = json.load(f)
                resp = _req.post(
                    "https://music.youtube.com/youtubei/v1/browse",
                    headers=headers,
                    json={"browseId": "FEmusic_liked_videos", "context": {"client": {"clientName": "WEB_REMIX", "clientVersion": "1.20240403.01.00"}}},
                    timeout=15,
                )
                print(f"  HTTP Status: {resp.status_code}")
                print(f"  Response (first 500 chars): {resp.text[:500]}")
            except Exception as debug_e:
                print(f"  Debug request also failed: {debug_e}")
        elif "401" in error_str or "Unauthorized" in error_str:
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

def _grab_deezer_arl() -> str | None:
    """Try to find the ARL cookie for Deezer in LibreWolf."""
    try:
        cookies = _grab_librewolf_cookies(["deezer.com"])
        return cookies.get("arl")
    except Exception:
        return None


def export_deezer() -> list[dict]:
    """Export Deezer favorites. Tries ARL from LibreWolf, then public API."""
    import requests

    # Try to get ARL from LibreWolf cookies
    arl = _grab_deezer_arl()
    if arl:
        print(f"Found Deezer ARL in LibreWolf cookies (len={len(arl)})")
        session = requests.Session()
        session.cookies.set("arl", arl, domain=".deezer.com")

        # Verify the ARL is valid
        resp = session.get("https://api.deezer.com/user/me", timeout=30)
        resp.raise_for_status()
        me = resp.json()

        if "error" not in me:
            user_id = me.get("id")
            print(f"  Authenticated as {me.get('name', 'unknown')} (ID: {user_id})")
            return export_deezer_public(user_id)
        else:
            print(f"  ARL from LibreWolf is invalid/expired: {me['error']}")
            print("  Falling back to public API...")

    # No valid ARL — check if we can get user ID from public profile
    print("No valid Deezer ARL found in LibreWolf cookies.")
    print("  Deezer requires authentication for private libraries.")
    print("  Options:")
    print("    1. Log into deezer.com in LibreWolf (may require VPN if blocked in your country)")
    print("    2. Pass --deezer-arl YOUR_ARL manually")
    print("    3. Pass --deezer-user-id YOUR_ID for public favorites")
    return []


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

def download_tracks(tracks):
    """Download tracks using SpotiFLAC."""
    # Flexible download path
    download_path = Path("favorites-flac")
    try:
        # Try preferred path, fallback to current dir
        pref_path = Path("/home/fulgidus/Music")
        if os.access(pref_path.parent, os.W_OK):
            download_path = pref_path
        download_path.mkdir(parents=True, exist_ok=True)
    except Exception:
        download_path = Path("favorites-flac")
        download_path.mkdir(parents=True, exist_ok=True)
    
    spotiflac_bin = Path("./spotiflac")
    if not spotiflac_bin.exists():
        print("\n🚀 SpotiFLAC CLI not found. Compiling with headless tag...")
        try:
            import subprocess
            # Clone SpotiFLAC if not exists
            repo_dir = Path("SpotiFLAC")
            if not repo_dir.exists():
                subprocess.run(["git", "clone", "https://github.com/spotbye/SpotiFLAC-Next.git", "SpotiFLAC"], check=True)
            
            # Build headless CLI
            print("Compiling SpotiFLAC from source...")
            subprocess.run(["go", "mod", "tidy"], cwd=repo_dir, check=True)
            subprocess.run(["go", "build", "-o", "../spotiflac", "."], cwd=repo_dir, check=True)
            print("✅ SpotiFLAC CLI compiled successfully.")
        except Exception as e:
            print(f"❌ Failed to compile SpotiFLAC: {e}")
            sys.exit(1)

    print(f"\n🎵 Starting automatic FLAC download to {download_path}...")
    import subprocess
    for t in tracks:
        query = f"{t['artist']} - {t['title']} - {t['album']}"
        print(f"  Searching FLAC for: {query}")
        try:
            # SpotiFLAC CLI usage: ./spotiflac "search:Artist - Title - Album"
            cmd = [
                str(spotiflac_bin.absolute()),
                f"search:{query}"
            ]
            # Note: We assume the user has configured output path in spotiflac settings
            # or we could try to pass it if SpotiFLAC CLI supports it.
            subprocess.run(cmd, check=True)
            print(f"    ✅ Downloaded.")
        except Exception as e:
            print(f"    ❌ Failed: {e}")

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
    parser.add_argument("--debug-cookies", action="store_true", help="Show cookie extraction details for debugging")
    parser.add_argument("--download", action="store_true", help="Automatically download tracks in FLAC to /home/fulgidus/Music")

    args = parser.parse_args()

    if args.setup:
        setup_from_browser()
        sys.exit(0)

    if args.debug_cookies:
        try:
            profile = _find_librewolf_profile()
            print(f"Profile: {profile}")
            cookies_db = profile / "cookies.sqlite"
            import tempfile as _tf
            with _tf.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            shutil.copy2(cookies_db, tmp_path)
            conn = sqlite3.connect(str(tmp_path))
            # Check schema
            cols = conn.execute("PRAGMA table_info(moz_cookies)").fetchall()
            print(f"\nmoz_cookies columns: {[c[1] for c in cols]}")
            # Show auth cookies with their originAttributes
            try:
                rows = conn.execute(
                    "SELECT name, host, length(value), originAttributes FROM moz_cookies "
                    "WHERE host LIKE '%youtube.com%' OR host LIKE '%google.com%' "
                    "ORDER BY originAttributes, name"
                ).fetchall()
                print(f"\nYouTube/Google cookies ({len(rows)} total):")
                for name, host, val_len, attrs in rows:
                    print(f"  {name:30s} {host:35s} len={val_len:5d} attrs={attrs}")
            except sqlite3.OperationalError as e:
                print(f"  originAttributes column missing: {e}")
                rows = conn.execute(
                    "SELECT name, host, length(value) FROM moz_cookies "
                    "WHERE host LIKE '%youtube.com%' OR host LIKE '%google.com%' "
                    "ORDER BY name"
                ).fetchall()
                print(f"\nYouTube/Google cookies ({len(rows)} total):")
                for name, host, val_len in rows:
                    print(f"  {name:30s} {host:35s} len={val_len}")
            conn.close()
            tmp_path.unlink(missing_ok=True)
        except Exception as e:
            print(f"Error: {e}")
        sys.exit(0)

    if not args.ytmusic and not args.deezer and not args.download:
        parser.print_help()
        sys.exit(1)

    all_tracks = []

    # If --download is passed without a source flag, but favorites.json exists, use it.
    if args.download and not (args.ytmusic or args.deezer) and Path(args.output).exists():
        try:
            with open(args.output, "r") as f:
                all_tracks = json.load(f)
            print(f"Loaded {len(all_tracks)} tracks from {args.output}")
        except Exception as e:
            print(f"❌ Error loading {args.output}: {e}")
            sys.exit(1)

    if args.download and not (args.ytmusic or args.deezer):
        # Case: standalone --download using loaded favorites.json
        if not all_tracks:
            print("❌ No tracks to download. Provide --ytmusic/--deezer or ensure favorites.json exists.")
            sys.exit(1)
        
        # Reuse the download logic
        download_tracks(all_tracks)
        sys.exit(0)

    if args.ytmusic:
        tracks = export_ytmusic()
        all_tracks.extend(tracks)
        if args.download and tracks:
            download_tracks(tracks)

    if args.deezer:
        if args.deezer_arl:
            # Manual ARL override
            import requests
            session = requests.Session()
            session.cookies.set("arl", args.deezer_arl, domain=".deezer.com")
            resp = session.get("https://api.deezer.com/user/me", timeout=30)
            me = resp.json()
            if "error" in me:
                print(f"  ERROR: Invalid ARL token: {me['error']}")
                sys.exit(1)
            user_id = me.get("id")
            print(f"  Authenticated as {me.get('name', 'unknown')} (ID: {user_id})")
            all_tracks.extend(export_deezer_public(user_id))
        elif args.deezer_user_id:
            all_tracks.extend(export_deezer_public(args.deezer_user_id))
        else:
            # Auto: try LibreWolf cookies, then give helpful error
            all_tracks.extend(export_deezer())

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
