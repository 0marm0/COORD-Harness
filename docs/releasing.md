# Releasing

The source repository is public. A tagged version, package, signed binary, or
store submission is still a deliberate distribution event, not an inference
from a test run. CI validates a candidate; it never publishes an artifact or
grants rights that the maintainer has not already authorized.

## Authority and candidate identity

1. Confirm every contributor and source owner authorizes publication and MIT
   relicensing of the included material.
2. Choose a committed candidate ref. The Git index, candidate commit tree, and
   worktree must be byte-identical. Staged-but-uncommitted or unstaged changes
   are not releasable.
3. Keep maintainer-only forbidden vocabulary, signer keys, allowed-signers files,
   remote receipts, and candidate manifests outside this repository. The public
   gate never embeds or auto-discovers private names.
4. Before any provider mutation, run the opaque history scanner with the same
   maintainer-only vocabulary:

   ```bash
   python tools/privacy_hygiene.py --history \
     --vocabulary /absolute/private/path/forbidden-phrases.txt
   ```

   It reports only digests and object labels. A missing official vocabulary is
   a release blocker even when the public CI baseline passes.

Generate a frozen manifest and a portable receipt template into a dedicated
maintainer directory:

```bash
RELEASE_EVIDENCE=/absolute/private/path/to/release-evidence
python tools/publication_gate.py \
  --candidate-ref HEAD \
  --forbidden-vocabulary /absolute/private/path/forbidden-vocabulary.json \
  --require-gitleaks \
  --local-only \
  --write-candidate-manifest "$RELEASE_EVIDENCE/candidate-manifest.json" \
  --write-external-receipt-template "$RELEASE_EVIDENCE/external-history-receipt.json"
```

The manifest is canonical JSON. Its `manifest_sha256` binds every indexed path,
mode, object ID, byte count, content SHA-256, and the candidate tree. The command
fails on index/worktree drift, a ref/tree mismatch, unmerged entries, symlinks,
submodules, runtime files, opaque or large binaries, archives, SQLite databases,
unsafe image metadata, public/private vocabulary matches, reachable-history
findings, or lifecycle failure.

## Secret scanning

The release path requires [Gitleaks](https://github.com/gitleaks/gitleaks).
The gate runs it twice: once against an exact `git checkout-index` export and
once against all locally reachable history. CI pins its installation and invokes
`--require-gitleaks`.

When the binary is unavailable, the gate runs a conservative built-in scan for
common token and private-key formats. The report says
`engine=builtin-fallback` and `assurance=FALLBACK`. This is safe for an early
local check because findings still fail closed, but it is not sufficient for
release CI or a READY verdict. Do not rename another binary to `gitleaks` or
edit the report.

## External private-remote history receipt

A local clone cannot prove that provider-owned pull-request refs, cached views,
release attachments, forks, or mirrors are absent. Push the exact candidate to
a brand-new private remote, fetch every branch/tag plus provider-owned ref, and
reclone it into an empty directory. In that clean clone:

```bash
set -euo pipefail
PROVIDER_REFS="$RELEASE_EVIDENCE/provider-refs.txt"
PROVIDER_REFS_AFTER="$RELEASE_EVIDENCE/provider-refs-after-fetch.txt"
PROVIDER_COMMITS="$RELEASE_EVIDENCE/provider-commits.txt"
PROVIDER_OBJECTS="$RELEASE_EVIDENCE/provider-objects.txt"

git ls-remote --refs origin | LC_ALL=C sort > "$PROVIDER_REFS"
test -s "$PROVIDER_REFS"
git fetch --force --prune origin "+refs/*:refs/remotes/origin/provider/*"
git ls-remote --refs origin | LC_ALL=C sort > "$PROVIDER_REFS_AFTER"
cmp "$PROVIDER_REFS" "$PROVIDER_REFS_AFTER"
while IFS=$'\t' read -r oid ref; do
  mapped="refs/remotes/origin/provider/${ref#refs/}"
  test "$(git rev-parse --verify "$mapped")" = "$oid"
done < "$PROVIDER_REFS"

cut -f1 "$PROVIDER_REFS" | git rev-list --stdin | LC_ALL=C sort -u > "$PROVIDER_COMMITS"
cut -f1 "$PROVIDER_REFS" | git rev-list --objects --stdin | LC_ALL=C sort -u > "$PROVIDER_OBJECTS"
test -s "$PROVIDER_COMMITS"
test -s "$PROVIDER_OBJECTS"
shasum -a 256 < "$PROVIDER_REFS"
shasum -a 256 < "$PROVIDER_COMMITS"
shasum -a 256 < "$PROVIDER_OBJECTS"
gitleaks git . --redact --log-opts=--all
```

The `pipefail`, non-empty checks, explicit provider-ref fetch, stable before/after snapshots, and
tip-by-tip verification are required. Record the line counts of the three evidence files
as `provider_ref_count`, `reachable_commit_count`, and `reachable_object_count`.
Put those counts and digests, the exact source strings from the generated receipt,
the actual Gitleaks name/version/status, the external vocabulary digest, and the
provider review facts into the generated receipt. For a brand-new remote, explicitly verify through the provider UI/API
that server-owned refs, release assets, forks, and mirrors are absent; an empty
category is still a checked category.

Sign the exact receipt bytes with a maintainer SSH signing key:

```bash
ssh-keygen -Y sign \
  -f /absolute/private/path/release_signing_key \
  -n coordharness-release \
  "$RELEASE_EVIDENCE/external-history-receipt.json"
```

The corresponding allowed-signers file is external and uses the same identity
passed to the gate. Re-run the full decision:

```bash
python tools/publication_gate.py \
  --candidate-ref HEAD \
  --forbidden-vocabulary /absolute/private/path/forbidden-vocabulary.json \
  --require-gitleaks \
  --external-history-receipt "$RELEASE_EVIDENCE/external-history-receipt.json" \
  --external-history-signature "$RELEASE_EVIDENCE/external-history-receipt.json.sig" \
  --allowed-signers /absolute/private/path/allowed_signers \
  --signer-identity release-maintainer
```

Only a report with `local_status=PASS`, a cryptographically verified remote
receipt bound to the same commit/tree/manifest, and `release_status=READY` may
advance. Changing either the receipt or candidate invalidates the signature or
binding. READY still does not supply rights or authorization.

## Build, package, and product gates

Before publication:

- Run every Python, browser, publication, documentation, and provenance suite.
- Build both wheel and sdist; install each in a clean virtual environment outside
  the checkout and exercise installed entry points and packaged resources.
- Build and test the actual distributable native schemes:
  `CoordCockpitMac`, `CoordMenuBar`, `CoordCockpitWindow`, and
  `CoordCockpitIOS` (simulator), without signing secrets.
- Review [assets/provenance.json](assets/provenance.json) and
  [third-party-notices.md](third-party-notices.md).
- Run the [friend acceptance](friend-acceptance.md) command on a clean macOS
  account. Keep its automated preflight distinct from the friend’s human visual
  judgment, and use synthetic sample data only.
- Confirm the intended version, changelog, compatibility notes, and
  [feature-status declarations](feature-status.json).
- Obtain explicit maintainer approval for each tag, package, signed binary, or store distribution.

## Versioning and human boundary

Until a stable public release, preview surfaces may change with release notes.
Stable lifecycle changes should remain backward compatible within a minor line
or include a migration and upgrade note. Snapshot endpoints stay API-versioned.

The gates can verify declared bytes, patterns, package behavior, reachable
history, signatures, and exact pinned identity. They cannot grant copyright,
relicensing rights, ethical provenance, or approval to publish. Human review
remains the final authorization boundary.
