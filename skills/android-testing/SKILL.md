---
name: android-testing
description: Test an Android app (APK, screen, flow, device-only bug) on a real phone or emulator over adb using the android_* tools. Use when the task involves installing/running an APK, checking how a screen looks on the device, reproducing a bug that only happens on the phone, or reading logcat.
metadata:
  {
    "alpha":
      {
        "emoji": "📱",
        "requires": { "bins": ["adb"] },
      },
  }
---

# Testing on an Android device

## Before touching the phone — ask

When the work involves testing an APK or anything on Android, **ask the user
first whether they want it tested on their own phone** (instead of assuming
an emulator or just handing over the file). Use `ask_choice`. Only proceed on
the device after a yes.

## Limits — these are not negotiable

- Touch **only the app and screens the user asked for**. A screenshot shows
  everything that is open (notifications, conversations): do not browse, do
  not read or report anything outside the app under test.
- **Never** send a message, pay, delete data or change a device setting
  (including airplane mode, Wi-Fi, accounts) unless the user explicitly told
  you to in this conversation.
- No uninstall and no "clear app data" on your own initiative — both erase
  the user's data. If a clean install is really needed, say why and ask.
- If the screen is locked or shows something unrelated to the test, stop and
  tell the user instead of navigating around it.

## Tools

Load them with `tool_search("select:android_devices,android_screenshot,android_ui_dump,android_input,android_app,android_logcat")`.

| Tool | Use |
|---|---|
| `android_devices` | First call: is a device connected, which model, which display ids |
| `android_app` | `install` an APK from the workspace (keeps data), `launch`, `stop` |
| `android_ui_dump` | What is on screen as text + exact tap coordinates |
| `android_screenshot` | How the screen LOOKS (layout, images, visual bugs) |
| `android_input` | `tap`, `long_press`, `swipe`, `text`, `key` |
| `android_logcat` | Logs after reproducing a crash/error |

## Loop

1. `android_devices`. No device → relay the instructions in the error (cable,
   USB debugging, accept the prompt on the phone) and stop.
2. `android_app install` (if there is a new build) → `android_app launch`.
3. Observe → act → verify, one step at a time:
   - `android_ui_dump` to find the element and its `x,y`;
   - `android_input` to act;
   - `android_ui_dump` (or `android_screenshot`) again to confirm the effect.
   Never chain several inputs without checking the screen in between.
4. On a crash or error: `android_logcat` with the app's `package` (if the app
   died, call it without `package` and `grep` the package name).
5. Report what you did, what you saw, and what failed — with the log lines.

## Seeing the screen

- Prefer `android_ui_dump` for text and tap targets: it is exact.
- Use `android_screenshot` when appearance matters or the dump fails (games,
  video, WebView-only screens). If your model has no vision, Gemini describes
  the image for you — pass `question` saying what to check. Its coordinates
  are approximate; confirm with the UI dump before tapping.

## Gotchas

- **Foldables (Galaxy Z Fold)** have two displays. `android_screenshot`
  auto-picks one and lists the rest in `other_displays`; pass `display` to
  choose. The cover screen is the narrow one (904x2316 on the Fold 4).
- `android_input text` types **ASCII only** — no accents, no emoji. Use
  unaccented text in tests.
- Tap the field before typing; `key=enter` submits, `key=back` closes the
  keyboard.
- Each `android_input` / `android_app` call asks the user for approval. That
  is intended: it is their phone.
