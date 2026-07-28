package com.wheelforge.api.build;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;

import com.wheelforge.api.common.ApiException;
import com.wheelforge.api.requirements.RequirementItemEntity;
import com.wheelforge.api.requirements.RequirementItemRepository;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.data.domain.Pageable;
import org.springframework.http.HttpStatus;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

@ExtendWith(MockitoExtension.class)
class BuildResultServiceTest {
  private static final UUID USER_ID = UUID.fromString("cae6ea8d-0afe-41df-aeea-fc0e5aceabfb");
  private static final UUID TASK_ID = UUID.fromString("fe3b9a09-e696-4104-beb7-d8fd1fb85d24");
  private static final String FILE_ID = "2b0e75d6-c238-4f39-9bf9-5e246eb7c4cd";
  private static final LocalDateTime CREATED_AT = LocalDateTime.of(2026, 7, 23, 1, 2, 3);

  @Mock private BuildTaskRepository taskRepository;
  @Mock private BuildLogRepository logRepository;
  @Mock private ResolvedPackageRepository resolvedRepository;
  @Mock private RequirementItemRepository itemRepository;

  private BuildResultService service;

  @BeforeEach
  void setUp() {
    service =
        new BuildResultService(taskRepository, logRepository, resolvedRepository, itemRepository);
  }

  @Test
  void readsOnlySequencesStrictlyAfterTheCursorAscendingAndCapsOnePollAt500() {
    givenOwnedTask();
    given(
            logRepository.findByBuildTaskIdAndSequenceNoGreaterThanOrderBySequenceNoAsc(
                eq(TASK_ID.toString()), eq(41L), any(Pageable.class)))
        .willReturn(
            List.of(
                log(42, "first", object("{\"step\":1}")),
                log(43, "second", object("{\"step\":2}"))));

    assertThat(service.logs(USER_ID, TASK_ID, 41))
        .extracting(BuildResultService.BuildLogView::sequence)
        .containsExactly(42L, 43L);

    var pageable = ArgumentCaptor.forClass(Pageable.class);
    verify(logRepository)
        .findByBuildTaskIdAndSequenceNoGreaterThanOrderBySequenceNoAsc(
            eq(TASK_ID.toString()), eq(41L), pageable.capture());
    assertThat(pageable.getValue().getPageSize()).isEqualTo(500);
    assertThat(pageable.getValue().getPageNumber()).isZero();
  }

  @Test
  void rejectsNegativeLogCursorBeforeReadingTaskOrLogs() {
    assertThatThrownBy(() -> service.logs(USER_ID, TASK_ID, -1))
        .isInstanceOf(ApiException.class)
        .satisfies(
            exception -> {
              var apiException = (ApiException) exception;
              assertThat(apiException.status()).isEqualTo(HttpStatus.BAD_REQUEST);
              assertThat(apiException.code()).isEqualTo("INVALID_LOG_CURSOR");
            });

    verifyNoInteractions(taskRepository, logRepository);
  }

  @Test
  void returnsStructuredResolvedPackageJsonWithoutPersistenceIdentifiers() {
    givenOwnedTask();
    JsonNode attempts = object("[{\"version\":\"2.32.4\",\"selected\":true}]");
    given(resolvedRepository.findAllByBuildTaskIdOrderByNormalizedNameAsc(TASK_ID.toString()))
        .willReturn(
            List.of(
                resolved(
                    "requests",
                    "2.32.4",
                    "DIRECT",
                    ">=2.31",
                    "2.31.0",
                    "UPGRADE",
                    "Selected compatible Wheel",
                    attempts,
                    "STATIC_PASSED",
                    "PYPI")));

    BuildResultService.ResolvedPackageView result =
        service.resolvedPackages(USER_ID, TASK_ID).getFirst();

    assertThat(result.normalizedName()).isEqualTo("requests");
    assertThat(result.candidateAttempts().path(0).path("selected").asBoolean()).isTrue();
    assertThat(result.wheelTags()).containsExactly("py3-none-any");
    assertThat(result.sha256()).isEqualTo("a".repeat(64));
  }

  @Test
  void ordersFirstSupportedDirectOccurrencesThenAlphabeticalTransitivesAndKeepsStoredDirections() {
    givenOwnedTask();
    given(itemRepository.findAllByRequirementFileIdOrderByLineNoAsc(FILE_ID))
        .willReturn(
            List.of(
                item(2, "requests", ">=99", true),
                item(4, "urllib3", "~=2.0", true),
                item(6, "numpy", "==2.0.0", true),
                item(7, "missing", "==1.0", true),
                item(8, "requests", "<3", true),
                item(1, "unsupported", "", false)));
    given(resolvedRepository.findAllByBuildTaskIdOrderByNormalizedNameAsc(TASK_ID.toString()))
        .willReturn(
            List.of(
                resolved(
                    "certifi",
                    null,
                    "TRANSITIVE",
                    null,
                    null,
                    "ADDED",
                    "Dependency",
                    object("[]"),
                    "DOWNLOADED",
                    "PYPI"),
                resolved(
                    "requests",
                    "1.0",
                    "DIRECT",
                    "ignored",
                    "99",
                    "UPGRADE",
                    "Stored decision",
                    object("[]"),
                    "STATIC_PASSED",
                    "PYPI"),
                resolved(
                    "anyio",
                    "4.4",
                    "TRANSITIVE",
                    null,
                    null,
                    "ADDED",
                    "Dependency",
                    object("[]"),
                    "DOWNLOADED",
                    "ALIYUN"),
                resolved(
                    "urllib3",
                    "1.26",
                    "DIRECT",
                    "ignored",
                    "2.0",
                    "DOWNGRADE",
                    "Stored decision",
                    object("[]"),
                    "STATIC_PASSED",
                    "TSINGHUA"),
                resolved(
                    "numpy",
                    "2.0.0",
                    "DIRECT",
                    "ignored",
                    "2.0.0",
                    "UNCHANGED",
                    "Stored decision",
                    object("[]"),
                    "STATIC_PASSED",
                    "PYPI")));

    List<VersionComparisonRow> rows = service.versionComparison(USER_ID, TASK_ID);

    assertThat(rows)
        .extracting(VersionComparisonRow::packageName)
        .containsExactly("requests", "urllib3", "numpy", "missing", "anyio", "certifi");
    assertThat(rows)
        .extracting(VersionComparisonRow::changeDirection)
        .containsExactly("UPGRADE", "DOWNGRADE", "UNCHANGED", "UNRESOLVED", "ADDED", "ADDED");
    assertThat(rows.getFirst().originalConstraint()).isEqualTo(">=99");
    assertThat(rows.getFirst().finalVersion()).isEqualTo("1.0");
    assertThat(rows.get(3))
        .isEqualTo(
            new VersionComparisonRow(
                "missing",
                "DIRECT",
                "==1.0",
                null,
                null,
                "UNRESOLVED",
                "No compatible Wheel was resolved",
                "MISSING",
                null));
    assertThat(rows.subList(4, 6))
        .extracting(VersionComparisonRow::dependencyType)
        .containsOnly("TRANSITIVE");
    verify(logRepository, never())
        .findByBuildTaskIdAndSequenceNoGreaterThanOrderBySequenceNoAsc(
            any(), anyLong(), any(Pageable.class));
  }

  @Test
  void treatsUnknownCrossUserAndSoftDeletedTasksIdenticallyAcrossResultReads() {
    given(
            taskRepository.findByIdAndUserIdAndDeletedAtIsNull(
                TASK_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.empty());

    assertNotFound(() -> service.logs(USER_ID, TASK_ID, 0));
    assertNotFound(() -> service.resolvedPackages(USER_ID, TASK_ID));
    assertNotFound(() -> service.versionComparison(USER_ID, TASK_ID));

    verifyNoInteractions(logRepository, resolvedRepository, itemRepository);
  }

  private void givenOwnedTask() {
    given(
            taskRepository.findByIdAndUserIdAndDeletedAtIsNull(
                TASK_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(task()));
  }

  private BuildTaskEntity task() {
    return new BuildTaskEntity(
        TASK_ID.toString(),
        USER_ID.toString(),
        FILE_ID,
        "d29ec24f-095c-45f6-ac96-acdef0e7d8ad",
        null,
        BuildStatus.SUCCESS,
        SolveMode.COMPATIBLE,
        new BuildTaskEntity.TargetSnapshot(
            "d29ec24f-095c-45f6-ac96-acdef0e7d8ad",
            "linux-aarch64-cp311",
            "LINUX",
            "AARCH64",
            "CPYTHON",
            "3.11",
            "3.11.9",
            "manylinux2014_aarch64",
            List.of("cp311", "abi3", "none"),
            "STATIC",
            "wheel-tags-v1",
            0),
        CREATED_AT,
        null);
  }

  private BuildLogEntity log(long sequence, String message, JsonNode context) {
    return new BuildLogEntity(
        UUID.randomUUID().toString(),
        TASK_ID.toString(),
        sequence,
        "DOWNLOAD",
        "INFO",
        message,
        context,
        CREATED_AT);
  }

  private ResolvedPackageEntity resolved(
      String name,
      String finalVersion,
      String dependencyType,
      String originalConstraint,
      String strictVersion,
      String changeDirection,
      String changeReason,
      JsonNode attempts,
      String wheelStatus,
      String packageSource) {
    return new ResolvedPackageEntity(
        UUID.randomUUID().toString(),
        TASK_ID.toString(),
        name,
        finalVersion,
        dependencyType,
        originalConstraint,
        strictVersion,
        changeDirection,
        changeReason,
        attempts,
        name + "-wheel.whl",
        List.of("py3-none-any"),
        packageSource,
        "a".repeat(64),
        wheelStatus,
        null);
  }

  private RequirementItemEntity item(
      int lineNumber, String normalizedName, String specifier, boolean supported) {
    return new RequirementItemEntity(
        UUID.randomUUID().toString(),
        FILE_ID,
        lineNumber,
        normalizedName,
        List.of(),
        specifier,
        null,
        normalizedName + specifier,
        supported,
        supported ? null : "UNSUPPORTED",
        supported ? null : "Unsupported requirement");
  }

  private JsonNode object(String json) {
    try {
      return new ObjectMapper().readTree(json);
    } catch (Exception exception) {
      throw new AssertionError(exception);
    }
  }

  private void assertNotFound(Runnable read) {
    assertThatThrownBy(read::run)
        .isInstanceOf(ApiException.class)
        .satisfies(
            exception -> {
              var apiException = (ApiException) exception;
              assertThat(apiException.status()).isEqualTo(HttpStatus.NOT_FOUND);
              assertThat(apiException.code()).isEqualTo("NOT_FOUND");
              assertThat(apiException.getMessage()).isEqualTo("Build task was not found");
            });
  }
}
