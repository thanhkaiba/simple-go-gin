#!/bin/bash
# Camera attendance: stop it and remove it from this Mac. Double-click this file in Finder.
set -uo pipefail

APP_HOME="${FACE_ATTENDANCE_HOME:-$HOME/Library/Application Support/FaceAttendance}"
PLIST="$HOME/Library/LaunchAgents/com.faceattendance.camera.plist"

launchctl unload "$PLIST" 2>/dev/null
rm -f "$PLIST" "$HOME/Desktop/Camera attendance.webloc"
echo "Stopped; it no longer starts at login."

if [ -d "$APP_HOME" ]; then
  read -r -p "Also delete all data (attendance records, photos, settings) in $APP_HOME? [y/N] " answer || answer=n
  if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
    rm -rf "$APP_HOME"
    echo "Everything was deleted."
  else
    rm -rf "$APP_HOME/venv" "$APP_HOME/tools" "$APP_HOME/models"
    echo "The program was removed. Records, photos and settings were kept in $APP_HOME;"
    echo "installing again picks them up."
  fi
fi
read -r -p "Press Enter to close this window." _ || true
