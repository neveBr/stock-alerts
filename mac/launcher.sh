#!/bin/bash
# Stock Alerts.app – opens setup / status dialogs. The app itself runs in the background (launchd).
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
SCRIPT="$RES/stock_news.py"
APP_DIR="$HOME/StockAlerts"
PLIST="$HOME/Library/LaunchAgents/com.stockalerts.bot.plist"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

dlg() {  # dlg "text" "Button1,Button2" -> prints the clicked button (the last one is the default)
  osascript - "$1" "$2" 2>/dev/null <<'AS'
on run argv
  set AppleScript's text item delimiters to ","
  set b to text items of (item 2 of argv)
  tell me to activate
  return button returned of (display dialog (item 1 of argv) with title "Stock Alerts" buttons b default button (count of b))
end run
AS
}
ask() {  # ask "text" "default answer" -> prints the answer; fails on Cancel
  osascript - "$1" "$2" 2>/dev/null <<'AS'
on run argv
  tell me to activate
  return text returned of (display dialog (item 1 of argv) with title "Stock Alerts" default answer (item 2 of argv) buttons {"Cancel", "Continue"} default button 2)
end run
AS
}

PY=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$p" ] && "$p" -c "" >/dev/null 2>&1; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  dlg "Stock Alerts needs Python 3, which comes with Apple's free Command Line Tools.

Click Install in the next window, wait until it finishes (a few minutes), then open Stock Alerts again." "OK" >/dev/null
  xcode-select --install >/dev/null 2>&1
  exit 0
fi

run_install() {
  OUT="$("$PY" "$SCRIPT" --install 2>&1 </dev/null)"
  if [ $? -eq 0 ]; then
    dlg "✅ Stock Alerts is running in the background.

• Checks the news every 2 minutes and answers Telegram commands within seconds.
• Starts by itself when you log in to this Mac.
• Keeps the Mac awake while it runs (closing a laptop lid still puts it to sleep).

Send /status to the bot to check it. Open Stock Alerts again to update, change settings or remove it." "OK" >/dev/null
  else
    dlg "❌ Setup didn't finish:

$(echo "$OUT" | grep -v '^\[' | tail -4)" "OK" >/dev/null
  fi
}

setup_telegram() {
  TOKEN="$(ask "Paste your Telegram bot token.

You get it from @BotFather in Telegram. It looks like 123456789:AAH..." "")" || exit 0
  TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
  INFO="$(STOCK_ALERTS_TOKEN="$TOKEN" "$PY" "$SCRIPT" --detect-chats 2>/dev/null)" || {
    dlg "Telegram didn't accept this token, or there is no internet. Check the token and try again." "OK" >/dev/null; exit 0; }
  BOT="${INFO%%|*}"
  [ "$(dlg "Now open Telegram and send /start to @$BOT – in your private chat with the bot, and in the alerts group.

Then click Continue." "Cancel,Continue")" = "Continue" ] || exit 0
  INFO="$(STOCK_ALERTS_TOKEN="$TOKEN" "$PY" "$SCRIPT" --detect-chats 2>/dev/null)"
  FOUND="${INFO#*|}"
  CHATS="$(ask "Chats that get the alerts: IDs separated by commas (group IDs start with a minus).

Found: ${FOUND:-none yet. Send /start to the bot and open the app again, or type the IDs.}" "$FOUND")" || exit 0
  if [ -z "$(printf '%s' "$CHATS" | tr -d ' ,')" ]; then
    dlg "No chats were entered, so nothing was installed." "OK" >/dev/null; exit 0
  fi
  STOCK_ALERTS_TOKEN="$TOKEN" STOCK_ALERTS_CHATS="$CHATS" run_install
}

if [ -f "$PLIST" ] && [ -f "$APP_DIR/local_secrets.json" ]; then
  if "$PY" "$SCRIPT" --running; then S="✅ Stock Alerts is running in the background."
  else S="⚠️ Stock Alerts is installed but not running right now. Click Update / restart."; fi
  case "$(dlg "$S

Update / restart installs this version of the app and keeps your stock list and settings." "More…,Update / restart,OK")" in
    "Update / restart") STOCK_ALERTS_KEEP=1 run_install ;;
    "More…")
      case "$(dlg "What would you like to do?" "Remove Stock Alerts,Change Telegram settings,Cancel")" in
        "Change Telegram settings") setup_telegram ;;
        "Remove Stock Alerts")
          "$PY" "$SCRIPT" --uninstall >/dev/null 2>&1
          dlg "Stock Alerts was stopped and won't start with the Mac anymore.

Your list and settings stay in the StockAlerts folder in your home folder. Delete that folder to remove everything." "OK" >/dev/null ;;
      esac ;;
  esac
else
  [ "$(dlg "Welcome to Stock Alerts 📈

It runs in the background on this Mac and sends Telegram alerts about news that could move your stocks, with a short Hebrew summary of each article.

Setup takes a minute. You'll need your Telegram bot token." "Cancel,Set up")" = "Set up" ] && setup_telegram
fi
exit 0
