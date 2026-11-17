# Recurring root-disk fill investigation — 1 November 2026

## Outcome

SpaceSleuth 0.2.0 was run from `/dev/shm` on `192.168.1.250`, so the diagnostic itself did not consume the already-full root filesystem. The source was checked out at commit `8c3739425724d59b0bf677b531411cdc1c9aee26`. Its Python runtime, dependencies and output were also kept in `/dev/shm`.

The live evidence identifies a repeatable build/restart loop rather than a one-off large file:

- `nft-playground.service` runs `npm run start` in `/var/www/nft-playground.dev`.
- `looking-glass.service` runs `npm run start` in `/var/www/looking-glass.nft-playground.dev`.
- Both projects define `start` as `next build && next start`.
- Both systemd units set `Restart=always` and `RestartSec=5`.
- The Next.js build fails with `TypeError: Cannot read properties of undefined (reading 'prototype')`, followed by `Error: Call retries were exceeded` and a service exit status of 1.
- systemd then launches another full production build. At inspection time, the units reported 123,038 and 123,201 restarts respectively.
- SpaceSleuth caught short-lived Node build processes writing at approximately 6.3–6.8 MiB/s. `/var/www/nft-playground.dev/.next` was 657 MiB, and its webpack pack files were actively being replaced.
- Free space fell from about 269 MiB to about 61 MiB during the investigation despite the earlier manual cleanup.

PM2 was a secondary writer: `/var/www/.pm2/pm2.log` was 6,639,477,609 bytes (about 6.2 GiB), with the `www-data` PM2 daemon contributing roughly 1.7–3.5 MiB/s during samples. Earlier, rsyslog and journald also formed a feedback loop when `/var/log/syslog` could not be extended because the disk was full. These amplify the incident but do not explain the repeated bulk regeneration as directly as the two Next.js build loops.

After Kieran explicitly approved stopping the two confirmed writers, `nft-playground.service` and `looking-glass.service` were stopped. Both read back as `inactive (dead)` with `MainPID=0`, and no `next build` process remained. They were not disabled or reconfigured, and no server data was deleted. A 20-second stability check at 23:51–23:52 UTC showed only an 8 KiB filesystem change, compared with the earlier multi-megabyte-per-second decline; the PM2 log and surviving `.next/trace` file sizes did not change. The root filesystem nevertheless had only 53,895,168 bytes available and still reported 100% usage, so cleanup and configuration repair remain necessary before either service is restarted. The temporary SpaceSleuth monitor was stopped after evidence capture. The retained raw evidence is `spacesleuth-root-after-fix-120s.jsonl` in this directory.

## Recovery completed — 2 November 2026

After Kieran approved the recovery, the two verified generated `.next` trees (657 MiB and 841 MiB) were removed and the verified 6,639,477,609-byte PM2 daemon log was truncated. This recovered approximately 7.7 GiB without deleting source, databases, uploads or application logs.

The deployed repositories already had unrelated state (`package-lock.json` was untracked in both, and `submodules/libcip54` was modified in `nft-playground.dev`); that state was preserved. Each `package.json` received only this reviewed change:

```diff
-    "start": "next build && next start",
+    "start": "next start",
```

The applications are Next.js 12.3.1. The failing builds used system Node.js 26.7.0; the same source built successfully under the official Node.js 22.23.2 LTS binary installed alongside the system runtime at `/opt/node-v22.23.2-linux-x64`. Reversible systemd drop-ins now bind only these two services to Node 22, change `Restart` from `always` to `on-failure`, set a ten-second delay and cap starts at five per five minutes. Both explicit production builds completed with warnings but exit code 0; the retained logs are `nft-playground-build.log` and `looking-glass-build.log`.

`/etc/logrotate.d/pm2-www-data` now rotates `/var/www/.pm2/pm2.log` and `/var/www/.pm2/logs/*.log` daily, or earlier at 50 MiB, retaining seven compressed rotations via `copytruncate`. `systemd-analyze verify`, `logrotate --debug` and `nginx -t` all succeeded.

Both services were restarted successfully and remained `active (running)` with zero restarts. Local checks returned HTTP 200 twice on ports 3000 and 3002, stable process IDs and only a 4 KiB filesystem change over 30 seconds. A final 20-second SpaceSleuth run measured 0 B/s growth in both `.next` trees and PM2; ordinary journald/nginx/rsyslog activity remained in the byte-to-low-KiB/s range. Its retained evidence is `spacesleuth-post-recovery.jsonl`.

SpaceSleuth 0.2.0 is now persistently installed from commit `8c3739425724d59b0bf677b531411cdc1c9aee26` at `/opt/space-sleuth`, with its Python/uv runtime at `/var/lib/space-sleuth` and the command `/usr/local/sbin/space-sleuth`. Run it with `sudo space-sleuth / --interval 5` and place optional JSONL output on a filesystem with adequate headroom.

The nginx origin is healthy: configuration validation succeeds, ports 80/443 are listening, and direct TLS virtual-host checks return HTTP 200 for both names. External delivery remains separate: Cloudflare returned HTTP 521 for `nft-playground.dev`, while `looking-glass.nft-playground.dev` had no public DNS record. Restoring those public routes requires the current Cloudflare DNS/origin configuration or router port-forwarding state; no unsupported external change was attempted.

## Documentation media

The raw JSONL monitoring record is the most useful machine-verifiable evidence. No screenshot, video or generated illustration was added: the relevant behaviour is a headless write-rate/restart sequence, and a static terminal capture would add no information beyond the retained timestamped data while risking exposure of unrelated server output.

## Tool fit

No DanceFlow component applies to Linux filesystem writer tracing. SpaceSleuth directly supplied the required process and open-file write evidence; adding a music, movement, rendering or browser tool would not improve the diagnosis.

## Blogworthiness

**Blogworthiness: PASS.** Reader-interest angle: a practical failure-and-recovery story showing why repeatedly clearing space could not solve a disk incident when two systemd-managed Next.js services rebuilt themselves every five seconds, and how live writer monitoring separated the primary regeneration loop from PM2 and logging amplifiers. Evidence is the before-and-after SpaceSleuth JSONL, systemd unit definitions, package scripts, restart counters, successful build logs, HTTP checks and final zero-growth samples above. Duplicate check: current public web searches for `SpaceSleuth`, `Space Sleuth`, disk filling, PM2 and Next.js on `kieransimkin.co.uk` returned no matching article; authenticated pending-draft inventory was not available in this server session, so no article has been prepared or published. Next action: **blocked** only on an authenticated pending-draft duplicate check and removal of private infrastructure details; named continuation is **SpaceSleuth catches the build loop**.
