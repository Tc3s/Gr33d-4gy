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

AUTO_YES=false
for arg in "$@"; do
    if [[ "$arg" == "-y" || "$arg" == "--yes" ]]; then
        AUTO_YES=true
    fi
done

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
    echo -e "${YELLOW}[*] Cài đặt qua:${NC}"
    echo -e "    Debian/Ubuntu/Kali : sudo apt-get install python3-dbus"
    echo -e "    Arch Linux         : sudo pacman -S python-dbus"
    echo -e "    Fedora/RHEL        : sudo dnf install python3-dbus"
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
chmod 755 "$TARGET_BIN"
echo -e "${GREEN}[+] Đã cài đặt executable vào: ${TARGET_BIN}${NC}"

# 5. Kiểm tra và cập nhật PATH
SHELL_CONFIGS=()
[[ -f "$HOME/.bashrc" ]] && SHELL_CONFIGS+=("$HOME/.bashrc")
[[ -f "$HOME/.zshrc" ]] && SHELL_CONFIGS+=("$HOME/.zshrc")

if [[ ":$PATH:" != *":$TARGET_DIR:"* ]]; then
    echo -e "${YELLOW}[!] Thư mục $TARGET_DIR chưa có trong biến môi trường \$PATH.${NC}"
    for cfg in "${SHELL_CONFIGS[@]}"; do
        if ! grep -q 'export PATH="$HOME/.local/bin:$PATH"' "$cfg" 2>/dev/null; then
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$cfg"
            echo -e "${GREEN}[+] Đã thêm PATH export vào $cfg${NC}"
        fi
    done
    export PATH="$HOME/.local/bin:$PATH"
fi

# 6. Tạo thư mục dữ liệu ~/.gemini_accounts và siết chặt quyền hạn (0700)
mkdir -p "$HOME/.gemini_accounts"
chmod 700 "$HOME/.gemini_accounts"
echo -e "${GREEN}[+] Thư mục dữ liệu: ~/.gemini_accounts (Quyền hạn: 0700)${NC}"

# 7. Tùy chọn thiết lập alias 'agys'
for cfg in "${SHELL_CONFIGS[@]}"; do
    if ! grep -q "alias agys=" "$cfg" 2>/dev/null; then
        ans="y"
        if [ "$AUTO_YES" = false ] && [ -t 0 ]; then
            echo -e "\n${BLUE}[?] Bạn có muốn tạo alias viết tắt 'agys' trong $(basename "$cfg")? (Y/n)${NC}"
            read -r user_ans
            [[ -n "$user_ans" ]] && ans="$user_ans"
        fi
        if [[ "$ans" =~ ^[Yy]$ ]]; then
            echo "" >> "$cfg"
            echo "# Antigravity Multi-Account Supervisor alias" >> "$cfg"
            echo "alias agys='agy-supervisor'" >> "$cfg"
            echo -e "${GREEN}[+] Đã thêm alias: agys -> agy-supervisor vào $(basename "$cfg")${NC}"
        fi
    fi
done

echo -e "\n${GREEN}======================================================================${NC}"
echo -e "${GREEN}             CÀI ĐẶT HOÀN TẤT THÀNH CÔNG!${NC}"
echo -e "${GREEN}======================================================================${NC}"
echo -e "Các lệnh thông dụng:"
echo -e "  ${YELLOW}agy-supervisor${NC}            : Khởi chạy agy với tự động xoay quota & skip permissions"
echo -e "  ${YELLOW}agy-supervisor status${NC}     : Kiểm tra trạng thái các slot tài khoản"
echo -e "  ${YELLOW}agy-supervisor reset [all|N]${NC}: Reset cooldowns quota của các slot"
echo -e "  ${YELLOW}agy-supervisor add${NC}        : Thêm tài khoản mới (Slot N+1)"
echo -e "  ${YELLOW}agy-supervisor login <N>${NC}  : Đăng nhập tài khoản cho Slot N"
echo -e "  ${YELLOW}agy-supervisor switch <N>${NC} : Chuyển ngay lập tức sang Slot N"
echo -e "  ${YELLOW}make test${NC}                 : Chạy bộ kiểm thử tự động (17 unit & integration tests)"
echo -e "  ${YELLOW}make health${NC}               : Chạy chẩn đoán toàn diện hệ thống"
echo -e "======================================================================\n"

# Chạy thử status
"$TARGET_BIN" status
