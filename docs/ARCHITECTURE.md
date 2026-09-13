# Architecture & Systems Engineering Deep-Dive

Tài liệu này phân tích chi tiết cấu trúc kỹ thuật tầng thấp (Low-Level Systems Engineering) của `agy-supervisor`.

---

## 1. Vấn Đề Nhân Linux & Terminal Execution Model

### Hạn chế chết người của việc bọc TUI qua PTY Proxy hoặc Script thông thường
Antigravity CLI (`agy`) sử dụng framework giao diện TUI **BubbleTea** (viết bằng Go). Khi BubbleTea khởi động:
1. Nó đưa terminal vào chế độ **Raw Mode**: xóa các cờ `ICANON` (không chờ Enter mới đọc), `ECHO` (không tự in ký tự gõ), và `OPOST` (xử lý output thủ công).
2. Nó phát chuỗi ANSI **`\x1b[?1049h` (`smcup`)** để chuyển toàn bộ màn hình sang Alternate Screen Buffer, và **`\x1b[?25l`** để ẩn con trỏ chuột.

Khi xây dựng trình giám sát (Supervisor), có hai cạm bẫy kinh điển trong lập trình hệ thống Linux:
* **Cạm bẫy PTY Proxy thiếu raw mode**: Nếu mở một PTY master/slave nhưng không đặt `sys.stdin` của tiến trình cha vào raw mode (`tty.setraw()`), driver TTY của hệ điều hành vẫn ở chế độ canonical (cooked mode). Toàn bộ phím gõ, phím mũi tên điều hướng, phím tắt, autocomplete sẽ bị đóng băng hoặc in ra ký tự rác (`^[[A`) cho đến khi người dùng bấm Enter. Ngoài ra, việc dùng vòng lặp `select.select()` ở tiến trình cha tạo ra độ trễ (latency) và dễ bị lỗi desync kích thước màn hình (SIGWINCH).
* **Cạm bẫy Background Process Group (`SIGTTIN` / `SIGTTOU`)**: Nếu chạy `agy &` trong background bằng script thông thường, nhân Linux sẽ gửi tín hiệu **`SIGTTIN`** hoặc **`SIGTTOU`**, lập tức dừng tiến trình.

### Kiến Trúc Tối Ưu: Direct Foreground Execution + Daemon Log Watcher
`agy-supervisor` triển khai mô hình thực thi trực tiếp trên Foreground TTY kết hợp luồng giám sát ngầm:

```mermaid
sequenceDiagram
    participant User as Terminal Host (/dev/pts/X)
    participant Supervisor as agy-supervisor (Parent Process)
    participant agy as agy CLI (BubbleTea Child)
    participant Watcher as Log Watcher (Daemon Thread)
    participant Log as cli.log (~/.gemini/.../cli.log)

    Supervisor->>Supervisor: wait_for_presence_lock_release()
    Supervisor->>Supervisor: signal.signal(SIGINT, SIG_IGN) (Bảo vệ cha khỏi Ctrl+C)
    Supervisor->>agy: subprocess.Popen(["agy", "--dangerously-skip-permissions", ...])
    Note over agy, User: agy thừa hưởng trực tiếp stdin, stdout, stderr (fd 0, 1, 2)<br/>BubbleTea điều khiển 100% Native TTY: 0ms lag, chuẩn ANSI, vi-mode, chuột & autocomplete mượt mà!
    
    Supervisor->>Watcher: Khởi chạy luồng theo dõi cli.log (Daemon)
    
    rect rgb(240, 240, 240)
        Note over User, agy: Người dùng tương tác trực tiếp với agy CLI bình thường
        User->>agy: Gõ lệnh, mũi tên, phím tắt
        agy->>User: Render TUI trực tiếp lên màn hình
        agy->>Log: Ghi log hoạt động / API responses
    end

    alt Phát hiện Quota Exhausted (HTTP 429 / RESOURCE_EXHAUSTED)
        Log->>Watcher: Chunk: "(RESOURCE_EXHAUSTED (code 429): Individual quota reached... Resets in 4h46m23s.)"
        Watcher->>Watcher: is_genuine_quota_log() -> True, parse_reset_seconds() -> 17183s
        Watcher->>agy: proc.send_signal(SIGINT)
        Note over agy: agy nhận SIGINT: commit SQLite WAL, nhả lock, dừng an toàn
        agy-->>Supervisor: proc.wait() hoàn tất
        Supervisor->>Supervisor: mark_exhausted(curr_slot, reset_secs)
        Supervisor->>Supervisor: rotate_to_next() (Apply token & D-Bus Keyring Slot mới)
        Supervisor->>User: Thông báo: "[+] Tự động chuyển sang Slot 2. Nối tiếp phiên chat..."
        Supervisor->>agy: Khởi chạy lại agy với cờ: ["-c", "--dangerously-skip-permissions"]
    end
```

#### Bảo vệ trạng thái Terminal bằng RAII (`TerminalGuard`)
Class `TerminalGuard` lưu cấu hình `termios` gốc trước khi khởi chạy và cam kết phục hồi trong khối `finally:`:
```python
class TerminalGuard:
    def __enter__(self):
        if self.is_tty:
            self.orig_termios = termios.tcgetattr(sys.stdin.fileno())
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.restore()

    def restore(self):
        if self.is_tty and self.orig_termios:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, self.orig_termios)
        # Phát chuỗi ANSI khôi phục toàn diện:
        sys.stdout.write("\x1b[?1049l\x1b[?25h\x1b[?1000l\x1b[?1002l\x1b[?1006l\x1b[?2004l\r\n")
        sys.stdout.flush()
```

---

## 2. Tích Hợp D-Bus Secret Service (GNOME Keyring)

### Cơ chế lưu token ngầm của `agy`
Dịch ngược nhị phân ELF của `agy` cho thấy sự hiện diện của thư viện Go:
```text
google3/third_party/golang/github_com/zalando/go_keyring
```
Trên các bản phân phối Linux Desktop có GNOME hoặc Secret Service:
1. `agy` lưu trữ token đăng nhập trong GNOME Keyring với định danh:
   - **Service**: `gemini`
   - **Username**: `antigravity`
   - **Schema**: `org.freedesktop.Secret.Generic`
2. **Thứ tự ưu tiên**: Khi khởi động, `agy` kiểm tra GNOME Keyring **trước**. Nếu tìm thấy token trong Keyring, nó sẽ bỏ qua file `~/.gemini/oauth_creds.json`.

### Cách xử lý của `agy-supervisor`
`agy-supervisor` giao tiếp trực tiếp với D-Bus Session Bus (`org.freedesktop.secrets`):
* **Khi đăng nhập tài khoản mới (`agy-supervisor login <N>`)**:
  Supervisor gọi `item.Delete()` trên D-Bus để gỡ bỏ hoàn toàn credential cũ khỏi GNOME Keyring, đồng thời xóa file tạm `~/.gemini/oauth_creds.json`. `agy` khởi động trong trạng thái hoàn toàn "sạch", buộc phải mở trình duyệt cho bạn đăng nhập tài khoản mới.
* **Khi xoay vòng tài khoản (`apply_slot`)**:
  Supervisor đọc `keyring_secret.json` tương ứng của slot và gọi `col.CreateItem()` để ghi đè token vào GNOME Keyring, đồng thời ghi đè file JSON trong `~/.gemini/`. Điều này đảm bảo tính nhất quán 100% dù `agy` đọc từ nguồn nào.

---

## 3. Quy Trình Ngắt An Toàn 2 Giai Đoạn (Staged Shutdown)

Khi phát hiện cạn kiệt quota, việc ngắt đột ngột bằng `SIGKILL` hoặc `SIGTERM` có thể làm hỏng trạng thái cơ sở dữ liệu. `agy-supervisor` áp dụng quy trình ngắt có kiểm soát:

```mermaid
flowchart TD
    Detect["Phát hiện Quota Hết"] --> S1["Giai đoạn 1: Gửi SIGINT (tương đương Ctrl+C)"]
    S1 --> W1["Chờ tối đa 2.0 giây (polling 100ms)"]
    W1 --> Check1{"Tiến trình đã dừng?"}
    Check1 -- Yes --> Committed["BubbleTea tự flush database, commit SQLite WAL, nhả presence lock an toàn!"]
    Check1 -- No --> S2["Giai đoạn 2: Gửi SIGTERM"]
    S2 --> W2["Chờ tối đa 0.5 giây"]
    W2 --> Check2{"Tiến trình đã dừng?"}
    Check2 -- Yes --> Done["Dừng thành công"]
    Check2 -- No --> S3["Giai đoạn 3: Gửi SIGKILL (bắt buộc)"]
```

---

## 4. Quản Lý File Lock & Tranh Chấp SQLite WAL

### Presence Lock
Mỗi phiên làm việc của `agy` giữ một file lock độc quyền dạng advisory lock qua `flock`:
```text
~/.gemini/antigravity-cli/presence/<conversation_id>.lock
```
Nếu tiến trình `agy -c` mới được spawn trước khi kernel dọn xong file descriptor của tiến trình cũ, tiến trình mới sẽ bị `EWOULDBLOCK`.
`agy-supervisor` tích hợp hàm kiểm tra trước khi spawn:
```python
def wait_for_presence_lock_release(self, timeout=3.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        locked = False
        if PRESENCE_DIR.exists():
            for lock_file in PRESENCE_DIR.glob("*.lock"):
                try:
                    with open(lock_file, "r") as f:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except (BlockingIOError, OSError):
                    locked = True
                    break
        if not locked:
            return True
        time.sleep(0.1)
    return False
```

### SQLite WAL
Database `conversation_summaries.db` và `conversations/<uuid>.db` hoạt động ở chế độ Write-Ahead Logging (`-wal` và `-shm`). Việc gửi `SIGINT` trước giúp SQLite driver của Go tự động thực hiện checkpoint và đóng kết nối sạch sẽ, ngăn ngừa triệt để lỗi `SQLITE_BUSY`.
