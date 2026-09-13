# Hướng Dẫn Xử Lý Sự Cố (Troubleshooting)

Tài liệu giải quyết các tình huống lỗi thường gặp khi sử dụng `agy-supervisor`.

---

### 1. `agy-supervisor login <N>` không mở trình duyệt mà tự đăng nhập lại tài khoản cũ

* **Nguyên nhân**: Token cũ vẫn còn được lưu trong **GNOME Keyring (D-Bus Secret Service)**.
* **Cách xử lý**:
  1. Đảm bảo bạn đang sử dụng phiên bản `agy-supervisor` mới nhất (có tích hợp D-Bus).
  2. Kiểm tra gói `python3-dbus`:
     ```bash
     python3 -c "import dbus; print('OK')"
     ```
     Nếu báo lỗi `ModuleNotFoundError`, hãy cài đặt:
     ```bash
     sudo apt-get install python3-dbus
     ```
  3. Chạy lại:
     ```bash
     agy-supervisor login <N>
     ```

---

### 2. Trình duyệt không tự động mở khi bắt đầu OAuth flow

* **Nguyên nhân**: Môi trường terminal chưa cấu hình biến `$BROWSER` hoặc đang chạy qua SSH / tmux không có X11 forwarding.
* **Cách xử lý**:
  1. Kiểm tra URL in trên terminal: `agy` sẽ in một dòng thông báo dạng:
     ```text
     Please visit https://accounts.google.com/o/oauth2/v2/auth?...
     ```
  2. Sao chép liên kết đó và dán thủ công vào trình duyệt trên máy của bạn để hoàn tất đăng nhập.

---

### 3. Muốn làm mới (re-login) hoặc thay thế tài khoản ở một Slot cụ thể

Ví dụ bạn muốn đổi tài khoản ở **Slot 2** sang một tài khoản Google khác:
```bash
agy-supervisor login 2
```
Script sẽ tự động dọn dẹp token cũ của Slot 2, mở trình duyệt cho bạn đăng nhập tài khoản mới và ghi đè an toàn.

---

### 4. Muốn xóa hoàn toàn một Slot đã cấu hình

Chỉ cần xóa thư mục của slot đó trong `~/.gemini_accounts/`:
```bash
rm -rf ~/.gemini_accounts/3
```
Sau đó kiểm tra lại bằng:
```bash
agy-supervisor status
```

---

### 5. Lỗi `presence lock` hoặc `Session already in use`

* **Hiện tượng**: `agy` báo lỗi phiên trò chuyện đang bị khóa bởi tiến trình khác.
* **Cách xử lý**:
  `agy-supervisor` đã tích hợp hàm tự động chờ nhả lock. Tuy nhiên nếu một phiên `agy` bị treo cứng ở background:
  ```bash
  killall -9 agy
  rm -f ~/.gemini/antigravity-cli/presence/*.lock
  ```
  Sau đó khởi chạy lại `agy-supervisor`.
