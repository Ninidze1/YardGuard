# YardGuard

YardGuard watches your security cameras with AI on your own Windows PC and tells you when
something worth a look happens: someone checking parked cars at night, a person lingering by the
gate, a car blocking the driveway, a parcel taken from the front door.

It works with the cameras and video recorder (NVR) you already have. Everything runs on your PC.
Nothing is uploaded unless you connect a service such as Telegram yourself.

---

## Contents

- [What it does](#what-it-does)
- [What you need](#what-you-need)
- [Quick start](#quick-start)
- [Setting up your cameras](#setting-up-your-cameras)
- [Optional add-ons](#optional-add-ons)
- [Alerts on your phone and other apps](#alerts-on-your-phone-and-other-apps)
- [Privacy](#privacy)
- [Updating](#updating)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)
- [For developers](#for-developers)
- [Licence](#licence)

---

## What it does

- **Live view.** All your cameras on one screen, with people and vehicles marked as the AI sees them.
- **Smart alerts, not motion spam.** YardGuard follows people and cars over time, so a passing
  shadow or rain does not raise an alarm. Built-in rules:
  - *Checking cars*: one person walks up to several different parked cars.
  - *Person at night*: someone close to parked cars during the night hours you choose.
  - *Loitering*: someone stays in a watched spot for longer than you allow.
  - *Car moved*: a parked car moves or shakes (it may have been hit or broken into).
- **Areas you draw yourself** on the camera picture: watch zones, entrances, crossing lines that
  count people or cars in and out, parking spots (taken / free), "keep clear" areas, a front-door
  spot that notices parcels left or taken, a gate that is open or closed.
- **Incidents.** Alerts that belong together are grouped into one incident with snapshots, a short
  video clip from a few seconds before to a few seconds after, and buttons to mark it as seen or a
  false alarm.
- **Number plates** (optional add-on): reads licence plates at a barrier or driveway and keeps a log.
- **AI brain** (optional add-on): a second, smarter AI model double-checks alerts before they
  reach you, and can look for things you describe in words ("hand on a car door handle").
- **Alerts where you want them:** Telegram, ntfy push, email, Discord, webhooks, Home Assistant,
  and a phone web app.
- **Runs on an ordinary PC.** An NVIDIA graphics card makes it fast; without one it still works, just
  checks fewer pictures per second.

## What you need

| | |
|---|---|
| **Windows** | Windows 10 (version 1809 or newer) or Windows 11, 64-bit, Intel or AMD processor. On Windows 11, **Smart App Control must be off** (see [below](#smart-app-control)). |
| **Graphics card** | An NVIDIA graphics card (GeForce RTX 20 series or newer) is recommended. Without one, YardGuard uses the processor, which is slower. |
| **Cameras** | IP cameras or a recorder (NVR) that offer an RTSP video stream: Dahua (also Imou, Amcrest, Lorex), Hikvision (also HiLook, EZVIZ), and most other brands through a custom RTSP address. |
| **Disk space** | About 8 GB for YardGuard itself, plus space for snapshots and clips (capped at 5 GB by default). The AI brain add-on needs about 12 GB more. |
| **Internet** | Only for the first setup (about 1 to 4 GB of downloads) and for the optional add-ons. |

You do **not** need to install Python, ffmpeg or anything else. YardGuard brings its own copies and
keeps them inside its folder. No administrator rights are needed.

## Quick start

1. **Download YardGuard.**
   - Easiest: on this page click the green **Code** button, then **Download ZIP**. Before you open
     the ZIP, right-click it, choose **Properties**, tick **Unblock** (if you see it) and click OK.
     Then extract it to a short path such as `C:\YardGuard` or `D:\YardGuard`.
   - Or, if you use Git: `git clone https://github.com/Ninidze1/YardGuard.git D:\YardGuard`
2. **Double-click `YardGuard.cmd`** in that folder.
   - The first time, a small setup window appears. It downloads Python, the AI libraries and the
     video tools into the YardGuard folder. This takes about 5 to 20 minutes, depending on your
     internet speed. You can close it at any time; the next start continues where it stopped.
   - If Windows asks "Do you want to run this file?", click **Run**.
3. **Follow the setup wizard.** It finds the recorders and cameras on your network, asks for the
   login, checks which channels have video, and lets you pick the cameras to watch.
4. That's it. From now on, double-click `YardGuard.cmd` to start YardGuard. It opens straight away.

> Tip: to start YardGuard from the desktop, right-click `YardGuard.cmd`, choose
> **Send to > Desktop (create shortcut)**.

The AI models download by themselves when they are first needed (from about 5 MB for the fast
model to about 110 MB for the most accurate one). You can switch models in **Settings > AI models**.

## Setting up your cameras

YardGuard reads the same video stream that your recorder's own app uses, called **RTSP**.

- **Use the recorder if you have one.** Connect YardGuard to the NVR instead of to each camera: one
  login gives access to every channel.
- **Create a view-only user.** In your recorder's or camera's settings, add a new user that may only
  watch live video (no settings, no playback, no PTZ). Give that user to YardGuard. Your main admin
  password then never leaves the recorder.
- **Check that RTSP is turned on.** On most Dahua and Hikvision devices it is on by default
  (port 554). Some brands have it under *Network > Advanced* or *Integration protocol*.
- **Same network.** The PC must be on the same home network as the recorder (or able to reach it).
- **Other brands.** Choose *Other brand (custom RTSP path)* in the wizard and enter the stream path
  from the camera's manual, for example `/Streaming/Channels/101` or `/live/ch00_0`.
- **Passwords are safe.** The camera password is encrypted with your Windows account and stays on
  this PC (in `config.json`, which only your Windows user can decrypt).

## Optional add-ons

You install these from inside YardGuard, under **Settings**. They are not needed for normal use.

### AI brain (LocateAnything)

A large AI model by NVIDIA that takes a second, careful look at alerts and can find things you
describe in plain words.

- Download: about **7.6 GB** (the model plus its own AI libraries); about 12 GB on disk.
- Needs an **NVIDIA graphics card with about 9 GB of memory** while it runs. You can start and stop
  it at any time to free the graphics card for games.
- **Licence: free for personal, non-commercial use only** (NVIDIA's licence for LocateAnything-3B,
  see [huggingface.co/nvidia/LocateAnything-3B](https://huggingface.co/nvidia/LocateAnything-3B)).
  Do not use it in a business without checking that licence.

### Licence plates

Reads number plates on cameras you choose (best at a barrier or driveway, with a clear view).

- Download: about **110 MB** (a plate detector and a text reader, PARSeq).
- Plates are only stored on this PC, in the alerts database.
- Check the rules in your country before you record and keep number plates.

## Alerts on your phone and other apps

Set these up in **Integrations**. Each one has a step-by-step guide and a **Send test** button. You
choose which alerts go where (for example only critical ones at night).

| Service | What you get |
|---|---|
| **Telegram** | Messages with snapshots from your own bot. You can also ask for status and arm or disarm monitoring from the chat. |
| **ntfy** | Free push notifications to the ntfy app on Android or iPhone, through ntfy.sh or your own ntfy server. |
| **Email** | Alerts by email through Gmail, Outlook, Yahoo or any mail server (with an app password). |
| **Discord** | Messages with pictures in a Discord channel, through a channel webhook. |
| **Webhook** | A JSON message to any web address, for your own automations (Node-RED, n8n, Zapier...). |
| **Home Assistant** | Cameras, alerts and a monitoring switch appear in Home Assistant automatically, through MQTT. |
| **Phone web app** | Open YardGuard's incidents and live status in your phone's browser on your home Wi-Fi. To use it away from home without changing your router, install [Tailscale](https://tailscale.com/download/windows) on the PC and on the phone; YardGuard then gives you a private, secure address. |

## Privacy

- **Video stays on your PC.** YardGuard analyses the camera streams locally. No cloud service, no
  account, no telemetry.
- **Data stays in the YardGuard folder:** settings in `config.json`, snapshots, clips and the alerts
  database in `events\` (you can choose another folder in Settings).
- **Cloud services only when you connect them.** Alerts and pictures leave your PC only to the
  services you set up yourself in Integrations (Telegram, email, and so on).
- **Internet use:** the first setup and the add-ons download software and AI models from their
  official sources (python.org via NuGet, PyPI, PyTorch, GitHub, Hugging Face). Every download is
  checked against a known checksum.

## Updating

1. Get the new version: `git pull` in the YardGuard folder, or download the new ZIP and copy its
   files over the old folder (your `config.json`, `events`, `models` and `.venv` are kept).
2. Close YardGuard.
3. Double-click **`Update.cmd`**. It installs the libraries the new version needs and keeps your
   cameras, settings, alerts and AI models.

## Uninstalling

- In YardGuard: **Settings > About > Uninstall**, or
- double-click **`Uninstall.cmd`** in the YardGuard folder.

It shows exactly what will be removed and asks you to type `YES`. It removes the YardGuard folder
(including its own Python, ffmpeg, AI libraries and models), the snapshots and alerts folder, and
desktop shortcuts. Nothing else on your PC is touched, and your recorder's recordings are not
affected.

## Troubleshooting

The setup log is in `.cache\setup.log`, the app log in `.cache\yardguard.log` (both inside the
YardGuard folder). The setup window has an **Open log** button.

**"Windows protected your PC" or "Do you want to run this file?"**
Windows asks this for files downloaded from the internet. Click **More info > Run anyway** or **Run**.
To avoid it, unblock the ZIP before extracting it (right-click > Properties > Unblock).

<a id="smart-app-control"></a>**Smart App Control** ("An Application Control policy has blocked this file")
Windows 11's Smart App Control blocks the AI libraries YardGuard runs on (PyTorch and its
graphics card files). YardGuard therefore needs it **off**; setup and the app tell you when it is on.

- Open *Windows Security > App & browser control > Smart App Control settings* and choose **Off**.
- Windows does not let you turn Smart App Control back on without resetting Windows. Your
  antivirus (Microsoft Defender) keeps protecting the PC either way.
- Then double-click `YardGuard.cmd` again.

**Windows Firewall asks about Python**
This appears when you turn on the phone web app or Home Assistant. Allow access on **Private**
networks only (your home network), not on Public networks.

**No NVIDIA graphics card, or "AI on processor (slow)"**
YardGuard works without an NVIDIA card but checks fewer pictures per second. Use the *Fast* AI
model (Settings > AI models) and watch fewer cameras at once. If you do have an NVIDIA card:
- update the driver from [nvidia.com/drivers](https://www.nvidia.com/drivers) (version 570 or newer)
  and restart the PC;
- then run `Update.cmd` so YardGuard installs the graphics card version of its AI libraries.
- If your card is too old for the current AI libraries (detection stops with a CUDA error), open
  a Command Prompt in the YardGuard folder and run `Update.cmd -Torch cpu` to use the processor.

**A camera is "Offline" or "Camera not reachable"**
- Check that the PC and the recorder are on the same network, and that you can open the recorder in
  its own app.
- Check the user name and password (Settings > Cameras > Edit). A recorder may lock a user for 30
  minutes after several wrong passwords.
- Check that RTSP is enabled and the port is right (usually 554).
- Try the *sub stream* (lower resolution) for that camera if the network is slow.

**"The YardGuard folder is too deep inside other folders"**
Some AI library files have long names. Move the YardGuard folder to a short path such as
`C:\YardGuard`, then double-click `YardGuard.cmd` again.

**I moved the YardGuard folder and now it does not start**
Double-click `YardGuard.cmd`. It notices the move and repairs its Python environment (this takes a
few minutes, the downloads are reused).

## For developers

```
YardGuard.cmd      start (installs on first run)    Update.cmd / Uninstall.cmd
app.py             app entry point                  run.cmd   run a script with the app's Python
yardguard/         the app: engine, tracking, rules, areas, plates, AI brain, UI
  integrations/    Telegram, ntfy, email, Discord, webhook, MQTT / Home Assistant, phone web app
  web/             phone web app (HTML, icons)
setup/             installer (bootstrap.ps1), pinned versions and hashed requirements
scripts/           console tools: check_stream.py, test_camera.py
tests/             test scripts (exit code 0 = passed)
```

- `YardGuard.cmd` starts the app (and runs `setup\bootstrap.ps1` the first time).
  `setup\bootstrap.ps1 -Unattended -NoLaunch` installs without a window;
  `-Torch cpu|cuda` forces the PyTorch build.
- `run.cmd <script>` runs a script with the app's Python, with every cache inside the folder:
  - `run.cmd app.py` starts the app with a console,
  - `run.cmd tests\test_engine.py` runs a test.
- Tests are plain scripts in `tests\` (exit code 0 = passed). Fast, self-contained ones:
  `test_engine.py`, `test_areas.py`, `test_overlay.py`, `test_plates.py --unit`,
  `test_gui_wiring.py`, `test_integrations_*.py`, `test_settings_ui.py`. Scripts that need the
  developer's own cameras, snapshots or recorded clips print `SKIP` and exit 0 when that data is
  missing.
- Exact versions with SHA-256 hashes are in `requirements.txt` and `setup\torch-cu128.txt` /
  `setup\torch-cpu.txt`. After changing packages in `.venv`, run
  `run.cmd setup\lock_requirements.py` to write them again. PyTorch's version is set in that
  script (`TORCH_PIN`). The pinned Python and
  ffmpeg downloads and their checksums are in `setup\versions.json`.
- `run.cmd build.py` builds the optional `YardGuard.exe` / `Uninstall.exe` launchers (PyInstaller).

## Licence

YardGuard is free software under the **GNU Affero General Public License v3.0** (AGPL-3.0); see
[LICENSE](LICENSE). It uses Ultralytics YOLO, which is AGPL-3.0 as well. If you run a modified
version for other people over a network, you must offer them its source code.

Third-party components and their licences are listed in [THIRD_PARTY.md](THIRD_PARTY.md). Note
in particular that the optional **LocateAnything** AI brain is for **non-commercial use only**.

YardGuard is a hobby project with no warranty. It helps you notice things; it is not a certified
alarm system.
