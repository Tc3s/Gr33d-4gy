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

### 7. Lỗi `CLIENT_ID_MISMATCH`: Token bị lệch Client ID (Nguồn gốc từ `@google/gemini-cli`)

* **Hiện tượng**:
  Khi chạy `agy-supervisor status`, `agy-supervisor probe` hoặc khi chuyển slot, hệ thống cảnh báo:
  ```text
  ===========================================================================
  [!] CẢNH BÁO: PHÁT HIỆN TOKEN KHÔNG TƯƠNG THÍCH CHO SLOT 1!
  [*] Project Number     : 681255809395
  [*] Diagnostic         : FOREIGN TOKEN CORRUPTION: Token was minted for Google client 'gemini-cli (@google/gemini-cli)'...
  💡 HƯỚNG DẪN KHẮC PHỤC (REMEDIATION):
     1. Chạy lệnh: agy-supervisor login 1
     2. Đăng nhập đúng tài khoản trên trình duyệt Antigravity.
     ⚠️ KHÔNG copy file oauth_creds.json từ gemini-cli hoặc các công cụ khác sang!
  ===========================================================================
  ```

* **Nguyên nhân**:
  Token của slot đó được cấp cho client ID của Gemini CLI (`681255809395`), không khớp với Client ID chính thức của Antigravity (`1071006060591`). Khi refresh sẽ dính lỗi HTTP 401 `unauthorized_client`.

* **Cách xử lý**:
  Đăng nhập lại chính chủ cho slot đó qua trình duyệt:
  ```bash
  agy-supervisor login <N>
  # Ví dụ:
  agy-supervisor login 1
  ```
  Sau khi hoàn tất xác thực trên trình duyệt, Supervisor sẽ tự động lưu token với Client ID hợp lệ.

---

### 8. Tài Khoản Bị Trạng Thái `NOTICE_BLOCKED` (Google Cảnh Báo Điều Khoản ToS)

* **Hiện tượng**:
  Slot tài khoản hiển thị trạng thái `NOTICE_BLOCKED` và thời gian cooldown lên tới 10 năm (cách ly an toàn).

* **Nguyên nhân**:
  Backend Google phát hiện hành vi bất thường hoặc tài khoản bị tạm khóa / yêu cầu xác minh điều khoản (`Terms of service update required` / `Account temporarily disabled`).

* **Cách xử lý**:
  1. Kiểm tra tài khoản trên trình duyệt xem Google có yêu cầu xác minh số điện thoại hoặc đồng ý điều khoản dịch vụ mới hay không.
  2. Sau khi tài khoản trên web đã hoạt động bình thường, đăng nhập lại slot:
     ```bash
     agy-supervisor login <N>
     ```
  3. Reset trạng thái slot về `ACTIVE`:
     ```bash
     agy-supervisor reset <N>
     ```

---

### 9. Rotational Circuit Breaker Kích Hoạt: "Phát hiện 4 lần chuyển slot liên tiếp trong <60s"

* **Hiện tượng**:
  Màn hình thông báo `ROTATIONAL CIRCUIT BREAKER TRIPPED!` và tạm dừng xoay tua 30 giây.

* **Nguyên nhân**:
  Tất cả các tài khoản trong pool đều đang gặp sự cố (hết hạn ngạch, lỗi xác thực hoặc token hỏng), khiến supervisor phải nhảy slot liên tục.

* **Cách xử lý**:
  1. Kiểm tra tình trạng sức khỏe tổng thể của các tài khoản:
     ```bash
     agy-supervisor probe
     ```
  2. Khắc phục các tài khoản bị lỗi auth hoặc thêm tài khoản mới có quota khả dụng:
     ```bash
     agy-supervisor add
     ```

---

### 10. Công Cụ Chẩn Đoán & Kiểm Thử (Diagnostics & Testing)

Kiểm tra nhanh tính hợp lệ của token, thời hạn hết hạn, và kết nối backend:
```bash
agy-supervisor probe
```

Kiểm tra toàn bộ cấu hình, quyền hạn file, kết nối D-Bus và trạng thái từng slot:
```bash
make health
# Hoặc chạy trực tiếp:
python3 scripts/health_check.py
```

Chạy bộ kiểm thử tự động toàn diện:
```bash
# 1. Chạy 178 bài kiểm thử Master Unit & Hồi quy:
make test

# 2. Chạy 115 bài kiểm thử End-to-End Hộp đen (Tiers 1-4):
make e2e

# 3. Chạy toàn bộ 293 bài tests:
make test-all
```
