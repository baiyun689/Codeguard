package com.codeguard.ci.webhook;

import com.codeguard.ci.guard.ReviewGuard;
import com.codeguard.ci.job.JobRepository;
import com.codeguard.ci.job.JobScheduler;
import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.WebhookPayload;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Map;
import java.util.Optional;
import java.util.Set;

@RestController
public class GitHubWebhookController {

    private static final Logger log = LoggerFactory.getLogger(GitHubWebhookController.class);
    private static final Set<String> ALLOWED_ACTIONS = Set.of("opened", "reopened", "synchronize");
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final WebhookVerifier verifier;
    private final JobRepository repo;
    private final JobScheduler scheduler;
    private final ReviewGuard guard;

    public GitHubWebhookController(String secret, JobRepository repo, JobScheduler scheduler, ReviewGuard guard) {
        this.verifier = new WebhookVerifier(secret);
        this.repo = repo;
        this.scheduler = scheduler;
        this.guard = guard;
    }

    public GitHubWebhookController(String secret, JobRepository repo, JobScheduler scheduler) {
        this(secret, repo, scheduler, null);
    }

    @PostMapping("/webhooks/github")
    public ResponseEntity<?> handle(HttpServletRequest request, @RequestBody(required = false) byte[] rawBody) {
        // 验证 Webhook 请求签名。
        String sig = request.getHeader("X-Hub-Signature-256");
        byte[] body = rawBody == null ? new byte[0] : rawBody;
        if (!verifier.verify(sig, body)) {
            return ResponseEntity.status(401).body("signature mismatch");
        }

        // 检查请求速率限制。
        if (guard != null && !guard.tryAcquireWebhook(100)) {
            return ResponseEntity.status(429).header("Retry-After", "120").body(Map.of("error", "rate_limited"));
        }

        // 非 PR 事件直接返回空的成功响应。
        String event = request.getHeader("X-GitHub-Event");
        if (!"pull_request".equals(event)) {
            return ResponseEntity.status(200).body("ignored: " + event);
        }

        try {
            JsonNode root = MAPPER.readTree(body);
            String action = root.path("action").asText();

            if (!ALLOWED_ACTIONS.contains(action)) {
                return ResponseEntity.status(200).body("skipped action: " + action);
            }

            WebhookPayload payload = extractPayload(root);

            // 检查是否已存在相同提交的审查任务。
            Optional<ReviewJob> existing = repo.findByDedupKey(
                payload.repoFullName(), payload.prNumber(), payload.headSha());
            if (existing.isPresent() && existing.get().getStatus() == ReviewJob.Status.PENDING) {
                boolean accepted = scheduler.submit(existing.get());
                return ResponseEntity.status(accepted ? 202 : 503).body(Map.of(
                    "status", accepted ? "accepted" : "queue_full",
                    "job_id", existing.get().getId()
                ));
            }
            if (existing.isPresent() && existing.get().getStatus() != ReviewJob.Status.FAILED) {
                return ResponseEntity.status(200).body(Map.of(
                    "status", "already_processed",
                    "job_id", existing.get().getId(),
                    "job_status", existing.get().getStatus().name()
                ));
            }

            ReviewJob job = new ReviewJob(payload);
            var inserted = repo.insert(job);
            if (inserted.isEmpty()) {
                return ResponseEntity.status(200).body(Map.of("status", "duplicate"));
            }

            boolean accepted = scheduler.submit(inserted.get());
            if (accepted) {
                return ResponseEntity.status(202).body(Map.of("status", "accepted", "job_id", inserted.get().getId()));
            } else {
                return ResponseEntity.status(503).body(Map.of("status", "queue_full"));
            }

        } catch (Exception e) {
            log.error("webhook 处理异常", e);
            return ResponseEntity.status(500).body("internal error");
        }
    }

    private WebhookPayload extractPayload(JsonNode root) {
        JsonNode repo = root.path("repository");
        JsonNode pr = root.path("pull_request");
        JsonNode head = pr.path("head");
        JsonNode base = pr.path("base");
        JsonNode installation = root.path("installation");

        return new WebhookPayload(
            repo.path("full_name").asText(),
            repo.path("clone_url").asText(),
            pr.path("number").asInt(),
            head.path("sha").asText(),
            base.path("ref").asText(),
            head.path("ref").asText(),
            installation.path("id").asLong()
        );
    }
}
