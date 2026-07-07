package com.codeguard.agent.ast;

import org.junit.jupiter.api.Test;
import java.util.List;
import static org.junit.jupiter.api.Assertions.*;

class ASTContextFormatterTest {

    @Test
    void formatsSimpleClassTier0() {
        DiffASTResult r = new DiffASTResult("Service.java", true,
                List.of(new DiffASTResult.ClassDef("Service", "class", "", List.of(),
                        List.of("Repo repo"), 1, 20)),
                List.of(new DiffASTResult.MethodDef("run", "void",
                        List.of("String"), List.of("input"),
                        "public", List.of(), List.of("@Override"), 5, 10)),
                List.of(new DiffASTResult.CFNode("IF", 6, 8, "input == null")),
                List.of(new DiffASTResult.CallEdgeDef("run", "save", "repo", 7)));
        String text = ASTContextFormatter.format(r, fakeDiff("Service.java", 6), 5000);
        assertTrue(text.contains("AST for:"));
        assertTrue(text.contains("class: Service"));
        assertTrue(text.contains("@Override public void run"));
        assertTrue(text.contains("-> calls: repo.save"));
        assertTrue(text.contains("IF [L6-L8] input == null"));
    }

    @Test
    void triggersTier1WhenOverBudget() {
        List<DiffASTResult.MethodDef> methods = new java.util.ArrayList<>();
        for (int i = 0; i < 50; i++) {
            methods.add(new DiffASTResult.MethodDef("veryLongMethodName" + i, "VeryLongReturnType",
                    List.of("VeryLongParamType"), List.of("veryLongParamName"),
                    "public", List.of("static"), List.of("@Override"), i * 2 + 2, i * 2 + 3));
        }
        DiffASTResult r = new DiffASTResult("Big.java", true,
                List.of(new DiffASTResult.ClassDef("Big", "class", "", List.of(), List.of(), 1, 100)),
                methods, List.of(), List.of());
        String text = ASTContextFormatter.format(r, fakeDiff("Big.java", 42), 250);
        assertTrue(text.contains("AST for:"));
        assertTrue(text.contains("class: Big"));
        assertTrue(text.contains("Methods (changed)"));
    }

    @Test
    void triggersTier2WhenStillOverBudget() {
        List<DiffASTResult.MethodDef> methods = new java.util.ArrayList<>();
        for (int i = 0; i < 100; i++) {
            methods.add(new DiffASTResult.MethodDef("m" + i, "void", List.of(), List.of(),
                    "public", List.of(), List.of(), i * 2 + 2, i * 2 + 3));
        }
        DiffASTResult r = new DiffASTResult("Huge.java", true,
                List.of(new DiffASTResult.ClassDef("Huge", "class", "", List.of(), List.of(), 1, 200)),
                methods, List.of(), List.of());
        String text = ASTContextFormatter.format(r, fakeDiff("Huge.java", 5), 60);
        assertTrue(text.contains("AST for:"));
        assertTrue(text.contains("class: Huge"));
        assertTrue(text.contains("Methods: "));
    }

    @Test
    void methodsSortedWithChangedOnesFirst() {
        DiffASTResult r = new DiffASTResult("Svc.java", true,
                List.of(new DiffASTResult.ClassDef("Svc", "class", "", List.of(), List.of(), 1, 30)),
                List.of(
                        new DiffASTResult.MethodDef("unchanged", "void", List.of(), List.of(),
                                "public", List.of(), List.of(), 2, 4),
                        new DiffASTResult.MethodDef("changed", "void", List.of(), List.of(),
                                "public", List.of(), List.of(), 10, 12)
                ),
                List.of(), List.of());
        String text = ASTContextFormatter.format(r, fakeDiff("Svc.java", 10), 5000);
        int idxChanged = text.indexOf("changed");
        int idxUnchanged = text.indexOf("unchanged");
        assertTrue(idxChanged >= 0 && idxUnchanged >= 0,
                "Both methods should appear in output");
        assertTrue(idxChanged < idxUnchanged,
                "diff-range methods should appear before non-diff methods, but 'changed' at " + idxChanged + " was after 'unchanged' at " + idxUnchanged);
    }

    @Test
    void packagePrivateVisibilityOmitted() {
        DiffASTResult r = new DiffASTResult("Pkg.java", true,
                List.of(new DiffASTResult.ClassDef("Pkg", "class", "", List.of(), List.of(), 1, 5)),
                List.of(new DiffASTResult.MethodDef("doIt", "void", List.of(), List.of(),
                        "package-private", List.of(), List.of(), 2, 4)),
                List.of(), List.of());
        String text = ASTContextFormatter.format(r, fakeDiff("Pkg.java", 2), 5000);
        assertFalse(text.contains("package-private"), "package-private should not appear in output");
        assertTrue(text.contains("void doIt"));
    }

    @Test
    void controlFlowOnlyForChangedLines() {
        DiffASTResult r = new DiffASTResult("Ctrl.java", true,
                List.of(new DiffASTResult.ClassDef("Ctrl", "class", "", List.of(), List.of(), 1, 20)),
                List.of(new DiffASTResult.MethodDef("run", "void", List.of(), List.of(),
                        "public", List.of(), List.of(), 2, 20)),
                List.of(
                        new DiffASTResult.CFNode("IF", 3, 5, "x > 0"),
                        new DiffASTResult.CFNode("FOR", 15, 18, "i < 10")
                ),
                List.of());
        String text = ASTContextFormatter.format(r, fakeDiff("Ctrl.java", 4), 5000);
        assertTrue(text.contains("IF"), "IF in changed range should appear");
        assertFalse(text.contains("FOR"), "FOR outside changed range should NOT appear");
    }

    @Test
    void returnsEmptyForParseFailure() {
        DiffASTResult r = new DiffASTResult("Bad.java", false, List.of(), List.of(), List.of(), List.of());
        String text = ASTContextFormatter.format(r, "", 1000);
        assertEquals("", text);
    }

    /** Construct minimal fake diff text that marks a specific line as changed. */
    private static String fakeDiff(String filePath, int changedLine) {
        return "diff --git a/" + filePath + " b/" + filePath + "\n"
                + "--- a/" + filePath + "\n"
                + "+++ b/" + filePath + "\n"
                + "@@ -" + (changedLine - 1) + ",1 +" + changedLine + ",1 @@\n"
                + "+changed line content\n";
    }
}
