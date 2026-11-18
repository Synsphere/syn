# Syn repository agent instructions

## Full-source patch integrity (mandatory)

- Before modifying an existing file, retrieve and inspect its **complete** current
  contents from the precise branch/commit under development. Do not derive
  edits from truncated tool output, partial excerpts, cached snippets, or an
  assumed earlier patch state.
- Record the base commit and SHA/blob identifiers for each edited file. Do not
  silently assume a local development branch equals `master`.
- Generate normal unified Git patches and validate them using
  `git apply --check`, `git apply`, and `git diff --check` against complete
  matching source files. Run focused tests and report their actual results.
- If the real checkout is unavailable, explicitly distinguish a fixture or
  clean-reference verification from a user's working tree. Never label a
  patch as verified against an unavailable checkout.
- Preserve original line endings, existing user settings, secrets, state and
  operator/agent identities; do not invent replacement file bodies or truncate
  unrelated functions to make a patch apply.
- When patch context, base revision or content is uncertain, fail closed and
  report the limitation rather than inventing a context hunk.

## Runtime qualification and safety

- Treat P2P listeners, router mappings and DNS resolution as local evidence,
  never proof of Internet ingress. A remote witness needs the actual negotiated
  transport, authenticated pinned PeerID and fresh challenge.
- Keep operator/agent custody separate from transport test fixtures. Do not
  install new identities or change router/network policy during diagnostics.
- Fail qualification on missing runtime dependencies; do not claim that a
  fallback transport passed the originally requested protocol.
