# agy-supervisor ⚡

> **Production-Grade Multi-Account Quota Supervisor & Auto-Failover for Google Antigravity CLI (`agy`)**

`agy-supervisor` là công cụ quản lý, giám sát và tự động xoay vòng hạn mức (quota) cho nhiều tài khoản Google Pro / Advanced trên Google Antigravity CLI trong **duy nhất 1 cửa sổ terminal**, bảo toàn 100% ngữ cảnh hội thoại, tự động bypass quyền thực thi (`--dangerously-skip-permissions`), và loại bỏ triệt để nguy cơ bị Google gắn cờ lạm dụng (Sybil & Abuse Detection).

---

## 🌟 Tính Năng Cốt Lõi

- **Duy Nhất 1 Terminal Session**: Tự động chuyển tài khoản trong vòng ~2-3 giây ngay khi tài khoản hiện tại hết quota. Không cần mở nhiều tab terminal, không cần đăng nhập lại qua trình duyệt giữa chừng.
- **Bảo Toàn Ngữ Cảnh Cuộc Trò Chuyện 100%**: Cơ sở dữ liệu SQLite của Antigravity được lưu cục bộ (`~/.gemini/antigravity-cli/conversations/`). Khi xoay tài khoản, supervisor gọi `agy -c` nối tiếp chính xác luồng trao đổi mà không mất một bước lịch sử nào.
- **Mặc Định YOLO Mode (`--dangerously-skip-permissions`)**: Tự động bảo đảm cờ bypass toàn bộ yêu cầu cấp quyền chạy lệnh bash hoặc sửa file, tối ưu cho developer / security researcher.
- **Dynamic $N$-Account Scaling**: Không giới hạn 3 tài khoản. Bạn có thể mở rộng lên $N$ tài khoản ($1 \dots N$) thông qua lệnh `agy-supervisor add`.
- **Đồng Bộ Hai Tầng Chuẩn Xác (Dual-Layer Sync)**:
  - Tầng 1: Đồng bộ file hệ thống (`~/.gemini/oauth_creds.json`, `~/.gemini/google_accounts.json`).
  - Tầng 2: Cập nhật in-place **GNOME Keyring (D-Bus Secret Service)** thông qua `SetSecret` và collection fallback (`/default` & `/collection/login`), giải quyết triệt để lỗi cache token ngầm của Linux desktop.
- **100% Native TUI Responsiveness**: agy chạy trực tiếp trên foreground terminal TTY, không bị đóng băng hay trễ như các giải pháp PTY proxy/pipe. Hỗ trợ đầy đủ phím điều hướng, phím tắt, autocomplete, chuột và tự động thích ứng khi resize terminal.
- **Engine Bắt Log Streaming Siêu Bền Vững (Line-Buffered Watcher)**:
  - Xử lý triệt để cạm bẫy cắt đôi từ khóa (`RESOURCE_EXHAUSTED`) xuyên qua ranh giới chunk I/O (`line_buffer`).
  - Tự động phát hiện và reset con trỏ offset khi file log bị truncate hoặc xoay vòng in-place (`cur_size < tracked_pos`).
  - Bắt trọn vẹn cả HTTP 429 (`RESOURCE_EXHAUSTED`), gRPC Status 8 & 14, và HTTP 503 (`Model Overloaded / Service Unavailable`).
- **Thang Thoát Tín Hiệu Chống Deadlock (Signal Escalation Ladder)**:
  - Khi quota cạn kiệt, watcher thread gửi tín hiệu theo thang: `SIGINT` (chờ 2.5s) $\rightarrow$ `SIGTERM` (chờ 1.0s) $\rightarrow$ `SIGKILL`.
  - Tránh hoàn toàn deadlock tại `proc.wait()` khi child process phớt lờ `SIGINT`.
- **Ghi Đè Trạng Thái Nguyên Tử (Atomic State Writes)**:
  - `supervisor_state.json` luôn được ghi ra `.tmp`, ép xả đĩa bằng `os.fsync()`, và thay thế nguyên tử bằng `os.replace()`, ngăn ngừa hỏng file khi mất nguồn hoặc ngắt đột ngột.
- **Bảo Vệ Chống Bão Lỗi & Chống Ban Account (Anti-Abuse Engine)**:
  - Giữ nguyên 1 `installation_id` cố định của máy trạm (tránh tạo dấu vết giả mạo phần cứng).
  - Độ trễ chuyển giao an toàn $t_{\text{handover}} \ge 2.0\text{s}$ để bẻ gãy tương quan trên đồ thị phát hiện lạm dụng của Google.
  - Lọc sạch toàn bộ log false-positive (`doRefreshQuota: skipped (throttled)`).
  - **Master Circuit Breaker**: Tự động ngắt khi toàn bộ $N$ tài khoản đều cạn quota, đếm ngược chính xác đến **00:05 PST** (giờ reset quota của Google), hỗ trợ lệnh giải phóng `agy-supervisor reset`.

---

## 🏗️ Kiến Trúc Hoạt Động

```mermaid
flowchart TD
    subgraph Host["Terminal Của Bạn (/dev/pts/X)"]
        User["Bàn phím & Màn hình (Direct TTY 0/1/2)"]
    end

    subgraph Supervisor["agy-supervisor (Python Foreground Manager)"]
        Guard["TerminalGuard (RAII tcgetattr / tcsetattr)"]
        SigCtrl["Signal Isolator (SIGTERM/SIGHUP Forwarders)"]
        StateMachine["Quota State Machine & Circuit Breaker"]
        LogWatcher["Line-Buffered Log Watcher (Zero Chunk Slicing)"]
        Escalator["Signal Escalator (SIGINT -> SIGTERM -> SIGKILL)"]
        Keyring["D-Bus Secret Service (In-Place SetSecret)"]
    end

    subgraph NativeChild["Phiên agy Trực Tiếp (Native Foreground)"]
        BubbleTea["BubbleTea TUI (Native Raw Mode, Direct TTY 0/1/2)"]
        Engine["Jetski Client Engine"]
    end

    User <-->|Native 0ms Latency, Full Keybindings & Mouse| BubbleTea
    LogWatcher -->|Line-buffered Parsing: 429 / 503 / RESOURCE_EXHAUSTED| StateMachine
    StateMachine -->|1. Kích hoạt leo thang tín hiệu| Escalator
    Escalator -->|Gửi SIGINT -> SIGTERM -> SIGKILL| BubbleTea
    StateMachine -->|2. Check flock presence lock (TOCTOU-safe)| StateMachine
    StateMachine -->|3. Anti-Abuse Gap >= 2.0s| StateMachine
    StateMachine -->|4. Nạp Token + D-Bus In-Place + agy -c| Keyring
    Keyring -->|Khởi chạy phiên mới với lịch sử SQLite| BubbleTea
```

---

## 🚀 Cài Đặt Nhanh

### Yêu cầu hệ thống
- Hệ điều hành: Linux (Ubuntu, Debian, Kali, Arch, Fedora...).
- Python 3.8+ (khuyên dùng Python 3.10+).
- Module D-Bus cho Python:
  - Ubuntu/Debian/Kali: `sudo apt-get install python3-dbus`
  - Arch Linux: `sudo pacman -S python-dbus`
  - Fedora/RHEL: `sudo dnf install python3-dbus`
- Đã cài đặt Antigravity CLI (`agy`).

### Lệnh cài đặt 1 dòng
```bash
git clone https://github.com/your-username/agy-supervisor.git
cd agy-supervisor
./install.sh
```
File thực thi sẽ được cài vào `~/.local/bin/agy-supervisor` và tự động thêm alias `agys` vào `~/.bashrc` / `~/.zshrc`.

---

## 📖 Hướng Dẫn Sử Dụng

### 1. Thiết lập các Tài khoản Google (Chỉ làm 1 lần)

* **Xem danh sách slot hiện tại**:
  ```bash
  agy-supervisor status
  ```

* **Lưu tài khoản hiện tại vào Slot 1**:
  Nếu bạn đang đăng nhập sẵn một tài khoản trên `agy`:
  ```bash
  agy-supervisor save 1
  ```

* **Thêm tài khoản mới (Slot 2, Slot 3, Slot $N+1$)**:
  ```bash
  agy-supervisor add
  # hoặc chỉ định slot cụ thể:
  agy-supervisor login 2
  agy-supervisor login 3
  ```
  1. Trình duyệt sẽ mở ra trang xác thực OAuth của Google.
  2. Đăng nhập tài khoản Google Pro mới.
  3. Khi giao diện `agy` xuất hiện trên terminal, gõ `/exit` (hoặc bấm `Ctrl+D`).
  4. Tool sẽ tự động bắt token và lưu vĩnh viễn vào slot tương ứng.

* **Hoặc dùng Wizard tương tác tự động**:
  ```bash
  agy-supervisor setup
  ```

---

### 2. Khởi Động Làm Việc Hàng Ngày

Chỉ cần gõ:
```bash
agy-supervisor
# hoặc dùng alias viết tắt:
agys
```

Bạn có thể truyền bất kỳ tham số nào của `agy`, ví dụ:
```bash
agy-supervisor --model gemini-2.5-pro --effort high
```

* **Trải nghiệm**:
  - Bạn tương tác trực tiếp với giao diện TUI như bình thường. Mọi yêu cầu cấp quyền đều được auto-approve (`--dangerously-skip-permissions`).
  - Khi Tài khoản 1 hết quota, màn hình thông báo:
    ```text
    [!] Hết quota ở Slot 1 (user1@gmail.com). Đang kiểm tra xoay tua...
    [+] Tự động chuyển sang Slot 2 (user2@gmail.com). Nối tiếp phiên chat...
    ```
  - Phiên làm việc tự động nối tiếp (`agy -c`) trong tích tắc, giữ nguyên toàn bộ lịch sử hội thoại và ngữ cảnh.

---

### 3. Bảng Lệnh CLI Đầy Đủ

| Lệnh | Chức năng |
| :--- | :--- |
| `agy-supervisor [args...]` | Khởi chạy agy với auto-failover & bypass permissions |
| `agy-supervisor status` | Xem bảng trạng thái tất cả slot, email và thời gian reset quota |
| `agy-supervisor reset [all\|<N>]` | Reset trạng thái cooldown quota của các slot về `ACTIVE` |
| `agy-supervisor add` | Tự động tạo Slot $N+1$ và mở trình duyệt để nạp thêm tài khoản |
| `agy-supervisor login <N>` | Đăng nhập tài khoản cho Slot $N$ |
| `agy-supervisor switch <N>` | Chuyển ngay lập tức sang Slot $N$ trong 0.1s |
| `agy-supervisor save <N>` | Lưu token đang active trong `~/.gemini` vào Slot $N$ |
| `agy-supervisor setup` | Trình wizard tương tác thiết lập nhanh toàn bộ tài khoản |

---

### 4. Công Cụ Tự Động Hóa & Kiểm Thử (Makefile)

Repository được trang bị bộ công cụ kiểm thử và chẩn đoán toàn diện:

```bash
# 1. Chạy bộ kiểm thử hồi quy & edge-case (17 test cases bao phủ toàn bộ các tầng)
make test

# 2. Kiểm tra sức khỏe hệ thống, D-Bus session, và hạn dùng token
make health

# 3. Chạy kiểm chứng thực tế quá trình xoay tua & nối tiếp phiên chat trên hệ thống thật
make verify
```

---

## 🔒 Tại Sao Giải Pháp Này Không Bị Google Ban?

| Nguy cơ bị Google gắn cờ | Cơ chế bảo vệ của `agy-supervisor` |
| :--- | :--- |
| **Bị coi là bot farm do random `installation_id`** | Giữ nguyên **1 `installation_id` duy nhất** của máy trạm. Google hiểu đây là 1 máy tính hợp lệ của developer chứa nhiều profile. |
| **Bị Sybil Detection phát hiện lách quota ($t < 50\text{ms}$)** | Enforce độ trễ chuyển giao an toàn $t \ge 2.0\text{s}$, mô phỏng hành vi đổi profile tự nhiên của con người. |
| **Bão lỗi 4xx (4xx Thrashing Storm)** | **Master Circuit Breaker** tự động ngắt khi toàn bộ $N$ tài khoản cạn quota, đếm ngược đến 00:05 PST, tuyệt đối không spam request lỗi. |
| **Xung đột token & Desync** | Tự động sync ngược refresh token mới nhất về slot khi đổi tài khoản, bảo vệ phiên làm việc lâu dài. |
| **Bỏ sót lỗi sập node (503 / UNAVAILABLE)** | Nhận diện lỗi máy chủ quá tải để xoay tua sang tài khoản trên phân vùng khác thay vì treo vô hạn. |

Chi tiết xem tại tài liệu chuyên sâu: [docs/ANTI_ABUSE_GUIDE.md](docs/ANTI_ABUSE_GUIDE.md).

---

## 📚 Tài Liệu Kỹ Thuật Chuyên Sâu

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): Phân tích chi tiết kiến trúc nhân Linux, Direct Foreground TUI, Signal Escalation Ladder, Line-Buffered Log Watcher, D-Bus Secret Service và SQLite WAL.
- [docs/ANTI_ABUSE_GUIDE.md](docs/ANTI_ABUSE_GUIDE.md): Nghiên cứu chuyên sâu về hệ thống chống gian lận quota của Google (RPM/RPD, SybilRank, JA4 Fingerprint).
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md): Các lỗi thường gặp (Circuit Breaker, Headless/SSH D-Bus, Presence Locks, Buffer Corruption) và cách khắc phục triệt để.

---

## 📄 Bản Quyền
Phát hành theo giấy phép [MIT License](LICENSE).
