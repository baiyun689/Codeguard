package com.codeguard.ci.executor;

import com.codeguard.ci.github.GitHubClient;
import com.codeguard.ci.model.ReviewJob;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.*;

public class ResultFeedback {

    private static final Logger log = LoggerFactory.getLogger(ResultFeedback.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final int MAX_ANNOTATIONS = 50;
    private static final int MAX_LINE_COMMENTS = 10;

    private final GitHubClient client;

    public ResultFeedback(GitHubClient client) {
        this.client = client;
    }

    public void postResults(ReviewJob job) {
        if (client == null || job.getResultJson() == null || job.getResultJson().isBlank()) return;

        try {
            JsonNode root = MAPPER.readTree(job.getResultJson());
            JsonNode issues = root.path("issues");
            if (!issues.isArray()) return;

            List<JsonNode> issueList = new ArrayList<>();
            issues.forEach(issueList::add);

            // Check Run
            long checkRunId = client.createCheckRun(job.getRepo(), job.getHeadSha(), job.getInstallationId());
            String conclusion = determineConclusion(issueList);
            String title = "发现 " + issueList.size() + " 个问题";
            String summary = buildSummary(issueList);
            List<GitHubClient.IssueAnnot> annotations = buildAnnotations(issueList);
            client.completeCheckRun(job.getRepo(), checkRunId, conclusion, title,
                summary, annotations, job.getInstallationId());

            // Line comments (high-severity only)
            postHighSeverityComments(job, issueList);

        } catch (Exception e) {
            log.error("结果反馈失败: {}", job.dedupKey(), e);
        }
    }

    private String determineConclusion(List<JsonNode> issues) {
        boolean hasCritical = issues.stream()
            .anyMatch(i -> "CRITICAL".equals(i.path("severity").asText()));
        if (hasCritical) return "failure";
        return issues.isEmpty() ? "success" : "neutral";
    }

    private String buildSummary(List<JsonNode> issues) {
        StringBuilder sb = new StringBuilder("## Codeguard 审查结果\n\n");
        sb.append("共发现 **").append(issues.size()).append("** 个问题\n\n");
        sb.append("| 级别 | 类型 | 文件 | 行号 | 问题 |\n");
        sb.append("|------|------|------|------|------|\n");

        int crit = 0, warn = 0, info = 0;
        for (JsonNode i : issues) {
            String sev = i.path("severity").asText();
            sb.append("| ").append(severityIcon(sev)).append(" ").append(sev)
              .append(" | ").append(i.path("type").asText())
              .append(" | ").append(i.path("file").asText())
              .append(" | ").append(i.path("line").asInt())
              .append(" | ").append(ellipsis(i.path("message").asText(), 80))
              .append(" |\n");
            switch (sev) {
                case "CRITICAL": crit++; break;
                case "WARNING": warn++; break;
                default: info++;
            }
        }
        sb.append("\n📊 统计: CRITICAL=").append(crit)
          .append(" WARNING=").append(warn)
          .append(" INFO=").append(info);
        return sb.toString();
    }

    private List<GitHubClient.IssueAnnot> buildAnnotations(List<JsonNode> issues) {
        List<GitHubClient.IssueAnnot> annots = new ArrayList<>();
        int limit = Math.min(issues.size(), MAX_ANNOTATIONS);
        for (int i = 0; i < limit; i++) {
            JsonNode issue = issues.get(i);
            annots.add(new GitHubClient.IssueAnnot(
                issue.path("file").asText(),
                Math.max(issue.path("line").asInt(), 1),
                toAnnotationLevel(issue.path("severity").asText()),
                issue.path("message").asText()
            ));
        }
        return annots;
    }

    /**
     * 将源文件绝对行号映射为 unified diff 中的 new-side 行号。
     * <p>
     * 解析 diff 文本，找到目标文件对应的 hunk，在 hunk 内逐行推进 new-side 行号计数，
     * 匹配到 absoluteLine 时返回对应的 new-side 行号。
     *
     * @param diffText     unified diff 全文
     * @param targetFile   目标文件路径
     * @param absoluteLine 源文件中的绝对行号
     * @return diff 内的 new-side 行号；找不到返回 -1
     */
    static int mapToDiffLine(String diffText, String targetFile, int absoluteLine) {
        if (diffText == null || diffText.isEmpty() || targetFile == null || absoluteLine <= 0) {
            return -1;
        }

        // 找 "diff --git a/" + targetFile 开头的文件块
        String fileMarker = "diff --git a/" + targetFile;
        int fileStart = diffText.indexOf(fileMarker);
        if (fileStart == -1) {
            fileMarker = "diff --git b/" + targetFile;
            fileStart = diffText.indexOf(fileMarker);
            if (fileStart == -1) return -1;
        }

        int nextFileStart = diffText.indexOf("diff --git ", fileStart + fileMarker.length());
        String fileBlock = nextFileStart == -1
            ? diffText.substring(fileStart)
            : diffText.substring(fileStart, nextFileStart);

        java.util.regex.Pattern hunkPattern = java.util.regex.Pattern.compile(
            "@@ -(\\d+)(?:,\\d+)? \\+(\\d+)(?:,\\d+)? @@");
        java.util.regex.Matcher m = hunkPattern.matcher(fileBlock);

        while (m.find()) {
            int newLine = Integer.parseInt(m.group(2));
            int bodyStart = m.end();
            int bodyEnd = fileBlock.indexOf("@@", bodyStart);
            if (bodyEnd == -1) bodyEnd = fileBlock.length();
            String body = fileBlock.substring(bodyStart, bodyEnd);

            for (String line : body.split("\n")) {
                if (line.startsWith("-")) continue;
                if (line.startsWith("+") || line.startsWith(" ")) {
                    if (newLine == absoluteLine) return newLine;
                    newLine++;
                }
            }
        }
        return -1;
    }

    private void postHighSeverityComments(ReviewJob job, List<JsonNode> issues) {
        List<JsonNode> criticals = issues.stream()
            .filter(i -> "CRITICAL".equals(i.path("severity").asText()))
            .filter(i -> i.path("confidence").asDouble(1.0) >= 0.7)
            .sorted((a, b) -> Double.compare(
                b.path("confidence").asDouble(1.0),
                a.path("confidence").asDouble(1.0)))
            .limit(MAX_LINE_COMMENTS)
            .toList();

        List<String> failedIssues = new ArrayList<>();

        for (JsonNode issue : criticals) {
            try {
                int absoluteLine = Math.max(issue.path("line").asInt(), 1);
                int diffLine = mapToDiffLine(job.getDiffText(), issue.path("file").asText(), absoluteLine);
                String body = String.format("🔴 **%s**: %s\n\n建议: %s",
                    issue.path("type").asText(),
                    issue.path("message").asText(),
                    issue.path("suggestion").asText("无"));
                if (diffLine > 0) {
                    boolean ok = client.createPRComment(job.getRepo(), job.getPrNumber(),
                        job.getHeadSha(), issue.path("file").asText(),
                        diffLine,
                        body, job.getInstallationId());
                    if (!ok) {
                        failedIssues.add(String.format("- `%s:%d` **%s**: %s",
                            issue.path("file").asText(), absoluteLine,
                            issue.path("type").asText(), issue.path("message").asText()));
                    }
                } else {
                    failedIssues.add(String.format("- `%s:%d` **%s**: %s",
                        issue.path("file").asText(), absoluteLine,
                        issue.path("type").asText(), issue.path("message").asText()));
                }
            } catch (Exception e) {
                log.warn("行级评论失败: {}", e.getMessage());
                failedIssues.add(String.format("- `%s:%d` **%s**: %s",
                    issue.path("file").asText(),
                    Math.max(issue.path("line").asInt(), 1),
                    issue.path("type").asText(),
                    issue.path("message").asText()));
            }
        }

        // 无法精确定位到行的 issue 降级为一条 PR 普通评论
        if (!failedIssues.isEmpty()) {
            try {
                String summary = "### 🔴 以下问题未能定位到具体代码行（LLM 行号与 diff 不匹配）\n\n"
                    + String.join("\n", failedIssues)
                    + "\n\n> 请人工确认这些问题在 PR diff 中的实际位置。";
                client.createIssueComment(job.getRepo(), job.getPrNumber(),
                    summary, job.getInstallationId());
            } catch (Exception e) {
                log.error("降级评论也失败了: {}", e.getMessage());
            }
        }
    }

    private String severityIcon(String sev) {
        return switch (sev) {
            case "CRITICAL" -> "🔴";
            case "WARNING" -> "🟡";
            default -> "🔵";
        };
    }

    private String toAnnotationLevel(String sev) {
        return switch (sev) {
            case "CRITICAL" -> "failure";
            case "WARNING" -> "warning";
            default -> "notice";
        };
    }

    private String ellipsis(String s, int max) {
        if (s == null) return "";
        return s.length() <= max ? s : s.substring(0, max - 3) + "...";
    }
}
