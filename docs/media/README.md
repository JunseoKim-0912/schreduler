# Demo GIF

`README.md` and `README.ko.md` show `docs/media/demo.gif` right under the live-demo link. Record it from the live demo
account so the data looks real and nothing personal is on screen.

## Setup

- Browser window about **1280 × 800**, zoom 100 %, screen language **Eng**, no bookmarks bar or other tabs visible.
- Open <https://schreduler.up.railway.app>, press **Try the demo**. The yellow "Demo account · Prototype" banner should be
  in the shot — it tells viewers this is a prototype.
- Recorder: ScreenToGif (Windows) or Kap (macOS). 12–15 fps, then export at about 960 px wide. Keep the file under
  ~5 MB so GitHub shows it inline.
- Do one dry run first so the assistant's reply is already cached in your head — the real recording should not wait on
  typing mistakes. Trim any wait for the AI reply to about a second.

## Shot list (under 30 seconds)

| Time | Screen | What to do |
|---|---|---|
| 0–3 s | Calendar tab | The demo week is visible: lectures, the lab, a deadline pinned on top. Pause a beat. |
| 3–9 s | Events tab | Type a biweekly request and send it, e.g. **"Study group every other Monday 6–8pm during Fall term"**. |
| 9–15 s | Confirmation card | Let the card settle: title, time, "every other week on Mon", the next 3 dates, any *guessed* badge. Hover or point at the dates. |
| 15–17 s | Card | Press **Create**. The result bubble shows the change was saved. |
| 17–23 s | Calendar tab | Show this week and press **Next week ▶** once, so the every-other-week rhythm is visible (on one week, not the other). |
| 23–29 s | Events tab → Recent changes | Press **Undo** on the change, then flip back to the calendar to show the study group is gone. |

## After recording

1. Save as `docs/media/demo.gif` (exact name — the READMEs point to it).
2. Check it renders on the GitHub page and loops cleanly (end on the calendar, close to the opening frame).
3. Commit it on its own: `git add docs/media/demo.gif && git commit -m "Add the demo GIF to the README"`.
