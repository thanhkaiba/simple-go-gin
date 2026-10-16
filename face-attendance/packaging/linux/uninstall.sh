#!/bin/sh
# Removes the program; attendance data in ~/.local/share/face-attendance is kept.
dest="$HOME/.local/opt/FaceAttendance"
"$dest/FaceAttendance" stop || true
"$dest/FaceAttendance" remove-autostart || true
rm -rf "$dest" "$HOME/.local/bin/face-attendance" "$HOME/.local/share/applications/face-attendance.desktop" \
       "${XDG_DATA_HOME:-$HOME/.local/share}/face-attendance/versions"
echo "Removed. Data is still in ${XDG_DATA_HOME:-$HOME/.local/share}/face-attendance; delete that folder to remove it too."
