package com.wheelforge.api.build;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.util.List;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;
import tools.jackson.databind.JsonNode;

@Entity
@Table(name = "resolved_packages")
public class ResolvedPackageEntity {
  @Id
  @Column(name = "id", nullable = false, length = 36)
  private String id;

  @Column(name = "build_task_id", nullable = false, length = 36)
  private String buildTaskId;

  @Column(name = "normalized_name", nullable = false, length = 255)
  private String normalizedName;

  @Column(name = "final_version", length = 100)
  private String finalVersion;

  @Column(name = "dependency_type", nullable = false, length = 20)
  private String dependencyType;

  @Column(name = "original_constraint", length = 500)
  private String originalConstraint;

  @Column(name = "strict_version", length = 100)
  private String strictVersion;

  @Column(name = "change_direction", nullable = false, length = 30)
  private String changeDirection;

  @Column(name = "change_reason", length = 1000)
  private String changeReason;

  @JdbcTypeCode(SqlTypes.JSON)
  @Column(name = "attempts_json", nullable = false, columnDefinition = "json")
  private JsonNode candidateAttempts;

  @Column(name = "wheel_filename", length = 500)
  private String wheelFilename;

  @JdbcTypeCode(SqlTypes.JSON)
  @Column(name = "wheel_tags", columnDefinition = "json")
  private List<String> wheelTags;

  @Column(name = "package_source_code", length = 50)
  private String packageSource;

  @Column(name = "sha256", length = 64)
  private String sha256;

  @Column(name = "wheel_status", nullable = false, length = 30)
  private String wheelStatus;

  @Column(name = "error_message", length = 2000)
  private String errorMessage;

  protected ResolvedPackageEntity() {}

  public ResolvedPackageEntity(
      String id,
      String buildTaskId,
      String normalizedName,
      String finalVersion,
      String dependencyType,
      String originalConstraint,
      String strictVersion,
      String changeDirection,
      String changeReason,
      JsonNode candidateAttempts,
      String wheelFilename,
      List<String> wheelTags,
      String packageSource,
      String sha256,
      String wheelStatus,
      String errorMessage) {
    this.id = id;
    this.buildTaskId = buildTaskId;
    this.normalizedName = normalizedName;
    this.finalVersion = finalVersion;
    this.dependencyType = dependencyType;
    this.originalConstraint = originalConstraint;
    this.strictVersion = strictVersion;
    this.changeDirection = changeDirection;
    this.changeReason = changeReason;
    this.candidateAttempts = candidateAttempts;
    this.wheelFilename = wheelFilename;
    this.wheelTags = wheelTags == null ? null : List.copyOf(wheelTags);
    this.packageSource = packageSource;
    this.sha256 = sha256;
    this.wheelStatus = wheelStatus;
    this.errorMessage = errorMessage;
  }

  public String getNormalizedName() {
    return normalizedName;
  }

  public String getFinalVersion() {
    return finalVersion;
  }

  public String getDependencyType() {
    return dependencyType;
  }

  public String getOriginalConstraint() {
    return originalConstraint;
  }

  public String getStrictVersion() {
    return strictVersion;
  }

  public String getChangeDirection() {
    return changeDirection;
  }

  public String getChangeReason() {
    return changeReason;
  }

  public JsonNode getCandidateAttempts() {
    return candidateAttempts;
  }

  public String getWheelFilename() {
    return wheelFilename;
  }

  public List<String> getWheelTags() {
    return wheelTags == null ? null : List.copyOf(wheelTags);
  }

  public String getPackageSource() {
    return packageSource;
  }

  public String getSha256() {
    return sha256;
  }

  public String getWheelStatus() {
    return wheelStatus;
  }

  public String getErrorMessage() {
    return errorMessage;
  }
}
