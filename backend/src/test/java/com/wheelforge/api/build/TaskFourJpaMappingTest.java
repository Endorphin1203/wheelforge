package com.wheelforge.api.build;

import static org.assertj.core.api.Assertions.assertThat;

import jakarta.persistence.Column;
import jakarta.persistence.Table;
import java.lang.reflect.Field;
import java.util.Arrays;
import java.util.Set;
import java.util.stream.Collectors;
import org.hibernate.annotations.JdbcTypeCode;
import org.junit.jupiter.api.Test;

class TaskFourJpaMappingTest {
  @Test
  void mapsBuildLogsToTheExactBaselineColumnsWithStructuredContextJson() {
    assertMapping(
        BuildLogEntity.class,
        "build_logs",
        Set.of(
            "id",
            "build_task_id",
            "sequence_no",
            "stage",
            "level",
            "message",
            "context_json",
            "created_at"));
    assertThat(field(BuildLogEntity.class, "context").getAnnotation(JdbcTypeCode.class))
        .isNotNull();
  }

  @Test
  void mapsResolvedPackagesToTheExactBaselineColumnsWithStructuredJsonFields() {
    assertMapping(
        ResolvedPackageEntity.class,
        "resolved_packages",
        Set.of(
            "id",
            "build_task_id",
            "normalized_name",
            "final_version",
            "dependency_type",
            "original_constraint",
            "strict_version",
            "change_direction",
            "change_reason",
            "attempts_json",
            "wheel_filename",
            "wheel_tags",
            "package_source_code",
            "sha256",
            "wheel_status",
            "error_message"));
    assertThat(
            field(ResolvedPackageEntity.class, "candidateAttempts")
                .getAnnotation(JdbcTypeCode.class))
        .isNotNull();
    assertThat(field(ResolvedPackageEntity.class, "wheelTags").getAnnotation(JdbcTypeCode.class))
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
