# GitHub Apps for implementation and review

The orchestrator can use separate GitHub App installations for implementation and review. It signs
a short-lived JWT and exchanges it for an installation token kept only in memory. Tokens, JWTs,
and private key contents are never written to YAML, persistence, or logs.

## Create and install the Apps

1. Create a **GitHub App** for the account or organization that owns the repository.
2. Grant the implementer App **Contents: Read and write** and **Pull requests: Read and write**.
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
```

When either App is configured, `app_id`, `installation_id`, and exactly one private-key source are
required. `app` is the reviewer identity; `implementer_app` is independent. Never commit the PEM
or place it in versioned YAML or `.env.example`. Optionally configure `bot_name` and `bot_user_id`
together for known commit attribution; otherwise the orchestrator resolves the App bot identity.

The standard orchestrator installation already includes the PyJWT cryptography support required
to sign GitHub App JWTs. No separate `pip install "PyJWT[crypto]"` step is needed.

Without `implementer_app`, commits, pushes, and PR creation retain ambient Git and `gh`
authentication. With it, `gh pr create` and `gh pr view` receive `GH_TOKEN` only in their
subprocess environment, and commit author/committer metadata uses the App bot identity. HTTPS
pushes receive a process-local `http.extraHeader`; SSH and unsupported transports safely retain
ambient push authentication. Reviewer publication independently uses the reviewer `app` token.

The Textual TUI exposes the two roles under **GitHub Apps** (or `c`). It writes only App IDs,
installation IDs, and private-key references to the selected repository's `.orchestrator.yml`; it
never accepts or writes private key contents or tokens.

## Review behavior

- `APPROVED` publishes `APPROVE`, including non-blocking suggestions.
- `CHANGES_REQUESTED` publishes `REQUEST_CHANGES`, including blocking findings.
- `NEEDS_HUMAN` publishes a neutral comment without claiming an approval decision.

Installation tokens are cached only in memory and refreshed before expiry. Token, network, or
GitHub errors are recorded and logged but never mutate the structured reviewer decision or its
terminal workflow status.
