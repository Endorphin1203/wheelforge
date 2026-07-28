package com.wheelforge.api.db;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.Test;

class TaskFiveForwardMigrationSqlTest {
  @Test
  void v2ReconcilesBuiltInsWithoutOverwritingOperationalOrConfigValues() throws Exception {
    String migration = migration("V2__seed_builtin_data.sql");
    String sourceMigration = migration.substring(0, migration.indexOf("insert into system_config"));

    assertThat(sourceMigration)
        .contains("where code = 'tsinghua'")
        .contains("where code = 'aliyun'")
        .contains("where code = 'pypi'")
        .contains("where not exists")
        .contains("select 1 from package_sources where code =")
        .contains("case when not exists")
        .contains("select 1 from package_sources where id =")
        .contains("else uuid()")
        .doesNotContain("on duplicate key update")
        .doesNotContain("enabled =")
        .doesNotContain("priority_no =")
        .doesNotContain("timeout_seconds =")
        .doesNotContain("failure_count =")
        .doesNotContain("version_no =");
    assertThat(migration).doesNotContain("config_value = values(config_value)");
  }

  @Test
  void v3AddsRetentionPaginationAndActiveDownloadIndexes() throws Exception {
    String migration = migration("V3__artifact_retention_and_pagination_indexes.sql");

    assertThat(migration)
        .contains("artifacts(cleaned_at, expires_at, id)")
        .contains("artifacts(build_task_id, created_at, id)")
        .contains("download_records(artifact_id, completed, downloaded_at)");
  }

  private String migration(String name) throws IOException {
    try (var input = getClass().getResourceAsStream("/db/migration/" + name)) {
      assertThat(input).as(name).isNotNull();
      return new String(input.readAllBytes(), StandardCharsets.UTF_8).toLowerCase();
    }
  }
}
