<img width="1081" height="1081" alt="image" src="https://github.com/user-attachments/assets/f5dab320-0cad-44ce-8457-16206f4b03a1" />

# Vibe Player

Vibe Player is a small Linux desktop companion app that displays the currently playing
track's album artwork with playback progress and media controls. It discovers players
through the session's MPRIS2 D-Bus service.

## Features

- Displays album art, title, artist, album, and track progress.
- Provides previous, play/pause, and next controls when the artwork is hovered over.
- Fetches synchronized lyrics from LRCLIB and displays them over the artwork.
- Highlights word timings when LRCLIB provides them. For line-synced lyrics,
  word timing is estimated and may not match the vocal precisely.
- Settings toggle the artwork fade, always-on-top behavior, and lyrics overlay.
  These preferences and the window size and position are saved under
  `$XDG_CONFIG_HOME/mplaying/settings.json` or `~/.config/mplaying/settings.json`.
- Right-click the artwork to copy a screenshot.

## Controls

- Hover over the artwork to reveal playback and settings controls.
- Drag the artwork to move the window; drag the bottom-right corner to resize it.
- Press `Ctrl+,` to open Settings.
- Press `Ctrl+L` to toggle the lyrics overlay.
- Right-click the artwork to copy a screenshot.

## Requirements

Vibe Player currently targets Linux desktop sessions with a working MPRIS2 player
and session D-Bus. It also requires Python packages, which are listed in `requirements.txt`.

## Run From Source

Run this from the Linux environment that has access to the session D-Bus:

```sh
python3 now_playing.py
```

Install the Python packages from `requirements.txt` in that environment if they
are not already available.

## Build an AppImage
You can build an AppImage of the App for easy running, run:

```sh
bash packaging/build_appimage.sh
```

The script uses `PyInstaller` and `linuxdeploy`, writing the resulting
`MPlaying-x86_64.AppImage` to `dist/`. See [`packaging/README.md`](packaging/README.md) for build details and prerequisites.

## AI and Disclaimer

This project was written with assistance from AI tools.

This software is provided **AS IS**, without warranties or guarantees of any
kind. Use it at your own risk. To the fullest extent permitted by applicable
law, the author(s) accept no responsibility or liability for any loss, damage,
or other claim arising from its use, inability to use, or modification.

Not affiliated with Tom Lehrer in any way, his music was used as it's public domain and it slaps <3
