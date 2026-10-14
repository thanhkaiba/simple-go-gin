# Chấm công bằng camera

Máy tính trong văn phòng đọc hình từ camera, nhận diện khuôn mặt **ngay tại máy** (InsightFace), tính ai đúng giờ, ai đi muộn, rồi gửi thông báo lên **Google Chat**. Khuôn mặt nào không nhận ra được thì gửi cho admin xác nhận trên một trang web nhỏ.

```
Camera (RTSP) ──► Máy văn phòng: phát hiện mặt → lọc ảnh xấu → theo dõi qua nhiều khung → so với ảnh mẫu
                    │
                    ├─ chắc chắn ─────────► ghi giờ đến
                    └─ không chắc ────────► hàng chờ "chưa xác định" → admin chọn đúng người
                    │
                    └─► Google Chat: cảnh báo 10:00, người đến muộn, khuôn mặt lạ, tổng kết 11:00
```

## Quy tắc chấm công

Giờ giấc lấy trong `config.yaml`, mặc định như sau:

| Tình huống | Kết quả |
|---|---|
| Thấy lần đầu **trước 10:00** | Đúng giờ. Sau 10:00 có thấy lại cũng không tính muộn |
| **Đến 10:00 vẫn chưa thấy** | Đi muộn |
| Thấy lần đầu **sau 10:00** | Đi muộn. Google Chat báo ngay: "🟠 *A* đến lúc 10:23, muộn 23 phút" |
| Cả buổi sáng không thấy | Ghi "Không thấy trên camera" trong bản tổng kết |
| Khuôn mặt không nhận ra | Gửi admin xác nhận. Admin chọn đúng người thì giờ đến lấy theo thời điểm trong ảnh |

**Các tin nhắn Google Chat** (mỗi ngày gom vào một luồng):

- **10:00:** danh sách những người chưa thấy.
- **Ngay khi có người đến sau 10:00:** báo người đó đến muộn.
- **Khi có khuôn mặt lạ:** gom lại tối đa 5 phút một lần, kèm link để admin xác nhận.
- **Khi admin xác nhận** một người đã có trong danh sách 10:00 nhưng thực ra đến trước 10:00: gửi tin đính chính "không tính đi muộn".
- **11:00:** tổng kết ngày.
- **Khi camera hoặc máy tính ngừng hoạt động** trong khung giờ: các tin nhắn trên ghi rõ khoảng thời gian bị gián đoạn, để không ai bị tính oan.

**⚠️ Camera chỉ chạy trong khung giờ** (mặc định 09:00–11:00). Ai đến **trước 9:00** mà không đi qua camera lần nào nữa sau 9:00 sẽ bị tính đi muộn. Nếu nhân viên hay đến sớm, hãy đặt `window_start` sớm hơn, ví dụ `"08:00"`.

## 1. Chuẩn bị máy

Dùng một máy riêng chạy cả ngày, Windows 10/11 hoặc Ubuntu. Không dùng laptop cá nhân của ai.

- **Python 3.10–3.12.** InsightFace chưa hỗ trợ tốt các bản mới hơn.
- **Windows:** cài thêm [Microsoft C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) (chọn "Desktop development with C++"), vì InsightFace cần biên dịch khi cài.
- **Tắt chế độ ngủ:** Power → Sleep → Never.
- **BIOS:** bật *Restore on AC Power Loss* để máy tự bật lại khi có điện.
- **Windows Update:** đặt giờ cập nhật ngoài khung 8:00–12:00.
- **Nên cắm UPS** cho máy, router và camera.

```bash
cd face-attendance
python -m venv .venv
.venv\Scripts\activate          # Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Lần chạy đầu tiên, InsightFace tự tải bộ model `buffalo_l` (khoảng 300 MB) về `~/.insightface`.

## 2. Cấu hình

```bash
copy config.example.yaml config.yaml        # Linux: cp
copy employees.example.csv employees.csv
```

**`employees.csv`:** danh sách nhân viên, gồm 2 cột `id,name`. Sửa được bằng Excel, nhớ lưu dạng CSV UTF-8.

**Camera Ezviz:**

1. Trong app EZVIZ, bật **RTSP** hoặc **LAN Live View** nếu dòng camera có mục này.
2. Đặt `camera.url` thành:
   ```
   rtsp://admin:<MÃ_XÁC_MINH>@<IP_CAMERA>:554/H.264
   ```
   Mã xác minh gồm 6 chữ in hoa trên tem dưới đáy camera.
3. Thử trước bằng VLC: Media → Open Network Stream.
4. Nên vào router **cố định IP cho camera** (DHCP reservation).

## 3. Đăng ký khuôn mặt nhân viên

Đặt ảnh theo cấu trúc thư mục:

```
faces/
  NV001/  anh1.jpg anh2.jpg ...
  NV002/  ...
```

- Mỗi người **5–10 ảnh**: nhìn thẳng, hơi nghiêng trái/phải, có và không đeo kính, nếu được thì thêm 1–2 ảnh đeo khẩu trang.
- **Ảnh chụp từ chính camera ở cửa** là tốt nhất, vì cùng góc và cùng ánh sáng. Có thể lấy ngay từ tập ảnh đã gán nhãn của đội trước.

```bash
python -m attendance enroll --faces faces
```

Chạy lại lệnh này bất cứ lúc nào thì ảnh đăng ký cũ được thay bằng ảnh mới. Khuôn mặt hệ thống học được qua các lần admin xác nhận vẫn được giữ lại.

## 4. Chọn ngưỡng nhận diện (quan trọng cho độ chính xác)

Dùng ảnh đã gán nhãn để chọn `accept_score` và `min_margin`, sao cho **không bao giờ nhận nhầm người**:

```bash
# Có bộ ảnh thử riêng: eval/<mã NV>/*.jpg, và eval/_unknown/*.jpg cho người không phải nhân viên
python -m attendance evaluate --gallery faces --test eval

# Chỉ có một bộ ảnh: mỗi ảnh được thử với các ảnh còn lại
python -m attendance evaluate --gallery faces
```

Lệnh in ra bảng kết quả và một dòng `Đề xuất: accept_score: ...  min_margin: ...`. Chép hai giá trị này vào `config.yaml`, nên cộng thêm khoảng 0.02 vào `accept_score` để có biên an toàn.

Khi chạy thật, một người chỉ được xác nhận khi **ít nhất 3 khung hình rõ nét cùng đồng ý** (`votes_required`). Vì vậy tỉ lệ đúng mỗi lượt đi qua sẽ cao hơn tỉ lệ trên từng ảnh mà lệnh này in ra.

## 5. Google Chat

1. Mở Space của team admin → tên Space → **Apps & integrations** → **Webhooks** → **Add webhook**.
2. Chép URL vào `google_chat.webhook_url`.
3. Gửi thử:
   ```bash
   python -m attendance test-chat
   ```

Khi mất mạng, tin nhắn được giữ lại trên máy (tối đa 24 giờ) và tự gửi khi có mạng.

## 6. Chạy

```bash
python -m attendance run
```

**Trang admin:** mở `http://<IP máy>:8000`. Nhớ đặt `web.password` trong config.

- **Hôm nay:** ai đúng giờ, ai đi muộn, ai không thấy, kèm ảnh. Xem được cả ngày cũ.
- **Khuôn mặt chưa xác định:** chọn đúng người rồi bấm "Xác nhận". Hệ thống tính giờ đến theo ảnh, và **học thêm khuôn mặt đó** để lần sau tự nhận ra. Nếu là khách thì bấm "Bỏ qua".
- **Xem từ xa:** mặc định trang chỉ mở được trong mạng công ty. Muốn xem từ ngoài thì cài [Tailscale](https://tailscale.com) lên máy này và máy của admin. Không mở port ra internet.

**Lệnh khác:**
- `python -m attendance report --day 2026-10-14`: in bản tổng kết của một ngày ra màn hình.

### Chạy tự động khi bật máy

**Windows:** dùng [NSSM](https://nssm.cc).
```bat
nssm install ChamCong "C:\cham-cong\face-attendance\.venv\Scripts\python.exe" "-m attendance run"
nssm set ChamCong AppDirectory "C:\cham-cong\face-attendance"
nssm start ChamCong
```

**Linux:** `/etc/systemd/system/cham-cong.service`
```ini
[Unit]
Description=Cham cong camera
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
sudo systemctl enable --now cham-cong
```

### Cảnh báo khi máy chết

Tạo một check miễn phí trên [Healthchecks.io](https://healthchecks.io) (chu kỳ 1 phút, cho trễ 5 phút), rồi chép URL ping vào `healthcheck_url`. Nếu máy tắt hoặc chương trình dừng, Healthchecks.io sẽ gửi email báo.

Log nằm ở `data/attendance.log`, ảnh khuôn mặt ở `data/crops/`, dữ liệu ở `data/attendance.db`.

## Mẹo tăng độ chính xác

**Vị trí camera** quan trọng hơn mọi cài đặt phần mềm:
- Đặt ở chỗ ai cũng **phải đi qua và nhìn về phía camera**.
- Ngang tầm mặt hoặc cao hơn một chút, chúc xuống dưới 15°.
- Mặt người trong khung hình nên rộng **từ 80 px trở lên**.

**Ánh sáng:** chiếu từ phía trước mặt người. **Không** để camera nhìn ra cửa kính hay cửa sổ (ngược sáng). Khi hình tối, hệ thống tự tăng tương phản (`enhance_low_light`), nhưng thêm đèn vẫn hiệu quả hơn.

**Khẩu trang, kính râm:**
- Thêm ảnh mẫu có khẩu trang.
- Ca nào vẫn không chắc chắn sẽ vào hàng chờ cho admin, không bị đoán bừa.
- Thực tế nhất là quy định "nhìn camera thì kéo khẩu trang xuống 1 giây".

**Camera Wi-Fi để trên bàn hay rớt hình.** Nếu có điều kiện, chuyển sang camera có dây (PoE).

**Máy yếu, xử lý không kịp:** giảm `process_fps` xuống 3, hoặc `det_size` xuống 480. Đổi lại, mặt ở xa sẽ khó phát hiện hơn.

## Cách hệ thống tránh nhận nhầm

1. **SCRFD + căn chỉnh 5 điểm:** khuôn mặt được căn thẳng trước khi tính đặc trưng bằng ArcFace (`buffalo_l`).
2. **Lọc chất lượng:** khung hình có mặt nhỏ, mờ, quay nghiêng nhiều hoặc độ tin cậy thấp **không được bỏ phiếu**.
3. **So với nhiều ảnh mẫu:** mỗi người có nhiều ảnh mẫu. Điểm của một người là trung bình 2 ảnh mẫu giống nhất, để một ảnh mẫu xấu không làm lệch kết quả.
4. **Cách biệt rõ:** người giống nhất phải hơn người thứ hai ít nhất `min_margin`.
5. **Nhiều khung hình đồng ý:** cần `votes_required` khung hình cùng chọn một người, và số phiếu phải gấp đôi tổng phiếu của những người khác.
6. **Không chắc thì không đoán:** những ca còn lại vào hàng chờ cho admin. Mỗi lần admin xác nhận, hệ thống có thêm ảnh mẫu thật từ camera.

## Phát triển

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Các test không cần InsightFace hay camera.
