package com.wheelforge.api.admin;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import java.time.LocalDateTime;

@Entity
@Table(name = "package_sources")
public class PackageSourceEntity {
  @Id
  @Column(name = "id", nullable = false, length = 36)
  private String id;

  @Column(name = "code", nullable = false, unique = true, length = 50)
  private String code;

  @Column(name = "display_name", nullable = false, length = 100)
  private String displayName;

  @Column(name = "base_url", nullable = false, length = 500)
  private String baseUrl;

  @Column(name = "priority_no", nullable = false)
  private int priorityNo;

  @Column(name = "enabled", nullable = false)
  private boolean enabled;

  @Column(name = "timeout_seconds", nullable = false)
  private int timeoutSeconds;

  @Column(name = "failure_count", nullable = false)
  private long failureCount;

  @Version
  @Column(name = "version_no", nullable = false)
  private long versionNo;

  @Column(name = "updated_at", nullable = false)
  private LocalDateTime updatedAt;

  protected PackageSourceEntity() {}

  public PackageSourceEntity(
      String id,
      String code,
      String displayName,
      String baseUrl,
      int priorityNo,
      boolean enabled,
      int timeoutSeconds,
      long failureCount,
      LocalDateTime updatedAt) {
    this.id = id;
    this.code = code;
    this.displayName = displayName;
    this.baseUrl = baseUrl;
    this.priorityNo = priorityNo;
    this.enabled = enabled;
    this.timeoutSeconds = timeoutSeconds;
    this.failureCount = failureCount;
    this.updatedAt = updatedAt;
  }

  public String getId() {
    return id;
  }

  public String getCode() {
    return code;
  }

  public String getDisplayName() {
    return displayName;
  }

  public String getBaseUrl() {
    return baseUrl;
  }

  public int getPriorityNo() {
    return priorityNo;
  }

  public boolean isEnabled() {
    return enabled;
  }

  public int getTimeoutSeconds() {
    return timeoutSeconds;
  }

  public long getFailureCount() {
    return failureCount;
  }

  public long getVersionNo() {
    return versionNo;
  }

  public LocalDateTime getUpdatedAt() {
    return updatedAt;
  }

  public void update(boolean enabled, int priorityNo, int timeoutSeconds, LocalDateTime now) {
    this.enabled = enabled;
    this.priorityNo = priorityNo;
    this.timeoutSeconds = timeoutSeconds;
    this.updatedAt = now;
  }
}
