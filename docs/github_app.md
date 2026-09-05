# GitHub App for automated reviews

The orchestrator creates pull requests with the normal `gh` authentication. To publish formal
reviews, configure a GitHub App installed on the target repository. For every publication it signs
a short-lived JWT, exchanges it for an installation token, and gives that token only to `gh api`.
Tokens are never written to YAML, persistence, or logs.

## Create and install the App

1. Create a **GitHub App** for the account or organization that owns the repository.
2. Grant **Pull requests: Read and write** repository permission.
3. Generate an App private key and store the PEM outside the repository.
4. Install the App only on repositories the orchestrator should review. Record the App ID and the
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
    app:
      app_id: 123456
      installation_id: 987654
      private_key_path: C:/secure/github-app-private-key.pem
      # Alternative to the path, never use both:
      # private_key_env: GITHUB_APP_PRIVATE_KEY
```

When `app` is configured, `app_id`, `installation_id`, and exactly one private-key source are
required. Never commit the PEM or place it in versioned YAML or `.env.example`.

Without `app`, review publication uses the ambient `gh` authentication. With it, only review
publication receives `GH_TOKEN` containing the installation token; pull request creation retains
the normal `gh` identity.

## Review behavior

- `APPROVED` publishes `APPROVE`, including non-blocking suggestions.
- `CHANGES_REQUESTED` publishes `REQUEST_CHANGES`, including blocking findings.
- `NEEDS_HUMAN` publishes a neutral comment without claiming an approval decision.

Installation tokens are cached only in memory and refreshed before expiry. Token, network, or
GitHub errors are recorded and logged but never mutate the structured reviewer decision or its
terminal workflow status.
