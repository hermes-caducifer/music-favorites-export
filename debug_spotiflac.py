#!/usr/bin/env python3
"""
Debug script to test what queries work with your SpotiFLAC binary.
Put this in your music-favorites-export directory and run:
    uv run debug_spotiflac.py
"""

import json
import subprocess
import sys
from pathlib import Path

def load_tracks():
    """Load tracks from favorites.json"""
    try:
        with open("favorites.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("❌ favorites.json not found. Run --ytmusic or --deezer first.")
        return []
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in favorites.json: {e}")
        return []

def test_query(query):
    """Test a single query with the spotiflac binary"""
    spotiflac_bin = Path("./spotiflac")
    if not spotiflac_bin.exists():
        print("❌ spotiflac binary not found in current directory")
        return False
    
    print(f"🧪 Testing: {query}")
    print(f"   Command: {spotiflac_bin.absolute()} \"{query}\"")
    
    try:
        result = subprocess.run(
            [str(spotiflac_bin), query],
            capture_output=True,
            text=True,
            timeout=15
        )
        
        if result.returncode == 0:
            print("   ✅ SUCCESS")
            return True
        else:
            print(f"   ❌ FAILED (exit {result.returncode})")
            if result.stderr.strip():
                print(f"   STDERR: {result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        print("   ⏰ TIMEOUT (15s)")
        return False
    except Exception as e:
        print(f"   💥 EXCEPTION: {e}")
        return False

def main():
    print("🔍 SpotiFLAC Binary Query Tester")
    print("=" * 50)
    
    tracks = load_tracks()
    if not tracks:
        return
    
    print(f"📥 Loaded {len(tracks)} tracks from favorites.json\n")
    
    # Show first few tracks as examples
    print("📋 Example tracks from your library:")
    for i, track in enumerate(tracks[:3]):
        artist = track.get('artist', 'Unknown')
        title = track.get('title', 'Unknown')
        album = track.get('album')
        query = f"{artist} - {title}"
        if album and album != "None":
            query += f" - {album}"
        print(f"   {i+1}. {query}")
    print()
    
    # Interactive testing
    while True:
        print("\nOptions:")
        print("  1. Test a specific track by number")
        print("  2. Test a custom query")
        print("  3. Show first 10 tracks with cleaned queries")
        print("  4. Exit")
        
        choice = input("\nSelect option (1-4): ").strip()
        
        if choice == "1":
            try:
                idx = int(input("Track number: ")) - 1
                if 0 <= idx < len(tracks):
                    track = tracks[idx]
                    artist = track.get('artist', 'Unknown')
                    title = track.get('title', 'Unknown')
                    album = track.get('album')
                    query = f"{artist} - {title}"
                    if album and album != "None":
                        query += f" - {album}"
                    test_query(query)
                else:
                    print("❌ Invalid track number")
            except ValueError:
                print("❌ Please enter a number")
                
        elif choice == "2":
            query = input("Enter query to test: ").strip()
            if query:
                test_query(query)
            else:
                print("❌ Empty query")
                
        elif choice == "3":
            print("\n📋 First 10 tracks with query variations:")
            for i, track in enumerate(tracks[:10]):
                artist = track.get('artist', 'Unknown')
                title = track.get('title', 'Unknown')
                album = track.get('album')
                
                base = f"{artist} - {title}"
                if album and album != "None":
                    full = f"{base} - {album}"
                else:
                    full = base
                
                print(f"   {i+1}. {full}")
                
                # Try cleaned version
                cleaned = full.replace('(', '').replace(')', '')
                cleaned = cleaned.replace('[', '').replace(']', '')
                cleaned = ' '.join(cleaned.split())
                if cleaned != full:
                    print(f"       → Cleaned: {cleaned}")
            print()
                
        elif choice == "4":
            print("👋 Goodbye!")
            break
            
        else:
            print("❌ Invalid option")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Interrupted. Goodbye!")
