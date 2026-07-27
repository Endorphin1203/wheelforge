package com.wheelforge.api.build;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;

import com.wheelforge.api.common.ApiException;
import com.wheelforge.api.common.jobs.BuildJobEntity;
import com.wheelforge.api.common.jobs.BuildJobRepository;
import com.wheelforge.api.common.jobs.BuildJobService;
import com.wheelforge.api.contracts.JobPayload;
import com.wheelforge.api.requirements.RequirementFileEntity;
import com.wheelforge.api.requirements.RequirementFileRepository;
import com.wheelforge.api.target.TargetProfileEntity;
import com.wheelforge.api.target.TargetProfileRepository;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.HttpStatus;
import org.springframework.transaction.support.TransactionSynchronizationManager;

@ExtendWith(MockitoExtension.class)
class BuildTaskServiceTest {
  private static final UUID USER_ID = UUID.fromString("cae6ea8d-0afe-41df-aeea-fc0e5aceabfb");
  private static final UUID FILE_ID = UUID.fromString("2b0e75d6-c238-4f39-9bf9-5e246eb7c4cd");
  private static final UUID PROFILE_ID = UUID.fromString("d29ec24f-095c-45f6-ac96-acdef0e7d8ad");
  private static final UUID TASK_ID = UUID.fromString("fe3b9a09-e696-4104-beb7-d8fd1fb85d24");
  private static final Instant NOW = Instant.parse("2026-07-23T01:02:03Z");

  @Mock BuildTaskRepository taskRepository;
  @Mock RequirementFileRepository fileRepository;
  @Mock TargetProfileRepository profileRepository;
  @Mock BuildJobRepository jobRepository;

  @AfterEach
  void clearTransactionState() {
    TransactionSynchronizationManager.setActualTransactionActive(false);
  }

  @Test
  void permitsOnlyDeclaredStateTransitions() {
    var stateMachine = new BuildStateMachine();
    Map<BuildStatus, List<BuildStatus>> declared =
        Map.ofEntries(
            Map.entry(
                BuildStatus.CREATED,
                List.of(
                    BuildStatus.PARSING,
                    BuildStatus.QUEUED,
                    BuildStatus.CANCELLED,
                    BuildStatus.FAILED)),
            Map.entry(
                BuildStatus.PARSING,
                List.of(BuildStatus.QUEUED, BuildStatus.CANCELLED, BuildStatus.FAILED)),
            Map.entry(
                BuildStatus.QUEUED,
                List.of(BuildStatus.RESOLVING, BuildStatus.CANCELLED, BuildStatus.FAILED)),
            Map.entry(
                BuildStatus.RESOLVING,
                List.of(BuildStatus.DOWNLOADING, BuildStatus.CANCELLED, BuildStatus.FAILED)),
            Map.entry(
                BuildStatus.DOWNLOADING,
                List.of(BuildStatus.VALIDATING, BuildStatus.CANCELLED, BuildStatus.FAILED)),
            Map.entry(
                BuildStatus.VALIDATING,
                List.of(
                    BuildStatus.PACKAGING,
                    BuildStatus.PARTIAL_SUCCESS,
                    BuildStatus.CANCELLED,
                    BuildStatus.FAILED)),
            Map.entry(
                BuildStatus.PACKAGING,
                List.of(
                    BuildStatus.SUCCESS,
                    BuildStatus.PARTIAL_SUCCESS,
                    BuildStatus.CANCELLED,
                    BuildStatus.FAILED)),
            Map.entry(BuildStatus.SUCCESS, List.of()),
            Map.entry(BuildStatus.PARTIAL_SUCCESS, List.of()),
            Map.entry(BuildStatus.FAILED, List.of()),
            Map.entry(BuildStatus.CANCELLED, List.of()));
    for (BuildStatus from : BuildStatus.values()) {
      for (BuildStatus to : BuildStatus.values()) {
        if (declared.get(from).contains(to)) {
          assertThatCode(() -> stateMachine.requireTransition(from, to)).doesNotThrowAnyException();
        } else {
          assertThatThrownBy(() -> stateMachine.requireTransition(from, to))
              .isInstanceOf(ApiException.class)
              .extracting(exception -> ((ApiException) exception).status())
              .isEqualTo(HttpStatus.CONFLICT);
        }
      }
    }
  }

  @Test
  void createsQueuedTaskWithExactProfileSnapshotAndStrictBuildPayload() throws Exception {
    TransactionSynchronizationManager.setActualTransactionActive(true);
    given(fileRepository.findByIdAndUserId(FILE_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(parsedFile()));
    given(profileRepository.findById(PROFILE_ID.toString()))
        .willReturn(Optional.of(enabledProfile()));
    given(taskRepository.save(any())).willAnswer(invocation -> invocation.getArgument(0));
    given(jobRepository.save(any())).willAnswer(invocation -> invocation.getArgument(0));
    var service = service();

    BuildTaskService.BuildTaskView view =
        service.create(
            USER_ID, new BuildTaskService.CreateBuildTaskRequest(FILE_ID, PROFILE_ID, null));

    assertThat(view.id()).isEqualTo(TASK_ID);
    assertThat(view.status()).isEqualTo(BuildStatus.QUEUED);
    assertThat(view.solveMode()).isEqualTo(SolveMode.COMPATIBLE);
    assertThat(view.targetSnapshot()).isEqualTo(expectedSnapshot());
    var jobCaptor = ArgumentCaptor.forClass(BuildJobEntity.class);
    verify(jobRepository).save(jobCaptor.capture());
    JobPayload wire =
        JobPayload.databaseWireMapper()
            .readValue(jobCaptor.getValue().getPayloadJson(), JobPayload.class);
    assertThat(wire.jobType()).isEqualTo("BUILD");
    assertThat(wire.subjectId()).isEqualTo(TASK_ID.toString());
    assertThat(wire.payload().path("requirementFileId").asText()).isEqualTo(FILE_ID.toString());
    assertThat(wire.payload().path("normalizedObjectKey").asText())
        .isEqualTo("users/alice/normalized.txt");
    assertThat(wire.payload().path("targetSnapshot").path("profileVersion").asLong()).isEqualTo(0);
    assertThat(wire.payload().propertyNames())
        .containsExactlyInAnyOrder(
            "requirementFileId", "normalizedObjectKey", "solveMode", "targetSnapshot");
    assertThat(wire.payload().path("targetSnapshot").propertyNames())
        .containsExactlyInAnyOrder(
            "profileId",
            "profileCode",
            "os",
            "architecture",
            "pythonImplementation",
            "pythonVersion",
            "pythonFullVersion",
            "platformTag",
            "abiTags",
            "validationType",
            "validationPolicyVersion",
            "profileVersion");
  }

  @Test
  void rejectsCreationFromInvalidOwnedInputsAndUnsupportedMode() {
    var service = service();
    given(fileRepository.findByIdAndUserId(FILE_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.empty());
    assertThatThrownBy(
            () ->
                service.create(
                    USER_ID,
                    new BuildTaskService.CreateBuildTaskRequest(FILE_ID, PROFILE_ID, null)))
        .isInstanceOf(ApiException.class)
        .extracting(exception -> ((ApiException) exception).status())
        .isEqualTo(HttpStatus.NOT_FOUND);

    given(fileRepository.findByIdAndUserId(FILE_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(unparsedFile()));
    assertThatThrownBy(
            () ->
                service.create(
                    USER_ID,
                    new BuildTaskService.CreateBuildTaskRequest(FILE_ID, PROFILE_ID, null)))
        .isInstanceOf(ApiException.class)
        .extracting(exception -> ((ApiException) exception).status())
        .isEqualTo(HttpStatus.CONFLICT);

    given(fileRepository.findByIdAndUserId(FILE_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(parsedFile()));
    given(profileRepository.findById(PROFILE_ID.toString()))
        .willReturn(Optional.of(disabledProfile()));
    assertThatThrownBy(
            () ->
                service.create(
                    USER_ID,
                    new BuildTaskService.CreateBuildTaskRequest(FILE_ID, PROFILE_ID, null)))
        .isInstanceOf(ApiException.class)
        .extracting(exception -> ((ApiException) exception).status())
        .isEqualTo(HttpStatus.CONFLICT);
    assertThatThrownBy(
            () ->
                service.create(
                    USER_ID,
                    new BuildTaskService.CreateBuildTaskRequest(FILE_ID, PROFILE_ID, "STRICT")))
        .isInstanceOf(ApiException.class)
        .extracting(exception -> ((ApiException) exception).status())
        .isEqualTo(HttpStatus.BAD_REQUEST);
  }

  @Test
  void listsAndReadsOnlyNonDeletedOwnedTasks() {
    var active = task(BuildStatus.QUEUED, null, null);
    given(taskRepository.findAllByUserIdAndDeletedAtIsNullOrderByCreatedAtDesc(USER_ID.toString()))
        .willReturn(List.of(active));
    given(
            taskRepository.findByIdAndUserIdAndDeletedAtIsNull(
                TASK_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(active));
    var service = service();

    assertThat(service.list(USER_ID))
        .extracting(BuildTaskService.BuildTaskView::id)
        .containsExactly(TASK_ID);
    assertThat(service.get(USER_ID, TASK_ID).id()).isEqualTo(TASK_ID);
  }

  @Test
  void cancelsReadyQueuedTaskAndBuildJobTogether() {
    var task = task(BuildStatus.QUEUED, null, null);
    given(
            taskRepository.findByIdAndUserIdAndDeletedAtIsNullForUpdate(
                TASK_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(task));
    given(
            jobRepository.cancelReadyBuildJob(
                TASK_ID.toString(), NOW.atOffset(ZoneOffset.UTC).toLocalDateTime()))
        .willReturn(1);
    var service = service();

    service.cancel(USER_ID, TASK_ID);

    assertThat(task.getStatus()).isEqualTo(BuildStatus.CANCELLED.name());
    assertThat(task.isCancelRequested()).isTrue();
    assertThat(task.getFinishedAt()).isEqualTo(NOW.atOffset(ZoneOffset.UTC).toLocalDateTime());
  }

  @Test
  void preservesCancellationFlagWhenWorkerClaimWinsTheReadyJobCas() {
    var task = task(BuildStatus.QUEUED, null, null);
    given(
            taskRepository.findByIdAndUserIdAndDeletedAtIsNullForUpdate(
                TASK_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(task));
    given(
            jobRepository.cancelReadyBuildJob(
                TASK_ID.toString(), NOW.atOffset(ZoneOffset.UTC).toLocalDateTime()))
        .willReturn(0);
    var service = service();

    service.cancel(USER_ID, TASK_ID);

    assertThat(task.getStatus()).isEqualTo(BuildStatus.QUEUED.name());
    assertThat(task.isCancelRequested()).isTrue();
    assertThat(task.getFinishedAt()).isNull();
  }

  @Test
  void marksRunningTaskForCancellationWithoutChangingItsStatus() {
    var task = task(BuildStatus.RESOLVING, null, null);
    given(
            taskRepository.findByIdAndUserIdAndDeletedAtIsNullForUpdate(
                TASK_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(task));

    service().cancel(USER_ID, TASK_ID);

    assertThat(task.getStatus()).isEqualTo(BuildStatus.RESOLVING.name());
    assertThat(task.isCancelRequested()).isTrue();
  }

  @Test
  void cancellingTerminalTaskDoesNotRewriteItsResult() {
    var finishedAt = NOW.minusSeconds(60).atOffset(ZoneOffset.UTC).toLocalDateTime();
    var task = task(BuildStatus.VALIDATING, null, null);
    task.transitionTo(BuildStatus.FAILED, finishedAt);
    given(
            taskRepository.findByIdAndUserIdAndDeletedAtIsNullForUpdate(
                TASK_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(task));

    service().cancel(USER_ID, TASK_ID);

    assertThat(task.isCancelRequested()).isFalse();
    assertThat(task.getStatus()).isEqualTo(BuildStatus.FAILED.name());
    assertThat(task.getFinishedAt()).isEqualTo(finishedAt);
  }

  @ParameterizedTest
  @EnumSource(
      value = BuildStatus.class,
      names = {"FAILED", "PARTIAL_SUCCESS", "CANCELLED"})
  void retriesEligibleTerminalTaskUsingHistoricalSnapshot(BuildStatus retryableStatus)
      throws Exception {
    TransactionSynchronizationManager.setActualTransactionActive(true);
    var source = task(retryableStatus, null, null);
    given(
            taskRepository.findByIdAndUserIdAndDeletedAtIsNull(
                TASK_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(source));
    given(fileRepository.findByIdAndUserId(FILE_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(parsedFile()));
    given(taskRepository.save(any())).willAnswer(invocation -> invocation.getArgument(0));
    given(jobRepository.save(any())).willAnswer(invocation -> invocation.getArgument(0));
    var service = service(() -> UUID.fromString("3d3f75c3-49e0-493a-9443-b60800a90709"));

    BuildTaskService.BuildTaskView retry = service.retry(USER_ID, TASK_ID);

    assertThat(retry.id()).isNotEqualTo(TASK_ID);
    assertThat(retry.sourceTaskId()).isEqualTo(TASK_ID);
    assertThat(retry.targetSnapshot()).isEqualTo(source.snapshot());
    verifyNoInteractions(profileRepository);
    var jobCaptor = ArgumentCaptor.forClass(BuildJobEntity.class);
    verify(jobRepository).save(jobCaptor.capture());
    JobPayload wire =
        JobPayload.databaseWireMapper()
            .readValue(jobCaptor.getValue().getPayloadJson(), JobPayload.class);
    assertThat(wire.payload().propertyNames())
        .containsExactlyInAnyOrder(
            "requirementFileId", "normalizedObjectKey", "solveMode", "targetSnapshot");
    assertThat(wire.payload().path("targetSnapshot").toString())
        .isEqualTo(objectMapperSnapshot(source).toString());
  }

  @ParameterizedTest
  @EnumSource(
      value = BuildStatus.class,
      names = {
        "CREATED",
        "PARSING",
        "QUEUED",
        "RESOLVING",
        "DOWNLOADING",
        "VALIDATING",
        "PACKAGING",
        "SUCCESS"
      })
  void rejectsRetryOfEveryActiveStatusAndSuccess(BuildStatus rejectedStatus) {
    var service = service();
    given(
            taskRepository.findByIdAndUserIdAndDeletedAtIsNull(
                TASK_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(task(rejectedStatus, null, null)));

    assertThatThrownBy(() -> service.retry(USER_ID, TASK_ID))
        .isInstanceOf(ApiException.class)
        .extracting(exception -> ((ApiException) exception).status())
        .isEqualTo(HttpStatus.CONFLICT);
    verifyNoInteractions(fileRepository, profileRepository, jobRepository);
  }

  @Test
  void retryTreatsDeletedOrCrossUserTaskAsNotFound() {
    given(
            taskRepository.findByIdAndUserIdAndDeletedAtIsNull(
                TASK_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.empty());

    assertThatThrownBy(() -> service().retry(USER_ID, TASK_ID))
        .isInstanceOf(ApiException.class)
        .extracting(exception -> ((ApiException) exception).status())
        .isEqualTo(HttpStatus.NOT_FOUND);
    verifyNoInteractions(fileRepository, profileRepository, jobRepository);
  }

  @Test
  void softDeletesTaskIdempotently() {
    var task = task(BuildStatus.QUEUED, null, null);
    given(taskRepository.findByIdAndUserIdForUpdate(TASK_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(task));
    var service = service();

    service.delete(USER_ID, TASK_ID);
    service.delete(USER_ID, TASK_ID);

    assertThat(task.getDeletedAt()).isEqualTo(NOW.atOffset(ZoneOffset.UTC).toLocalDateTime());
  }

  private BuildTaskService service() {
    return service(
        new java.util.function.Supplier<>() {
          private int calls;

          @Override
          public UUID get() {
            return calls++ == 0 ? TASK_ID : UUID.fromString("3d3f75c3-49e0-493a-9443-b60800a90709");
          }
        });
  }

  private BuildTaskService service(java.util.function.Supplier<UUID> taskIdSupplier) {
    return new BuildTaskService(
        taskRepository,
        fileRepository,
        profileRepository,
        new BuildJobService(
            jobRepository, Clock.fixed(NOW, ZoneOffset.UTC), () -> UUID.randomUUID()),
        Clock.fixed(NOW, ZoneOffset.UTC),
        taskIdSupplier);
  }

  private RequirementFileEntity parsedFile() {
    return new RequirementFileEntity(
        FILE_ID.toString(),
        "ignored",
        "requirements.txt",
        1,
        "a".repeat(64),
        "original",
        "users/alice/normalized.txt",
        "PARSED",
        NOW.atOffset(ZoneOffset.UTC).toLocalDateTime());
  }

  private RequirementFileEntity unparsedFile() {
    return new RequirementFileEntity(
        FILE_ID.toString(),
        "ignored",
        "requirements.txt",
        1,
        "a".repeat(64),
        "original",
        "normalized",
        "PARSING",
        NOW.atOffset(ZoneOffset.UTC).toLocalDateTime());
  }

  private TargetProfileEntity enabledProfile() {
    return profile(true);
  }

  private TargetProfileEntity disabledProfile() {
    return profile(false);
  }

  private TargetProfileEntity profile(boolean enabled) {
    return new TargetProfileEntity(
        PROFILE_ID.toString(),
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
        enabled);
  }

  private BuildTaskEntity.TargetSnapshot expectedSnapshot() {
    return new BuildTaskEntity.TargetSnapshot(
        PROFILE_ID.toString(),
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
        0);
  }

  private tools.jackson.databind.JsonNode objectMapperSnapshot(BuildTaskEntity source) {
    return JobPayload.databaseWireMapper().valueToTree(source.snapshot());
  }

  private BuildTaskEntity task(
      BuildStatus status, String sourceTaskId, java.time.LocalDateTime deletedAt) {
    return new BuildTaskEntity(
        TASK_ID.toString(),
        USER_ID.toString(),
        FILE_ID.toString(),
        PROFILE_ID.toString(),
        sourceTaskId,
        status,
        SolveMode.COMPATIBLE,
        BuildTaskEntity.TargetSnapshot.from(enabledProfile(), 0),
        NOW.atOffset(ZoneOffset.UTC).toLocalDateTime(),
        deletedAt);
  }
}
