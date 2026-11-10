<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="packaging/logo-dark.svg">
    <img src="packaging/logo.svg" alt="Reels Manager" height="80">
  </picture>
</h1>

A Mac app for your saved Instagram reels. You can browse them, open and play them, download them, and move them between collections. It also shows the reels that are in no collection. Search covers captions, accounts, and the text that appears on screen in each reel.

## Run

Open Reels Manager from Applications.

From the source folder:

```bash
uv run python -m reels
```

## Build the app

```bash
./packaging/build_app.sh
```

This writes `dist/Reels Manager.app`. Copy it to `/Applications`. The icon source is `packaging/icon.svg`. Run `./packaging/make_icon.sh` after editing it to regenerate `packaging/ReelsManager.icns`, then rebuild the app.

## Connect your account

1. Log in at instagram.com in Safari, Chrome, Arc, Brave, or Edge.
2. In the app, click Use my browser login. If you are logged in to several browsers, the app uses the one where you used Instagram most recently.
3. For Chrome, Arc, Brave, and Edge, macOS asks to share the browser's "Safe Storage" key. Click Allow. Always Allow would let any program on the Mac read every cookie in that browser without a prompt.
4. The app reads only the instagram.com cookies and starts the first sync.

Safari stores cookies unencrypted, so there is no Keychain prompt. macOS blocks the Safari cookie file unless Reels Manager has Full Disk Access (System Settings, Privacy & Security, Full Disk Access). Full Disk Access lets the app read all of your files.

Paste a cookie by hand, at the bottom of the connect screen, works with any browser. Copy the Cookie request header from any www.instagram.com request in the DevTools Network tab.

The login is stored in `~/Library/Application Support/Reels Manager/session.json` with owner-only permissions. Anyone with it can act as you on Instagram. Click Disconnect to delete it.
