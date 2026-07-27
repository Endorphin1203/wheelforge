package com.wheelforge.api.build;

import com.wheelforge.api.common.ApiException;
import com.wheelforge.api.common.jobs.BuildJobService;
import com.wheelforge.api.requirements.RequirementFileEntity;
import com.wheelforge.api.requirements.RequirementFileRepository;
import com.wheelforge.api.target.TargetProfileEntity;
import com.wheelforge.api.target.TargetProfileRepository;
import jakarta.validation.constraints.NotNull;
import java.time.Clock;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
import java.util.function.Supplier;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.ObjectMapper;

@Service
public class BuildTaskService {
  private final BuildTaskRepository taskRepository;
  private final RequirementFileRepository fileRepository;
  private final TargetProfileRepository profileRepository;
  private final BuildJobService jobService;
  private final Clock clock;
  private final Supplier<UUID> idSupplier;

  @Autowired
  public BuildTaskService(
      BuildTaskRepository taskRepository,
      RequirementFileRepository fileRepository,
      TargetProfileRepository profileRepository,
      BuildJobService jobService) {
    this(
        taskRepository,
        fileRepository,
        profileRepository,
        jobService,
        Clock.systemUTC(),
        UUID::randomUUID);
  }

  public BuildTaskService(
      BuildTaskRepository taskRepository,
      RequirementFileRepository fileRepository,
      TargetProfileRepository profileRepository,
      BuildJobService jobService,
      Clock clock,
      Supplier<UUID> idSupplier) {
    this.taskRepository = taskRepository;
    this.fileRepository = fileRepository;
    this.profileRepository = profileRepository;
    this.jobService = jobService;
    this.clock = clock;
    this.idSupplier = idSupplier;
  }

  @Transactional
  public BuildTaskView create(UUID userId, CreateBuildTaskRequest request) {
    SolveMode solveMode = parseSolveMode(request.solveMode());
    RequirementFileEntity file = ownedFile(userId, request.requirementFileId());
    if (!"PARSED".equals(file.getParseStatus()) || file.getNormalizedObjectKey() == null) {
      throw ApiException.conflict("REQUIREMENT_FILE_NOT_PARSED", "Requirement file is not parsed");
    }
    TargetProfileEntity profile =
        profileRepository
            .findById(request.targetProfileId().toString())
            .filter(TargetProfileEntity::isEnabled)
            .orElseThrow(
                () ->
                    ApiException.conflict(
                        "TARGET_PROFILE_UNAVAILABLE", "Target profile is unavailable"));
    var task =
        new BuildTaskEntity(
            idSupplier.get().toString(),
            userId.toString(),
            file.getId(),
            profile.getId(),
            null,
            BuildStatus.QUEUED,
            solveMode,
            BuildTaskEntity.TargetSnapshot.from(profile, profile.getVersionNo()),
            now(),
            null);
    taskRepository.save(task);
    enqueueBuild(task, file.getNormalizedObjectKey());
    return view(task);
  }

  @Transactional(readOnly = true)
  public List<BuildTaskView> list(UUID userId) {
    return taskRepository
        .findAllByUserIdAndDeletedAtIsNullOrderByCreatedAtDesc(userId.toString())
        .stream()
        .map(BuildTaskService::view)
        .toList();
  }

  @Transactional(readOnly = true)
  public BuildTaskView get(UUID userId, UUID taskId) {
    return view(activeOwnedTask(userId, taskId));
  }

  @Transactional
  public BuildTaskView cancel(UUID userId, UUID taskId) {
    BuildTaskEntity task = lockedActiveOwnedTask(userId, taskId);
    BuildStatus status = BuildStatus.valueOf(task.getStatus());
    if (isTerminal(status)) {
      return view(task);
    }
    task.requestCancellation();
    LocalDateTime now = now();
    if (status == BuildStatus.QUEUED && jobService.cancelReadyBuildJob(task.getId(), now) == 1) {
      task.transitionTo(BuildStatus.CANCELLED, now);
    }
    return view(task);
  }

  @Transactional
  public BuildTaskView retry(UUID userId, UUID taskId) {
    BuildTaskEntity source = activeOwnedTask(userId, taskId);
    BuildStatus sourceStatus = BuildStatus.valueOf(source.getStatus());
    if (sourceStatus != BuildStatus.FAILED
        && sourceStatus != BuildStatus.PARTIAL_SUCCESS
        && sourceStatus != BuildStatus.CANCELLED) {
      throw ApiException.conflict("BUILD_TASK_NOT_RETRYABLE", "Build task is not retryable");
    }
    RequirementFileEntity file = ownedFile(userId, UUID.fromString(source.getRequirementFileId()));
    if (file.getNormalizedObjectKey() == null) {
      throw ApiException.conflict("REQUIREMENT_FILE_NOT_PARSED", "Requirement file is not parsed");
    }
    var retry =
        new BuildTaskEntity(
            idSupplier.get().toString(),
            userId.toString(),
            source.getRequirementFileId(),
            source.getTargetProfileId(),
            source.getId(),
            BuildStatus.QUEUED,
            SolveMode.valueOf(source.getSolveMode()),
            source.snapshot(),
            now(),
            null);
    taskRepository.save(retry);
    enqueueBuild(retry, file.getNormalizedObjectKey());
    return view(retry);
  }

  @Transactional
  public void delete(UUID userId, UUID taskId) {
    BuildTaskEntity task =
        taskRepository
            .findByIdAndUserIdForUpdate(taskId.toString(), userId.toString())
            .orElseThrow(() -> ApiException.notFound("Build task was not found"));
    task.softDelete(now());
  }

  private void enqueueBuild(BuildTaskEntity task, String normalizedObjectKey) {
    ObjectMapper mapper = com.wheelforge.api.contracts.JobPayload.databaseWireMapper();
    var payload = mapper.createObjectNode();
    payload.put("requirementFileId", task.getRequirementFileId());
    payload.put("normalizedObjectKey", normalizedObjectKey);
    payload.put("solveMode", task.getSolveMode());
    payload.set("targetSnapshot", mapper.valueToTree(task.snapshot()));
    jobService.enqueue("BUILD", UUID.fromString(task.getId()), payload);
  }

  private RequirementFileEntity ownedFile(UUID userId, UUID fileId) {
    return fileRepository
        .findByIdAndUserId(fileId.toString(), userId.toString())
        .orElseThrow(() -> ApiException.notFound("Requirement file was not found"));
  }

  private BuildTaskEntity activeOwnedTask(UUID userId, UUID taskId) {
    return taskRepository
        .findByIdAndUserIdAndDeletedAtIsNull(taskId.toString(), userId.toString())
        .orElseThrow(() -> ApiException.notFound("Build task was not found"));
  }

  private BuildTaskEntity lockedActiveOwnedTask(UUID userId, UUID taskId) {
    return taskRepository
        .findByIdAndUserIdAndDeletedAtIsNullForUpdate(taskId.toString(), userId.toString())
        .orElseThrow(() -> ApiException.notFound("Build task was not found"));
  }

  private SolveMode parseSolveMode(String value) {
    if (value == null) {
      return SolveMode.COMPATIBLE;
    }
    try {
      return SolveMode.valueOf(value);
    } catch (IllegalArgumentException exception) {
      throw ApiException.badRequest("UNSUPPORTED_SOLVE_MODE", "Solve mode is unsupported");
    }
  }

  private LocalDateTime now() {
    return LocalDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
  }

  private static boolean isTerminal(BuildStatus status) {
    return status == BuildStatus.SUCCESS
        || status == BuildStatus.PARTIAL_SUCCESS
        || status == BuildStatus.FAILED
        || status == BuildStatus.CANCELLED;
  }

  private static BuildTaskView view(BuildTaskEntity task) {
    return new BuildTaskView(
        UUID.fromString(task.getId()),
        UUID.fromString(task.getRequirementFileId()),
        UUID.fromString(task.getTargetProfileId()),
        task.getSourceTaskId() == null ? null : UUID.fromString(task.getSourceTaskId()),
        BuildStatus.valueOf(task.getStatus()),
        task.getProgress(),
        task.getCurrentStage(),
        SolveMode.valueOf(task.getSolveMode()),
        task.snapshot(),
        task.isCancelRequested(),
        task.getFailureCode(),
        task.getFailureMessage(),
        task.getCreatedAt(),
        task.getStartedAt(),
        task.getFinishedAt());
  }

  public record CreateBuildTaskRequest(
      @NotNull UUID requirementFileId, @NotNull UUID targetProfileId, String solveMode) {}

  public record BuildTaskView(
      UUID id,
      UUID requirementFileId,
      UUID targetProfileId,
      UUID sourceTaskId,
      BuildStatus status,
      int progress,
      String currentStage,
      SolveMode solveMode,
      BuildTaskEntity.TargetSnapshot targetSnapshot,
      boolean cancelRequested,
      String failureCode,
      String failureMessage,
      LocalDateTime createdAt,
      LocalDateTime startedAt,
      LocalDateTime finishedAt) {}
}
