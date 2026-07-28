package com.wheelforge.api.db;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.ResultSet;
import org.flywaydb.core.Flyway;
import org.flywaydb.core.api.MigrationVersion;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

@EnabledIfEnvironmentVariable(named = "WF_TEST_JDBC_URL", matches = ".+")
class TaskFiveMigrationUpgradeTest {
  @Test
  void upgradesPartiallySeededV1WithoutLosingOperationalValues() throws Exception {
    Flyway v1 = configure().target(MigrationVersion.fromVersion("1")).load();
    v1.clean();
    v1.migrate();
    try (var connection = v1.getConfiguration().getDataSource().getConnection();
        var statement = connection.createStatement()) {
      statement.executeUpdate(
          """
          insert into package_sources
            (id, code, display_name, base_url, priority_no, enabled, timeout_seconds,
             failure_count, version_no, updated_at)
          values
            ('90000000-0000-0000-0000-000000000001', 'TSINGHUA', 'Tampered',
             'https://evil.example/simple', 77, false, 99, 5, 4, '2026-07-27 00:00:00')
          """);
      statement.executeUpdate(
          """
          insert into system_config
            (config_key, config_value, description, updated_by, updated_at, version_no)
          values
            ('retentionEnabled', 'false', 'Existing operator value', null,
             '2026-07-27 00:00:00', 7)
          """);
    }

    Flyway latest = configure().load();
    latest.migrate();

    try (var connection = latest.getConfiguration().getDataSource().getConnection();
        var statement = connection.createStatement()) {
      try (ResultSet source =
          statement.executeQuery(
              """
              select display_name, base_url, priority_no, enabled, timeout_seconds,
                     failure_count, version_no
                from package_sources where code = 'TSINGHUA'
              """)) {
        assertThat(source.next()).isTrue();
        assertThat(source.getString("display_name")).isEqualTo("Tsinghua PyPI");
        assertThat(source.getString("base_url"))
            .isEqualTo("https://pypi.tuna.tsinghua.edu.cn/simple");
        assertThat(source.getInt("priority_no")).isEqualTo(77);
        assertThat(source.getBoolean("enabled")).isFalse();
        assertThat(source.getInt("timeout_seconds")).isEqualTo(99);
        assertThat(source.getLong("failure_count")).isEqualTo(5);
        assertThat(source.getLong("version_no")).isEqualTo(4);
      }
      try (ResultSet config =
          statement.executeQuery(
              """
              select config_value, description, version_no
                from system_config where config_key = 'retentionEnabled'
              """)) {
        assertThat(config.next()).isTrue();
        assertThat(config.getString("config_value")).isEqualTo("false");
        assertThat(config.getString("description")).isEqualTo("Existing operator value");
        assertThat(config.getLong("version_no")).isEqualTo(7);
      }
    }
    assertThat(latest.info().current().getVersion().getVersion()).isEqualTo("3");
  }

  private org.flywaydb.core.api.configuration.FluentConfiguration configure() {
    return Flyway.configure()
        .cleanDisabled(false)
        .dataSource(
            System.getenv("WF_TEST_JDBC_URL"),
            System.getenv("WF_TEST_DATABASE_USER"),
            System.getenv("WF_TEST_DATABASE_PASSWORD"))
        .locations("classpath:db/migration");
  }
}
