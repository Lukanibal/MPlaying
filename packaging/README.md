# AppImage build

Build inside the Distrobox where MPlaying currently runs. The build creates a
dedicated virtual environment with system site packages so it can use that
container's `dbus-python` and Tcl/Tk, then bundles the Python application with
PyInstaller. It uses `linuxdeploy` in the container when available, otherwise
invokes the host copy through `distrobox-host-exec`. The AppDir is created in
the shared project directory so either environment can access it.

The container needs:

- `python3` with `venv`, Tcl/Tk, `dbus-python`, and `libdbus-1.so.3`
- `linuxdeploy` with AppImage output support, in the container or host
- network access to install the Python build requirements

On Fedora-based Distroboxes, install Tkinter if it is missing:

```sh
sudo dnf install python3-tkinter tk
```

Run from the project root:

```sh
bash packaging/build_appimage.sh
```

The result is written to `dist/`. Launch the AppImage inside the same Distrobox
session so it inherits the session D-Bus address and can discover MPRIS
players. The `dbus-python` extension is built for this container's Python ABI;
the image still relies on the container's `libdbus`. It is intended for this
Distrobox environment, not arbitrary Linux distributions.

To use another build virtual environment, set `BUILD_VENV` before running the
script. It must be created with `--system-site-packages` so `dbus-python` is
visible to PyInstaller.