package com.codeguard.ci.webhook;

import com.codeguard.ci.job.JobRepository;
import com.codeguard.ci.job.JobScheduler;
import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.WebhookPayload;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.javalin.Javalin;
import io.javalin.http.Context;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Map;
import java.util.Optional;
import java.util.Set;

public class GitHubWebhookController {

    private static final Logger log = LoggerFactory.getLogger(GitHubWebhookController.class);
    private static final Set<String> ALLOWED_ACTIONS = Set.of("opened", "reopened", "synchronize");
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final WebhookVerifier verifier;
    private final JobRepository repo;
    private final JobScheduler scheduler;

    public GitHubWebhookController(String secret, JobRepository repo, JobScheduler scheduler) {
        this.verifier = new WebhookVerifier(secret);
        this.repo = repo;
        this.scheduler = scheduler;
    }

    public void register(Javalin app) {
        app.post("/webhooks/github", this::handle);
    }

    void handle(Context ctx) {
        // Layer 1: Verify signature
        String sig = ctx.header("X-Hub-Signature-256");
        byte[] body = ctx.bodyAsBytes();
        if (!verifier.verify(sig, body)) {
            ctx.status(401).result("signature mismatch");
            return;
        }

        // Non-PR events → 200 empty
        String event = ctx.header("X-GitHub-Event");
        if (!"pull_request".equals(event)) {
            ctx.status(200).result("ignored: " + event);
            return;
        }

        try {
            JsonNode root = MAPPER.readTree(body);
            String action = root.path("action").asText();

            if (!ALLOWED_ACTIONS.contains(action)) {
                ctx.status(200).result("skipped action: " + action);
                return;
            }

            WebhookPayload payload = extractPayload(root);

            // Idempotency check
            Optional<ReviewJob> existing = repo.findByDedupKey(
                payload.repoFullName(), payload.prNumber(), payload.headSha());
            if (existing.isPresent() && existing.get().getStatus() != ReviewJob.Status.FAILED) {
                ctx.status(200).json(Map.of(
                    "status", "already_processed",
                    "job_id", existing.get().getId(),
                    "job_status", existing.get().getStatus().name()
                ));
                return;
            }

            ReviewJob job = new ReviewJob(payload);
            var inserted = repo.insert(job);
            if (inserted.isEmpty()) {
                ctx.status(200).json(Map.of("status", "duplicate"));
                return;
            }

            boolean accepted = scheduler.submit(inserted.get());
            if (accepted) {
                ctx.status(202).json(Map.of("status", "accepted", "job_id", inserted.get().getId()));
            } else {
                ctx.status(503).json(Map.of("status", "queue_full"));
            }

        } catch (Exception e) {
            log.error("webhook 处理异常", e);
            ctx.status(500).result("internal error");
        }
    }

    private WebhookPayload extractPayload(JsonNode root) {
        JsonNode repo = root.path("repository");
        JsonNode pr = root.path("pull_request");
        JsonNode head = pr.path("head");
        JsonNode installation = root.path("installation");

        return new WebhookPayload(
            repo.path("full_name").asText(),
            repo.path("clone_url").asText(),
            pr.path("number").asInt(),
            head.path("sha").asText(),
            head.path("ref").asText(),
            installation.path("id").asLong()
        );
    }
}
