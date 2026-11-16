# gchat-tui

A terminal client for Google Chat that works with **personal @gmail accounts**.

> **Unofficial.** This project isn't affiliated with or endorsed by Google. It
> uses Google Chat's private web API, which can change at any time and break
> this tool, and automating your account this way may go against Google's Terms
> of Service. Use it at your own risk.

## Features

- DMs, group chats and spaces: list, read, send, mark as read
- Live updates: new messages, reactions and "… is typing" arrive instantly
- Select a message to reply in its thread, or react with 👍 or ❤️
- Shows when others are typing, and tells them when you are
- irssi-style layout that uses your terminal's own color palette
- New messages show in the terminal tab title (`(3) Google Chat`), which flashes
  while the terminal is in the background
- Logs in with your existing browser session (Firefox, Chrome, Chromium or Brave)

Thread replies in spaces show inline (marked `↳`), not as separate threads.

## Requirements

- Linux
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Firefox, Chrome, Chromium or Brave, signed in to https://chat.google.com

## Install

    git clone https://github.com/rahutchinson/gchat-tui
    cd gchat-tui
    uv sync
    uv run playwright install firefox   # the headless browser gchat-tui drives

## Usage

    uv run gchat-tui                  # the TUI
    uv run gchat-tui --check          # health check: exit 0 if login and parsing work
    uv run gchat-tui --list-browsers  # which browsers have a Google login
    uv run gchat-tui --browser chrome # import the login from a specific browser
    uv run gchat-tui --show-browser   # visible browser, for debugging

Keys:

| Where | Key | Does |
|---|---|---|
| chat list | ↑/↓, Enter | pick and open a conversation |
| compose | Enter | send |
| compose (empty) | ↑ | select the latest message |
| selected message | ↑/↓ or k/j | move between messages |
| selected message | r or Enter | reply in that message's thread |
| selected message | t / h | toggle 👍 / ❤️ |
| selected message | Esc | back to typing |
| compose | Esc | cancel a reply, or back to the chat list |
| anywhere | PgUp/PgDn, Ctrl+R, Ctrl+Q | scroll, refresh, quit |

## How it works

The official Chat API only works with Google Workspace accounts, so gchat-tui
runs a **headless Firefox** (via Playwright) that stays signed in to
chat.google.com and makes the same API calls the web app makes, using `fetch()`
inside the page.

- **Login:** on first run, or whenever Google signs the session out, cookies are
  copied from your everyday browser: whichever one used Google most recently,
  or the one you pick with `--browser`. Chrome-family cookies are decrypted
  with the key from your desktop keyring (GNOME Keyring / KWallet). After that,
  the headless browser keeps its own profile and Google renews the session
  while it's open.
- **Data:** responses are protobuf in Google's "pblite" JSON form, read by
  protobuf field number. Field numbers come from
  [purple-googlechat](https://github.com/EionRobb/purple-googlechat)'s
  `googlechat.proto`. Request/response format knowledge is in `src/gchat_tui/api.py`.
- **Live updates:** the web app's own event stream (a WebChannel) is teed out of
  the page; events are base64 protobuf and are decoded by field number in
  `src/gchat_tui/stream.py`. An event about a conversation triggers a re-fetch
  through the normal API, so little of the event format is relied on. Polling
  remains as a fallback: every 30 s while the stream works, every 4 s if it
  goes quiet.

## Your data

Everything stays on your machine. gchat-tui talks only to Google. It keeps
its own browser profile, which includes a signed-in Google session, plus a log,
in `~/.local/share/gchat-tui/`. That folder is private to your user account
(mode `700`). Treat it like a password: deleting it signs gchat-tui out.

## Troubleshooting

1. Run `uv run gchat-tui --check`.
2. "Login failed": open chat.google.com in your browser, make sure you're signed
   in, then retry. `--list-browsers` shows which browser it will use. If that
   doesn't help, delete `~/.local/share/gchat-tui/profile`.
3. Parsing errors usually mean Google changed a field. Compare a fresh capture
   against `api.py` (see the field-number comments) and upstream
   `googlechat.proto`.
4. Log: `~/.local/share/gchat-tui/gchat-tui.log`

## License

MIT. See [LICENSE](LICENSE).
