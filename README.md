# mwsupplylimited.github.io

Public website for **MW Supply Limited**, an independent iOS app studio. Serves
the company landing page plus per-app support and privacy pages.

Published via GitHub Pages at <https://mwsupplylimited.github.io>.

## Pages

| File | URL | Purpose |
|---|---|---|
| `index.html` | `/` | Company landing — apps list & contact. |
| `scicalc.html` | `/scicalc.html` | SciCalc support page (used as Support URL on the App Store). |
| `scicalc-privacy.html` | `/scicalc-privacy.html` | SciCalc privacy policy (used as Privacy Policy URL on the App Store). |

## Editing

Pages are hand-written HTML with inline CSS — no build step, no dependencies.
Edit the file and push to `main`; GitHub Pages rebuilds within a minute.

To add a new app:

1. Add a card to the `<h2>Apps</h2>` section in `index.html`.
2. Create `appname.html` (support) and `appname-privacy.html` (privacy),
   copying the SciCalc files as templates.
3. Update the contact email if support routing changes.
