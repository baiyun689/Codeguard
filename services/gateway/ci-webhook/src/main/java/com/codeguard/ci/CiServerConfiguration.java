package com.codeguard.ci;

import com.codeguard.ci.executor.ResultFeedback;
import com.codeguard.ci.executor.ReviewExecutorImpl;
import com.codeguard.ci.github.GitHubClient;
import com.codeguard.ci.guard.ReviewGuard;
import com.codeguard.ci.job.JobRepository;
import com.codeguard.ci.job.JobScheduler;
import com.codeguard.ci.webhook.GitHubWebhookController;
import com.codeguard.common.*;
import com.codeguard.toolserver.GatewaySettings;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.boot.SpringBootConfiguration;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean;
import org.springframework.boot.autoconfigure.jdbc.DataSourceAutoConfiguration;
import org.springframework.context.annotation.*;
import org.springframework.core.type.AnnotatedTypeMetadata;
import java.util.concurrent.TimeUnit;

@SpringBootConfiguration(proxyBeanMethods = false)
@EnableAutoConfiguration(exclude = DataSourceAutoConfiguration.class)
@Import({GatewayHttpConfiguration.class, CiServerConfiguration.EnabledWebhook.class})
public class CiServerConfiguration {
    @Bean GatewayMetrics gatewayMetrics() { return new GatewayMetrics(); }
    @Bean(initMethod = "start", destroyMethod = "close")
    AlertEvaluator alertEvaluator(GatewayMetrics metrics) { return new AlertEvaluator(metrics); }

    @Bean OperationalController operationalController(GatewaySettings settings, GatewayMetrics metrics,
            AlertEvaluator alerts, ObjectProvider<JobScheduler> schedulers) {
        JobScheduler scheduler = schedulers.getIfAvailable();
        boolean pythonReady = scheduler == null || probePython(settings.pythonCommand());
        return new OperationalController(
                () -> scheduler == null || (pythonReady && scheduler.isReady()), metrics, alerts);
    }

    static boolean probePython(String python) {
        try {
            Process process = new ProcessBuilder(python, "--version").start();
            boolean finished = process.waitFor(5, TimeUnit.SECONDS);
            if (!finished) process.destroyForcibly();
            return finished && process.exitValue() == 0;
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            return false;
        } catch (Exception unavailable) {
            return false;
        }
    }

    static final class WebhookEnabled implements Condition {
        @Override public boolean matches(ConditionContext context, AnnotatedTypeMetadata metadata) {
            return !context.getBeanFactory().getBean(GatewaySettings.class).webhookSecret().isBlank();
        }
    }

    @Configuration(proxyBeanMethods = false)
    @Conditional(WebhookEnabled.class)
    static class EnabledWebhook {
        @Bean(destroyMethod = "close")
        @ConditionalOnMissingBean(JobRepository.class)
        JobRepository jobRepository(GatewaySettings settings) {
            return JobRepository.mysql(settings.jobDbUrl(), settings.jobDbUser(), settings.jobDbPassword());
        }
        @Bean GitHubClient gitHubClient(GatewaySettings settings) {
            if (settings.githubAppId().isBlank() || settings.githubPrivateKey().isBlank()) return null;
            return new GitHubClient(settings.githubAppId(), settings.githubPrivateKey());
        }
        @Bean ResultFeedback resultFeedback(ObjectProvider<GitHubClient> clients) {
            GitHubClient client = clients.getIfAvailable();
            return client == null ? null : new ResultFeedback(client);
        }
        @Bean ReviewExecutorImpl reviewExecutor(GatewaySettings settings) {
            return new ReviewExecutorImpl(settings.workspaceDir(), settings.githubToken(),
                    settings.reviewTimeout(), settings.pythonCommand());
        }
        @Bean(initMethod = "start", destroyMethod = "close")
        JobScheduler jobScheduler(JobRepository repository, GatewaySettings settings,
                ReviewExecutorImpl executor, ObjectProvider<ResultFeedback> feedbacks, GatewayMetrics metrics) {
            ResultFeedback feedback = feedbacks.getIfAvailable();
            return new JobScheduler(repository, settings.maxConcurrentReviews(), executor,
                    settings.retryDelay(), settings.shutdownGrace(),
                    feedback == null ? null : feedback::postResults, metrics);
        }
        @Bean ReviewGuard reviewGuard(GatewaySettings settings) {
            return new ReviewGuard(settings.webhookRateLimit());
        }
        @Bean GitHubWebhookController webhookController(GatewaySettings settings, JobRepository repository,
                JobScheduler scheduler, ReviewGuard guard) {
            return new GitHubWebhookController(settings.webhookSecret(), repository, scheduler, guard);
        }
    }
}
