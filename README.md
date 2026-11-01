# WoW Addon Updater (Decky Loader plugin)

A Gaming Mode helper for World of Warcraft addons on Steam Deck / SteamOS.

It does **not** download or update addons itself. CurseForge's own Linux app does that. This plugin:

1. Checks whether the CurseForge app has been added to Steam as a non-Steam game. If not, it shows how to add it.
2. Once it has, lists every WoW version found on the system (Retail, Classic, Classic Era, PTR, ...) with the addon count and when each `AddOns` folder last changed.

Everything is read-only: the plugin never writes to `shortcuts.vdf`, your WoW folders, or anything else.

## Status

| Step | State |
|---|---|
| Detect the CurseForge shortcut | done (untested on a real Deck) |
| List WoW versions | done (untested on a real Deck) |
| Updates section (installed vs latest, Install update button) | done; the first install of it must be done with the curl commands below |
| Launch CurseForge from the plugin | not started: needs testing on a Deck |
| "Updated just now" detection after CurseForge closes | not started |
| Per-version update (driving the CurseForge UI) | not started: unverified that this is possible |

## Install on your Deck (no building needed)

GitHub builds the plugin on every push and publishes it as the
[latest release](https://github.com/ubermoc/Decky-Loader-Wow-mods-update-/releases/tag/latest).
In a terminal (works in fish or bash), run these one at a time:

```
cd ~/Downloads
curl -L -o wow-addon-updater.zip https://github.com/ubermoc/Decky-Loader-Wow-mods-update-/releases/download/latest/WoW.Addon.Updater.zip
sudo rm -rf "$HOME/homebrew/plugins/WoW Addon Updater"
sudo unzip -o wow-addon-updater.zip -d "$HOME/homebrew/plugins/"
sudo systemctl restart plugin_loader
```

## Updating from inside the plugin

The **Updates** section at the bottom of the panel shows the installed version, the latest version, and an **Install update** button. It downloads the latest release zip from this repository only, checks that the archive is safe and complete, swaps the files in, and rolls back if anything fails.

## Building it yourself (needs pnpm 9+ and zip)

```
./scripts/package.sh            # run from the repository folder
```

## How it finds things

- **CurseForge shortcut:** parses `userdata/<id>/config/shortcuts.vdf` under the native, legacy-symlink and Flatpak Steam roots, and matches "curseforge" in the shortcut name or executable path.
- **WoW installs:** reads `libraryfolders.vdf` for every Steam library (internal drive, SD card), then looks in `steamapps/compatdata/*/pfx/drive_c/Program Files (x86)/World of Warcraft` for flavor folders such as `_retail_` and `_classic_`.

## Development

```
python3 -m unittest discover -s tests -v   # backend logic
pnpm install && pnpm run build             # frontend
```

## Unverified assumptions

- `_retail_` and `_classic_` are confirmed by Steam Deck guides. The other flavor folder names (`_classic_era_`, `_ptr_`, ...) are from memory.
- The plugin now uses the `root` flag so it can overwrite its own files when you press **Install update**. Scanning works either way.
- After **Install update**, Decky has to reload the plugin: open Decky's plugin list (gear icon) and choose Reload. I haven't confirmed there is a way to trigger that automatically.

## License

MIT
