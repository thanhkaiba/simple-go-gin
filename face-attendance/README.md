# Camera attendance

A computer in the office reads the camera stream, recognises faces **locally**, works out who arrived on time and who is late, and posts to **Google Chat**. Everything is managed from a web page: employees and their photos, holidays and leave, corrections, the live camera view and all settings.

```
Camera (RTSP) ──► Office computer: detect faces → drop poor frames → follow each face across frames → match
                    │
                    ├─ confident ──────► record arrival time
                    └─ not confident ──► "unidentified" queue → admin picks the right person
                    │
                    ├─► Google Chat: not-seen list at the late time, late arrivals, unidentified faces, summary
                    └─► Admin web page: attendance, corrections, employees, holidays & leave, live camera, settings
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
| On leave, business trip, working remotely | Never counted as late or missing |
| Company holiday | No notifications at all |
| Face not recognised | Sent to the admin. Once confirmed, the arrival time is the time of the photo |

**Google Chat messages.** All of a day's messages go in one thread.

- **10:00:** everyone not seen yet, plus who is on leave and who has no reference photos.
- **Late arrival:** as it happens.
- **Unidentified faces:** batched every 5 minutes at most, with a link to review them.
- **Correction:** a short update whenever the admin fixes something after the 10:00 list went out.
- **11:00:** daily summary.
- **Warnings:**
  - when the camera or computer was down during the camera hours, or the camera picture was black, blank or frozen;
  - when two employees' reference photos look alike.

**⚠️ The camera only runs during the camera hours** (default 09:00–11:00). Someone who arrives **before 09:00** and does not pass the camera again after 09:00 is counted as late. If people come in early, set the start time earlier, e.g. 08:00.

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
- **No YOLO.** A generic YOLO detector gives no facial landmarks (needed for alignment), and Ultralytics YOLOv8 is AGPL-3.0.
- **No `insightface` package.** The ONNX models run directly on onnxruntime, so nothing needs compiling.
- **No cloud.** Recognition stays on the office computer and images never leave it.

## 1. Install

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

### Linux

Same as macOS. On a machine with an NVIDIA GPU, install `onnxruntime-gpu` instead of `onnxruntime` and set `use_gpu: true`.

Then open **http://localhost:8000**, or `http://<computer IP>:8000` from another computer.

## 2. First-time setup (on the admin page)

1. **Settings:**
   - **Camera address:** for Ezviz, `rtsp://admin:<VERIFICATION_CODE>@<CAMERA_IP>:554/H.264`. The code is the 6 capital letters on the camera label; first enable RTSP / LAN Live View in the EZVIZ app if your model has it. Give the camera a fixed IP in the router.
   - **Working hours** and **work days**.
   - **Google Chat webhook:** in the space, open the space name → *Apps & integrations* → *Webhooks* → *Add webhook*. Then click **Send a test message**.
   - **Address of this page:** used for links in chat messages.
   - **A password.**
2. **Employees:**
   - Add people one by one, or import a CSV file with columns `id,name`.
   - Open each person and **upload 5–10 photos**: looking straight, slightly left and right, with and without glasses, and one with a mask if they often wear one. Frames from the door camera itself work best.
   - The page warns about blurred photos, photos without a face, and photos that look like another employee.
3. **Holidays & leave:** enter public holidays, make-up work days and known leave.
4. **Live camera:** click *Start preview* to see what the camera sees, with each face labelled. Use it to position the camera. Green boxes mean a face is recognised; orange boxes show why a face cannot be used (too small, blurred, turned away).

## 3. Daily use

- **Attendance:** who is on time, late, not seen or on leave, with photos; pick a date to see past days. Every row can be corrected:
  - **Change time / Add arrival:** enter an arrival by hand, e.g. someone who missed the camera.
  - **Wrong person? It was… → Move:** the camera recorded the wrong person; the time and photo move to the right one.
  - **Delete:** remove a wrong arrival.
- **Unidentified:** faces the system was not sure about. Pick the right person and the arrival is recorded at the photo time; the face is also added to their reference photos, so they are recognised next time. *Ignore* is for visitors.
- **Change log:** every change made on the page (corrections, confirmations, photos, employees, calendar, settings), with who made it and when.

## 4. Tune recognition with your photos (recommended)

With labelled photos (e.g. from earlier tests), find the threshold at which nobody is identified as someone else:

```bash
# Folders: faces/<employee id>/*.jpg; optionally eval/<employee id>/*.jpg and eval/_unknown/*.jpg (non-employees)
python -m attendance evaluate --gallery faces --test eval --backends insightface opencv
```

Enter the recommended values in **Settings → Recognition**, adding about 0.02 to the match threshold as a safety margin. Include hard photos (dark, blurred, side views, masks); they are what separates the backends.

`python -m attendance enroll --faces faces` imports the same folders as reference photos, as an alternative to uploading on the web page.

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

**Alert when the computer stops.** Create a free check on [Healthchecks.io](https://healthchecks.io) with a 1-minute period and 5-minute grace, and enter its ping URL in Settings. If the computer or the program stops, Healthchecks.io sends an email.

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

**Camera placement** matters more than any setting.
- Mount it where everyone **must walk towards it**.
- Face height or slightly above, tilted down less than 15°.
- Faces at least 80 px wide (check on the Live camera page).

**Light** should come from in front of people. **Never** point the camera at a glass door or window (backlight).

**Masks and sunglasses:**
- Upload photos with masks.
- Anything still uncertain goes to the admin rather than being guessed.

**Wi-Fi cameras on a desk** drop frames. A wired PoE camera is more reliable.

**Slow computer:** lower *Frames per second* in Settings, or use `backend: opencv` (about 10× faster).

## How wrong identifications are avoided

1. **5-point alignment:** every face is aligned before its embedding is computed.
2. **Quality gate:** small, blurred, turned-away or weakly detected faces **do not vote**.
3. **Several references per person:** a person's score is the mean of their two closest reference photos.
4. **Clear margin:** the best match must beat the runner-up by a margin.
5. **Several frames must agree:** at least twice as many votes as everyone else combined. A track that ends one vote short is still accepted when all its votes agree and the match is very strong (fast walkers).
6. **No guessing:** everything else goes to the admin, and each confirmation teaches the system that face.
7. **Look-alike check:** employees whose photos match each other are flagged before they cause mix-ups.
8. **Camera health:** a black, blank or frozen picture counts as an outage instead of "nobody came".

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests need no face models or camera.
