# Music Favorites Exporter

Exporta i tuoi brani preferiti da **YouTube Music** e **Deezer** in un file JSON.
Poi usa [SpotiFLAC-Next](https://github.com/spotbye/SpotiFLAC-Next) per scaricarli in FLAC.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## YouTube Music

```bash
# Prima volta: autenticazione interattiva (si apre il browser)
ytmusicapi setup --file browser.json

# Poi esporta
python3 export.py --ytmusic
```

`ytmusicapi setup` ti chiederà di copiare gli header HTTP dalla DevTools del browser.
Istruzioni dettagliate: https://ytmusicapi.readthedocs.io/en/latest/setup.html

## Deezer

### Preferiti pubblici
```bash
python3 export.py --deezer --deezer-user-id 12345678
```

Per trovare il tuo user ID: vai su https://www.deezer.com/profile — il numero nell'URL è il tuo ID.

### Preferiti privati (richiede ARL token)
```bash
python3 export.py --deezer --deezer-arl YOUR_ARL_TOKEN
```

Per trovare l'ARL token: DevTools → Application → Cookies → deezer.com → `arl`

## Esporta tutto

```bash
python3 export.py --ytmusic --deezer --deezer-user-id 12345678
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
pip install deemix
deemix --arl YOUR_ARL_TOKEN -p ./downloads -f FLAC "ARTIST - TITLE"
```

## Formati di output alternativi

```bash
# Spotify URI (per spotDL)
python3 export.py --ytmusic --format spotify-uri

# Plain text (un brano per riga)
python3 export.py --ytmusic --format plain
```
