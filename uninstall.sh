#!/usr/bin/env bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PURGE_DATA=false
for arg in "$@"; do
    if [[ "$arg" == "--purge" ]]; then
        PURGE_DATA=true
    fi
done

echo -e "${RED}======================================================================${NC}"
echo -e "${RED}          Gỡ Cài Đặt Antigravity Multi-Account Supervisor${NC}"
echo -e "${RED}======================================================================${NC}"

# Xóa binary
TARGET_BIN="$HOME/.local/bin/agy-supervisor"
if [[ -f "$TARGET_BIN" ]]; then
    rm -f "$TARGET_BIN"
    echo -e "${GREEN}[+] Đã xóa file thực thi: ${TARGET_BIN}${NC}"
else
    echo -e "${YELLOW}[*] Không tìm thấy ${TARGET_BIN}${NC}"
fi

# Xóa alias trong ~/.bashrc và ~/.zshrc
SHELL_CONFIGS=()
[[ -f "$HOME/.bashrc" ]] && SHELL_CONFIGS+=("$HOME/.bashrc")
[[ -f "$HOME/.zshrc" ]] && SHELL_CONFIGS+=("$HOME/.zshrc")

for cfg in "${SHELL_CONFIGS[@]}"; do
    if grep -q "alias agys=" "$cfg" 2>/dev/null; then
        sed -i '/# Antigravity Multi-Account Supervisor alias/d' "$cfg"
        sed -i '/alias agys=/d' "$cfg"
        echo -e "${GREEN}[+] Đã gỡ alias 'agys' khỏi $(basename "$cfg")${NC}"
    fi
done

# Hỏi về thư mục dữ liệu ~/.gemini_accounts
if [[ -d "$HOME/.gemini_accounts" ]]; then
    if [ "$PURGE_DATA" = true ]; then
        rm -rf "$HOME/.gemini_accounts"
        echo -e "${GREEN}[+] Đã xóa thư mục dữ liệu ~/.gemini_accounts (Purge mode)${NC}"
    elif [ -t 0 ]; then
        echo -e "\n${YELLOW}[?] Bạn có muốn xóa toàn bộ dữ liệu token đã lưu trong ~/.gemini_accounts? (y/N)${NC}"
        read -r ans
        if [[ "$ans" =~ ^[Yy]$ ]]; then
            rm -rf "$HOME/.gemini_accounts"
            echo -e "${GREEN}[+] Đã xóa thư mục dữ liệu ~/.gemini_accounts${NC}"
        else
            echo -e "${BLUE}[*] Giữ nguyên thư mục dữ liệu tại ~/.gemini_accounts${NC}"
        fi
    else
        echo -e "${BLUE}[*] Môi trường non-interactive: Giữ nguyên thư mục dữ liệu tại ~/.gemini_accounts (dùng --purge để xóa)${NC}"
    fi
fi

echo -e "\n${GREEN}[+] Gỡ cài đặt hoàn tất.${NC}"
