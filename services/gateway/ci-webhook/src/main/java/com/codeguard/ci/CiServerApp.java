package com.codeguard.ci;

import com.codeguard.ci.executor.ResultFeedback;
import com.codeguard.ci.executor.ReviewExecutorImpl;
import com.codeguard.ci.github.GitHubClient;
import com.codeguard.ci.guard.ReviewGuard;
import com.codeguard.ci.job.JobRepository;
import com.codeguard.ci.job.JobScheduler;
import com.codeguard.ci.webhook.GitHubWebhookController;
import com.codeguard.common.GatewayMetrics;
import com.codeguard.common.OperationalController;
import com.codeguard.toolserver.GatewaySettings;
import io.javalin.Javalin;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.concurrent.TimeUnit;

/** CI Webhook 服务 —— GitHub PR 自动审查链路。监听端口 8080。 */
public final class CiServerApp {
    private static final Logger log = LoggerFactory.getLogger(CiServerApp.class);

    private final Javalin app;
    private final GatewaySettings settings;
    private final GatewayMetrics metrics;
    private JobRepository jobRepository;
    private JobScheduler scheduler;
    private boolean enabled;
    private boolean pythonReady = true;

    public CiServerApp() {
        this(GatewaySettings.fromEnv());
    }

    CiServerApp(GatewaySettings settings) {
        this.settings = settings;
        this.metrics = new GatewayMetrics();
        this.app = Javalin.create(cfg -> {
            cfg.showJavalinBanner = false;
            cfg.http.maxRequestSize = 10_000_000L;
        });

        if (!settings.webhookSecret().isBlank()) {
            configure();
            enabled = true;
        } else {
            log.info("未配置 CODEGUARD_WEBHOOK_SECRET, CI Webhook 未启用");
            enabled = false;
        }
        new OperationalController(this::ready, metrics).register(app);
    }

    private void configure() {
        jobRepository = JobRepository.mysql(settings.jobDbUrl(), settings.jobDbUser(), settings.jobDbPassword());
        GitHubClient githubClient = null;
        if (!settings.githubAppId().isBlank() && !settings.githubPrivateKey().isBlank()) {
            githubClient = new GitHubClient(settings.githubAppId(), settings.githubPrivateKey());
        }
        ResultFeedback feedback = githubClient == null ? null : new ResultFeedback(githubClient);
        var executor = new ReviewExecutorImpl(settings.workspaceDir(), settings.githubToken(),
            settings.reviewTimeout(), settings.pythonCommand());
        scheduler = new JobScheduler(jobRepository, settings.maxConcurrentReviews(), executor,
            settings.retryDelay(), settings.shutdownGrace(), feedback == null ? null : feedback::postResults, metrics);
        pythonReady = probePython(settings.pythonCommand());
        scheduler.start();

        var guard = new ReviewGuard(settings.webhookRateLimit());
        new GitHubWebhookController(settings.webhookSecret(), jobRepository, scheduler, guard).register(app);
        log.info("GitHub webhook 端点已启用: POST /webhooks/github (python ready={})", pythonReady);
    }

    private boolean ready() {
        if (!enabled) return true;
        if (scheduler == null) return true;
        return pythonReady && jobRepository != null && jobRepository.ping() && scheduler.isReady();
    }

    private static boolean probePython(String python) {
        try {
            Process process = new ProcessBuilder(python, "--version").start();
            boolean finished = process.waitFor(5, TimeUnit.SECONDS);
            if (!finished) process.destroyForcibly();
            return finished && process.exitValue() == 0;
        } catch (Exception unavailable) {
            log.error("Python Agent 初始化检查失败: {}", unavailable.getMessage());
            return false;
        }
    }

    public void start() {
        int port = Integer.parseInt(System.getenv().getOrDefault("CODEGUARD_CI_PORT", "8080"));
        app.start(port);
        log.info("CI Webhook 服务已启动, 端口 {} (enabled={})", port, enabled);
    }

    public void stop() {
        app.stop();
        if (scheduler != null) scheduler.close();
        if (jobRepository != null) jobRepository.close();
        log.info("CI Webhook 服务已停止");
    }

    public Javalin javalin() { return app; }
}
