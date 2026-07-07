package com.codeguard.ci.job;

import com.codeguard.ci.model.ReviewJob;
import com.codeguard.ci.model.ReviewJob.Status;

import java.sql.*;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

/**
 * H2 文件模式持久化 ReviewJob。
 * 支持幂等插入（MERGE INTO）和启动恢复（findUnfinished）。
 */
public class JobRepository implements AutoCloseable {

    private final Connection conn;

    /**
     * @param dbPath H2 数据库文件路径（不含 jdbc:h2:file: 前缀）
     */
    public JobRepository(String dbPath) {
        try {
            this.conn = DriverManager.getConnection("jdbc:h2:file:" + dbPath + ";DB_CLOSE_DELAY=-1");
            initTable();
        } catch (SQLException e) {
            throw new RuntimeException("无法打开 H2 数据库: " + dbPath, e);
        }
    }

    private void initTable() throws SQLException {
        String sql = """
            CREATE TABLE IF NOT EXISTS review_jobs (
                id              BIGINT AUTO_INCREMENT PRIMARY KEY,
                repo            VARCHAR(255) NOT NULL,
                pr_number       INT NOT NULL,
                head_sha        VARCHAR(40) NOT NULL,
                base_ref        VARCHAR(255),
                clone_url       VARCHAR(512),
                installation_id BIGINT,
                status          VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                result_json     CLOB,
                retry_count     INT DEFAULT 0,
                error_message   VARCHAR(1024),
                diff_text       CLOB,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (repo, pr_number, head_sha)
            )
            """;
        try (Statement stmt = conn.createStatement()) {
            stmt.execute(sql);
        }
    }

    /**
     * 幂等插入。使用 MERGE INTO 保证同一 (repo, pr_number, head_sha) 只存一行。
     *
     * @param job 待插入的 ReviewJob
     * @return 如果新行被创建返回包含 job 的 Optional；如果已存在（重复）返回 Optional.empty()
     */
    public Optional<ReviewJob> insert(ReviewJob job) {
        // 先检查是否已存在，用于区分"新建"和"重复"
        if (findByDedupKey(job.getRepo(), job.getPrNumber(), job.getHeadSha()).isPresent()) {
            return Optional.empty();
        }

        String sql = """
            MERGE INTO review_jobs (repo, pr_number, head_sha, base_ref, clone_url, installation_id,
                                    status, result_json, retry_count, error_message, diff_text, created_at, updated_at)
            KEY (repo, pr_number, head_sha)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """;
        try (PreparedStatement ps = conn.prepareStatement(sql, Statement.RETURN_GENERATED_KEYS)) {
            ps.setString(1, job.getRepo());
            ps.setInt(2, job.getPrNumber());
            ps.setString(3, job.getHeadSha());
            ps.setString(4, job.getBaseRef());
            ps.setString(5, job.getCloneUrl());
            ps.setLong(6, job.getInstallationId());
            ps.setString(7, job.getStatus().name());
            ps.setString(8, job.getResultJson());
            ps.setInt(9, job.getRetryCount());
            ps.setString(10, job.getErrorMessage());
            ps.setString(11, job.getDiffText());
            ps.setTimestamp(12, Timestamp.from(job.getCreatedAt()));
            ps.setTimestamp(13, Timestamp.from(job.getUpdatedAt()));

            ps.executeUpdate();
            try (ResultSet keys = ps.getGeneratedKeys()) {
                if (keys.next()) {
                    job.setId(keys.getLong(1));
                }
            }
        } catch (SQLException e) {
            throw new RuntimeException("插入 ReviewJob 失败: " + job.dedupKey(), e);
        }
        return Optional.of(job);
    }

    /**
     * 按去重键查询，用于幂等性检查。
     */
    public Optional<ReviewJob> findByDedupKey(String repo, int prNumber, String headSha) {
        String sql = "SELECT * FROM review_jobs WHERE repo = ? AND pr_number = ? AND head_sha = ?";
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            ps.setString(1, repo);
            ps.setInt(2, prNumber);
            ps.setString(3, headSha);
            try (ResultSet rs = ps.executeQuery()) {
                if (rs.next()) {
                    return Optional.of(mapRow(rs));
                }
            }
        } catch (SQLException e) {
            throw new RuntimeException("查询 ReviewJob 失败: " + repo + ":" + prNumber + ":" + headSha, e);
        }
        return Optional.empty();
    }

    /**
     * 更新 job 的状态、结果、重试次数、错误信息和更新时间。
     * 按 id 定位。
     */
    public void update(ReviewJob job) {
        String sql = """
            UPDATE review_jobs
            SET status = ?, result_json = ?, retry_count = ?, error_message = ?, diff_text = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """;
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            ps.setString(1, job.getStatus().name());
            ps.setString(2, job.getResultJson());
            ps.setInt(3, job.getRetryCount());
            ps.setString(4, job.getErrorMessage());
            ps.setString(5, job.getDiffText());
            ps.setLong(6, job.getId());
            ps.executeUpdate();
        } catch (SQLException e) {
            throw new RuntimeException("更新 ReviewJob 失败: id=" + job.getId(), e);
        }
    }

    /**
     * 查找所有未完成的 job（PENDING / RUNNING / RETRYING），用于启动恢复。
     */
    public List<ReviewJob> findUnfinished() {
        String sql = "SELECT * FROM review_jobs WHERE status IN ('PENDING', 'RUNNING', 'RETRYING')";
        List<ReviewJob> jobs = new ArrayList<>();
        try (PreparedStatement ps = conn.prepareStatement(sql);
             ResultSet rs = ps.executeQuery()) {
            while (rs.next()) {
                jobs.add(mapRow(rs));
            }
        } catch (SQLException e) {
            throw new RuntimeException("查询未完成 job 失败", e);
        }
        return jobs;
    }

    /**
     * 将 ResultSet 当前行映射为 ReviewJob。
     * 使用 package-private 构造器和 setter，不使用反射。
     */
    private ReviewJob mapRow(ResultSet rs) throws SQLException {
        ReviewJob job = new ReviewJob(
            rs.getString("repo"),
            rs.getInt("pr_number"),
            rs.getString("head_sha"),
            rs.getString("base_ref"),
            rs.getString("clone_url")
        );
        job.setIdFromDb(rs.getLong("id"));
        job.setStatusFromDb(Status.valueOf(rs.getString("status")));
        job.setResultJsonFromDb(rs.getString("result_json"));
        job.setRetryCountFromDb(rs.getInt("retry_count"));
        job.setErrorMessageFromDb(rs.getString("error_message"));
        job.setDiffTextFromDb(rs.getString("diff_text"));
        job.setInstallationIdFromDb(rs.getLong("installation_id"));

        Timestamp createdAtTs = rs.getTimestamp("created_at");
        if (createdAtTs != null) {
            job.setCreatedAtFromDb(createdAtTs.toInstant());
        }
        Timestamp updatedAtTs = rs.getTimestamp("updated_at");
        if (updatedAtTs != null) {
            job.setUpdatedAtFromDb(updatedAtTs.toInstant());
        }
        return job;
    }

    @Override
    public void close() {
        try {
            if (conn != null && !conn.isClosed()) {
                conn.close();
            }
        } catch (SQLException e) {
            throw new RuntimeException("关闭数据库连接失败", e);
        }
    }
}
