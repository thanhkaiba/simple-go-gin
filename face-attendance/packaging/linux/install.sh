#!/bin/sh
# Face Attendance for Linux: installs the program for this user, starts it now and at every login.
# Run from the unpacked folder:  ./install.sh
set -e
here=$(cd "$(dirname "$0")" && pwd)
dest="$HOME/.local/opt/FaceAttendance"
"$dest/FaceAttendance" stop >/dev/null 2>&1 || true
rm -rf "$dest"
mkdir -p "$dest" "$HOME/.local/bin" "$HOME/.local/share/applications"
cp -R "$here/FaceAttendance/." "$dest/"
ln -sf "$dest/FaceAttendance" "$HOME/.local/bin/face-attendance"
cat > "$HOME/.local/share/applications/face-attendance.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Face Attendance
Comment=Open the camera attendance page
Exec="$dest/FaceAttendance"
Terminal=false
Categories=Office;
DESKTOP
nohup "$dest/FaceAttendance" >/dev/null 2>&1 &
echo "Installed in $dest. It starts at login and opens http://localhost:8000 in the browser."
echo "Data, models and updates are kept in ${XDG_DATA_HOME:-$HOME/.local/share}/face-attendance."
echo "Without a desktop (server): run 'face-attendance --background' from a systemd user service instead."
