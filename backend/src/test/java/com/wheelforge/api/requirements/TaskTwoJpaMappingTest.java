package com.wheelforge.api.requirements;

import static org.assertj.core.api.Assertions.assertThat;

import com.wheelforge.api.common.jobs.BuildJobEntity;
import com.wheelforge.api.target.TargetProfileEntity;
import jakarta.persistence.Column;
import jakarta.persistence.Table;
import java.lang.reflect.Field;
import java.util.Arrays;
import java.util.Set;
import java.util.stream.Collectors;
import org.hibernate.annotations.JdbcTypeCode;
import org.junit.jupiter.api.Test;

class TaskTwoJpaMappingTest {
  @Test
  void mapsRequirementFilesToTheExactFlywayColumns() {
    assertMapping(
        RequirementFileEntity.class,
        "requirement_files",
        Set.of(
            "id",
            "user_id",
            "original_name",
            "detected_encoding",
            "size_bytes",
            "sha256",
            "original_object_key",
            "normalized_object_key",
            "parse_status",
            "parse_error",
            "created_at",
            "version_no"));
  }

  @Test
  void mapsRequirementItemsToTheExactFlywayColumns() {
    assertMapping(
        RequirementItemEntity.class,
        "requirement_items",
        Set.of(
            "id",
            "requirement_file_id",
            "line_no",
            "normalized_name",
            "extras_json",
            "specifier",
            "marker_text",
            "original_text",
            "supported",
            "error_code",
            "error_message"));
    assertThat(field(RequirementItemEntity.class, "extras").getAnnotation(JdbcTypeCode.class))
        .isNotNull();
  }

  @Test
  void mapsTargetProfilesToTheExactFlywayColumns() {
    assertMapping(
        TargetProfileEntity.class,
        "target_profiles",
        Set.of(
            "id",
            "code",
            "os",
            "architecture",
            "python_implementation",
            "python_version",
            "python_full_version",
            "platform_tag",
            "abi_tags",
            "validation_type",
            "validation_policy_version",
            "enabled",
            "version_no"));
    assertThat(field(TargetProfileEntity.class, "abiTags").getAnnotation(JdbcTypeCode.class))
        .isNotNull();
  }

  @Test
  void mapsBuildJobsToTheExactFlywayColumns() {
    assertMapping(
        BuildJobEntity.class,
        "build_jobs",
        Set.of(
            "id",
            "job_type",
            "payload_version",
            "subject_id",
            "payload_json",
            "status",
            "priority_no",
            "available_at",
            "attempts",
            "max_attempts",
            "lease_owner",
            "execution_id",
            "lease_expires_at",
            "heartbeat_at",
            "last_error",
            "created_at",
            "started_at",
            "finished_at",
            "version_no"));
    assertThat(field(BuildJobEntity.class, "payloadJson").getAnnotation(JdbcTypeCode.class))
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
