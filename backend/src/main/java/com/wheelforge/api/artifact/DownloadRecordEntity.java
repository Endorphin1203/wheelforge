package com.wheelforge.api.artifact;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.LocalDateTime;

@Entity
@Table(name = "download_records")
public class DownloadRecordEntity {
  @Id
  @Column(name = "id", nullable = false, length = 36)
  private String id;

  @Column(name = "artifact_id", nullable = false, length = 36)
  private String artifactId;

  @Column(name = "user_id", nullable = false, length = 36)
  private String userId;

  @Column(name = "ip_address", nullable = false, length = 45)
  private String ipAddress;

  @Column(name = "user_agent", length = 1000)
  private String userAgent;

  @Column(name = "completed", nullable = false)
  private boolean completed;

  @Column(name = "downloaded_at", nullable = false)
  private LocalDateTime downloadedAt;

  protected DownloadRecordEntity() {}

  public DownloadRecordEntity(
      String id,
      String artifactId,
      String userId,
      String ipAddress,
      String userAgent,
      boolean completed,
      LocalDateTime downloadedAt) {
    this.id = id;
    this.artifactId = artifactId;
    this.userId = userId;
    this.ipAddress = ipAddress;
    this.userAgent = userAgent;
    this.completed = completed;
    this.downloadedAt = downloadedAt;
  }

  public String getId() {
    return id;
  }

  public String getArtifactId() {
    return artifactId;
  }

  public String getUserId() {
    return userId;
  }

  public String getIpAddress() {
    return ipAddress;
  }

  public String getUserAgent() {
    return userAgent;
  }

  public boolean isCompleted() {
    return completed;
  }

  public LocalDateTime getDownloadedAt() {
    return downloadedAt;
  }

  public void markCompleted() {
    completed = true;
  }
}
