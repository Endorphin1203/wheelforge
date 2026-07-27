package com.wheelforge.api.build;

import com.wheelforge.api.target.TargetProfileEntity;
import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import java.time.LocalDateTime;
import java.util.List;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

@Entity
@Table(name = "build_tasks")
public class BuildTaskEntity {
  private static final BuildStateMachine STATE_MACHINE = new BuildStateMachine();

  @Id
  @Column(name = "id", nullable = false, length = 36)
  private String id;

  @Column(name = "user_id", nullable = false, length = 36)
  private String userId;

  @Column(name = "requirement_file_id", nullable = false, length = 36)
  private String requirementFileId;

  @Column(name = "target_profile_id", nullable = false, length = 36)
  private String targetProfileId;

  @Column(name = "source_task_id", length = 36)
  private String sourceTaskId;

  @Column(name = "execution_id", length = 36)
  private String executionId;

  @Column(name = "status", nullable = false, length = 30)
  private String status;

  @Column(name = "progress", nullable = false)
  private int progress;

  @Column(name = "current_stage", length = 50)
  private String currentStage;

  @Column(name = "solve_mode", nullable = false, length = 20)
  private String solveMode;

  @JdbcTypeCode(SqlTypes.JSON)
  @Column(name = "target_snapshot", nullable = false, columnDefinition = "json")
  private TargetSnapshot targetSnapshot;

  @Column(name = "cancel_requested", nullable = false)
  private boolean cancelRequested;

  @Column(name = "failure_code", length = 100)
  private String failureCode;

  @Column(name = "failure_message", length = 2000)
  private String failureMessage;

  @Column(name = "created_at", nullable = false)
  private LocalDateTime createdAt;

  @Column(name = "started_at")
  private LocalDateTime startedAt;

  @Column(name = "finished_at")
  private LocalDateTime finishedAt;

  @Column(name = "deleted_at")
  private LocalDateTime deletedAt;

  @Version
  @Column(name = "version_no", nullable = false)
  private long versionNo;

  protected BuildTaskEntity() {}

  public BuildTaskEntity(
      String id,
      String userId,
      String requirementFileId,
      String targetProfileId,
      String sourceTaskId,
      BuildStatus status,
      SolveMode solveMode,
      TargetSnapshot targetSnapshot,
      LocalDateTime createdAt,
      LocalDateTime deletedAt) {
    this.id = id;
    this.userId = userId;
    this.requirementFileId = requirementFileId;
    this.targetProfileId = targetProfileId;
    this.sourceTaskId = sourceTaskId;
    this.status = status.name();
    this.solveMode = solveMode.name();
    this.targetSnapshot = targetSnapshot;
    this.createdAt = createdAt;
    this.deletedAt = deletedAt;
  }

  public String getId() {
    return id;
  }

  public String getUserId() {
    return userId;
  }

  public String getRequirementFileId() {
    return requirementFileId;
  }

  public String getTargetProfileId() {
    return targetProfileId;
  }

  public String getSourceTaskId() {
    return sourceTaskId;
  }

  public String getExecutionId() {
    return executionId;
  }

  public String getStatus() {
    return status;
  }

  public int getProgress() {
    return progress;
  }

  public String getCurrentStage() {
    return currentStage;
  }

  public String getSolveMode() {
    return solveMode;
  }

  public TargetSnapshot snapshot() {
    return targetSnapshot;
  }

  public boolean isCancelRequested() {
    return cancelRequested;
  }

  public String getFailureCode() {
    return failureCode;
  }

  public String getFailureMessage() {
    return failureMessage;
  }

  public LocalDateTime getCreatedAt() {
    return createdAt;
  }

  public LocalDateTime getStartedAt() {
    return startedAt;
  }

  public LocalDateTime getFinishedAt() {
    return finishedAt;
  }

  public LocalDateTime getDeletedAt() {
    return deletedAt;
  }

  public void requestCancellation() {
    cancelRequested = true;
  }

  public void transitionTo(BuildStatus next, LocalDateTime now) {
    STATE_MACHINE.requireTransition(BuildStatus.valueOf(status), next);
    status = next.name();
    if (isTerminal(next)) {
      finishedAt = now;
    }
  }

  public void softDelete(LocalDateTime now) {
    if (deletedAt == null) {
      deletedAt = now;
    }
  }

  private static boolean isTerminal(BuildStatus value) {
    return value == BuildStatus.SUCCESS
        || value == BuildStatus.PARTIAL_SUCCESS
        || value == BuildStatus.FAILED
        || value == BuildStatus.CANCELLED;
  }

  public record TargetSnapshot(
      String profileId,
      String profileCode,
      String os,
      String architecture,
      String pythonImplementation,
      String pythonVersion,
      String pythonFullVersion,
      String platformTag,
      List<String> abiTags,
      String validationType,
      String validationPolicyVersion,
      long profileVersion) {
    public TargetSnapshot {
      abiTags = List.copyOf(abiTags);
    }

    public static TargetSnapshot from(TargetProfileEntity profile, long profileVersion) {
      return new TargetSnapshot(
          profile.getId(),
          profile.getCode(),
          profile.getOs(),
          profile.getArchitecture(),
          profile.getPythonImplementation(),
          profile.getPythonVersion(),
          profile.getPythonFullVersion(),
          profile.getPlatformTag(),
          profile.getAbiTags(),
          profile.getValidationType(),
          profile.getValidationPolicyVersion(),
          profileVersion);
    }
  }
}
