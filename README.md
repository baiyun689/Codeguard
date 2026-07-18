# Codeguard

AI-powered pull request review with risk-aware multi-agent analysis.

Codeguard receives GitHub pull request events, analyzes the exact code change with a Python review council, and reports structured findings through GitHub Check Runs and pull request comments. A Java Gateway owns webhook verification, job persistence, isolated workspaces, retries, and code-access guardrails.

## Features

- Reviews pull requests for security, behavioral, and maintainability risks.
- Routes changed hunks by risk before running task-scoped specialist reviewers.
- Plans and gathers supporting, counter, and severity evidence before producing a verdict.
- Publishes Check Runs, diff annotations, and high-confidence critical comments to GitHub.
- Verifies webhook signatures and deduplicates jobs by repository, pull request, and commit SHA.
- Persists jobs in H2 and restores unfinished work after a restart.
- Exposes liveness, readiness, and Prometheus metrics endpoints.
- Runs the Python Agent and Java Gateway in one container with Docker Compose.

## How It Works

```text
GitHub pull_request webhook
        |
        v
Java Gateway
  verify signature -> persist/deduplicate -> schedule -> prepare SHA workspace
        |
        v
Python Agent
  diff tasks -> risk routing -> specialist discovery -> evidence -> council verdict
        |
        v
GitHub Check Run, annotations, and pull request comments
```

The Python Agent owns review reasoning and orchestration. The Java Gateway owns deterministic facts and operational guardrails; it does not call an LLM or decide whether a finding is valid.

## Quick Start with Docker Compose

Prerequisites:

- Docker Engine with Docker Compose v2
- A GitHub App installed on the repositories to review
- A publicly reachable HTTPS endpoint for GitHub webhooks
- An API key for the configured LLM provider

Clone the repository, create the deployment configuration, and create the secrets directory:

```bash
git clone https://github.com/baiyun689/codeguard.git
cd codeguard
cp .env.example .env
mkdir -p secrets
```

PowerShell equivalents:

```powershell
git clone https://github.com/baiyun689/codeguard.git
Set-Location codeguard
Copy-Item .env.example .env
New-Item -ItemType Directory -Force secrets | Out-Null
```

Edit `.env` and set at least:

```dotenv
CODEGUARD_WEBHOOK_SECRET=replace-with-a-long-random-secret
CODEGUARD_GITHUB_APP_ID=123456
CODEGUARD_API_KEY=replace-with-your-provider-key
CODEGUARD_GITHUB_PRIVATE_KEY_FILE=./secrets/github-app.pem
```

Save the private key downloaded from GitHub as `./secrets/github-app.pem`. Compose mounts that file read-only and sets the in-container `CODEGUARD_GITHUB_PRIVATE_KEY_FILE` automatically.

Start the stable release. The default image is `ghcr.io/baiyun689/codeguard:latest`:

```bash
docker compose up -d
```

To run the continuously published `edge` image on Bash:

```bash
CODEGUARD_IMAGE_TAG=edge docker compose up -d
```

On PowerShell:

```powershell
$env:CODEGUARD_IMAGE_TAG = "edge"
docker compose up -d
```

To build from the current checkout instead of relying on a published image:

```bash
docker compose up -d --build
```

The Gateway always listens on port `9090` inside the container. Change only the host-side port with `CODEGUARD_HOST_PORT`, for example:

```dotenv
CODEGUARD_HOST_PORT=8080
```

The public webhook URL would then be `https://your-host.example/webhooks/github` when a reverse proxy terminates HTTPS on the standard port, or `https://your-host.example:8080/webhooks/github` when exposing the mapped port directly.

## Configure a GitHub App

1. In GitHub, open **Settings > Developer settings > GitHub Apps > New GitHub App**.
2. Set the webhook URL to `https://your-host.example/webhooks/github`.
3. Choose a webhook secret and put the identical value in `CODEGUARD_WEBHOOK_SECRET`.
4. Set these repository permissions:
   - **Checks:** Read and write
   - **Contents:** Read-only
   - **Pull requests:** Read and write
   - **Metadata:** Read-only (GitHub grants this permission automatically)
5. Under webhook events, subscribe to **Pull request**. Codeguard handles the `opened`, `reopened`, and `synchronize` actions.
6. Create the App, copy its **App ID** into `CODEGUARD_GITHUB_APP_ID`, generate a private key, and save it as `./secrets/github-app.pem`.
7. Install the App on each organization or repository that Codeguard should review.

Public repositories can be cloned without an additional token. For private repositories, set `CODEGUARD_GITHUB_TOKEN` in `.env` to a token that can read the repository contents. The current clone path does not automatically reuse the GitHub App installation token.

Your webhook endpoint must be reachable from GitHub over HTTPS. If Codeguard is behind a reverse proxy, forward `/webhooks/github` to the host port selected by `CODEGUARD_HOST_PORT`.

## Configure the LLM

The default provider is OpenAI:

```dotenv
CODEGUARD_PROVIDER=openai
CODEGUARD_MODEL=gpt-4o-mini
CODEGUARD_API_KEY=replace-with-your-key
```

For an OpenAI-compatible endpoint, also set:

```dotenv
CODEGUARD_API_BASE_URL=https://provider.example/v1
CODEGUARD_STRUCTURED_METHOD=function_calling
```

Anthropic is available with `CODEGUARD_PROVIDER=claude`. `CODEGUARD_PROVIDER=mock` exercises the pipeline without a real model and is intended for development checks, not production review.

See [`.env.example`](.env.example) for all model, review-budget, and runtime settings.

## Verify the Deployment

Check container state and logs:

```bash
docker compose ps
docker compose logs -f codeguard
```

With the default host port:

```bash
curl --fail http://localhost:9090/health/ready
```

Then use the GitHub App settings page to send a test delivery, or open/update a pull request in an installed repository. A valid `pull_request` delivery is accepted asynchronously and should produce a Codeguard Check Run after review completes.

## Local CLI Usage

The Python Agent can review a local Git diff without GitHub:

```bash
cd services/agent
python -m venv .venv
```

Activate the virtual environment, then install and run:

```bash
pip install -e .
export CODEGUARD_API_KEY=replace-with-your-key
python -m codeguard_agent review --repo /path/to/repository --base HEAD
```

PowerShell:

```powershell
Set-Location services/agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
$env:CODEGUARD_API_KEY = "replace-with-your-key"
python -m codeguard_agent review --repo C:\path\to\repository --base HEAD
```

Set `CODEGUARD_PROVIDER=mock` for a zero-cost pipeline smoke test. Configure `CODEGUARD_TOOL_SERVER_URL=http://localhost:9090` when the local Agent should use a separately running Gateway for repository context tools.

## Configuration

Deployment settings:

| Variable | Default | Purpose |
|---|---|---|
| `CODEGUARD_IMAGE_TAG` | `latest` | Image tag under `ghcr.io/baiyun689/codeguard` |
| `CODEGUARD_HOST_PORT` | `9090` | Host port mapped to the container's fixed port `9090` |
| `CODEGUARD_WEBHOOK_SECRET` | required | Secret used to verify GitHub webhook signatures |
| `CODEGUARD_GITHUB_APP_ID` | required | GitHub App ID used for installation authentication |
| `CODEGUARD_GITHUB_PRIVATE_KEY_FILE` | `./secrets/github-app.pem` | Host path to the App private key mounted by Compose |
| `CODEGUARD_GITHUB_TOKEN` | empty | Repository read token required for private repository clones |
| `CODEGUARD_PROVIDER` | `openai` | LLM provider: `openai`, `claude`, or `mock` |
| `CODEGUARD_MODEL` | provider default | Model name |
| `CODEGUARD_API_KEY` | required by Compose | LLM provider API key |
| `CODEGUARD_API_BASE_URL` | empty | Optional compatible API endpoint |
| `CODEGUARD_MAX_CONCURRENT_REVIEWS` | `2` | Maximum reviews run concurrently in this instance |
| `CODEGUARD_REVIEW_TIMEOUT_SECONDS` | `600` | Python review process timeout |
| `CODEGUARD_RETRY_DELAY_SECONDS` | `30` | Delay before a retryable job is rescheduled |
| `CODEGUARD_SHUTDOWN_GRACE_SECONDS` | `30` | Maximum drain time during shutdown |
| `CODEGUARD_WEBHOOK_RATE_LIMIT` | `0.5` | Accepted webhook requests per second; `0` disables rate limiting |

Compose sets container-only paths and ports for the bundled deployment. Do not change `CODEGUARD_TOOL_SERVER_PORT`, `CODEGUARD_TOOL_SERVER_URL`, `CODEGUARD_JOB_DB_PATH`, or `CODEGUARD_WORKSPACE_DIR` unless you are maintaining a custom deployment.

## Operations and Observability

Codeguard currently supports a single Gateway instance. H2 persistence and the scheduler recover jobs within that instance, but the deployment does not implement multi-instance leader election, distributed locking, or shared-workspace coordination. Do not scale the Compose service above one replica.

Operational endpoints:

| Endpoint | Meaning |
|---|---|
| `GET /health` | Compatibility health endpoint; reports process liveness |
| `GET /health/live` | Liveness probe |
| `GET /health/ready` | Readiness of H2, the scheduler, and Python initialization; returns `503` when unavailable |
| `GET /metrics` | Prometheus text exposition |

Compose persists the H2 database in `gateway-data` and temporary SHA-scoped review workspaces in `job-workspaces`. Stop the service with `docker compose down`. Add `--volumes` only when you intentionally want to delete persisted job state and workspaces.

The image publishing workflow uses:

- `edge` for pushes to `master`
- semantic version tags such as `v1.2.3` for release images
- `latest` for the newest semantic version

After the package is published to GHCR for the first time, a repository owner may need to open the package settings on GitHub and change its visibility to **Public** before unauthenticated `docker compose up -d` can pull it.

## Development

Python checks:

```bash
cd services/agent
uv sync --group dev
uv run pytest tests/ -q
uv run ruff check src/
uv run mypy src/
```

Java checks:

```bash
cd services/gateway
mvn --batch-mode verify
```

Container build:

```bash
docker build -t codeguard:local .
```

## Contributing

Issues and pull requests are welcome. Keep changes focused, add deterministic tests for code changes, and run the relevant Python, Java, and container checks before submitting.

Commit messages use Conventional Commits:

```text
<type>(<scope>): <description>
```

Common types are `feat`, `fix`, `docs`, `refactor`, `test`, and `chore`.

## License

Codeguard is available under the [MIT License](LICENSE).
