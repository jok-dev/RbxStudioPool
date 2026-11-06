# Shipping signed binaries on GitHub Releases

Research for issue #4 (map #1, decision #19). Researched 2026-11-06.

Scope: a Rust workspace that ships the CLI `rsp`, a daemon, and a tray/menu-bar app on
Windows and macOS, built in GitHub Actions on `windows-latest` and `macos-latest`.

## Summary

- **dist (formerly cargo-dist) solves the easy half and none of the macOS half.** It
  generates a tag-triggered `release.yml`, builds per-target archives, makes the GitHub
  Release, and can Authenticode-sign Windows x64 builds through SSL.com eSigner or Azure
  Artifact Signing. For macOS it only runs `codesign`. It does not notarize, does not build
  universal binaries, and has no `.app` bundle support. Our menu-bar app needs all three,
  so the macOS release job would be hand-written anyway.
- **Recommendation: write the GitHub Actions release workflow by hand.** It is two jobs
  plus a release job. The macOS job builds both arches, merges them with `lipo`, assembles
  and signs the `.app`, notarizes with `notarytool`, and staples. The Windows job signs with
  `azure/artifact-signing-action` or SignTool. Treat dist as a reference for workflow
  structure rather than a dependency.
- **macOS costs 99 USD a year (Apple Developer Program).** That buys the Developer ID
  Application certificate and the notary service. Without them, a browser-downloaded
  `.app` is blocked. Since macOS Sequoia the only way past the block is
  System Settings > Privacy & Security > Open Anyway. A CLI fetched with `curl` and
  unpacked with `tar` is never quarantined, so Gatekeeper does not check it.
- **Windows signing does not remove SmartScreen warnings on day one.** Microsoft states
  that EV certificates no longer bypass SmartScreen. Even signed files show the
  "unrecognized app" prompt until reputation builds up. What signing gives you is a
  verified publisher name, reputation that carries over between releases, and passing
  Smart App Control. Azure Artifact Signing starts at $9.99/month, but **individual
  developers must be in the US or Canada.** Elsewhere, an individual needs a traditional
  certificate held in a cloud HSM (for example SSL.com eSigner) or must ship unsigned.

## 1. dist vs hand-rolled GitHub Actions

### What dist does

- dist's pipeline is plan → build → host → publish → announce. Its key idea is that "it
  generates its own CI scripts". `dist init` writes `release.yml`, which fires on a git tag,
  builds every target, and creates or updates the GitHub Release.
  [dist book: introduction](https://axodotdev.github.io/cargo-dist/book/introduction.html)
- The supported targets are listed per triple, including `x86_64-apple-darwin`,
  `aarch64-apple-darwin`, `x86_64-pc-windows-msvc` and `aarch64-pc-windows-msvc`. There is no
  universal macOS target.
  [dist config reference, `targets`](https://github.com/axodotdev/cargo-dist/blob/main/book/src/reference/config.md)
  Universal binaries have been an open feature request since 2023:
  [axodotdev/cargo-dist#77](https://github.com/axodotdev/cargo-dist/issues/77).
- The project is active. v0.34.0 shipped on 2026-11-01, after 0.33.x in September and
  October 2026, and the repo is not archived. Checked with
  `gh release list -R axodotdev/cargo-dist`.

### dist's Windows signing

- dist supports "SSL.com's eSigner cloud signing service or Azure Artifact Signing". It
  signs EXEs, the PowerShell installer and MSIs.
  [dist book: Windows signing](https://axodotdev.github.io/cargo-dist/book/supplychain-security/signing/windows.html)
  - SSL.com: set `ssldotcom-windows-sign = "prod"` and add the secrets `SSLDOTCOM_USERNAME`,
    `SSLDOTCOM_PASSWORD`, `SSLDOTCOM_TOTP_SECRET` and `SSLDOTCOM_CREDENTIAL_ID`.
  - Azure: set `[workspace.metadata.dist.azure-windows-sign]` with `endpoint`,
    `account-name` and `certificate-profile-name`. Login uses GitHub OIDC with
    `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID`, which identify the
    principal and are not credentials. Jobs run in a `release` environment.
  - Limitation: "Other Windows architectures are not supported", meaning x86_64 only.
  - Both options are marked `[unstable]` in the config source
    ([`config/v0.rs`](https://github.com/axodotdev/cargo-dist/blob/main/cargo-dist/src/config/v0.rs)).

### dist's macOS signing

- The config has `macos-sign = true`. The book has no page for it; the behaviour below
  comes from the source
  ([`sign/macos.rs`](https://docs.rs/crate/cargo-dist/latest/source/src/sign/macos.rs)).
  - It reads `CODESIGN_IDENTITY`, `CODESIGN_CERTIFICATE` (a base64 .p12),
    `CODESIGN_CERTIFICATE_PASSWORD` and an optional `CODESIGN_OPTIONS`.
  - It imports the certificate into a temporary keychain and runs
    `codesign --sign <id> [--options <CODESIGN_OPTIONS>] --keychain <kc> <file>`.
  - It does not pass `--timestamp`. `--options runtime` is passed only if you set
    `CODESIGN_OPTIONS=runtime`.
  - There is no notarization. The module doc says: "In the future, this module will also
    support notarization."
  - It has no `.app` or bundle handling and signs each file it is given.
- Users have reported notarization failing with "The executable does not have the hardened
  runtime enabled", and needing `--timestamp` added:
  [axodotdev/cargo-dist#1534](https://github.com/axodotdev/cargo-dist/issues/1534) (open).
  A macOS `.pkg` installer is still a feature request:
  [#469](https://github.com/axodotdev/cargo-dist/issues/469).

### Verdict

| Need | dist | Hand-rolled |
| --- | --- | --- |
| Tag-triggered release, upload to GitHub Release | yes | ~30 lines (`gh release create`) |
| Windows Authenticode, x64 | yes (unstable) | `azure/artifact-signing-action` or SignTool |
| macOS universal binary | no (#77) | `lipo -create` |
| macOS Developer ID + hardened runtime + timestamp | partial (env-var workaround) | `codesign -o runtime --timestamp` |
| Notarization + stapling | no | `notarytool submit --wait` + `stapler staple` |
| Menu-bar `.app` with `LSUIElement` | no | write `Info.plist` + directory layout |
| Shell/PowerShell installers, Homebrew, updater | yes | not needed at first |

We would have to replace the parts of dist that matter most to us. On top of that,
editing a `release.yml` that dist generated means `dist init` regenerates over the
changes, or you turn on dist's custom-job hooks. Hand-rolling is the smaller total.

## 2. macOS

### Universal vs per-arch

- Apple: "A universal binary runs natively on both Apple silicon and Intel-based Mac
  computers, because it contains executable code for both architectures." Apple tells you
  to build each arch separately and merge them with
  `lipo -create -output universal_app x86_app arm_app`.
  [Building a universal macOS binary](https://developer.apple.com/documentation/apple-silicon/building-a-universal-macos-binary)
- For Rust:
  `cargo build --release --target aarch64-apple-darwin --target x86_64-apple-darwin`,
  then `lipo` per executable. Both targets build on the arm64 `macos-latest` runner.
- **Recommendation: universal.** A menu-bar `.app` should be one download that works on
  every Mac. That also means one notarization submission and one stapled bundle. Per-arch
  archives cost a second notarization and make users choose a file. The universal binary
  is roughly twice the size, which is fine at our scale. The app supports macOS 11+ only,
  matching the arm64 minimum.

### Developer ID signing

- **Cost:** "The Apple Developer Program is 99 USD per membership year." You can enroll as
  an individual (legal name, 2FA Apple Account). Organizations need a D-U-N-S number.
  [Apple: enroll](https://developer.apple.com/programs/enroll/)
- **Certificate:** sign Mach-O files, bundles, apps, command-line tools and disk images
  with a **Developer ID Application** certificate. Installer packages use a
  **Developer ID Installer** certificate.
  [Resolving common notarization issues](https://developer.apple.com/documentation/security/resolving-common-notarization-issues)
- **Notarization requirements:** every executable is signed, the Hardened Runtime is on
  (`--options=runtime`), the signature has a secure timestamp (`--timestamp`, served by
  `timestamp.apple.com`), the binaries link against the 10.9+ SDK, and there is no
  `get-task-allow` entitlement.
  [Notarizing macOS software before distribution](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution),
  [Resolving common notarization issues](https://developer.apple.com/documentation/security/resolving-common-notarization-issues)
- **Signing rules from Apple DTS:**
  ([Creating Distribution-Signed Code for Mac](https://developer.apple.com/forums/thread/701514),
  which defers to the official
  [Creating distribution-signed code for macOS](https://developer.apple.com/documentation/xcode/creating-distribution-signed-code-for-the-mac))
  - "Sign code from the inside out."
  - "do not pass the `--deep` option to `codesign`."
  - Give a non-bundled tool an identifier with
    `-i com.example.tool`, for example
    `codesign -s "Developer ID Application" -f --timestamp -o runtime -i <id> <file>`.
  - Use `ditto`, not `cp`, to copy code.
- **CI secrets:** GitHub's own guide stores the `.p12` base64-encoded as a secret
  (`BUILD_CERTIFICATE_BASE64`), along with `P12_PASSWORD` and a random `KEYCHAIN_PASSWORD`.
  The job then runs `security create-keychain`, `security import ... -f pkcs12`,
  `security set-key-partition-list -S apple-tool:,apple:`, and `security list-keychain`.
  [GitHub Docs: Installing an Apple certificate on macOS runners](https://docs.github.com/en/actions/how-tos/deploy/deploy-to-third-party-platforms/sign-xcode-applications)
  (dist does the same thing in `sign/macos.rs`).

### Notarization with notarytool

- `altool` stopped working on 2023-11-01. Use `xcrun notarytool submit <file> --wait`. The
  notary service accepts "disk images (UDIF format), signed flat installer packages, and
  ZIP archives", so a bare binary has to be zipped for submission. Credentials are an
  Apple ID + app-specific password + team ID, or a keychain profile made with
  `notarytool store-credentials`.
  [Customizing the notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)
  notarytool also takes an App Store Connect API key (`--key`, `--key-id`, `--issuer`).
  That is the better fit for CI, because the key is not tied to a person's Apple ID
  password. *This comes from `notarytool --help`, not a doc page fetched here; check it
  before use.*
- Processing is usually under 5 minutes and under 15 minutes for 98% of submissions.
  Apple asks you to "Limit notarizations to 75 per day". Always read the
  `notarytool log` output, even when a submission is accepted.
  [Customizing the notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)
- The service issues tickets for the top-level submission and each nested item. If you
  nest containers, "only notarize the outermost container".
  [Apple DTS: Packaging Mac Software for Distribution](https://developer.apple.com/forums/thread/701581)

### Stapling

- `stapler` works only with "UDIF disk images, signed 'flat' installer packages, and
  certain code signed executable bundles such as '.app'".
  [stapler(1)](https://keith.github.io/xcode-man-pages/stapler.1.html) (mirror of the
  Xcode man page)
  **A bare CLI Mach-O and a `.zip` cannot be stapled.**
- Without a stapled ticket, macOS looks the cdhash up on Apple's servers at first launch.
  If the Mac is offline, that lookup fails and Gatekeeper may block the launch.
  [Apple DTS: The Pros and Cons of Stapling](https://developer.apple.com/forums/thread/720093),
  [Packaging Mac Software for Distribution](https://developer.apple.com/forums/thread/701581)
  ("Stapling is recommended but not mandatory.")

### What Gatekeeper does without signing or notarization

- Apple: notarization has been required by default since Catalina. When an app can't be
  verified, "macOS can't verify that the app is free of malware". The user's only option
  is System Settings > Privacy & Security > **Open Anyway**.
  [Apple Support 102445](https://support.apple.com/en-us/102445)
- In macOS Sequoia (announced 2024-08-06), "users will no longer be able to Control-click
  to override Gatekeeper when opening software that isn't signed correctly or notarized".
  [Apple Developer News](https://developer.apple.com/news/?id=saqachfa)
- **Quarantine is what triggers the check:**
  [Apple DTS: Resolving Trusted Execution Problems](https://developer.apple.com/forums/thread/706442)
  - Safari and other user-level apps set `com.apple.quarantine` on downloads, and Archive
    Utility carries it over to the files it unpacks. "running a quarantined app always
    invokes Gatekeeper."
  - "Unix-y networking tools, like `curl` and `scp`, don't quarantine the files they
    download. Unix-y unarchiving tools, like `tar` and `unzip`, don't propagate
    quarantine."
- **What this means for us:**
  - **CLI via a `curl | sh` installer** (the dist model): the binary is never quarantined,
    so Gatekeeper does not check it. That is why unsigned CLIs installed this way work.
    The binary still has to be signed, even ad-hoc, because arm64 macOS refuses to run
    unsigned arm64 code. The Rust/Apple linker signs ad-hoc by default.
  - **CLI downloaded in a browser and double-clicked or run**: it is quarantined.
    Unnotarized, it is blocked. Notarized, it needs the online ticket lookup, because a
    bare binary can't be stapled.
  - **The `.app` in a browser-downloaded zip or dmg** is quarantined, so it must be signed,
    notarized and preferably stapled. Otherwise every user goes through Open Anyway.

### Packaging the menu-bar app next to the CLI

- `LSUIElement` is "A Boolean value indicating whether the app is an agent app that runs
  in the background and doesn't appear in the Dock". In Xcode it is called
  "Application is agent (UIElement)".
  [LSUIElement](https://developer.apple.com/documentation/bundleresources/information-property-list/lsuielement)
- Bundle layout: the main executable goes in `Contents/MacOS/`. A "helper tool" goes in
  `Contents/MacOS/` or `Contents/Helpers/`. Apple warns that "If you put content in the
  wrong location, you may encounter hard-to-debug code signing and distribution problems",
  which can surface only at notarization.
  [Placing content in a bundle](https://developer.apple.com/documentation/bundleresources/placing-content-in-a-bundle)
- **Proposed layout.** Ship one notarized, stapled `.dmg`, or a `.zip` with the stapled
  `.app` inside:

  ```
  RbxStudioPool.app/
    Contents/
      Info.plist            # CFBundleIdentifier, CFBundleExecutable, LSUIElement=true, LSMinimumSystemVersion=11.0
      MacOS/rsp-tray        # universal, main executable
      Helpers/rsp           # universal CLI, signed with its own -i identifier, signed first
      Helpers/rsp-daemon    # universal daemon, signed first
      Resources/AppIcon.icns
  ```

  Sign the helpers first, then the `.app` (inside-out, no `--deep`). Then build the dmg,
  sign the dmg, notarize the dmg, and staple the dmg. Users get `rsp` on their PATH from a
  symlink into the bundle (the tray app or an install command creates it), or from a
  separate `rsp-<ver>-macos-universal.tar.gz` that holds the same signed CLI for
  `curl`/Homebrew installs. Because `stapler` refuses bare binaries, that tarball relies on
  the online ticket check if it is ever quarantined.
- `cargo-bundle` exists for building `.app` directories, but it is third-party. With three
  binaries and one plist, a small script is simpler and easier to read.

## 3. Windows

### SmartScreen with and without signing

From Microsoft, [SmartScreen reputation for Windows app developers](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation)
(updated 2026-08):

- SmartScreen weighs **publisher reputation** (the signing certificate) and **file hash
  reputation**.
- **Unsigned:** "'Windows protected your PC'; User must choose 'Run anyway' before the app
  can run. Enterprise policy can prevent continuation entirely." An unsigned file's
  reputation "must build for each new version ... starting with zero reputation".
- **Signed (OV/EV):** the file is still "flagged as unrecognized until reputation
  accumulates", but the "verified publisher name is displayed". Reputation can carry over
  to new files signed by the same certificate.
- "**EV certificates no longer bypass SmartScreen** ... Paying a premium for EV solely to
  avoid SmartScreen warnings is no longer justified."
- Clearing the warning "can take several weeks and hundreds of clean installs". You
  cannot submit a file to speed this up for consumer endpoints.
- The Microsoft Store avoids the warning completely.

**Smart App Control** on Windows 11 is stricter. In enforcement mode, "unknown, unsigned
code are blocked by default". A file is allowed if Microsoft's app intelligence recognizes
it, or if it is "signed with a certificate issued by a certificate authority (CA) within
the Trusted Root Program". Its signature checks apply to all executables, not only
downloads. [Smart App Control](https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/overview).
So on machines that have Smart App Control on, **an unsigned `rsp.exe` can't run at all**.
This is the strongest argument for signing on Windows.

### Option A: Azure Artifact Signing (formerly Trusted Signing)

- Microsoft-managed signing. Keys are held in FIPS 140-3 Level 3 HSMs and certificate
  lifecycle is automatic. [Overview](https://learn.microsoft.com/en-us/azure/artifact-signing/overview)
- **Eligibility:** "Public Trust certificates are available to organizations in the United
  States, Canada, the European Union, the United Kingdom, Australia, New Zealand, Japan,
  South Korea, Singapore, Switzerland, Norway, and Israel. **Individual developers must be
  located in the United States or Canada.**"
  [Quickstart, prerequisites](https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart)
  - An individual needs an Azure billing account of type Individual whose legal name and
    address match a government ID.
  - Identity is verified through AU10TIX and Microsoft Verified ID.
  - Organization validation takes 1–20 business days.
- **Requirements:** a paid subscription; "doesn't support free, trial, or sponsored Azure
  subscriptions". The certificate subject (CN) is always the validated legal name, with no
  custom CN. No EV certificates are issued.
  [FAQ](https://learn.microsoft.com/en-us/azure/artifact-signing/faq)
- **Cost:** "Starts at $9.99/month"
  ([SmartScreen doc](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation)).
  - Basic includes 5,000 signatures/month and 1 certificate profile of each type.
    Premium includes 100,000 signatures/month and 10 profiles.
    [Pricing page](https://azure.microsoft.com/en-us/pricing/details/artifact-signing/);
    the dollar figures load dynamically and could not be read.
  - Billing is not pro-rated.
- **Certificates last 3 days, so timestamping is mandatory:** `/tr
  http://timestamp.acs.microsoft.com /td SHA256`.
  [Signing integrations](https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations)
- **CI:** use the GitHub Action
  [`azure/artifact-signing-action`](https://github.com/azure/artifact-signing-action), or
  SignTool + `Azure.CodeSigning.Dlib.dll` + `metadata.json`. Auth goes through
  `DefaultAzureCredential`, which supports GitHub OIDC with no long-lived secret.
  [Signing integrations](https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations)

### Option B: a traditional OV or EV certificate from a CA

- Since 2023-06-01 the CA/Browser Forum Code Signing Baseline Requirements say "CAs SHALL
  ensure that the Subscriber's Private Key is generated, stored, and used in a suitable
  Hardware Crypto Module". Certificates issued on or after 2026-03-01 are capped at
  **460 days** validity.
  [CA/B Forum CSBR](https://cabforum.org/working-groups/code-signing/requirements/)
  You can't keep a `.pfx` in a GitHub secret any more. CI needs the CA's cloud signing
  service or your own cloud HSM.
- Example vendor, SSL.com:
  - EV costs $349 for 1 year, falling to $149/yr on a 5-year term. Cloud HSM attestation
    is extra.
    [SSL.com EV pricing](https://www.ssl.com/certificates/ev-code-signing/buy/)
  - eSigner cloud signing is a separate subscription; the price is not on that page.
  - dist supports eSigner natively (see §1).
- Given that EV no longer helps with SmartScreen, an **OV** certificate is the
  traditional choice when Artifact Signing isn't available.

### Option C: ship unsigned at first

- Users click "More info → Run anyway" on every new version, and the app is blocked
  outright where Smart App Control is in enforcement mode or policy forbids it. This is
  acceptable for a small early-adopter group and not beyond that.

## 4. Proposed release workflow (for the decision in #19)

1. Trigger on a `v*` tag push. Tag the workspace version.
2. **macos job** (`macos-latest`):
   1. Add the `x86_64-apple-darwin` target and build all three binaries for both arches.
   2. `lipo` each binary into a universal one.
   3. Import the Developer ID cert into a temporary keychain.
   4. Sign helpers, then `rsp-tray`, then the `.app` (`--options runtime --timestamp`,
      inside-out).
   5. Build and sign the `.dmg`, then `notarytool submit --wait` with an App Store Connect
      API key.
   6. `stapler staple` the dmg.
   7. Also produce a `rsp-macos-universal.tar.gz` of the signed CLI.
3. **windows job** (`windows-latest`):
   1. Build `x86_64-pc-windows-msvc`, adding `aarch64` later if wanted.
   2. Sign each `.exe` with `azure/artifact-signing-action` (or the chosen CA's tool),
      timestamped.
   3. Zip the result.
4. **release job:** `gh release create $TAG --generate-notes` and upload the artifacts plus
   SHA-256 checksums. Optionally add GitHub artifact attestations
   (`actions/attest-build-provenance`).

**Secrets this needs:**

- `MACOS_CERT_P12_BASE64`, `MACOS_CERT_PASSWORD` and `KEYCHAIN_PASSWORD`.
- The ASC API key `.p8` plus its key ID and issuer ID.
- Azure OIDC IDs, or the CA's cloud-signing credentials.
- Store them in a protected `release` environment so that only tag builds can read them.

**Running costs:**

- Apple: 99 USD a year.
- Windows: Artifact Signing from $9.99/month if eligible (US/Canada individual, or an
  organization in a listed country). Otherwise a CA OV certificate plus a cloud-signing
  subscription, typically a few hundred USD a year.

## Open questions for the decision

- **Where is the publisher based, and does it sign as an individual or an
  organization?** This decides whether Artifact Signing is available.
- **Is "unsigned on Windows, signed and notarized on macOS" acceptable for v0?** The macOS
  side is the hard requirement for the `.app`. Windows unsigned still runs unless Smart
  App Control or policy blocks it.
- **Should `rsp` reach macOS users only inside the `.app` (symlinked onto PATH), or also as
  a standalone tarball / Homebrew tap?**
