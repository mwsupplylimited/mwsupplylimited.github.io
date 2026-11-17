# Departly public transit data publisher

This folder contains standalone data tooling and 26 selected open-transit data inputs:
the 24 OTA baseline files plus the English and Traditional Chinese tram CSV inputs. It contains no
Swift/app source, app configuration, credentials, user data or private repository checkout. The initial
44-file migration inventory is retained in the private validation record. No new blanket software license is
assigned by this migration; existing source attribution is preserved in the generator headers.

Sources include Hong Kong Transport Department data.gov.hk feeds, MTR Open Data, Lands Department
iGeoCom, operator public APIs and OpenStreetMap Overpass. Generator headers retain the exact endpoint and source context.
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

The owner approved and completed the free public cutover on 2026-11-17. The first public run
`44058904354` published data and verified all 24 served hashes, but GMB and Light Rail source refreshes
were degraded. Successful byte verification alone does not establish source freshness. Keep the
private Departly nightly disabled and the Mac LaunchAgent unloaded. Preserve the existing ETA
harvester and its URLs/assets unchanged; do not enable a paid private retry or raise any budget.

Use Python 3.12 for this tooling, matching the public Ubuntu workflow. Light Rail joins the current
MTR route CSV to Lands Department's WGS84 iGeoCom points, requiring complete unique station coverage
and matching English and Chinese names. It preserves alphabetic station codes and North/South
distinctions. `hkmap_tls.py` supplies the official 2025 Hongkong Post intermediate omitted by the
source server, pinned by DER SHA256; system roots, hostname and certificate checks remain required.
The certificate is public chain material, not a private key or a new root trust anchor.
An incomplete two-file rollback exits distinctly and stops the publisher before any site mutation.

GMB requests share a one-second start interval across workers and retries. Permanent HTTP denials
are not retried; transient failures have bounded retries and provider cooldowns apply to every GMB
worker. Invalid source JSON is rejected before caching or replacing data. The existing last-good
merge remains explicitly degraded. A cold rebuild still needs thousands of requests; no new cache
storage is enabled, and the 180-minute workflow ceiling remains in effect.

`departly-provider-check.yml` is a manual, read-only, bounded diagnostic on a standard public Ubuntu
runner. It uses no cache, uploaded artifact, publication or credentials and stops further GMB probes
when the provider requests a cooldown. Sparse HTTP success cannot prove sustained full-refresh
success. Complete a full nightly run and its served-data checks after changing the generators.
