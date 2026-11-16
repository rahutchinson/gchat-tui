# gchat-tui

Terminal client for Google Chat that works with **personal @gmail accounts**.

## How it works

The official Chat API only works with Workspace accounts, so this app runs a
**headless Firefox** (Playwright) that stays logged in to chat.google.com and
makes the same private API calls the web app makes, using `fetch()` inside the
page.

- **Login:** on first run (or whenever Google logs the session out), cookies are
  copied from your everyday Firefox profile. After that, the headless browser's
  own profile (`~/.local/share/gchat-tui/profile`) keeps them, and Google renews
  them while the page is open.
- **Data:** responses are protobuf in Google's "pblite" JSON form and are read by
  protobuf field number (from
  [purple-googlechat's proto](https://github.com/EionRobb/purple-googlechat/blob/master/googlechat.proto)).
  All format knowledge is in `src/gchat_tui/api.py`.
- **Updates:** the conversation list is polled every 4 seconds; the open
  conversation reloads when it changes.

## Usage

    uv run gchat-tui               # the TUI
    uv run gchat-tui --check       # health check: exit 0 if login and parsing work
    uv run gchat-tui --show-browser   # visible browser, for debugging

Keys: ↑/↓ + Enter open a conversation, type + Enter to send, Esc returns to the
list, PgUp/PgDn scroll, Ctrl+R refresh, Ctrl+Q quit.

Log: `~/.local/share/gchat-tui/gchat-tui.log`

## When it breaks

1. Run `uv run gchat-tui --check`.
2. "Login failed": open chat.google.com in Firefox, make sure you're signed in,
   then retry. If that doesn't work, delete `~/.local/share/gchat-tui/profile`.
3. Parsing errors: Google changed a field. Compare a fresh capture against
   `api.py` (see the field-number comments) and upstream `googlechat.proto`.
