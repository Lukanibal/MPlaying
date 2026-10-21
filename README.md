# MPlaying

MPlaying is a small Linux desktop companion that displays the currently playing
track as album artwork with playback and progress controls. It discovers players
through the session MPRIS2 D-Bus service.

## Features

- Displays album art, title, artist, album, playback status, and track progress.
- Provides previous, play/pause, and next controls when the artwork is hovered.
- Fetches synchronized lyrics from LRCLIB and displays them over the artwork.
- Highlights word timings when LRCLIB provides them. For line-synced lyrics,
  word timing is estimated and may not match the vocal precisely.
- Settings toggle the artwork fade, always-on-top behavior, and lyrics overlay.
  These preferences and the window size and position are saved under
  `$XDG_CONFIG_HOME/mplaying/settings.json` or `~/.config/mplaying/settings.json`.
- The window is undecorated, square, draggable, and resizable from its
  bottom-right corner. Right-click the artwork to copy a screenshot.

## Controls

- Hover over the artwork to reveal playback and settings controls.
- Drag the artwork to move the window; drag the bottom-right corner to resize it.
- Press `Ctrl+,` to open Settings.
- Press `Ctrl+L` to toggle the lyrics overlay.
- Right-click the artwork to copy a screenshot.

## Requirements

MPlaying currently targets Linux desktop sessions with a working MPRIS2 player
and session D-Bus. It also requires Python with Tkinter and `dbus-python`.
Python packages are listed in `requirements.txt` (Pillow and PyYAML).

## Run From Source

Run this from the Linux environment that has access to the session D-Bus:

```sh
python3 now_playing.py
```

Install the Python packages from `requirements.txt` in that environment if they
are not already available. Tkinter and `dbus-python` may need to come from the
Linux distribution's packages.

## Build an AppImage

The AppImage build is intended for the Distrobox environment where MPlaying is
used. From the project root inside that Distrobox, run:

```sh
bash packaging/build_appimage.sh
```

The script uses PyInstaller and `linuxdeploy`, writing the resulting
`MPlaying-x86_64.AppImage` to `dist/`. Run the AppImage inside the same Distrobox
session so it can access that session's D-Bus and MPRIS players. See
[`packaging/README.md`](packaging/README.md) for build details and prerequisites.

## AI and Disclaimer

This project was written with assistance from AI tools.

This software is provided **AS IS**, without warranties or guarantees of any
kind. Use it at your own risk. To the fullest extent permitted by applicable
law, the author(s) accept no responsibility or liability for any loss, damage,
or other claim arising from its use, inability to use, or modification.