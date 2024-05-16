#!/bin/bash -e

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

source "$SCRIPT_DIR/entrypoint-common.sh"

# install binaries for paru from paru-bin (paru takes too long to compile)
PARU="paru --noconfirm"
if ! pacman -Qq paru 2>/dev/null >/dev/null; then
  echo_color "$fg_cyan" "Installing AUR helper 'paru'" >> $status_file
  git clone https://aur.archlinux.org/paru-bin.git
  cd paru-bin
  makepkg --noconfirm -si
  cd ..
fi
if [ -n "$EXTRA_PKGS" ]; then
  echo_color "$fg_cyan" "Installing $EXTRA_PKGS" >> $status_file
  $PARU -S --needed $EXTRA_PKGS
fi
echo_color "$fg_cyan" "Clearing package cache and updating packages" >> $status_file
yes | paru -Sccd
$PARU -Syu
rm -rf paru-bin
sudo rm -rf /root/.cache

# add current user to realtime group (if present)
if getent group realtime 2>/dev/null >/dev/null; then
  current_user=$(id -un)
  echo_color "$fg_cyan" "Adding '$current_user' to realtime group" >> $status_file
  sudo usermod -aG realtime $current_user
fi
