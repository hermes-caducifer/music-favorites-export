# Music Favorites Exporter

Exporta i tuoi brani preferiti da **YouTube Music** e **Deezer** in un file JSON.
Poi usa [SpotiFLAC-Next](https://github.com/spotbye/SpotiFLAC-Next) per scaricarli in FLAC.

## Setup

```bash
uv sync
```

## YouTube Music

```bash
# Auto-setup: ruba i cookie da LibreWolf automaticamente
uv run music-export --ytmusic

# Oppure forza il setup senza esportare
uv run music-export --setup
```

Se hai effettuato il login a YouTube Music su LibreWolf, il setup è automatico — nessun browser wizard, nessun OAuth.

Se non usi LibreWolf, puoi ancora fare il setup manuale:
```bash
uv run ytmusicapi setup --file browser.json
```
Istruzioni dettagliate: https://ytmusicapi.readthedocs.io/en/latest/setup.html

## Deezer

### Preferiti pubblici
```bash
uv run music-export --deezer --deezer-user-id 12345678
```

Per trovare il tuo user ID: vai su https://www.deezer.com/profile — il numero nell'URL è il tuo ID.

### Preferiti privati (richiede ARL token)
```bash
uv run music-export --deezer --deezer-arl YOUR_ARL_TOKEN
```

Per trovare l'ARL token: DevTools → Application → Cookies → deezer.com → `arl`

## Esporta tutto

```bash
uv run music-export --ytmusic --deezer --deezer-user-id 12345678
```

Output: `favorites.json` con formato:
```json
[
  {"artist": "Artist Name", "title": "Track Title", "album": "Album", "source": "ytmusic"},
  ...
]
```

## Scaricare i FLAC

1. Scarica [SpotiFLAC-Next](https://github.com/spotbye/SpotiFLAC-Next/releases) per il tuo OS
2. Apri l'app
3. Incolla un link Spotify/URL del brano → scarica in FLAC da Tidal/Qobuz/Deezer/Amazon/Apple Music

Per batch: incolla i nomi "Artist - Title" dal file `favorites.json` nella search bar di SpotiFLAC-Next.

### Alternativa: deemix (gratis, Deezer diretto)

```bash
uvx deemix --arl YOUR_ARL_TOKEN -p ./downloads -f FLAC "ARTIST - TITLE"
```

## Formati di output alternativi

```bash
# Plain text (un brano per riga)
uv run music-export --ytmusic --format plain

# Output su file custom
uv run music-export --ytmusic -o my_songs.txt
```
