package com.wheelforge.api.common.jobs;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import java.time.LocalDateTime;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

@Entity
@Table(name = "build_jobs")
public class BuildJobEntity {
  @Id
  @Column(name = "id", nullable = false, length = 36)
  private String id;

  @Column(name = "job_type", nullable = false, length = 30)
  private String jobType;

  @Column(name = "payload_version", nullable = false)
  private int payloadVersion;

  @Column(name = "subject_id", nullable = false, length = 36)
  private String subjectId;

  @JdbcTypeCode(SqlTypes.JSON)
  @Column(name = "payload_json", nullable = false, columnDefinition = "json")
  private String payloadJson;

  @Column(name = "status", nullable = false, length = 20)
  private String status;

  @Column(name = "priority_no", nullable = false)
  private int priorityNo;

  @Column(name = "available_at", nullable = false)
  private LocalDateTime availableAt;

  @Column(name = "attempts", nullable = false)
  private int attempts;

  @Column(name = "max_attempts", nullable = false)
  private int maxAttempts;

  @Column(name = "lease_owner", length = 100)
  private String leaseOwner;

  @Column(name = "execution_id", length = 36)
  private String executionId;

  @Column(name = "lease_expires_at")
  private LocalDateTime leaseExpiresAt;

  @Column(name = "heartbeat_at")
  private LocalDateTime heartbeatAt;

  @Column(name = "last_error", length = 2000)
  private String lastError;

  @Column(name = "created_at", nullable = false)
  private LocalDateTime createdAt;

  @Column(name = "started_at")
  private LocalDateTime startedAt;

  @Column(name = "finished_at")
  private LocalDateTime finishedAt;

  @Version
  @Column(name = "version_no", nullable = false)
  private long versionNo;

  protected BuildJobEntity() {}

  public BuildJobEntity(
      String id,
      String jobType,
      int payloadVersion,
      String subjectId,
      String payloadJson,
      LocalDateTime availableAt) {
    this.id = id;
    this.jobType = jobType;
    this.payloadVersion = payloadVersion;
    this.subjectId = subjectId;
    this.payloadJson = payloadJson;
    this.status = "READY";
    this.priorityNo = 100;
    this.availableAt = availableAt;
    this.attempts = 0;
    this.maxAttempts = 3;
    this.createdAt = availableAt;
  }

  public String getId() {
    return id;
  }

  public String getJobType() {
    return jobType;
  }

  public int getPayloadVersion() {
    return payloadVersion;
  }

  public String getSubjectId() {
    return subjectId;
  }

  public String getPayloadJson() {
    return payloadJson;
  }

  public String getStatus() {
    return status;
  }

  public int getPriorityNo() {
    return priorityNo;
  }

  public LocalDateTime getAvailableAt() {
    return availableAt;
  }

  public int getAttempts() {
    return attempts;
  }

  public int getMaxAttempts() {
    return maxAttempts;
  }

  public LocalDateTime getCreatedAt() {
    return createdAt;
  }
}
