# VRSit 1.2 – sit in chairs and walk with your body in No Man's Sky VR

An [NMS.py](https://github.com/monkeyman192/NMS.py) code mod for PC VR (SteamVR).

* **Chairs work in VR.** The game hides the Sit prompt on chairs while VR is active. VRSit removes that check, so you can sit on any chair like in flat mode, with the normal sitting animation.
* **Automatic recentre at seat height.** After you take a chair in-game, sit down on a real chair. Once your head has dropped and stays still for 2 seconds, VRSit triggers the game's own recentre at seat height instead of standing height.
* **No floating body.** With "Show body" on, the game draws the seated VR body too high, hovering above the seat. VRSit lowers it while you sit, blending smoothly as you sit down and stand up.
* **Your body follows you around the room.** The game keeps your character at the centre of the play space and only lets the camera wander. VRSit walks the character after your head (with the game's own walking, so other players see it) and turns it to where you face, while your view stays exactly where your head is.
* **Settings in the NMS.py window.** Every option has a switch or slider in the VRSit tab of the NMS.py window, saved to `vr_sit_settings.json`.

Nothing is written to your game files or saves. Everything is changed in memory only while the mod runs.

## What's new in 1.2

* Your character walks after you as you move around your play space, and turns to where you look. The view is held still, so walking feels natural and the HUD comes along.
* Walking uses the game's own movement force (no stick dead zone). The old stick emulation is still there as an option.
* The body doesn't turn while a menu or a dialog screen is open (hand menus still let it turn).
* A VRSit tab in the NMS.py window with all settings, saved between sessions.
* Game functions are found by byte patterns, so small game updates are less likely to break the mod.

## Requirements

| What | Tested with |
| --- | --- |
| No Man's Sky, Steam, PC VR | build 180836 (Steam build 25732212) |
| SteamVR | 2.15, headset via Virtual Desktop |
| Python 3.13 (Setup.bat gets it for you) | 3.13.2 |
| NMS.py (`nmspy`), installs pyMHF with it | nmspy 181442.0, pyMHF 0.2.4 |

NMS.py mods depend on the game build. After a game update, wait for a matching NMS.py release before playing with mods.

## Install and start (one click)

1. Put this zip and **Setup.bat** (with `setup_nms_mods.ps1`, from the same download) into one folder. Don't unpack the zip. Other mod zips from this series (for example NipNip) can go into the same folder.
2. Double-click **Setup.bat**.

The first time, it installs [uv](https://docs.astral.sh/uv), sets up Python 3.13 with NMS.py in `%LOCALAPPDATA%\NMSMods`, finds No Man's Sky in your Steam libraries and configures NMS.py to start the game in VR. Every time, it installs the newest version of each mod zip in its folder into the game, starts SteamVR if needed and starts the game with the mods. Keep its window open while you play: closing it ends the game.

Later runs skip the setup and go straight to the game. `Setup.bat -Reinstall` redoes the setup; `Setup.bat -NoLaunch` only installs. If you already had NMS.py set up with another mod folder, Setup keeps that folder (your old settings are saved as `pymhf.local.toml.bak`).

## Manual install

1. Install Python 3.13 from python.org (not the Microsoft Store one, not 3.14). Tick "Add python.exe to PATH".
2. `python -m pip install nmspy==181442.0`
3. Run `pymhf run nmspy` once so NMS.py asks for your mod folder (it recommends `No Man's Sky\GAMEDATA\MODS`), then close the game.
4. Copy the `VRSit` folder into that mod folder, so you have `...\MODS\VRSit\vr_sit.py`.
5. To start in VR: put a file `steam_appid.txt` containing just `275850` into `No Man's Sky\Binaries\`, and add these lines to the `[pymhf.local_config]` section of `%APPDATA%\pymhf\nmspy\pymhf.local.toml` (adjust the path):

   ```toml
   exe = "C:/Program Files (x86)/Steam/steamapps/common/No Man's Sky/Binaries/NMS.exe"
   args = ["-HmdEnable", "1"]
   ```

6. Start SteamVR, then run `pymhf run nmspy`.

## Using it

**Chairs:** walk up to a chair and use its Sit prompt, sit down on your real chair and look straight ahead. About 2 seconds after you have settled, the view recentres at seat height. To get up, move with the stick.

**Walking around:** just walk in your play space. Your character follows, and turns to where you face. Use the stick as usual for longer distances.

## Settings

Open the **VRSit** tab in the NMS.py window. Changes apply at once and are saved to `vr_sit_settings.json` next to `vr_sit.py` ("Reset all to defaults" restores them).

| Group | Settings |
| --- | --- |
| Chairs | Sit in VR, seat-height recentre, body fix, how far below standing height you sit, how far the body is lowered, the head drop that counts as sitting, how long to sit still before the recentre |
| Body follow | Body walks after you, body turns to where you face (not in menus, not in dialogs), keep the view still, the old stick-emulation walking |
| Walking | When a stride starts and stops, how far behind your head the body stands, walking force range and gain, braking look-ahead |
| Turning | When turning starts and stops, turn speed |
| Stick | When your own stick use takes over |

## Troubleshooting

VRSit writes `vr_sit.log` next to `vr_sit.py`. A working session contains "SteamVR functions resolved", "chair VR gate patched" and "body correction ready" lines.

* **"signature matched 0 times"** means the game was updated and the mod needs an update. VRSit then skips that feature and does nothing harmful.
* **"body correction unavailable"** means the body fix couldn't find its game code. Chairs and the recentre still work.
* **No "SteamVR functions resolved" line** means VR was not running in the game. Start the game with Setup.bat, or check the `args` line from the manual install.
* **The game starts in flat mode or through Steam** means `steam_appid.txt` or the `exe` path is missing.
* **The body walks off on its own or jitters:** switch off "Body walks after you" in the VRSit tab and tell us.

## Uninstall

Delete the `VRSit` folder from your NMS.py mod folder. To stop NMS.py starting the game directly, delete `Binaries\steam_appid.txt`. `%LOCALAPPDATA%\NMSMods` holds the Python environment Setup.bat made.

## Known limitations

* The game's own recentre also turns your view to where your head points and moves your body slightly. That is normal recentre behaviour.
* Standing up physically does not stand your character up. Move with the stick for that.
* Seat height and body correction are fixed distances, tuned on one chair. Other chairs may need slightly different values in the VRSit tab.
