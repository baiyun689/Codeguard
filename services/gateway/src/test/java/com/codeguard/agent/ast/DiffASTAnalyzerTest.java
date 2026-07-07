package com.codeguard.agent.ast;

import org.junit.jupiter.api.Test;
import java.util.List;
import static org.junit.jupiter.api.Assertions.*;

class DiffASTAnalyzerTest {

    @Test
    void parsesSimpleClassWithMethod() {
        String source = """
            package com.example;
            import java.util.List;
            public class OrderService extends BaseService implements Auditable {
                private final OrderRepository orderRepo;
                public BigDecimal calculatePrice(Order order) {
                    return orderRepo.findById(order.getId());
                }
            }
            """;
        DiffASTResult r = DiffASTAnalyzer.analyze("src/main/java/com/example/OrderService.java", source);
        assertTrue(r.parseSucceeded());
        assertEquals(1, r.classes().size());
        DiffASTResult.ClassDef cls = r.classes().get(0);
        assertEquals("OrderService", cls.name());
        assertEquals("class", cls.type());
        assertEquals("BaseService", cls.superClass());
        assertEquals(List.of("Auditable"), cls.interfaces());
        assertTrue(cls.fields().contains("OrderRepository orderRepo"));
        assertEquals(1, r.methods().size());
        DiffASTResult.MethodDef m = r.methods().get(0);
        assertEquals("calculatePrice", m.name());
        assertEquals("BigDecimal", m.returnType());
        assertEquals("public", m.visibility());
    }

    @Test
    void parsesMethodAnnotationsAndModifiers() {
        String source = """
            public class Util {
                @Override
                @Deprecated
                public static final synchronized void process(String input) {}
            }
            """;
        DiffASTResult r = DiffASTAnalyzer.analyze("Util.java", source);
        assertTrue(r.parseSucceeded());
        assertEquals(1, r.methods().size());
        DiffASTResult.MethodDef m = r.methods().get(0);
        assertEquals("process", m.name());
        assertEquals("public", m.visibility());
        assertTrue(m.modifiers().contains("static"));
        assertTrue(m.modifiers().contains("final"));
        assertTrue(m.modifiers().contains("synchronized"));
        assertTrue(m.annotations().contains("@Override"));
        assertTrue(m.annotations().contains("@Deprecated"));
    }

    @Test
    void parsesCallEdges() {
        String source = """
            public class Service {
                public void doWork() {
                    userRepo.save(new User());
                    log.info("done");
                    helper.audit();
                }
            }
            """;
        DiffASTResult r = DiffASTAnalyzer.analyze("Service.java", source);
        assertTrue(r.parseSucceeded());
        List<DiffASTResult.CallEdgeDef> edges = r.callEdges();
        assertEquals(3, edges.size());
        assertTrue(edges.stream().allMatch(e -> e.callerMethod().equals("doWork")));
        assertTrue(edges.stream().anyMatch(e -> e.calleeMethod().equals("save") && e.calleeScope().equals("userRepo")));
        assertTrue(edges.stream().anyMatch(e -> e.calleeMethod().equals("info") && e.calleeScope().equals("log")));
        assertTrue(edges.stream().anyMatch(e -> e.calleeMethod().equals("audit") && e.calleeScope().equals("helper")));
    }

    @Test
    void parsesControlFlow() {
        String source = """
            public class Logic {
                public void check(int x) {
                    if (x > 0) {
                        for (int i = 0; i < x; i++) {
                            try { doThing(); } catch (Exception e) {}
                        }
                    }
                }
            }
            """;
        DiffASTResult r = DiffASTAnalyzer.analyze("Logic.java", source);
        assertTrue(r.parseSucceeded());
        List<DiffASTResult.CFNode> cfs = r.controlFlowNodes();
        assertEquals(3, cfs.size());
        assertTrue(cfs.stream().anyMatch(n -> n.type().equals("IF")));
        assertTrue(cfs.stream().anyMatch(n -> n.type().equals("FOR")));
        assertTrue(cfs.stream().anyMatch(n -> n.type().equals("TRY_CATCH")));
    }

    @Test
    void parseFailureReturnsNotSucceeded() {
        DiffASTResult r = DiffASTAnalyzer.analyze("Bad.java", "not valid java {{{");
        assertFalse(r.parseSucceeded());
        assertTrue(r.classes().isEmpty());
        assertTrue(r.methods().isEmpty());
    }

    @Test
    void emptyFileReturnsNotSucceeded() {
        DiffASTResult r = DiffASTAnalyzer.analyze("Empty.java", "");
        assertFalse(r.parseSucceeded());
    }

    @Test
    void interfaceAndEnum() {
        String source = """
            public interface Repository {
                void save(Entity e);
            }
            """;
        DiffASTResult r = DiffASTAnalyzer.analyze("Repository.java", source);
        assertTrue(r.parseSucceeded());
        assertEquals("interface", r.classes().get(0).type());
        assertEquals(1, r.methods().size());
    }

    @Test
    void packagePrivateVisibility() {
        String source = """
            class Helper {
                void doInternal() {}
            }
            """;
        DiffASTResult r = DiffASTAnalyzer.analyze("Helper.java", source);
        assertTrue(r.parseSucceeded());
        assertEquals("package-private", r.methods().get(0).visibility());
    }
}
