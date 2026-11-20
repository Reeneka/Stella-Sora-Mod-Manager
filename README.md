<img src="icon.png" width="96" align="right" alt="">

# Stella Sora Mod Manager

A Windows desktop app for installing, updating and removing Stella Sora mods. It backs up the original game files before replacing them, so you can restore them later.

## Use the exe

1. Download `StellaSoraModManager.exe` from the Releases page.
2. Put it in its own folder and run it.
3. Set your Stella Sora game folder in the app (use the Browse button).

The app creates these next to the exe:

- `Mods\` — put your mod files here
- `Backup\` — original game files and replaced mod versions
- `config.json` — your settings

## Run from source

Requires Python 3.13 on Windows.

```
pip install -r requirements.txt
python ssmod_gui.py
```

When run from source, `Mods\`, `Backup\` and `config.json` are created in the repo folder.

## Build the exe

```
pip install pyinstaller
pyinstaller StellaSoraModManager.spec
```

The exe is written to `dist\StellaSoraModManager.exe`.

To regenerate `icon.ico` and `icon.png`, run `python make_icon.py` (needs `pip install pillow`).

## Character names

The Name column comes from `names.txt`, which maps the number in a mod's file name to a character or NPC name (for example `char_l2d_14301` → Wraith). The app reads it offline.

When new characters or skins are released, run `python update_names.py` to rebuild `names.txt` from the English data in [Hiro420/StellaSoraData](https://github.com/Hiro420/StellaSoraData). You can also edit `names.txt` by hand. A `names.txt` placed next to the exe overrides the built-in one.
