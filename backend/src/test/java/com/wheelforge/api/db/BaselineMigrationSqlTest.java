package com.wheelforge.api.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

class BaselineMigrationSqlTest {
    private static final List<String> REQUIRED_TABLES = List.of(
        "users",
        "requirement_files",
        "requirement_items",
        "target_profiles",
        "build_tasks",
        "build_jobs",
        "resolved_packages",
        "build_logs",
        "package_sources",
        "artifacts",
        "download_records",
        "system_config"
    );

    @Test
    void declaresRequiredInnoDbUtf8mb4Tables() throws IOException {
        String migration = readMigration();

        for (String table : REQUIRED_TABLES) {
            assertThat(migration)
                .containsPattern("(?is)create\\s+table\\s+" + table + "\\s*\\(.*?\\)\\s*engine\\s*=\\s*InnoDB\\s+default\\s+charset\\s*=\\s*utf8mb4");
        }
    }

    private String readMigration() throws IOException {
        try (InputStream input = getClass().getResourceAsStream("/db/migration/V1__baseline.sql")) {
            assertThat(input).as("baseline migration resource").isNotNull();
            return new String(input.readAllBytes(), StandardCharsets.UTF_8);
        }
    }
}
