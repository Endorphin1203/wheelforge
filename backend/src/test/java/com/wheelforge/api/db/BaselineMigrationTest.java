package com.wheelforge.api.db;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.Connection;
import java.sql.DatabaseMetaData;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.List;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

@EnabledIfEnvironmentVariable(named = "WF_TEST_JDBC_URL", matches = ".+")
class BaselineMigrationTest {
  private static final List<String> REQUIRED_TABLES =
      List.of(
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
          "system_config");

  private static final List<String> REQUIRED_INDEXES =
      List.of(
          "ix_job_ready",
          "ix_job_lease",
          "ix_item_file",
          "uk_resolved_task_name",
          "ix_resolved_task",
          "uk_log_sequence",
          "ix_log_cursor",
          "ix_artifact_task",
          "ix_artifact_expiry",
          "ix_artifact_retention",
          "ix_artifact_task_created",
          "ix_download_artifact",
          "ix_download_active_lease",
          "ix_download_user_time",
          "ix_requirement_file_user",
          "ix_build_user_created",
          "ix_build_file",
          "ix_build_profile");

  @Test
  void createsBaselineSchema() throws Exception {
    var flyway =
        Flyway.configure()
            .dataSource(
                System.getenv("WF_TEST_JDBC_URL"),
                System.getenv("WF_TEST_DATABASE_USER"),
                System.getenv("WF_TEST_DATABASE_PASSWORD"))
            .locations("classpath:db/migration")
            .load();

    flyway.migrate();

    try (var connection = flyway.getConfiguration().getDataSource().getConnection()) {
      for (String table : REQUIRED_TABLES) {
        assertThat(tableExists(connection, table)).as("table %s", table).isTrue();
      }
      for (String index : REQUIRED_INDEXES) {
        assertThat(indexExists(connection, index)).as("index %s", index).isTrue();
      }
    }

    assertThat(flyway.info().current().getVersion().getVersion()).isEqualTo("4");
  }

  private boolean tableExists(Connection connection, String tableName) throws SQLException {
    DatabaseMetaData metadata = connection.getMetaData();
    try (ResultSet tables =
        metadata.getTables(connection.getCatalog(), null, tableName, new String[] {"TABLE"})) {
      return tables.next();
    }
  }

  private boolean indexExists(Connection connection, String indexName) throws SQLException {
    DatabaseMetaData metadata = connection.getMetaData();
    for (String table : REQUIRED_TABLES) {
      try (ResultSet indexes =
          metadata.getIndexInfo(connection.getCatalog(), null, table, false, false)) {
        while (indexes.next()) {
          if (indexName.equalsIgnoreCase(indexes.getString("INDEX_NAME"))) {
            return true;
          }
        }
      }
    }
    return false;
  }
}
