# Codeguard Gateway

The Java Gateway is Codeguard's webhook, job-execution, repository-fact, and operational guardrail service. It verifies GitHub events, persists and schedules reviews, runs the Python Agent in isolated commit workspaces, publishes results, and exposes guarded code-context tools. Review reasoning remains in the Python Agent.

## Start with Docker Compose

Use the repository-root Compose deployment for normal operation:

```bash
cp .env.example .env
mkdir -p secrets
# Edit .env and save the GitHub App key as secrets/github-app.pem.
docker compose up -d
```

The published image defaults to `ghcr.io/baiyun689/codeguard:latest`. The Gateway listens on port `9090` inside the container; `CODEGUARD_HOST_PORT` controls the host mapping.

See the root [README](../../README.md) for complete GitHub App, LLM, private-repository, image-tag, and reverse-proxy configuration.

## HTTP Endpoints

GitHub and operations:

- `POST /webhooks/github` verifies and accepts supported `pull_request` events.
- `GET /health` and `GET /health/live` report process liveness.
- `GET /health/ready` reports H2, scheduler, and Python readiness.
- `GET /metrics` exposes Prometheus metrics.

Agent tool sessions:

- `POST /api/v1/tools/session` creates a repository-scoped session.
- `DELETE /api/v1/tools/session/{id}` destroys a session.
- `POST /api/v1/tools/{name}` dispatches an allowed tool using the `X-Session-Id` header.

The tool registry includes guarded file content, sensitive API, caller, code-metric, and diff AST queries. Repository paths are constrained by the session sandbox.

## Local Development

Requirements:

- Java 21
- Maven 3.9+
- Python with the `codeguard_agent` package installed when exercising CI review jobs

Build and test the Gateway:

```bash
cd services/gateway
mvn --batch-mode verify
```

Run the packaged JAR:

```bash
java -jar target/codeguard-gateway.jar
```

On Windows, the helper script loads only missing `CODEGUARD_*` values from the repository-root `.env` and starts an already-built JAR:

```powershell
Set-Location services/gateway
mvn package
.\start-ci.ps1
```

The script does not build the JAR or read separate App ID/private-key files. Configure `CODEGUARD_GITHUB_APP_ID` and `CODEGUARD_GITHUB_PRIVATE_KEY_FILE` in the shell or root `.env`.

Without `CODEGUARD_WEBHOOK_SECRET`, the Gateway starts its tool and operational endpoints but does not register the GitHub webhook or CI scheduler.

## Runtime Notes

- The Gateway is single-instance. Do not run multiple replicas against the same H2 database or workspace volumes.
- `CODEGUARD_TOOL_SERVER_PORT` defaults to `9090` for direct JAR execution.
- `CODEGUARD_JOB_DB_PATH` defaults to `./data/codeguard-jobs`.
- `CODEGUARD_WORKSPACE_DIR` defaults to a directory under the system temporary directory.
- On shutdown, the HTTP server stops accepting webhooks before the scheduler drains active work.
