<p align="center">
  <img src="icon.png" width="128" alt="">
</p>

<h1 align="center">Stella Sora Mod Manager</h1>

<p align="center">
  A Windows desktop app for installing, updating and removing Stella Sora mods.<br>
  It backs up the original game files before replacing them, so you can restore them later.
</p>

<p align="center">
  <a href="https://github.com/Reeneka/Stella-Sora-Mod-Manager/releases/latest"><b>Download the latest release</b></a>
</p>

<h1 align="center">If there's hotfix or version update, REMEMBER to click the revert button before updating or else you will need to do Check Game Integrity(Steam)/Check Game Files(Yostar's Launcher) just to update the game.</h1>

## Use the exe

1. Download `StellaSoraModManager.exe` from the Releases page.
2. Put it in its own folder and run it.
3. Set your Stella Sora game folder in the app (use the Browse button).<br>
   Set it as the whole Stella Sora folder, example would be "D:\SteamLibrary\steamapps\common\Stella Sora" as this exe will scan through both StellaSora_Data and Persistent_Store to replace the files with your mods.

The app creates these next to the exe:

- `Mods\` -> put your mod files here
- `Backup\` -> original game files and replaced mod versions
- `config.json` -> your settings

Only Supports L2D and 2D(sprites) files for now. If there's a breakthrough in modding, i could probably update it to include chibi models but right now it's unlikely.

Requires Python 3.13 on Windows.

## Character names

The Name column comes from `names.txt`, which maps the number in a mod's file name to a character or NPC name (for example `char_l2d_14301` → Wraith). The app reads it offline.

When new characters or skins are released, run `python update_names.py` to rebuild `names.txt` from the English data in [Hiro420/StellaSoraData](https://github.com/Hiro420/StellaSoraData). You can also edit `names.txt` by hand. A `names.txt` placed next to the exe overrides the built-in one.
