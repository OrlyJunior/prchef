# GitHub Apps for planning, implementation, and review

The orchestrator can use separate GitHub App installations for task discovery, implementation, and review. It signs
a short-lived JWT and exchanges it for an installation token kept only in memory. Tokens, JWTs,
and private key contents are never written to YAML, persistence, or logs.

## Create and install the Apps

1. Create a **GitHub App** for the account or organization that owns the repository.
2. Grant the planner App **Issues: Read-only**. Grant the implementer App **Contents: Read and write** and **Pull requests: Read and write**.
   Add **Workflows: Read and write** only if the intended repository changes require it. Grant the
   reviewer App only **Pull requests: Read and write**.
3. Generate an App private key and store the PEM outside the repository.
4. Install each App only on repositories the orchestrator should operate on. Record the App ID and the
   installation ID (the latter is visible in the installation settings URL or App installations API).

Use a distinct App identity when GitHub disallows a PR author from approving or requesting changes
on its own pull request. Repository rules and the App's permissions still apply.

## Configure credentials

Use exactly one key source. An absolute, protected `private_key_path` is preferred. An environment
variable supports secret managers; literal `\\n` sequences are converted to newlines.

```yaml
integrations:
  github:
    command: [gh]
    timeout_seconds: 120
    # Reviewer identity. `app` is the established, backwards-compatible key.
    app:
      app_id: 123456
      installation_id: 987654
      private_key_path: C:/secure/github-app-private-key.pem
      # Alternative to the path, never use both:
      # private_key_env: GITHUB_APP_PRIVATE_KEY
    # Implementer identity. It may be a different installation or the same App.
    implementer_app:
      app_id: 654321
      installation_id: 456789
      private_key_env: IMPLEMENTER_GITHUB_APP_PRIVATE_KEY
    # Planner identity for GitHub issue and label reads.
    planner_app:
      app_id: 112233
      installation_id: 445566
      private_key_env: PLANNER_GITHUB_APP_PRIVATE_KEY
```

When an App is configured, `app_id`, `installation_id`, and exactly one private-key source are
required. `app` is the reviewer identity; `implementer_app` and `planner_app` are independent. Never commit the PEM
or place it in versioned YAML or `.env.example`. Optionally configure `bot_name` and `bot_user_id`
together for known commit attribution; otherwise the orchestrator resolves the App bot identity.

The standard orchestrator installation already includes the PyJWT cryptography support required
to sign GitHub App JWTs. No separate `pip install "PyJWT[crypto]"` step is needed.

Without `implementer_app`, commits, pushes, and PR creation retain ambient Git and `gh`
authentication. With it, `gh pr create`, `gh pr view`, and PR assignment receive `GH_TOKEN` only
in their subprocess environment. The created PR is assigned to the same resolved implementer bot
identity used for commit attribution; assignment failures are logged and do not fail an otherwise
successful workflow. HTTPS pushes receive a process-local `http.extraHeader`; SSH and unsupported
transports safely retain ambient push authentication. Reviewer publication independently uses the
reviewer `app` token and never assigns that identity to a PR.

With `planner_app`, issue lookup, open-issue browsing, and the complete label catalog use its
installation token through the configured `api_url`. API and authentication failures surface to
the caller without switching to the personal CLI identity. Without it, these reads retain their
existing `gh` commands. This identity does not change planner-agent file or Git permissions and
does not post the planner spec as a comment.

For explicit CLI issue reads, `--repository` identifies a GitHub origin. If multiple configured
checkouts share that origin and resolve to different Planner App settings, the CLI reports an
ambiguity instead of choosing either identity. Identical settings across those checkouts are safe
to use; checkouts without a Planner App continue to use `gh` when the origin is unambiguous.

The Textual TUI exposes all three roles under **GitHub Apps** (or `c`). It writes only App IDs,
installation IDs, and private-key references to the selected repository's
`repositories.<key>` entry in the resolved `orchestrator.yml`; it never accepts or writes private
key contents or tokens. Clearing an identity inherited from `defaults` writes an explicit `null`
for that repository, so the identity remains disabled there while other repositories keep the default.

## Review behavior

- `APPROVED` publishes `APPROVE`, including non-blocking suggestions.
- `CHANGES_REQUESTED` publishes `REQUEST_CHANGES`, including blocking findings.
- `NEEDS_HUMAN` publishes a neutral comment without claiming an approval decision.

Installation tokens are cached only in memory and refreshed before expiry. Token, network, or
GitHub errors are recorded and logged but never mutate the structured reviewer decision or its
terminal workflow status.
