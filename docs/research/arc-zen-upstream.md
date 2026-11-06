# Arc → Zen: upstream storage requirements

Research date: 2026-11-06. Sources are first-party documentation and upstream source. No personal browser data was inspected. Branches named `main`/`dev` move: verify the installed Zen/Firefox build and SQLite `PRAGMA user_version` before choosing a writer.

## Preserve the Arc relationship

Arc's own migration documentation confirms that spaces using the same Arc profile share logins and other profile data; different profiles remain separate. Therefore the session-preserving mapping is **one Arc profile → one Zen container, with every space in that profile bound to that same container**. Creating a cookie jar per space changes sharing; merging every profile into every container destroys isolation. This mapping preserves website sessions, not full browser-profile isolation of history/extensions/settings. [Arc first-party explanation](https://resources.arc.net/hc/en-us/articles/44413129117591-Move-from-Arc-to-Dia), [Mozilla containers](https://support.mozilla.org/en-US/kb/containers).

## Workspace container and tab container must agree

Zen's `ZenSpaceManager.mjs` uses numeric `containerTabId` for a workspace's default identity. Missing/non-numeric values fall back to 0. This default influences new tabs; existing tabs have their own identity. Set both the workspace's `containerTabId` and restored tabs' `userContextId`, including essentials/pins/open tabs, using the same source-profile mapping. A workspace UUID/name is not a cookie identity. [Zen workspace source](https://github.com/zen-browser/desktop/blob/dev/src/zen/spaces/ZenSpaceManager.mjs).

Current Zen's `ZenSessionManager.sys.mjs` reads/writes `zen-sessions.jsonlz4`, storing sidebar spaces/tabs/folders. Its SQL reads of `zen_workspaces`/`zen_pins` are explicitly a one-time migration from the older Places-based representation. Consequently writing only `places.sqlite` is insufficient for this generation of Zen. Do not assume that all older Zen versions share this format. [Zen session source](https://github.com/zen-browser/desktop/blob/dev/src/zen/sessionstore/ZenSessionManager.sys.mjs).

Gecko `originAttributes` distinguishes container identity (`userContextId`) and partition identity (`partitionKey`). A cookie belongs in the context in which its tab runs. Copying cookies only to the empty origin suffix makes them unavailable to container tabs. Copying them everywhere allows unrelated source accounts to collide. Partitioned cookies require their partition attributes, not just a container suffix. [Gecko security model](https://firefox-source-docs.mozilla.org/content-security/index.html).

## Cookie schema compatibility

Current Firefox `nsICookie.idl` defines SameSite NONE=0, LAX=1, STRICT=2, **UNSET=256**. Its expiry property is milliseconds since Unix epoch; creation/last-access/update times are microseconds. A mapping of unspecified Chromium SameSite to 3 is unsupported by this interface. [Cookie interface](https://github.com/mozilla-firefox/firefox/blob/main/netwerk/cookie/nsICookie.idl).

`CookiePersistentStorage.cpp` currently declares schema 17. Its migrations show schema 15 → 16 multiplies `expiry` by 1000, and schema 16 → 17 adds `updateTime` in microseconds. Thus milliseconds are right for schema ≥16, while a writer must handle or reject older schemas explicitly. The current unique key is `(name, host, path, originAttributes)`. Inspect both `user_version` and table columns; do not unconditionally insert a column missing from the destination. The source does not support the repository's comment claiming the unit change was around Firefox 108. [Schema and migrations](https://github.com/mozilla-firefox/firefox/blob/main/netwerk/cookie/CookiePersistentStorage.cpp).

## Chromium cookie decryption

Chromium's cookie DB version 24 introduced an exact 32-byte SHA-256 digest of the domain/host at the start of encrypted plaintext. Chromium verifies the digest against the row's host before accepting the value. Read `meta.version`, validate the prefix, then strip precisely 32 bytes for the applicable schema. A printable-byte heuristic is not equivalent: it can strip a legitimate old cookie or fail to strip/validate a modern value. [Versioned change introducing the format](https://chromium.googlesource.com/chromium/src/+/5ea6d65c622a3d5ff75db9dc0257ea3869f31289%5E%21/), [current cookie store](https://chromium.googlesource.com/chromium/src/+/main/net/extras/sqlite/sqlite_persistent_cookie_store.cc).

The version-pinned Chromium 130 macOS implementation uses a Keychain secret, PBKDF2 with salt `saltysalt`, 1003 iterations, a 128-bit AES key, `v10` prefix, and CBC with a 16-space IV. This establishes the legacy algorithm; it does not prove every later Arc build uses it. Detect unsupported encryption prefixes and report counts instead of silently claiming complete login migration. [Chromium 130 macOS implementation](https://chromium.googlesource.com/chromium/src/+/refs/tags/130.0.6723.91/components/os_crypt/sync/os_crypt_mac.mm).

## What cookie migration can promise

### Partitioned-cookie follow-up

The importer now maps Chromium HTTP(S) `top_frame_site_key` into Gecko's
`partitionKey` alongside `userContextId`, and sets `isPartitionedAttributeSet`.
Gecko serializes a site as `(scheme,host[,f])`, sanitizes colons, then applies
URL parameter encoding. Its `f` flag covers a same-site frame with an intervening
cross-site ancestor; Chromium's ancestor flag also covers ordinary cross-site
frames, so the flags must not be copied blindly. Unsupported/opaque sites and
destinations lacking the partition attribute column remain explicitly skipped.
[OriginAttributes serialization](https://github.com/mozilla-firefox/firefox/blob/main/caps/OriginAttributes.cpp),
[Gecko ancestor handling](https://github.com/mozilla-firefox/firefox/blob/main/toolkit/components/antitracking/StoragePrincipalHelper.cpp),
[Chromium partition serialization](https://github.com/chromium/chromium/blob/main/net/cookies/cookie_partition_key.cc),
[Gecko cookie persistence](https://github.com/mozilla-firefox/firefox/blob/main/netwerk/cookie/CookiePersistentStorage.cpp).

Web Storage is separate from cookies: `localStorage` persists by origin and `sessionStorage` is scoped to origin and tab. Cookie SQLite conversion cannot carry these storage areas. Sites that require tokens there, IndexedDB content, or server-side reauthentication may still require login; that is a limitation to report after fixing identity routing, not an explanation for routing defects. Do not copy Chromium storage directories blindly into Firefox: these are different implementations and this research establishes no compatible disk format. [Web Storage API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Storage_API).

## Acceptance checks for implementation

1. Synthetic Arc profiles A/B each have the same domain/name/path cookie with different values. Two spaces belong to A and one to B. Verify A spaces share exactly A's container and cookie value; B sees only B's; unrelated destination containers/default context remain untouched.
2. Verify every restored tab kind and each workspace's default container use the exact mapped numeric identity; new tabs opened after restart inherit it.
3. Run fixtures for supported schema generations, unspecified SameSite, modern host digest (valid/mismatched/empty/unicode payload), legacy unprefixed payload, expired/session cookies, and supported partitioned cookies. Explicitly report skipped unsupported formats.
4. Validate a disposable Zen profile after restart using a local test site which distinguishes accounts, and repeat migration to verify stable mapping without duplicate containers or account overwrites.
5. Preflight the selected destination path, supported schema, and closed-browser state; use consistent SQLite snapshots/backups and expose per-source-profile read/decrypted/imported/skipped totals without cookie values.
