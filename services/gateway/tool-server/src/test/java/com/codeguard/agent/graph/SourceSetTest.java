package com.codeguard.agent.graph;

import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

import static org.junit.jupiter.api.Assertions.assertEquals;

class SourceSetTest {
    @ParameterizedTest
    @CsvSource({
            "src/main/java/demo/App.java, MAIN",
            "src/test/java/demo/AppTest.java, TEST",
            "src/testFixtures/java/demo/Fixture.java, TEST",
            "target/generated-test-sources/demo/GeneratedTest.java, TEST",
            "target/generated-sources/demo/Generated.java, GENERATED"
    })
    void classifiesCommonJavaSourceLayouts(String path, SourceSet expected) {
        assertEquals(expected, SourceSet.fromPath(path));
    }
}
