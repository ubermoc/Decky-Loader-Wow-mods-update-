# WoW Addon Updater (Decky Loader plugin)

A Gaming Mode helper for World of Warcraft addons on Steam Deck / SteamOS.

This plugin:

1. Lists every WoW version found on the system (Retail, Classic, Classic Era, PTR, ...) with the addon count and when each `AddOns` folder last changed.
2. **Updates your addons from CurseForge by itself**, the way [wowforge-cli](https://github.com/m-triassi/wowforge-cli) does: it reads the `X-Curse-Project-ID` line in each addon's `.toc`, asks CurseForge's website API for the newest release for that WoW version, and swaps the addon folder in. Automatically shortly after Decky starts and every 6 hours (toggle in the panel), or on demand with **Update all addons now** / **Update <version> only**. It waits while WoW is running.
3. Can launch the CurseForge app (if you added it to Steam as a non-Steam game) and flags which versions changed during a session.

The plugin never writes to `shortcuts.vdf`. It **does** write to your `AddOns` folders when updating: each addon folder is replaced as a whole (files you added inside an addon's own folder are lost; `WTF`, saved variables and addons without a CurseForge id are never touched) and rolled back if the swap fails. Only addons that carry `X-Curse-Project-ID` are managed; hand-installed ones are ignored.

## Status

| Step | State |
|---|---|
| Detect the CurseForge shortcut | done (untested on a real Deck) |
| List WoW versions | done (untested on a real Deck) |
| Updates section (installed vs latest, Install update button) | done; the first install of it must be done with the curl commands below |
| Launch CurseForge from the plugin | done (untested on a real Deck; uses the undocumented `SteamClient.Apps.RunGame`) |
| "Updated just now" detection after CurseForge closes | done (untested on a real Deck; rescans on the undocumented `RegisterForAppLifetimeNotifications`, and when you reopen the panel) |
| Per-version update | done as direct CurseForge updates (not by driving the CurseForge app, which can't be done): untested on a real Deck; uses undocumented website endpoints that can change |

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
- The CurseForge website API (`/api/v1/mods/<id>/files`, no key) is undocumented. It worked when tested; it may break or be rate limited.
- Classic folders are matched on the client version from `.build.info` in the WoW folder (e.g. `5.5` against each file's `gameVersions`), because CurseForge's type ids are shared between several Classic versions. This covers Wrath, Cata and MoP clients. WoW Forever (`_forever_` folder, product `wowf`, client 1.60.x) is matched by its own CurseForge type id 88568. Retail uses CurseForge's Retail type id.
- Without a record of what it installed, an addon counts as current only if its `## Version:` appears in the newest release's name; otherwise it is downloaded once and recorded.
- The plugin now uses the `root` flag so it can overwrite its own files when you press **Install update**. Scanning works either way.
- After **Install update** the plugin runs `systemctl restart plugin_loader` three seconds later so the update loads by itself (same approach as Ubermoc Speech). If that ever fails, reload the plugin from Decky's plugin list.

## Updating through the CurseForge app (experimental)

The **Update through the CurseForge app** button launches CurseForge, goes to WoW's My Addons screen, presses its own **Update all** button, waits for it to finish and closes it. CurseForge is an Electron app, so the plugin controls it over Chromium's remote debugging port (local only, open only while it runs). Nothing to set up by hand:

1. **Launch flag:** the plugin passes `--remote-debugging-port=9222` for that one launch only (via Steam's `RunGame` launch-options argument), together with the shortcut's existing launch options read from `shortcuts.vdf`. If Steam ignores it, the error message says to add the flag to the shortcut by hand.
2. **Dev mode:** CurseForge only exposes its window to the debugging port when its own `dev-mode` setting is on (it creates windows with `devTools: isDevModeEnabled()`). Before launching, the plugin sets `"dev-mode": "true"` in `~/.config/CurseForge/storage.json` and puts the file back as it was afterwards (a one-time copy is kept as `storage.json.wowupdater-backup`). It refuses to do this while CurseForge is already open, since the app has to start fresh. CurseForge must have been run once so that file exists.
3. **WoW added in CurseForge** (Add a Game / Scan Now) with addons installed.

**Checked by running the real CurseForge 1.325.0 (Electron 42) headless:** the debugging port answers; the plugin's WebSocket client connects and runs JavaScript in the real window; the window is only listed with `dev-mode` on; clicking World of Warcraft goes to `#/game/1/my-library/my-addons`; the plugin closes the app itself, also on failure; the settings file is restored (apart from the app's own counters). **Not yet seen live:** the `update-all` button itself (class `update-all`, label "Update all (N)", read from the app's code), because the test machine had no WoW install, and whether Steam honours the launch argument. Overwolf can change the interface at any time. The direct updater above needs none of this.

## Troubleshooting

Each run logs a line per WoW version (tracked, updated, current, failed counts and the first errors) to Decky's plugin log: `~/homebrew/logs/WoW Addon Updater/` on the Deck (newest file = current session), or `sudo journalctl -u plugin_loader.service -b`.

## Authorship

Copyright (c) 2026 ubermoc. Every source file carries a copyright header and the provenance marker `wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e` (also compiled into the plugin's JavaScript and printed in its log at startup). Searching GitHub or a suspect project for that string shows whether code was copied. The dated git history in this repository is the strongest evidence of who wrote it first.

## License

Proprietary, all rights reserved. See [LICENSE](LICENSE). You may install and run the official release for personal use; copying, modifying, redistributing or using the code to train or prompt AI systems is not permitted without written permission.
