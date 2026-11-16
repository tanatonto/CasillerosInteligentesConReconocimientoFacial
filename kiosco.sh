#!/bin/bash
NAV=$(command -v chromium-browser || command -v chromium)
exec "$NAV" --kiosk --password-store=basic --noerrdialogs --disable-infobars \
  --no-first-run --disable-session-crashed-bubble --disable-pinch \
  --overscroll-history-navigation=0 --ozone-platform=wayland \
  --allow-file-access-from-files --lang=es \
  --disable-translate --disable-features=Translate,TranslateUI \
  file:///home/jpintria/proyecto/splash.html
