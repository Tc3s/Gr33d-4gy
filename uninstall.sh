#!/usr/bin/env bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

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

# Xóa alias trong ~/.bashrc
BASHRC="$HOME/.bashrc"
if grep -q "alias agys=" "$BASHRC" 2>/dev/null; then
    sed -i '/# Antigravity Multi-Account Supervisor alias/d' "$BASHRC"
    sed -i '/alias agys=/d' "$BASHRC"
    echo -e "${GREEN}[+] Đã gỡ alias 'agys' khỏi ~/.bashrc${NC}"
fi

# Hỏi về thư mục dữ liệu ~/.gemini_accounts
if [[ -d "$HOME/.gemini_accounts" ]]; then
    echo -e "\n${YELLOW}[?] Bạn có muốn xóa toàn bộ dữ liệu token đã lưu trong ~/.gemini_accounts? (y/N)${NC}"
    read -r ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        rm -rf "$HOME/.gemini_accounts"
        echo -e "${GREEN}[+] Đã xóa thư mục dữ liệu ~/.gemini_accounts${NC}"
    else
        echo -e "${BLUE}[*] Giữ nguyên thư mục dữ liệu tại ~/.gemini_accounts${NC}"
    fi
fi

echo -e "\n${GREEN}[+] Gỡ cài đặt hoàn tất.${NC}"
