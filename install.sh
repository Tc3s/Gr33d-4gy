#!/usr/bin/env bash
set -e

# ==============================================================================
# agy-supervisor Installer
# Production-Grade Antigravity Multi-Account Quota Supervisor
# ==============================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}        Cài Đặt Antigravity Multi-Account Supervisor (agy-supervisor)${NC}"
echo -e "${BLUE}======================================================================${NC}"

# 1. Kiểm tra Python 3
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}[!] Lỗi: Không tìm thấy python3 trên hệ thống. Vui lòng cài đặt Python 3.${NC}"
    exit 1
fi
PYTHON_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo -e "${GREEN}[+] Phát hiện Python: ${PYTHON_VER}${NC}"

# 2. Kiểm tra module python3-dbus
if ! python3 -c "import dbus" &>/dev/null; then
    echo -e "${YELLOW}[!] Cảnh báo: Chưa tìm thấy module 'dbus' của Python (cần cho GNOME Keyring sync).${NC}"
    echo -e "${YELLOW}[*] Cài đặt qua: sudo apt-get install python3-dbus (hoặc pip install dbus-python)${NC}"
else
    echo -e "${GREEN}[+] Phát hiện module D-Bus: OK (Hỗ trợ Secret Service Keyring)${NC}"
fi

# 3. Kiểm tra binary agy
if ! command -v agy &>/dev/null; then
    echo -e "${YELLOW}[!] Cảnh báo: Chưa tìm thấy lệnh 'agy' trong PATH.${NC}"
    echo -e "${YELLOW}[*] Hãy đảm bảo Antigravity CLI đã được cài đặt và có trong PATH.${NC}"
else
    AGY_PATH=$(command -v agy)
    echo -e "${GREEN}[+] Phát hiện agy binary: ${AGY_PATH}${NC}"
fi

# 4. Sao chép binary agy-supervisor vào ~/.local/bin
TARGET_DIR="$HOME/.local/bin"
mkdir -p "$TARGET_DIR"

SCRIPT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bin/agy-supervisor"
if [[ ! -f "$SCRIPT_SRC" ]]; then
    echo -e "${RED}[!] Không tìm thấy file source tại: ${SCRIPT_SRC}${NC}"
    exit 1
fi

TARGET_BIN="$TARGET_DIR/agy-supervisor"
cp "$SCRIPT_SRC" "$TARGET_BIN"
chmod +x "$TARGET_BIN"
echo -e "${GREEN}[+] Đã cài đặt executable vào: ${TARGET_BIN}${NC}"

# 5. Kiểm tra PATH
if [[ ":$PATH:" != *":$TARGET_DIR:"* ]]; then
    echo -e "${YELLOW}[!] Thư mục $TARGET_DIR chưa có trong biến môi trường \$PATH.${NC}"
    echo -e "${YELLOW}[*] Đang thêm export PATH=\"\$HOME/.local/bin:\$PATH\" vào ~/.bashrc...${NC}"
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    export PATH="$HOME/.local/bin:$PATH"
fi

# 6. Tạo thư mục dữ liệu ~/.gemini_accounts
mkdir -p "$HOME/.gemini_accounts"

# 7. Tùy chọn thiết lập alias trong ~/.bashrc
BASHRC="$HOME/.bashrc"
if ! grep -q "alias agys=" "$BASHRC" 2>/dev/null; then
    echo -e "\n${BLUE}[?] Bạn có muốn tạo alias viết tắt 'agys' cho 'agy-supervisor' trong ~/.bashrc? (Y/n)${NC}"
    read -r ans
    if [[ "$ans" =~ ^[Yy]$ || -z "$ans" ]]; then
        echo "" >> "$BASHRC"
        echo "# Antigravity Multi-Account Supervisor alias" >> "$BASHRC"
        echo "alias agys='agy-supervisor'" >> "$BASHRC"
        echo -e "${GREEN}[+] Đã thêm alias: agys -> agy-supervisor${NC}"
    fi
fi

echo -e "\n${GREEN}======================================================================${NC}"
echo -e "${GREEN}             CÀI ĐẶT HOÀN TẤT THÀNH CÔNG!${NC}"
echo -e "${GREEN}======================================================================${NC}"
echo -e "Các lệnh thông dụng:"
echo -e "  ${YELLOW}agy-supervisor${NC}          : Khởi chạy agy với tự động xoay quota & skip permissions"
echo -e "  ${YELLOW}agy-supervisor status${NC}   : Kiểm tra trạng thái các slot tài khoản"
echo -e "  ${YELLOW}agy-supervisor add${NC}      : Thêm tài khoản mới (Slot N+1)"
echo -e "  ${YELLOW}agy-supervisor login <N>${NC}: Đăng nhập tài khoản cho Slot N"
echo -e "  ${YELLOW}agy-supervisor switch <N>${NC}: Chuyển ngay lập tức sang Slot N"
echo -e "======================================================================\n"

# Chạy thử status
"$TARGET_BIN" status
