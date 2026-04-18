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
import sys
import time
from pathlib import Path

OUTPUT_FILE = "favorites.json"


# ---------------------------------------------------------------------------
# YouTube Music
# ---------------------------------------------------------------------------

def setup_from_browser():
    """Tries to grab cookies from LibreWolf and setup browser.json automatically."""
    try:
        import browser_cookie3
        from ytmusicapi import YTMusic
    except ImportError:
        print("ERROR: browser-cookie3 or ytmusicapi not installed.")
        return False

    print("Attempting to grab YouTube Music cookies from LibreWolf...")
    try:
        # LibreWolf is usually just a profile away from Firefox format
        cj = browser_cookie3.librewolf(domain_name='music.youtube.com')
        
        # We need to extract headers that ytmusicapi understands
        # ytmusicapi wants a 'headers' dict or a string block
        cookie_str = "; ".join([f"{c.name}={c.value}" for c in cj])
        
        if not cookie_str:
            print("❌ No cookies found for music.youtube.com in LibreWolf.")
            return False

        # Construct raw headers string (minimal set)
        headers_raw = f"Cookie: {cookie_str}\nUser-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"
        
        YTMusic.setup(filepath="browser.json", headers_raw=headers_raw)
        print("✅ browser.json generated successfully from LibreWolf cookies!")
        return True
    except Exception as e:
        print(f"❌ Failed to grab cookies: {e}")
        return False


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
        # If unauthorized, maybe cookies expired?
        if "401" in str(e) or "Unauthorized" in str(e):
            print("Auth error. Retrying setup...")
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
