package com.codeguard.toolserver;

import com.codeguard.ci.executor.ResultFeedback;
import com.codeguard.ci.executor.ReviewExecutorImpl;
import com.codeguard.ci.github.GitHubClient;
import com.codeguard.ci.guard.ReviewGuard;
import com.codeguard.ci.job.JobRepository;
import com.codeguard.ci.job.JobScheduler;
import com.codeguard.ci.webhook.GitHubWebhookController;
import io.javalin.Javalin;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.concurrent.TimeUnit;

/** Owns the HTTP server and all CI resources for one Gateway process. */
public final class ToolServerApp {
    private static final Logger log = LoggerFactory.getLogger(ToolServerApp.class);

    private final Javalin app;
    private final GatewaySettings settings;
    private final GatewayMetrics metrics;
    private JobRepository jobRepository;
    private JobScheduler scheduler;
    private boolean pythonReady = true;

    public ToolServerApp() {
        this(GatewaySettings.fromEnv());
    }

    ToolServerApp(GatewaySettings settings) {
        this.settings = settings;
        this.metrics = new GatewayMetrics();
        this.app = Javalin.create(cfg -> {
            cfg.showJavalinBanner = false;
            cfg.http.maxRequestSize = 10_000_000L;
        });
        new ToolServerController(metrics).registerRoutes(app);
        configureCi();
        new OperationalController(this::ready, metrics).register(app);
    }

    private void configureCi() {
        if (settings.webhookSecret().isBlank()) return;
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
        log.info("GitHub webhook 端点已启用: POST /webhooks/github");
    }

    private boolean ready() {
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

    public static int resolvePort() {
        return GatewaySettings.fromEnv().port();
    }

    public int port() { return settings.port(); }

    public void start(int port) {
        app.start(port);
        log.info("Codeguard 工具服务已启动,端口 {} (single-instance mode)", port);
    }

    public void stop() {
        app.stop(); // reject new webhooks before draining review workers
        if (scheduler != null) scheduler.close();
        if (jobRepository != null) jobRepository.close();
        log.info("Codeguard 工具服务已停止");
    }

    public Javalin javalin() { return app; }
}
