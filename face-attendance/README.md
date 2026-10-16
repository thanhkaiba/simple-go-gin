# Camera attendance

A computer in the office reads the camera stream, recognises faces **locally**, works out who arrived on time, who is late and who did not come, and posts to **Google Chat**. The web page is about that data: today's attendance with corrections, a late & absence report with CSV export, employees and their photos, the live camera view and settings. **Holidays and leave come from the HR system's API**.

```
Camera (RTSP) ──► Office computer: detect faces → drop poor frames → follow each face across frames → match
                    │
                    ├─ confident ──────► record arrival time
                    └─ not confident ──► "unidentified" queue → admin picks the right person
                    │
                    ├─► Google Chat: not-seen list at the late time, late arrivals, unidentified faces, summary
                    └─► Admin web page: attendance & corrections, late/absence report, employees, live camera, settings
HR system API ──► holidays, make-up work days, employee leave (refreshed every 15 min and before the late time)
```

Runs on **Windows 10/11**, **macOS** (Apple Silicon or Intel) and **Linux**.

## Attendance rules

Times are set on the Settings page; defaults shown.

| Situation | Result |
|---|---|
| First seen **before 10:00** | On time. Being seen again after 10:00 changes nothing |
| **Not seen by 10:00** | Late |
| First seen **after 10:00** | Late, posted right away: "🟠 *Bob* arrived at 10:23, 23 min late." |
| Not seen all morning | Listed as "Not seen on camera" in the summary |
| On leave, business trip, working remotely (from the HR system) | Never counted as late or missing |
| Holiday (from the HR system) | No notifications at all |
| Day when the camera did not work at all | Nobody is counted as "not seen" |
| Face not recognised | Sent to the admin. Once confirmed, the arrival time is the time of the photo |

**Google Chat messages.** All of a day's messages go in one thread.

- **10:00:** everyone not seen yet (each person @mentioned), plus who is on leave and who has no reference photos.
- **Late arrival:** as it happens, with an @mention.
- **Daily late message** when the camera stops (11:00 by default), as its own message: "Morning, coming late today: @Jennie Luu (10:15), and @Steven Hoang (10:32)". The wording can be changed in Settings; it can also be sent earlier from the Today page after checking it. Sending it for another day than today puts that day in front of the message.
- **Unidentified faces:** batched every 5 minutes at most, with a link to review them.
- **Correction:** a short update whenever the admin fixes something after the 10:00 list went out.
- **11:00:** daily summary.
- **Warnings:**
  - when the camera or computer was down during the camera hours, or the camera picture was black, blank or frozen;
  - when two employees' reference photos look alike.

**⚠️ The camera only runs during the camera hours** (default 08:00–11:00, late from 10:00). Someone who arrives **before 08:00** and does not pass the camera again after 08:00 is counted as late. If people come in even earlier, set the start time earlier.

## Model choice

The detection and recognition models were picked on their merits. Two backends are supported, and `evaluate` compares them on your own photos:

| | `insightface` (default) | `opencv` |
|---|---|---|
| Detector | SCRFD-10G (5 landmarks) | YuNet (5 landmarks) |
| Recogniser | ArcFace ResNet50, WebFace600K | SFace |
| Accuracy | Higher, notably on blurred and small faces | Lower |
| Speed (CPU) | ~200 ms per frame at 640 px | ~20 ms per frame |
| Licence | **Non-commercial research only.** Commercial use needs a licence from InsightFace (contact@insightface.ai) | MIT / Apache 2.0, free for commercial use |

> Company attendance is a commercial use. Either get an InsightFace licence or set `backend: opencv` in `config.yaml`. Run `evaluate` with both first to see the difference on your photos.

Other design decisions:
- **Faces come from a face detector, not YOLO.** A generic YOLO model gives no facial landmarks (needed for alignment). A person detector is only used, optionally, to follow people in groups (below).
- **No `insightface` package.** The ONNX models run directly on onnxruntime, so nothing needs compiling.
- **No cloud.** Recognition stays on the office computer and images never leave it.

## 1. Install

### Installer (recommended)

Download the installer for your computer from the newest [release](https://github.com/thanhkaiba/simple-go-gin/releases/latest). It contains Python and every library; only the face models (about 300 MB) are downloaded, by the program itself on its first start, with the progress shown on the page. No admin rights needed.

| | Windows | macOS | Linux |
|---|---|---|---|
| Download | `FaceAttendance-Setup-<version>.exe` | `FaceAttendance-<version>-macos-arm64.dmg` (Apple chip) or `-macos-x64.dmg` (Intel) | `FaceAttendance-<version>-linux-x64-installer.tar.gz` |
| Install | run it (SmartScreen: *More info → Run anyway*) | open the .dmg, drag *FaceAttendance* to *Applications*, then right-click it → *Open* the first time | unpack, run `./install.sh` |
| Settings, data, models | `%LOCALAPPDATA%\FaceAttendance` | `~/Library/Application Support/FaceAttendance` | `~/.local/share/face-attendance` |
| Remove | *Settings → Apps → Face Attendance* | run `/Applications/FaceAttendance.app/Contents/MacOS/FaceAttendance uninstall` in Terminal, then delete the app | `./uninstall.sh` |

The program starts at login, restarts itself if it stops, and opens the **Setup** page the first time. Opening it again just opens the page.

**Updates are automatic.** Every 6 hours the program looks for a newer release, downloads it into the data folder and, outside camera hours, restarts into it. *Settings → Access → Updates* shows the version and has *Check now*; untick the box to stop automatic updates. Data and settings are kept. Coming from the install script below: install over it; it uses the same data folder and stops the old copy.

To publish a new version: change `__version__` in `attendance/__init__.py` if you like, then push a tag (`git tag v1.2.0 && git push origin v1.2.0`). GitHub Actions (`.github/workflows/build.yml`) builds and tests the packages on Windows, macOS and Linux and publishes the release; installed copies update within hours. Build one locally with `pip install . pyinstaller certifi && pyinstaller packaging/face-attendance.spec`.

### Install script (from the source folder)

No commands needed, and no admin rights. The installer gets its own Python, the libraries and the models (about 300 MB the first time), starts the program now and at every login, puts a shortcut on the Desktop and opens the **Setup** page.

1. Download this folder (on GitHub: *Code → Download ZIP*) and unzip it.
2. Double-click the installer in the `face-attendance` folder:
   - **macOS:** `Install.command`. If macOS says it cannot be opened, right-click it → *Open* → *Open*. When asked whether Python may accept incoming network connections, click *Allow*.
   - **Windows:** `Install.bat`. If SmartScreen warns, click *More info* → *Run anyway*. When Windows Firewall asks about Python, allow it on *private networks*.
3. The browser opens the **Setup** page: camera address (with a picture check), a password for the page, the address other computers use, Google Chat (with a test message) and the employees. Each step is ticked when done.

| | macOS | Windows |
|---|---|---|
| Program, settings and data | `~/Library/Application Support/FaceAttendance` | `%LOCALAPPDATA%\FaceAttendance` |
| Starts automatically | at login (LaunchAgent; restarted if it stops; keeps the Mac awake) | at login (Startup folder; restarted if it stops) |
| Update | download the new version and run its `Install.command` (or `Update.command` in a git checkout) | download the new version and run its `Install.bat` |
| Remove | `Uninstall.command` | `Uninstall.bat` |

Updating keeps settings, attendance records and photos; *Uninstall* asks before deleting them. The program starts when that user logs in, so turn on automatic login on the office computer (see [Keep it running](#5-keep-it-running)). The program log is `data/attendance.log` in the folder above.

### Manual install

You need **Python 3.10 or newer**.

### Windows

1. Install Python from [python.org](https://www.python.org/downloads/). Tick **"Add python.exe to PATH"**.
2. Open *Command Prompt* in the `face-attendance` folder:
   ```bat
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   copy config.example.yaml config.yaml
   python -m attendance download-models
   python -m attendance run
   ```
3. When Windows Firewall asks, allow Python on **private networks**, so the admin page can be opened from other computers.

### macOS

1. Install Python from [python.org](https://www.python.org/downloads/) or with Homebrew (`brew install python@3.12`).
2. In *Terminal*, in the `face-attendance` folder:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   cp config.example.yaml config.yaml
   python -m attendance download-models
   python -m attendance run
   ```
3. When macOS asks whether Python may accept incoming network connections, click **Allow**.

Older Intel Macs (e.g. 2017 models) work. On macOS 12 or older use **Python 3.12** (not 3.13): pip then installs the last onnxruntime and OpenCV versions built for that macOS. See [Slow computer](#slow-computer-eg-a-2017-mac) for settings.

### Linux

Same as macOS. On a machine with an NVIDIA GPU, install `onnxruntime-gpu` instead of `onnxruntime` and set `use_gpu: true`.

Then open **http://localhost:8000**, or `http://<computer IP>:8000` from another computer.

## 2. First-time setup (on the admin page)

1. **Settings:**
   - **Camera address:** for Ezviz, `rtsp://admin:<VERIFICATION_CODE>@<CAMERA_IP>:554/Streaming/Channels/101` (the main, full-resolution stream; `.../102` is the low-resolution sub stream, and some models use `.../H.264` instead). The code is the 6 capital letters on the camera label; first enable RTSP / LAN Live View in the EZVIZ app if your model has it. Give the camera a fixed IP in the router.
   - **Working hours** and **work days**.
   - **Google Chat webhook:** in the space, open the space name → *Apps & integrations* → *Webhooks* → *Add webhook*. Then click **Send a test message**.
   - **Address of this page:** used for links in chat messages.
   - **A password.**
2. **Employees:**
   - Add people one by one, or import a CSV file with columns `id,name`.
   - Open each person and **upload 5–10 photos**: looking straight, slightly left and right, with and without glasses, and one with a mask if they often wear one. Frames from the door camera itself work best.
   - **Or skip the photos and let the camera collect faces**, as described in the next step.
   - The page warns about blurred photos, photos without a face, and photos that look like another employee.
3. **HR system** (Settings): enter the API address and token, then click *Load leave data from the HR system now*. See [HR system API](#hr-system-api) for the format.
4. **Live camera:** click *Start preview* to see what the camera sees, with each face labelled. Use it to position the camera. Green boxes mean a face is recognised; orange boxes show why a face cannot be used (too small, blurred, turned away).

## 3. Daily use

The menu at the top has everything:

- **Today:** everyone's status for the day.
  - **Tiles** (Late, Not seen, On time, On leave) show the counts. Click one to show only those people; type in the search box to find someone.
  - **‹ ›** move to the previous or next day; the date box jumps to any day.
  - The page refreshes itself every minute while it shows today.
  - **Correcting:** click a person (or their *Edit* button) to open a small window where you can:
    - see **every time the camera saw them** that day, with the photo; the one their arrival comes from is outlined in green;
    - fix **one sighting**: *It was… → Move* (it was someone else) or *Not them* (a visitor, say). The arrival is then **recomputed from the remaining sightings**. Example: Alice was taken for Bob at 9:20 and Bob really came at 10:15. Move the 9:20 sighting to Alice and Bob is shown late at 10:15, instead of disappearing;
    - **change or add the arrival time**, e.g. someone who missed the camera. A time typed in wins over the camera;
    - **fix a wrong person** for the arrival itself ("Recorded the wrong person? It was…");
    - **delete** a wrong arrival. If the person really comes in later, the camera records them again.

  The arrival is the earliest sighting that has not been removed. Every sighting is kept (with its photo, until the retention period), so nothing the camera saw is lost when something is corrected.
- **Late & absence report:** totals per employee for a period: work days, on time, late, minutes late, not seen, on leave, plus a day-by-day list.
  - Quick buttons choose this month, last month, the last 7 or 30 days.
  - Download a **summary CSV** or a **CSV with every day**; both open in Excel.
  - Days when the camera did not work are flagged and not counted as absences.
- **To review** (red number = how many people): faces the camera could not identify.
  - **Grouping:** photos that look like the same person are grouped ("Seen 6 times"), so each person is named only once.
  - **Naming a group:** click **"✓ It's …"** for the suggested person, pick someone from the list, or click **+ New employee** to create them on the spot (the next id is suggested).
  - **What confirming does:** records the person's arrival on every day they were seen and teaches the system up to 5 of their photos.
  - **Not an employee · ignore today:** hides a visitor for the rest of the day, including if they pass the camera again. If they come back on another day they are shown again; nothing is ignored for good. Ignored faces are listed under *Ignored today*, with a **Restore** button in case of a mistake.

  **Starting without any photos:** let the camera run for a day or two. Everyone who passes appears here; name each person once, and from then on the camera recognises them. Upload extra photos later for people it still finds hard.
- **Employees:** search, add, or import from CSV. Open a person to drag and drop their photos (with a preview before uploading), see their upcoming leave, or stop tracking them.
- **Live camera:** what the camera sees, with faces labelled; outside camera hours, turn it on for 15 minutes to adjust its position.
  - **Detection area:** click *Select area*, drag over the walkway on the picture and *Save area*. Only that part is analysed (the rest is dimmed): faces get bigger for the detector, which helps most with a camera that sees a wide room, and people sitting elsewhere are ignored. *Use whole picture* undoes it.
- **Accuracy:** how well the camera is doing, measured from your corrections on the Today page: the share of identifications that were the wrong person, the share of arrivals it did not recognise, mix-ups (who was taken for whom), and the people who need more photos. Use it to see whether a change of camera position, photos or settings helped.
- **Settings:** working hours, Google Chat (with a test button), HR system, camera, password and privacy. Rarely needed recognition values are under *Advanced*.
- **Change log:** every change made on the page, with who made it and when; searchable.

## HR system API

Holidays, make-up work days and leave are read from the HR system; they are not entered on this web page. The program calls:

```
GET <leave_api.url>?from=2026-10-08&to=2026-12-14
Authorization: Bearer <leave_api.token>
```

and expects JSON like this:

```json
{
  "holidays": [{"date": "2026-11-20", "name": "Teachers' Day"}],
  "workdays": [{"date": "2026-10-24", "name": "Make-up day"}],
  "leaves": [
    {"employee_id": "NV001", "start": "2026-10-15", "end": "2026-10-16", "type": "leave", "note": "Family"},
    {"employee_id": "NV002", "start": "2026-10-15", "type": "sick", "period": "morning"}
  ]
}
```

- **`employee_id`** must match the id on the Employees page.
- **`end`** defaults to `start`.
- **`type`** is free text. `leave`, `sick`, `business_trip` and `remote` get friendly labels; anything else is shown as sent.
- **`period`** is `full` (default), `morning` or `afternoon`. Attendance is checked in the morning, so afternoon-only leave is ignored.
- **Each refresh replaces** the data for the requested range, so cancelled leave disappears too.

**If the HR system uses a different format**, only the `parse()` function in `attendance/leave_api.py` needs changing.

**When refreshes happen:** every 15 minutes, once more 5 minutes before the late time, and when someone clicks *Refresh now* on the Attendance page.

**If the HR system is unreachable**, the last copy keeps being used. The Attendance page and the chat messages then say when the data was last updated.

## 4. Tune recognition with your photos (recommended)

With labelled photos (e.g. from earlier tests), find the threshold at which nobody is identified as someone else:

```bash
# Folders: faces/<employee id>/*.jpg; optionally eval/<employee id>/*.jpg and eval/_unknown/*.jpg (non-employees)
python -m attendance evaluate --gallery faces --test eval --backends insightface opencv
```

Enter the recommended values in **Settings → Recognition**, adding about 0.02 to the match threshold as a safety margin. Include hard photos (dark, blurred, side views, masks); they are what separates the backends.

- **Visits:** frames from one walk past the camera can go in a subfolder, `faces/<employee id>/<visit>/*.jpg`. Without `--test`, a whole visit is left out of the references while it is tested (frames of one walk are nearly identical, so testing them against each other flatters the result), and each visit is also scored the way the live system decides, by several agreeing frames.
- **Centering:** each backend is also tested with `center_embeddings` (needs photos of at least 8 people). It removes the direction that all faces from one camera share, which makes some people look similar to everyone; another team measured 4 wrong frames instead of 20 with it. Scores are lower with it, so it comes with its own threshold. If `evaluate` says it is more accurate, set in `config.yaml`:
  ```yaml
  recognition:
    center_embeddings: true
    accept_score: <the value evaluate recommends for "centered">
  ```

**From the camera's own data (after a week or two of use):** every correction you make is remembered, so the system can test settings on real camera snapshots without any extra labelling:

```bash
python -m attendance evaluate --from-data --days 30
```

It uses faces moved to the right person, faces named on the review page, faces removed as "not them" and ignored visitors (today is left out until it has been reviewed), and recommends `accept_score` / `min_margin`, raw and centered. Run it again every few weeks.

`python -m attendance enroll --faces faces` imports the same folders as reference photos, as an alternative to uploading on the web page.

### @mentions in Google Chat

Google Chat only turns a name into a real @mention (with a notification) when it knows the person's **Google Chat user ID**, a long number. A webhook message mentions someone with `<users/ID>`.

**Where to find the ID:** in the Google Admin console under *Directory → Users*, open the person; the number at the end of the page address is their ID.

**Where to enter it:**
- on the employee's page, then click **Send test mention** to check that it works;
- or in bulk, with a `chat_user` column in the employees CSV import.

The ID is a number; `users/123…` as copied from a page address is accepted too. Anything else is refused, because a wrong ID would silently become plain text.

People without an ID still appear as "@Name", but as plain text, without a notification. The Today page lists who is missing an ID.

## 5. Keep it running

**Computer settings.**
- **Windows:** Power → Sleep → *Never*. Set Windows Update *active hours* to cover the morning. In the BIOS, enable *Restore on AC Power Loss*.
- **macOS:**
  - System Settings → *Energy* (or *Battery → Options*): enable *Prevent automatic sleeping* and *Start up automatically after a power failure*.
  - Turn off automatic macOS updates during the morning.
- **All computers:** a UPS for the computer, router and camera is recommended.

**Start automatically.**

*Windows*, with [NSSM](https://nssm.cc):
```bat
nssm install Attendance "C:\attendance\face-attendance\.venv\Scripts\python.exe" "-m attendance run"
nssm set Attendance AppDirectory "C:\attendance\face-attendance"
nssm start Attendance
```

*macOS*: save as `~/Library/LaunchAgents/com.company.attendance.plist`, adjusting the paths, then run `launchctl load ~/Library/LaunchAgents/com.company.attendance.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.company.attendance</string>
  <key>WorkingDirectory</key><string>/Users/admin/face-attendance</string>
  <key>ProgramArguments</key><array>
    <string>/Users/admin/face-attendance/.venv/bin/python</string><string>-m</string><string>attendance</string><string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```
Also enable automatic login for that user (System Settings → Users & Groups), so the agent starts after a power cut.

*Linux*, `/etc/systemd/system/attendance.service`:
```ini
[Unit]
Description=Camera attendance
After=network-online.target

[Service]
WorkingDirectory=/opt/face-attendance
ExecStart=/opt/face-attendance/.venv/bin/python -m attendance run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now attendance
```

**If Google Chat is slow or unreachable**, messages wait in a queue on disk and are delivered later, in order; the camera keeps running meanwhile. Messages older than a day are dropped.

**Alert when the computer stops.**
 Create a free check on [Healthchecks.io](https://healthchecks.io) with a 1-minute period and 5-minute grace, and enter its ping URL in Settings. If the computer or the program stops, Healthchecks.io sends an email.

**Remote access.** The admin page only works inside the office network by default. For remote access, install [Tailscale](https://tailscale.com) on this computer and the admin's devices. Never open a port to the internet.

## Privacy

Face photos are personal data. Inform employees and get their consent.

- **Camera snapshots** (arrivals and unidentified faces) are deleted automatically after the number of days set in Settings (default 90). Arrival times are kept.
- **Reference photos** are kept until deleted on the employee's page.
- **Deleting an employee** removes all of their face data.

Everything is stored in the `data/` folder:
- `attendance.db`: database
- `faces/`: reference photos
- `crops/`: camera snapshots
- `attendance.log`: log

## Getting the best accuracy

**Camera placement** matters more than any setting. Then set a **detection area** on the Live camera page around the walkway.
- Mount it where everyone **must walk towards it**.
- Face height or slightly above, tilted down less than 15°.
- Faces at least 80 px wide (check on the Live camera page).
- The detector looks at the picture shrunk to `det_size` (640 px by default). A 1080p camera is shrunk three times, so an 80 px face becomes 27 px for the detector; if faces are small or far away, set `det_size: 960` in `config.yaml` (slower, more accurate).

**Camera angle.** Head turn and tilt are estimated from 5 points on the face, and a camera on a desk
pointing up (or above a door pointing down) makes every face look tilted. The system therefore
**learns how this camera normally sees faces** from the first ~30 clear matches, and judges turn and
tilt relative to that (Settings → Recognition → Advanced shows what it learned; the Live camera page
too). After moving the camera, click *Learn again* there. Still, keep the camera below about 15° of
tilt: faces seen steeply from below or above are harder for any model.

**Light** should come from in front of people. **Never** point the camera at a glass door or window (backlight).

**Masks and sunglasses:**
- Upload photos with masks.
- Anything still uncertain goes to the admin rather than being guessed.

**Wi-Fi cameras on a desk** drop frames. A wired PoE camera is more reliable.

## Groups of people (person tracking)

When several people walk in together, faces turn away and cover each other for a moment. In the
default **face** mode each face is followed on its own, so a face that disappears for more than
`track_timeout_s` starts a new track and its earlier votes are lost.

**Person** mode (Settings → Camera → Groups of people, or `tracking.mode: person`) also detects
and follows whole bodies:
- [YOLOX](https://github.com/Megvii-BaseDetection/YOLOX) person detector (Apache-2.0) and
  [ByteTrack](https://github.com/ifzhang/ByteTrack) (MIT, re-implemented in numpy). A
  half-hidden person is kept by low-confidence detections instead of being dropped.
- A face track stays attached to its person while the face is out of sight, so votes from
  every frame where the face was visible add up, and faces of two people are never merged.
- Faces are also searched in each person's head region at full resolution, which finds the
  small faces at the back of a group.
- The arrival time is when the person first appeared, not when their face was first sharp.

Install the model once:
```bash
python -m attendance download-models --person            # yolox_tiny (fast, recommended)
python -m attendance download-models --person yolox_s    # more accurate, about 3x slower
```
Then set `tracking.person_model: models/yolox_s.onnx` if you chose `yolox_s`. If the model
file is missing the camera keeps working in face mode and the Live camera page says why.
A YOLOv8 ONNX export also works, but Ultralytics models are AGPL-3.0: check the licence first.

**Is this computer fast enough?** Record a minute of people walking in (any video file) and run:
```bash
python -m attendance benchmark entrance.mp4
```
It runs the video through both modes and prints the frames per second and who was recognised,
without touching the attendance data.

Measured on a 4-core server CPU, 1280x720, one person plus a group in view:

| | face mode | person mode (yolox_tiny) |
|---|---|---|
| `insightface` | ~360 ms per frame | ~375 ms per frame |
| `opencv` | ~40 ms per frame | ~115 ms per frame |

Person mode only searches the whole frame for faces every `full_frame_every` frames (4 by
default) and the head regions on every frame, so it costs little extra with `insightface`.

**Tested on a crowded doorway video** (people walking in close together, see *Tested on public
video* below), person mode did **not** recognise more people than face mode, and sent more faces to
review, so face mode stays the default. Try person mode with `benchmark` on your own entrance video
before switching.

### Slow computer (e.g. a 2017 Mac)

An Intel Mac from 2017 is roughly half as fast as the numbers above. In order of effect:
1. `recognition.backend: opencv`: about 10x faster (also free for commercial use). Check the
   accuracy difference on your photos with `evaluate` first.
2. *Frames per second* in Settings: 2-3 is enough for people walking past. When the computer
   is slower than this it simply skips frames; nothing breaks, but fewer frames vote.
3. In person mode: keep `yolox_tiny` and raise `full_frame_every`.
4. Keep the Live camera page closed when not needed (drawing the picture costs CPU).
5. `recognition.cpu_threads` (default: all CPUs but one) leaves a core for video decoding and this page; set it lower if the picture stutters, higher if nothing else runs on the computer.

Run `benchmark` on the real machine and pick the combination that reaches the frame rate you set.

## Tested on public video

The system was run, as if the video were the camera, on the [ChokePoint dataset](https://arma.sourceforge.net/chokepoint/)
(NICTA; Wong et al., CVPR Workshops 2011; CC BY-NC 4.0, research use, not included here): an office
doorway filmed by a camera above the door. Reference photos were cut from one recording session and
another session was played back.

| | insightface | opencv |
|---|---|---|
| One person at a time: 20 enrolled people + 5 strangers | 20/20 recognised, 0 wrong, 5/5 strangers sent to review | 20/20, 0 wrong, 5/5 |
| Time from appearing to being recognised (3-5 frames/s) | mostly 0.5-1 s | mostly 0.5-1 s |
| Crowded entry (people close together, backlight, glass door) | 18 people named, all correct; the rest sent to review | 15 named, all correct |
| Time per frame on a 4-core server CPU, crowded scene | ~670 ms | ~65 ms |

Lessons built into the settings:
- **A camera above head height** makes every face look tilted down. A face that fails the pose or
  sharpness check therefore still votes when it matches one person very clearly (at least
  `accept_score + 0.15`, with twice the usual margin); before this, 4 of the 20 people were missed.
- **Glass doors reflect faces**, which then show up as unknown faces to review. Set a detection area
  that leaves the glass out.

## How wrong identifications are avoided

1. **5-point alignment:** every face is aligned before its embedding is computed.
2. **Quality gate:** small, blurred, turned-away or weakly detected faces **do not vote**, unless a big, clearly detected face matches one person far above the threshold. Turn and tilt are measured from the camera's usual view of faces, learned automatically.
3. **Several references per person:** a person's score is the mean of their two closest reference photos.
4. **Clear margin:** the best match must beat the runner-up by a margin.
5. **Several frames must agree:** at least twice as many votes as everyone else combined. A track that ends one vote short is still accepted when all its votes agree and the match is very strong (fast walkers).
6. **No guessing:** everything else goes to the admin, and each confirmation teaches the system that face.
7. **Look-alike check:** employees whose photos match each other are flagged before they cause mix-ups.
8. **Varied references:** reference photos are enrolled together with their mirror image, and a confirmed face that is almost identical (≥ 97%) to one already kept is not stored again.
9. **Sharpest frame:** of the frames that arrived since the last one analysed, the one where the faces are sharpest is used (Wi-Fi cameras and people walking give many blurred frames).
10. **Camera health:** a black, blank or frozen picture counts as an outage instead of "nobody came"; days without any camera data are never counted as absences.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests need no face models or camera.
