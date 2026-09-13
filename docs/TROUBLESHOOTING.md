# Hướng Dẫn Xử Lý Sự Cố (Troubleshooting Guide)

Tài liệu giải quyết các tình huống lỗi thường gặp khi sử dụng `agy-supervisor`, kèm theo các bước chẩn đoán và khắc phục chuẩn xác.

---

### 1. Kích Hoạt Circuit Breaker: "Toàn bộ tài khoản khả dụng đều đã cạn quota!"

* **Hiện tượng**:
  Khi khởi chạy hoặc đang làm việc, màn hình thông báo:
  ```text
  ======================================================================
  [!] CIRCUIT BREAKER: Toàn bộ 3 tài khoản khả dụng đều đã cạn quota!
  [*] Cooldown ngắn nhất còn lại: khoảng 4 giờ 20 phút nữa.
      - Slot 1 (user1@example.com): chờ 4h 20m
      - Slot 2 (user2@example.com): chờ 4h 20m
      - Slot 3 (user3@example.com): chờ 4h 20m
  💡 Muốn nạp thêm tài khoản mới: agy-supervisor add
  💡 Nếu tài khoản đã có lại quota và muốn reset cooldown: agy-supervisor reset
  ======================================================================
  ```

* **Nguyên nhân**:
  Hệ thống bảo vệ chống bão lỗi phát hiện toàn bộ các tài khoản trong danh sách slot đều đã chạm ngưỡng quota ngày (RPD) hoặc quota giờ.

* **Cách xử lý**:
  1. **Phương án 1 (Nạp thêm tài khoản mới)**:
     ```bash
     agy-supervisor add
     ```
     Lệnh này sẽ tự động tạo Slot $N+1$ (ví dụ Slot 4) và mở trình duyệt để nạp thêm tài khoản Google Pro khác.
  2. **Phương án 2 (Reset trạng thái cooldown ngay lập tức)**:
     Nếu bạn biết hạn ngạch của tài khoản đã được phục hồi hoặc muốn bỏ qua thời gian chờ:
     ```bash
     # Reset toàn bộ các slot về ACTIVE:
     agy-supervisor reset
     # Hoặc reset riêng 1 slot cụ thể:
     agy-supervisor reset 1
     ```
  3. **Phương án 3 (Kiểm tra trạng thái thời gian thực)**:
     ```bash
     agy-supervisor status
     ```

---

### 2. `agy-supervisor login <N>` Không Mở Trình Duyệt Mà Tự Đăng Nhập Lại Tài Khoản Cũ

* **Nguyên nhân**:
  Token cũ vẫn còn được lưu trong **GNOME Keyring (D-Bus Secret Service)**. Khi thiếu module `dbus-python`, supervisor chỉ xóa được file JSON cục bộ nhưng không xóa được cache ngầm trong Keyring của Linux.

* **Cách xử lý**:
  1. Kiểm tra module `dbus`:
     ```bash
     python3 -c "import dbus; print('OK')"
     ```
  2. Nếu báo lỗi `ModuleNotFoundError`, hãy cài đặt:
     - **Debian / Ubuntu / Kali**: `sudo apt-get install python3-dbus`
     - **Arch Linux**: `sudo pacman -S python-dbus`
     - **Fedora / RHEL**: `sudo dnf install python3-dbus`
  3. Chạy lại lệnh đăng nhập cho slot cần cập nhật:
     ```bash
     agy-supervisor login <N>
     ```

---

### 3. Môi Trường Headless Hoặc SSH Session Không Có D-Bus SessionBus

* **Hiện tượng**:
  Khi chạy qua kết nối SSH hoặc container tối giản, chương trình cảnh báo hoặc không kết nối được Secret Service.

* **Cách xử lý**:
  Khởi chạy qua `dbus-run-session`:
  ```bash
  dbus-run-session agy-supervisor
  ```
  Hoặc thiết lập biến môi trường Session Bus trước khi chạy:
  ```bash
  export $(dbus-launch)
  agy-supervisor
  ```

---

### 4. Trình Duyệt Không Tự Mở Khi Bắt Đầu Đăng Nhập OAuth

* **Nguyên nhân**:
  Hệ thống chạy trên server không có giao diện đồ họa (headless), qua SSH không có X11 forwarding, hoặc chưa cấu hình biến `$BROWSER`.

* **Cách xử lý**:
  Khi `agy` chuẩn bị xác thực OAuth, nó sẽ in ra một liên kết dạng:
  ```text
  Please visit https://accounts.google.com/o/oauth2/v2/auth?...
  ```
  Chỉ cần sao chép liên kết này, dán vào trình duyệt web trên máy tính của bạn, chọn tài khoản Google cần đăng nhập và xác nhận. CLI sẽ tự động bắt callback hoàn tất.

---

### 5. Giao Diện Terminal Bị Lỗi Hiển Thị / Đóng Băng Sau Khi Crash

* **Hiện tượng**:
  Sau một sự cố ngoài ý muốn (ví dụ terminal bị kill đột ngột bằng `SIGKILL`), con trỏ chuột biến mất hoặc chữ gõ vào terminal không hiện lên màn hình.

* **Cách xử lý**:
  Gõ lệnh sau vào cửa sổ bash để phục hồi hoàn toàn trạng thái terminal:
  ```bash
  reset
  ```
  Hoặc chỉ cần chạy lại `agy-supervisor` — thành phần `TerminalGuard` tích hợp sẵn sẽ tự động phát các chuỗi escape ANSI phục hồi `\x1b[?1049l\x1b[?25h`.

---

### 6. Lỗi `presence lock` Hoặc `Session already in use`

* **Hiện tượng**:
  `agy` báo lỗi phiên trò chuyện đang bị khóa bởi tiến trình khác do một phiên trước đó bị tắt không sạch sẽ.

* **Cách xử lý**:
  Dọn dẹp các tiến trình và file lock cũ:
  ```bash
  killall -9 agy 2>/dev/null || true
  rm -f ~/.gemini/antigravity-cli/presence/*.lock
  ```
  Sau đó khởi chạy lại `agy-supervisor`.

---

### 7. Công Cụ Tự Động Chẩn Đoán Toàn Diện Hệ Thống (Health Check)

Để kiểm tra toàn bộ cấu hình, quyền hạn file, kết nối D-Bus, tính hợp lệ của token và trạng thái từng slot:
```bash
make health
# Hoặc chạy trực tiếp:
python3 scripts/health_check.py
```

Để chạy bộ 17 ca kiểm thử tự động xác thực toàn bộ logic hoạt động:
```bash
make test
# Hoặc chạy trực tiếp:
python3 tests/test_supervisor.py
```
