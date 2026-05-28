# mwsupplylimited.github.io

Public website for **MW Supply Limited**, an independent iOS app studio. Serves
the company landing page plus per-app support and privacy pages.

Published via GitHub Pages at <https://mwsupplylimited.github.io>.

## Pages

Per-app pages live in their own folder so each app has its own URL namespace.

| File | URL | Purpose |
|---|---|---|
| `index.html` | `/` | Company landing — list of apps & contact. |
| `scicalc/index.html` | `/scicalc/` | SciCalc support page (App Store Support URL). |
| `scicalc/privacy.html` | `/scicalc/privacy.html` | SciCalc privacy policy (App Store Privacy URL). |
| `videoframeexport/index.html` | `/videoframeexport/` | Video Frame Export support page. |
| `videoframeexport/privacy.html` | `/videoframeexport/privacy.html` | Video Frame Export privacy policy. |

## Editing

Pages are hand-written HTML with inline CSS — no build step, no dependencies.
Edit the file and push to `main`; GitHub Pages rebuilds within a minute.

To add a new app, mirror the SciCalc structure:

1. Create an `<appname>/` folder at the repo root.
2. Copy `scicalc/index.html` → `<appname>/index.html` (support page) and
   `scicalc/privacy.html` → `<appname>/privacy.html`, then edit for the new app.
3. Add a card linking to `<appname>/` in the `<h2>Apps</h2>` section of the
   root `index.html`.
4. URLs become `https://mwsupplylimited.github.io/<appname>/` (support) and
   `https://mwsupplylimited.github.io/<appname>/privacy.html` (privacy).
