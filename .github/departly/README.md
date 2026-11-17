# Departly public transit data publisher — review candidate

This folder contains standalone data tooling and 26 explicitly selected open-transit data inputs:
the 24 OTA baseline files plus the English and Traditional Chinese tram CSV inputs. It contains no
Swift/app source, app configuration, credentials, user data or private repository checkout. The exact
reviewed file inventory and hashes accompany this candidate. No new blanket software license is
assigned by this migration; existing source attribution is preserved in the generator headers.

Sources include Hong Kong Transport Department data.gov.hk feeds, MTR Open Data, operator public
APIs and OpenStreetMap Overpass. Generator headers retain the exact endpoint and source context.
Output remains at the existing `https://mwsupplylimited.github.io/departly/data/` URLs and formats.

The dedicated workflow belongs in the existing public `mwsupplylimited.github.io` repository. It uses
standard Ubuntu hosted compute and an ephemeral repository `GITHUB_TOKEN` with `contents:write` and
`pages:write`, plus `actions:read` only to check the cache storage limit. It uses no PAT, private
checkout, paid runner, storage-limit increase or budget change.
Standard public-repository compute is free under GitHub's current documented limits; this is not a
new spending authorization or a promise of a scheduling SLA. The owner's budget remains zero.

Generation runs under this folder's `Departly/Resources` compatibility layout. The publisher compares
row/deep counts with both the live output and the independently committed baseline under
`.github/departly/Departly/Resources`. All 24 files must exist before publication. Baselines are not
automatically committed back. Before generation, working resources are hydrated from the current
public checkout only after its complete 24-file manifest and all SHA256 hashes validate; failed
generators therefore keep the latest public copy, while the committed baseline still protects
against repeat corruption. A detached `origin/main` worktree stages only `departly/data`; push is
fast-forward only. Generator errors remain degraded; publication errors remain failures, with an
uncertain remote outcome stated for a failed push.

The cache policy changes from the entire `.transit_cache` directory to `*.json` only. API JSON and
crosswalk statistics persist; `td_gtfs.zip` does not, so each ephemeral runner downloads the current
GTFS feed without changing generator logic. Before using the cache action, a read-only API request
must prove the repository cache limit is between 1 and 10 GB. Unknown, unavailable, denied or larger
limits skip the cache action entirely and use the existing 180-minute cold-run ceiling. No payment
method is required and no cache storage limit is increased.

The workflow explicitly requests the existing legacy Pages build through the Pages API after git
publication. It does not change Pages settings, `main`/root source, domains, the dynamic Pages
deployment, Chronograde checks or any other app. It verifies the exact publication commit and all
24 served SHA256 hashes within a ten-minute verification deadline. A different concurrently built
site commit is tolerated while waiting for the expected commit, then fails at the bounded deadline
if it never appears; it never forces or rolls back another app's changes. A Pages or
CDN verification failure does not claim the already committed data remained unchanged.

Before publishing this currently local-only candidate, obtain the owner's approval to make this
explicit tooling/data subset public. Cutover requires disabling the private Departly nightly before
the new public schedule can publish, then one manual public verification and independent live
manifest/hash readback. Keep the Mac LaunchAgent unloaded. Do not run both publishers. Preserve the
existing ETA harvester and its URLs/assets unchanged. If verification fails, stop the new publisher
and investigate; do not enable a paid private retry or raise any budget.

Local stub tests do not establish that the ephemeral token can build this site's Pages deployment.
That supported API path and the first real generation/publication remain NOT RUN until approved.
