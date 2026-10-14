# Camera attendance

A PC in the office reads the camera stream, recognises faces **locally**, works out who arrived on time and who is late, and posts to **Google Chat**. Faces it cannot identify go to a small web page where an admin picks the right person.

```
Camera (RTSP) ──► Office PC: detect faces → drop poor frames → follow each face across frames → match
                    │
                    ├─ confident ──────► record arrival time
                    └─ not confident ──► "unidentified" queue → admin picks the right person
                    │
                    └─► Google Chat: 10:00 not-seen list, late arrivals, unidentified faces, 11:00 summary
```

## Attendance rules

Times come from `config.yaml`; defaults shown.

| Situation | Result |
|---|---|
| First seen **before 10:00** | On time. Being seen again after 10:00 changes nothing |
| **Not seen by 10:00** | Late |
| First seen **after 10:00** | Late, posted right away: "🟠 *Bob* arrived at 10:23, 23 min late." |
| Not seen all morning | Listed as "Not seen on camera" in the summary |
| Face not recognised | Sent to the admin. Once confirmed, the arrival time is the time of the photo |

**Google Chat messages.** All of a day's messages go in one thread.

- **10:00:** everyone not seen yet.
- **Late arrival:** as it happens.
- **Unidentified faces:** batched every 5 minutes at most, with a link to review them.
- **Correction:** sent when the admin confirms that someone on the 10:00 list actually arrived before 10:00.
- **11:00:** daily summary.
- **Camera or PC outage:** if either stops during the window, the messages say for which period, so nobody is marked late unfairly.

**⚠️ The camera only runs during the window** (default 09:00–11:00). Someone who arrives **before 09:00** and does not pass the camera again after 09:00 is counted as late. If people come in early, set `window_start` earlier, e.g. `"08:00"`.

## Model choice

The detection and recognition models were picked on their merits, not on what earlier prototypes used. Two backends are supported, and `evaluate` compares them on your own photos:

| | `insightface` (default) | `opencv` |
|---|---|---|
| Detector | SCRFD-10G (5 landmarks) | YuNet (5 landmarks) |
| Recogniser | ArcFace ResNet50, WebFace600K | SFace |
| Accuracy | Higher, notably on blurred and small faces | Lower |
| Speed (CPU) | ~200 ms per frame at 640 px | ~20 ms per frame |
| Licence | **Non-commercial research only.** Commercial use needs a licence from InsightFace (contact@insightface.ai) | MIT / Apache 2.0, free for commercial use |

Other design decisions:

- **No YOLO.**
  - A generic YOLO detector gives no facial landmarks, and landmark alignment matters more for accuracy than the detector itself.
  - Ultralytics YOLOv8 is also AGPL-3.0 licensed.
- **No `insightface` Python package.**
  - The two ONNX models run directly on onnxruntime, so installation needs no C++ compiler on Windows.
  - Only the model weights carry the licence restriction; the package code is not needed.
- **Recognition stays local.** No images leave the office. A cloud API (e.g. AWS Rekognition) could later be added for the hard cases only.

> Company attendance is a commercial use. Either get an InsightFace licence or switch to `backend: opencv`. Run `evaluate` with both backends first to see what the difference is on your photos.

## 1. Set up the PC

Use a dedicated PC that stays on, running Windows 10/11 or Ubuntu. Not someone's laptop.

- **Python 3.10 or newer.**
- **Disable sleep:** Power → Sleep → Never.
- **BIOS:** enable *Restore on AC Power Loss* so the PC comes back on after a power cut.
- **Windows Update:** set active hours so updates never restart the PC during the morning.
- **UPS:** recommended for the PC, router and camera.

```bash
cd face-attendance
python -m venv .venv
.venv\Scripts\activate          # Linux: source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml          # Windows: copy
cp employees.example.csv employees.csv
python -m attendance download-models        # backend from config.yaml; add --backend opencv for the other
```

## 2. Configure

**`employees.csv`:** two columns, `id,name`. It can be edited in Excel; save as "CSV UTF-8".

**Ezviz camera:**

1. In the EZVIZ app, enable **RTSP** or **LAN Live View** if your model has it.
2. Set `camera.url` to:
   ```
   rtsp://admin:<VERIFICATION_CODE>@<CAMERA_IP>:554/H.264
   ```
   The verification code is the 6 capital letters on the label under the camera.
3. Check the stream first in VLC: Media → Open Network Stream.
4. Give the camera a fixed IP in the router (DHCP reservation).

## 3. Enroll employees' faces

```
faces/
  NV001/  photo1.jpg photo2.jpg ...
  NV002/  ...
```

- **5–10 photos per person:** looking straight, slightly left and right, with and without glasses, and one or two with a mask if possible.
- **Frames from the door camera itself are best**, because they have the same angle and lighting. Existing labelled data can be used.

```bash
python -m attendance enroll --faces faces
```

Re-running replaces the enrolled photos. Faces learned from admin confirmations are kept. Faces are stored per model, so after switching backend, run `enroll` again.

## 4. Compare backends and choose thresholds (important)

Use labelled photos to set `accept_score` and `min_margin` so that **nobody is ever identified as someone else**.

```bash
# Separate test photos: eval/<employee id>/*.jpg, plus eval/_unknown/*.jpg for non-employees
python -m attendance evaluate --gallery faces --test eval --backends insightface opencv

# Only one set of photos: each photo is tested against all the others
python -m attendance evaluate --gallery faces --backends insightface opencv
```

For each backend the command prints a table and a line like `Recommended: accept_score: ...  min_margin: ...`. Copy the values for your chosen backend into `config.yaml`, and add about 0.02 to `accept_score` as a safety margin.

Include hard photos in the test set: dark, blurred, side views, masks. They are what separates the backends.

When running live, a person is only identified once **at least 3 good frames agree** (`votes_required`). The per-visit accuracy is therefore higher than the per-photo figures.

## 5. Google Chat

1. In the admin team's space: space name → **Apps & integrations** → **Webhooks** → **Add webhook**.
2. Put the URL in `google_chat.webhook_url`.
3. Test it:
   ```bash
   python -m attendance test-chat
   ```

Messages that cannot be sent (no internet) are kept on the PC and retried for up to 24 hours.

## 6. Run

```bash
python -m attendance run
```

**Admin page:** open `http://<PC IP>:8000`, and set `web.password` in the config.

- **Today:** who was on time, late or not seen, with photos. Any past day can be opened.
- **Unidentified faces:** pick the right person and click *Confirm*. Their arrival is recorded at the photo time, and the face is learned so they are recognised next time. *Ignore* is for visitors.
- **Remote access:** the page is only reachable inside the office network by default. For remote access install [Tailscale](https://tailscale.com) on this PC and the admin's device. Do not open a port to the internet.

**Other commands:**
- `python -m attendance report --day 2026-10-14`: print a day's summary.

### Start automatically

**Windows**, with [NSSM](https://nssm.cc):
```bat
nssm install Attendance "C:\attendance\face-attendance\.venv\Scripts\python.exe" "-m attendance run"
nssm set Attendance AppDirectory "C:\attendance\face-attendance"
nssm start Attendance
```

**Linux**, `/etc/systemd/system/attendance.service`:
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

### Alert when the PC stops

Create a free check on [Healthchecks.io](https://healthchecks.io) with a 1-minute period and 5-minute grace, and put its ping URL in `healthcheck_url`. If the PC or the program stops, Healthchecks.io sends an email.

Files:
- logs: `data/attendance.log`
- face photos: `data/crops/`
- database: `data/attendance.db`

## Getting the best accuracy

**Camera placement** matters more than any setting.
- Mount it where everyone **must walk towards it**.
- Face height or slightly above, tilted down less than 15°.
- Faces should be **at least 80 px wide** in the image.

**Light** should come from in front of people. **Never** point the camera at a glass door or window (backlight). Dark frames are contrast-enhanced automatically (`enhance_low_light`), but a lamp works better.

**Masks and sunglasses:**
- Enroll photos with masks.
- Anything still uncertain goes to the admin queue rather than being guessed.
- The most effective fix is asking people to lower their mask for a second at the camera.

**Wi-Fi cameras on a desk** drop frames. A wired PoE camera is more reliable.

**Slow PC:**
- Lower `process_fps` to 3, or `det_size` to 480 (distant faces are then harder to detect).
- Or use `backend: opencv`, which is about 10× faster.

## How wrong identifications are avoided

1. **5-point alignment:** every face is aligned before its embedding is computed.
2. **Quality gate:** frames where the face is small, blurred, turned away or weakly detected **do not vote**.
3. **Several references per person:** a person's score is the mean of their two closest reference photos, so one odd photo does not dominate.
4. **Clear margin:** the best match must beat the runner-up by `min_margin`.
5. **Several frames must agree:** `votes_required` frames must pick the same person, with at least twice the votes of everyone else combined.
6. **No guessing:** everything else goes to the admin. Each confirmation adds a real camera image to that person's references.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests need no models, camera or OpenCV.
