package com.codeguard.agent.tools;

import com.codeguard.agent.core.AgentContext;
import com.codeguard.agent.graph.ProjectKey;
import com.codeguard.agent.graph.ProjectSnapshotManager;
import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.*;

class CanonicalMethodNavigationTest {
    @Test
    void methodSourceExposesOwnerMemberNavigationWithoutAnotherClassRead(@TempDir Path repo) throws Exception {
        Files.writeString(repo.resolve("Base.java"), "class Base { protected int limit = 2; }");
        Files.writeString(repo.resolve("Child.java"), "class Child extends Base { int run() { return helper(limit); } int helper(int x) { return x; } }");
        var provider = new ProjectSnapshotManager().lazyProvider(ProjectKey.of(repo, "source-navigation"));
        var context = new AgentContext(repo);
        var reader = new ReadSymbolTool(provider);
        var method = reader.execute("{\"symbol_id\":\"java:Child#run()\"}", context);
        assertTrue(method.isSuccess(), method.getError());
        String header = method.getResult().split("\n\n", 2)[0];
        assertTrue(header.contains("java:Child#helper(int)"), header);
        assertFalse(header.contains("java:Base#limit"), "Inherited dependencies require semantic graph navigation");
        assertTrue(header.contains("members_owner_id: java:Child"), header);
    }

    @Test
    void typeMemberDirectoryDoesNotRequireReadingEverySourcePage(@TempDir Path repo) throws Exception {
        Files.writeString(repo.resolve("Large.java"), "class Large {\n" + "// padding\n".repeat(350)
                + "void target() {}\n}\n");
        var provider = new ProjectSnapshotManager().lazyProvider(ProjectKey.of(repo, "member-directory"));
        var result = new ReadSymbolTool(provider).execute("{\"symbol_id\":\"java:Large\"}", new AgentContext(repo));
        assertTrue(result.isSuccess(), result.getError());
        String header = result.getResult().split("\n\n", 2)[0];
        assertTrue(header.contains("java:Large#target()"), header);
        assertFalse(result.getResult().split("\n\n", 2)[1].contains("void target()"));
    }

    @Test
    void constructorCallIsNavigableInBothDirections(@TempDir Path repo) throws Exception {
        Files.writeString(repo.resolve("Option.java"), """
                class Option {
                    Option(boolean enabled) {}
                }
                """);
        Files.writeString(repo.resolve("Factory.java"), """
                class Factory {
                    Option build() { return new Option(false); }
                }
                """);
        var provider = new ProjectSnapshotManager().lazyProvider(ProjectKey.of(repo, "constructor-navigation"));
        var tool = new QueryRelationsTool(provider);
        var context = new AgentContext(repo);
        for (String query : new String[]{
                "{\"subject_symbol_id\":\"java:Factory#build()\",\"relation\":\"callees\"}",
                "{\"subject_symbol_id\":\"java:Option#<init>Option(boolean)\",\"relation\":\"callers\"}"
        }) {
            var response = tool.execute(query, context);
            assertTrue(response.isSuccess(), response.getError());
            var page = GraphToolSupport.JSON.readTree(response.getResult());
            assertTrue(page.path("relationships").toString().contains("java:Option#<init>Option(boolean)"), response.getResult());
            assertTrue(page.path("relationships").toString().contains("java:Factory#build()"), response.getResult());
        }
    }

    @Test
    void nestedTypeNavigatesToEnclosingDeclaration(@TempDir Path repo) throws Exception {
        Files.writeString(repo.resolve("Outer.java"), """
                class Outer {
                    class Nested { void run() {} }
                }
                """);
        var provider = new ProjectSnapshotManager().lazyProvider(ProjectKey.of(repo, "nested-owner"));
        var reader = new ReadSymbolTool(provider);
        var context = new AgentContext(repo);
        var nested = reader.execute("{\"symbol_id\":\"java:Outer.Nested\"}", context);
        assertTrue(nested.isSuccess(), nested.getError());
        assertTrue(nested.getResult().contains("owner_id: java:Outer\n"), nested.getResult());
        var outer = reader.execute("{\"symbol_id\":\"java:Outer\"}", context);
        assertTrue(outer.isSuccess(), outer.getError());
        assertTrue(outer.getResult().contains("\"id\":\"java:Outer.Nested\""), outer.getResult());
    }

    @Test
    void genericCallerAndCalleeKeepReadableIndexIdentity(@TempDir Path repo) throws Exception {
        Path root = repo.resolve("src/main/java/demo");
        Files.createDirectories(root);
        Files.writeString(root.resolve("Work.java"), "package demo; interface Work<T, E> { T get(); }");
        Files.writeString(root.resolve("Engine.java"), """
                package demo;
                class Engine {
                    <T, E> T execute(Work<T, E> work) { return invoke(work); }
                    <T, E> T invoke(Work<T, E> work) { return work.get(); }
                }
                """);
        var provider = new ProjectSnapshotManager().lazyProvider(ProjectKey.of(repo, "generic-navigation"));
        var tool = new QueryRelationsTool(provider);
        var context = new AgentContext(repo);
        for (String query : new String[]{
                "{\"subject_symbol_id\":\"java:demo.Work#get()\",\"relation\":\"callers\"}",
                "{\"subject_symbol_id\":\"java:demo.Engine#invoke(Work)\",\"relation\":\"callers\"}",
                "{\"subject_symbol_id\":\"java:demo.Engine#execute(Work)\",\"relation\":\"callees\",\"depth\":2}"
        }) {
            var response = tool.execute(query, context);
            assertTrue(response.isSuccess(), response.getError());
            JsonNode page = GraphToolSupport.JSON.readTree(response.getResult());
            assertFalse(page.path("relationships").isEmpty(), response.getResult());
            Set<String> symbols = new HashSet<>();
            page.path("symbols").forEach(symbol -> symbols.add(symbol.path("id").asText()));
            for (JsonNode edge : page.path("relationships")) {
                for (String end : new String[]{"sourceId", "targetId"}) {
                    String id = edge.path(end).asText();
                    assertTrue(symbols.contains(id), "missing navigable endpoint: " + response.getResult());
                    var source = new ReadSymbolTool(provider).execute(
                            GraphToolSupport.JSON.createObjectNode().put("symbol_id", id).toString(), context);
                    assertTrue(source.isSuccess(), source.getError());
                    assertTrue(source.getResult().contains("symbol_id: " + id));
                }
            }
        }
    }

    @Test
    void sameSimpleTypeNameOverloadsNeverShareCallers(@TempDir Path repo) throws Exception {
        Path root = repo.resolve("src/main/java");
        for (String pkg : new String[]{"demo", "left", "right"}) Files.createDirectories(root.resolve(pkg));
        Files.writeString(root.resolve("left/Item.java"), "package left; public class Item {}");
        Files.writeString(root.resolve("right/Item.java"), "package right; public class Item {}");
        Files.writeString(root.resolve("demo/Service.java"), """
                package demo;
                import left.Item;
                class Service {
                    void use(right.Item item) {}
                    void use(Item item) {}
                    void fromLeft() { use(new left.Item()); }
                    void fromRight() { use(new right.Item()); }
                }
                """);
        var provider = new ProjectSnapshotManager().lazyProvider(ProjectKey.of(repo, "distinct-overloads"));
        for (String parameter : new String[]{"Item", "right.Item"}) {
            var response = new QueryRelationsTool(provider).execute(GraphToolSupport.JSON.createObjectNode()
                    .put("subject_symbol_id", "java:demo.Service#use(" + parameter + ")")
                    .put("relation", "callers").toString(), new AgentContext(repo));
            assertTrue(response.isSuccess(), response.getError());
            JsonNode page = GraphToolSupport.JSON.readTree(response.getResult());
            assertTrue(page.path("relationships").isEmpty(), response.getResult());
            assertFalse(page.path("unresolved_relationships").isEmpty(), response.getResult());
            assertEquals("partial", page.path("coverage").asText());
            // 不同的完整符号标识保持独立；歧义求解结果不能合并声明或生成确定的调用关系。
            assertEquals("java:demo.Service#use(" + parameter + ")", page.path("subject_symbol_id").asText());
        }
    }
}
