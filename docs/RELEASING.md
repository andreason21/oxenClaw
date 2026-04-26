# Releasing sampyClaw

End-to-end runbook: from green main to a published version on PyPI
and a Windows `.msi` attached to a GitHub Release.

## What gets published per release

| Artifact | Where | How |
|---|---|---|
| `sampyclaw-X.Y.Z-py3-none-any.whl` | PyPI + GitHub Release | trusted publishing (OIDC) + `softprops/action-gh-release` |
| `sampyclaw-X.Y.Z.tar.gz` (sdist) | PyPI + GitHub Release | same |
| `sampyclaw_X.Y.Z_x64_en-US.msi` | GitHub Release | `cargo tauri build --bundles msi nsis` on `windows-latest` after `choco install wixtoolset --version=3.11.2`, optional signtool |
| `sampyClaw_X.Y.Z_x64-setup.exe` (NSIS) | GitHub Release | same build step, NSIS bundle |
| `sampyclaw_X.Y.Z_amd64_ubuntu22.04.deb` | GitHub Release | `cargo tauri build --bundles deb` on `ubuntu-22.04` |
| `sampyclaw_X.Y.Z_amd64_ubuntu24.04.deb` | GitHub Release | same on `ubuntu-24.04` |
| `sampyclaw_X.Y.Z_amd64_*.AppImage` | GitHub Release | `cargo tauri build --bundles appimage` |
| `*.msi.sig` / `*.exe.sig` / `*.AppImage.sig` | GitHub Release | Tauri updater Ed25519 signature, when `TAURI_SIGNING_PRIVATE_KEY` is set |
| `latest.json` (auto-updater manifest) | GitHub Release | derived in CI from the .msi (Windows) + 24.04 AppImage (Linux) — both signed |
| winget manifest | microsoft/winget-pkgs PR | `vedantmgoyal9/winget-releaser` (when `WINGET_TOKEN` secret set) |
| `SHA256SUMS.txt` | GitHub Release | `sha256sum` over every artifact |

The whole pipeline runs from `.github/workflows/release.yml` and
triggers on `v*` tag push (or manual `workflow_dispatch`). End users
who already installed the desktop app **don't need to download
anything** — the in-app updater polls `latest.json` and applies the
new version on next launch.

## One-time setup

### PyPI trusted publishing

PyPI's OIDC trusted-publishing flow lets the workflow upload without
storing an API token in GitHub secrets. Configure it once on the
PyPI project page:

- **Project**: `sampyclaw` (reserve the name on PyPI first if the
  repo is being released for the first time).
- **Owner**: `andreason21`
- **Repository name**: `sampyClaw`
- **Workflow name**: `release.yml`
- **Environment name**: `pypi`

The release workflow's `pypi-publish` job is gated on the `pypi`
environment, which both makes the OIDC mapping work and lets you add
a manual approval step on the environment if you want a human
checkpoint before every PyPI upload.

### Tauri updater signing (recommended for desktop releases)

The auto-updater verifies every downloaded bundle against an Ed25519
signature before applying it. Generate the keypair once locally:

```bash
cd desktop
cargo tauri signer generate -w ./tauri.key
# prints PUBLIC KEY (~ 60 chars) — paste into tauri.conf.json:
#   "plugins.updater.pubkey": "<PUBLIC>"
# saves PRIVATE KEY to ./tauri.key (NEVER commit)
```

Add the *contents of `tauri.key`* (not the path) as a GitHub secret:

- `TAURI_SIGNING_PRIVATE_KEY` — paste the file contents
- `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` — the passphrase you set during generate

The release workflow's `windows-build` step picks them up
automatically. Skipping these secrets keeps the build green but the
auto-updater rejects the unsigned bundle (so users get no auto
updates — they'd need to download the new MSI manually).

### winget-pkgs submission (optional)

For users to run `winget install sampyClaw.sampyClaw`, the workflow
opens a PR against [`microsoft/winget-pkgs`](https://github.com/microsoft/winget-pkgs)
on every stable release. Microsoft moderators auto-merge most PRs in
under an hour.

Setup:

1. Create a GitHub PAT (classic) with `public_repo` scope.
2. Add it as repo secret `WINGET_TOKEN`.
3. Fork `microsoft/winget-pkgs` to your account (one-time, the action
   pushes its branch there before opening the PR).

Without `WINGET_TOKEN` set, the `winget-submit` job is skipped — no
failure. Pre-release tags (`v0.2.0-rc.1`) are also skipped because
winget-pkgs rejects pre-releases.

First-release note: the very first submission needs a manual review +
approval by the Microsoft moderators (it adds your `Publisher` to
their index). Subsequent versions are usually auto-merged.

### Windows code-signing (optional)

Without signing, the `.msi` still installs but Windows SmartScreen
shows a "Microsoft Defender prevented an unrecognised app" prompt.
To sign in CI:

1. Export your authenticode `.pfx` and base64-encode it:
   `base64 -w 0 codesign.pfx > codesign.pfx.b64`
2. Add two GitHub repository secrets:
   - `WINDOWS_CERT_PFX` — paste the base64 string from above
   - `WINDOWS_CERT_PASSWORD` — the .pfx password
3. The `windows-build` job's `Optional code-signing` step picks them
   up automatically. Skipping the secrets keeps the artifacts
   unsigned.

For one-off local signing instead of CI: download the `.msi` from the
release page, run

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 \
              /a sampyclaw_*_x64_en-US.msi
```

then re-upload to the release.

## Cutting a release

```bash
# 1. Make sure main is green.
git checkout main && git pull
pytest -q                                  # 1051 pass / 33 skip baseline

# 2. Bump the version everywhere it lives. The script keeps
#    pyproject.toml + Cargo.toml + tauri.conf.json in sync.
python scripts/bump_version.py 0.2.0
python scripts/bump_version.py --check     # sanity

# 3. Commit + tag + push.
git commit -am "Release 0.2.0"
git tag v0.2.0
git push origin main v0.2.0
```

That last `git push --tags` is what kicks off `release.yml`. Within
~10 minutes you'll have:

- a green CI run with 6 jobs (version-check / test / python-build /
  windows-build / pypi-publish / github-release)
- the package on PyPI: `pip install sampyclaw==0.2.0`
- a GitHub Release with all artifacts attached + auto-generated
  changelog from commits since the previous tag

If anything fails partway through, the `concurrency: cancel-in-progress: false`
setting prevents cascading cancels. Re-run the failed job from the
Actions tab once you've fixed the issue. PyPI uploads use
`skip-existing: true` so re-runs after a successful upload are safe.

## Pre-releases

A tag like `v0.2.0-rc.1` is detected by the workflow (the regex
`vMAJOR.MINOR.PATCH(-...)?` accepts it) and the GitHub Release is
marked `prerelease: true`. The workflow **skips PyPI publishing and
winget submission** for pre-releases — both are reserved for stable
tags only, since:

- PyPI: trusted publishing OIDC claims must match the configured
  publisher exactly; without setup the rc upload fails with
  `invalid-publisher`. Configure trusted publishing once before your
  first stable release, then prereleases can be re-enabled by removing
  the `if: !contains(github.ref_name, '-')` gate on the `pypi-publish`
  job.
- winget-pkgs: the registry rejects pre-release versions outright.

GitHub Release attachments (wheel, sdist, .msi/.exe, .deb, .AppImage,
checksums, signatures, latest.json) **are** produced for prereleases
so the desktop app can still test auto-update flow against rc tags.

## Yanking a bad release

```bash
# PyPI: yank the version (still installable if pinned, but pip's
# resolver won't pick it for any unpinned install).
twine upload --skip-existing --repository pypi dist/*   # never; just an example
# Use the PyPI web UI: Release management → Yank.

# GitHub: delete the release + the tag if the binaries shouldn't be
# circulated.
gh release delete v0.2.0 --yes
git push --delete origin v0.2.0
git tag -d v0.2.0
```

For the desktop `.msi`, since it's already on user machines after a
download, yanking the GitHub Release doesn't recall installed copies.
If a critical security fix needs distribution, push `v0.2.1` quickly
and announce in the release notes that `v0.2.0` should be uninstalled.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `version-check` fails: "pyproject.toml version X != tag Y" | Forgot `python scripts/bump_version.py` before tagging. Reset the tag, bump, re-tag. |
| `pypi-publish` fails: "OIDC token not granted" | The `pypi` environment isn't configured on PyPI's trusted publishers, or the workflow filename / job name doesn't match what's on PyPI's side. |
| `windows-build` fails on `cargo tauri build` | Tauri 2.x ABI break; check `desktop/src-tauri/Cargo.toml` against the current Tauri release. |
| `.msi` installs but SmartScreen warns "unrecognised app" | Code-signing didn't run (no `WINDOWS_CERT_PFX` secret) or cert is untrusted. Check signtool output in the build log. |
| Release published but no `.msi` attached | WiX 3.11 didn't install on the runner. Check the `Install WiX 3.11 toolset` step output, then the `Verify .msi was produced` step which now hard-fails the job instead of letting it pass with NSIS-only. |
| CI runs forever / GitHub auto-cancels | Two tags pushed in quick succession; `concurrency: cancel-in-progress: false` keeps both runs alive. Wait for the first to finish or cancel manually. |
