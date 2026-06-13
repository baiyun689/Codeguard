package com.codeguard.toolserver;

import com.codeguard.toolserver.ToolSessionManager.Session;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 工具会话管理器测试:创建/取用/销毁,以及会话内工具注册。
 */
class ToolSessionManagerTest {

    @Test
    void createAndGetSession(@TempDir Path repo) {
        ToolSessionManager mgr = new ToolSessionManager();
        String id = mgr.create(repo, Set.of("src/App.java"));

        assertNotNull(id);
        Session s = mgr.get(id);
        assertNotNull(s);
        assertEquals(id, s.getId());
        // 本期唯一工具应已注册到会话。
        assertNotNull(s.getTool("get_file_content"));
        // 未注册的工具返回 null,由控制器转成结构化错误。
        assertNull(s.getTool("get_call_graph"));
    }

    @Test
    void missingSessionReturnsNull() {
        ToolSessionManager mgr = new ToolSessionManager();
        assertNull(mgr.get(null));
        assertNull(mgr.get("nonexistent"));
    }

    @Test
    void removeSession(@TempDir Path repo) {
        ToolSessionManager mgr = new ToolSessionManager();
        String id = mgr.create(repo, Set.of());
        assertNotNull(mgr.get(id));

        mgr.remove(id);
        assertNull(mgr.get(id));
    }

    @Test
    void contextCarriesScope(@TempDir Path repo) {
        ToolSessionManager mgr = new ToolSessionManager();
        String id = mgr.create(repo, Set.of("a.java", "b.java"));
        Session s = mgr.get(id);

        assertTrue(s.getContext().getAllowedFiles().contains("a.java"));
        assertSame(s.getContext(), mgr.get(id).getContext());
    }
}
