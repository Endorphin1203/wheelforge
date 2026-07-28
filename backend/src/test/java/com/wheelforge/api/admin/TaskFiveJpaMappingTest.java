package com.wheelforge.api.admin;

import static org.assertj.core.api.Assertions.assertThat;

import com.wheelforge.api.artifact.ArtifactEntity;
import com.wheelforge.api.artifact.DownloadRecordEntity;
import jakarta.persistence.Column;
import jakarta.persistence.Table;
import java.lang.reflect.Field;
import java.util.Arrays;
import java.util.Set;
import java.util.stream.Collectors;
import org.hibernate.annotations.JdbcTypeCode;
import org.junit.jupiter.api.Test;

class TaskFiveJpaMappingTest {
  @Test
  void mapsArtifactAndDownloadAuditToTheExactBaselineColumns() {
    assertMapping(
        ArtifactEntity.class,
        "artifacts",
        Set.of(
            "id",
            "build_task_id",
            "artifact_type",
            "filename",
            "object_key",
            "size_bytes",
            "sha256",
            "build_status",
            "validation_type",
            "expires_at",
            "download_count",
            "cleaned_at",
            "created_at",
            "version_no"));
    assertMapping(
        DownloadRecordEntity.class,
        "download_records",
        Set.of(
            "id",
            "artifact_id",
            "user_id",
            "ip_address",
            "user_agent",
            "completed",
            "downloaded_at"));
  }

  @Test
  void mapsAdminTablesAndKeepsSystemConfigAsStructuredJson() {
    assertMapping(
        PackageSourceEntity.class,
        "package_sources",
        Set.of(
            "id",
            "code",
            "display_name",
            "base_url",
            "priority_no",
            "enabled",
            "timeout_seconds",
            "failure_count",
            "version_no",
            "updated_at"));
    assertMapping(
        SystemConfigEntity.class,
        "system_config",
        Set.of(
            "config_key", "config_value", "description", "updated_by", "updated_at", "version_no"));
    assertThat(field(SystemConfigEntity.class, "configValue").getAnnotation(JdbcTypeCode.class))
        .isNotNull();
  }

  private static void assertMapping(Class<?> entityType, String tableName, Set<String> columns) {
    assertThat(entityType.getAnnotation(Table.class).name()).isEqualTo(tableName);
    assertThat(
            Arrays.stream(entityType.getDeclaredFields())
                .map(field -> field.getAnnotation(Column.class))
                .filter(java.util.Objects::nonNull)
                .map(Column::name)
                .collect(Collectors.toSet()))
        .isEqualTo(columns);
  }

  private static Field field(Class<?> type, String name) {
    try {
      return type.getDeclaredField(name);
    } catch (NoSuchFieldException exception) {
      throw new AssertionError(exception);
    }
  }
}
