# Arc → Zen diagnosis and repair plan

Investigated 2026-11-06. Scope: code review, primary-source research, and synthetic reproductions; no live Arc/Zen profile or Keychain access, no migration implementation changes.

## Conclusion

The current migration does not preserve Arc's profile-to-space-to-login relationship. It creates a container per space, discards the source profile identifier, leaves workspaces and pinned tabs in context 0, and copies the combined cookies from all Arc profiles into every destination container plus the default context. Login cookies with equal host/name/path therefore overwrite each other across accounts.

These are confirmed defects, but they do not prove the cause of this user's complete logout. Because the cookie writer also populates context 0, incorrect tab contexts alone do not imply all cookies are inaccessible. Cookie import being disabled, decryption failures, schema errors, or non-cookie login requirements must be distinguished in a diagnostic report.

See [upstream research](arc-zen-upstream.md) for primary citations and compatibility limits.

## Confirmed findings

1. **Source profile identity is lost.** `src/arc_pinned_tab_extractor.py:127` reads `profile.custom._0.directoryBasename` and `_build_spaces_info` defaults it to `Default`. But `ArcSpace` (line 62) has no profile field, and its construction at lines 436/451 drops that information. `src/extractors/arc.py:113` converts to `SpaceRecord`, which also lacks profile identity (`src/extractors/base.py:103`); legacy serialization cannot carry it either.
2. **The sharing model is wrong.** `src/zen_space_importer.py:94` allocates/reuses containers by space display name. Two spaces on one Arc profile should share website sessions; this code separates them. Equal space names can instead collide across unrelated profiles. Names are not durable source identifiers.
3. **Workspace/pinned/open tab identities disagree.** `src/zen_sessions_importer.py:111` hard-codes workspace `containerTabId=0`; `_build_tab` (line 143) hard-codes pinned/essential `userContextId=0`. `_process_space` (line 231) receives a mapped ID but only passes it to open tabs. Its comment saying pinned tabs are bound through the workspace container is contradicted by the workspace's own hard-coded zero.
4. **Cookies are mixed, duplicated, and overwritten.** `app/orchestrator.py:501` discovers every destination container, rather than using migration-owned profile mappings. `CookiesImporter.import_cookies` (`src/chromium_cookies_importer.py:457`) flattens all source DBs into one list without provenance. Lines 511–574 insert/update each cookie in the empty origin context and all discovered containers. Last source row wins for `(originAttributes,name,host,path)`, regardless of the intended account. Unselected source profiles and unrelated Zen containers are included.
5. **A total decryption failure can look successful.** `_decrypt_rows` logs failures but never updates structured skipped/error counters. If every row fails, `import_cookies` returns `read>0, imported=0, skipped=0`, without an error. The orchestrator treats that as a completed cookie step (`app/orchestrator.py:525`). The bridge also marks the worker done after handled step errors (`app/bridge.py:329`).
6. **Cookies are opt-in.** `app/frontend/app.js:69`, `MigrationOptions` (`app/orchestrator.py:90`), and `app/bridge.py:629` default cookie transfer off. Visible spaces do not establish that login transfer was requested or succeeded.
7. **Rerunning does not repair existing spaces.** `_merge_with_existing` (`src/zen_sessions_importer.py:348`) skips same-name workspaces and their tabs instead of correcting mappings. The active workspace preference is independently generated in `src/zen_space_importer.py:234`, rather than using the workspace UUID actually written by the sessions importer.

## Additional compatibility defects / risks

- `_SAMESITE_MAP` (`src/chromium_cookies_importer.py:276`) maps Chromium unspecified to 3; the current Gecko interface defines UNSET=256. Check supported builds, then correct the mapping and test browser loading.
- `_strip_host_hash` (line 258) guesses from printability. Chromium cookie DB v24 has an exact SHA256(host) prefix; read the DB version and verify the digest. This heuristic is a confirmed format mismatch; its contribution to this user's logout is unverified.
- The writer assumes `updateTime` exists and expiry uses milliseconds. Those assumptions fit current schema 17, but older databases need explicit support or rejection. The repository's claim that the unit change occurred around Firefox 108 is not established by the upstream source; schema 15→16 is the reliable boundary.
- Chromium partitioned-cookie identity is not selected or translated. Do not flatten it into ordinary cookies. Implement a verified mapping or explicitly skip/report it.
- Copying cookie values cannot transfer localStorage/sessionStorage/IndexedDB; no compatible Chromium→Gecko directory-copy route is established. Some sites may require login even after correct migration.

## Reproduction and existing coverage

`python docs/research/reproduce_arc_zen.py` exercises the actual extractor, session writer, and cookie writer with synthetic temporary data. OS key access is stubbed, not the source/target database path. It never reads live browser files.

Observed: exit 1, six failed invariants:

```
FAIL: Arc space retains source profile 'Profile 1' in writer payload
PASS: session importer succeeds
FAIL: workspace is bound to mapped container 7
FAIL: pinned and open tabs both use mapped container 7
FAIL: personal and work cookie contexts preserve different accounts
FAIL: unrelated default cookie context stays untouched
Cookie counters: read=2, inserted=3, merged=3
FAIL: all cookie decryptions failing returns a machine-readable failure
6 invariant(s) failed; no live browser data accessed.
```

Dependencies were installed in `/tmp/browser2zen-research-venv`, outside the repository. Commands run:

```
/tmp/browser2zen-research-venv/bin/python docs/research/reproduce_arc_zen.py
/tmp/browser2zen-research-venv/bin/python -m pytest
```

Existing suite result: **104 passed, 1 skipped**. Existing Zen writer tests check creation/file shape, not account isolation and restored identity. The reproduction is intentionally red pending implementation; it is not added to the normal passing suite.

## Fix plan, in implementation order

### 1. Preserve source identities and build one shared mapping

- Add optional source-profile identity to `ArcSpace`, `SpaceRecord`, and both serializers. Populate it on local and sync fallback extraction paths. Use the actual resolved profile directory as identity; display labels can come from metadata but must not determine routing.
- Map each selected Arc profile to one migration-owned Zen container, and each source space ID to a workspace UUID. Multiple spaces from one profile use the same container. Distinct profiles stay separate even with duplicate names.
- Persist source/destination identity mapping in a small migration manifest so repeated runs and repairs resolve the same objects. Do not reuse an unrelated container solely because its name matches.
- Missing/ambiguous profile paths must produce an actionable report; never silently fall back to another profile's cookie DB. Filter excluded spaces first, then import only their referenced profiles.

### 2. Apply that mapping everywhere

- Set workspace `containerTabId` and pinned/essential/open tab `userContextId` from the same profile mapping, including consistent placeholder/initial-state metadata where required by the supported Zen format.
- Set the active workspace preference from an actually emitted/reused UUID. Keep `zen-sessions.jsonlz4` as the target for supported current Zen builds.
- Pass explicit source-cookie-DB→target-container routes to the cookie importer. Retain source profile provenance through decryption and writing. Remove Arc's all-container/default-context fan-out. Ensure standalone cookie CLI entry points cannot reintroduce this behavior.
- For a one-profile source, use the same explicit routing rather than relying on every pre-existing destination container. Review Firefox/Safari callers so an Arc API change does not break their paths.

### 3. Make login transfer verifiable

- Preflight the selected destination path, closed-browser state, cookie schema, and encryption support before writing. Use consistent SQLite snapshots/backups, and fail if the required backup cannot be made.
- Validate AES-CBC padding and versioned host digests; reject unsupported formats explicitly. Correct SameSite, version expiry conversions, filter expired cookies, and define/report the existing session-cookie lifetime conversion rather than silently extending sessions.
- Report per-profile discovered/read/decrypted/inserted/updated/expired/unsupported/failed counts without cookie values. Return an error for total decryption failure and a partial result for mixed success. Aggregate step failures into the final migration status and display whether cookie import was enabled.
- Update README/CLAUDE/frontend descriptions: profile-level session sharing, opt-in state transfer, supported schema boundaries, and reauthentication limits.

### 4. Repair existing imports and verify after restart

- Back up the chosen Zen profile and retain existing workspace/tab IDs during repair. Earlier imports have no durable provenance; identify/confirm the imported workspace set before changing existing state, rather than adopting every same-name workspace.
- Patch imported workspace/tab contexts in place, then reimport each source profile into its mapped container. A normal rerun that skips existing names is insufficient.
- Previously mixed cookies cannot reliably be attributed to their source. Prefer a pre-migration backup or fresh dedicated migration containers; do not globally delete existing/default-container cookies to clean up unknown provenance.
- Promote the synthetic reproduction into regression tests. Add two spaces on profile A plus one on B, duplicate labels, excluded profiles, both Cookies path layouts, encrypted fixtures, schema versions, and idempotent repair cases.
- In a disposable supported Zen profile, use a local test site with distinct synthetic accounts: verify pins/open/new tabs read the same intended cookie jar, A spaces share sessions, B remains isolated, and results survive a full restart and rerun. Real website acceptance remains a separate final check.

Recommended behavior preserves Arc's website-session sharing through containers within the user's selected Zen profile. Full separation of browser-wide history, extensions, and settings requires separate Zen browser profiles and is a broader migration mode; do not imply that containers reproduce that isolation.
