package com.wheelforge.api.artifact;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import java.time.LocalDateTime;

@Entity
@Table(name = "artifacts")
public class ArtifactEntity {
  @Id
  @Column(name = "id", nullable = false, length = 36)
  private String id;

  @Column(name = "build_task_id", nullable = false, length = 36)
  private String buildTaskId;

  @Column(name = "artifact_type", nullable = false, length = 30)
  private String artifactType;

  @Column(name = "filename", nullable = false, length = 255)
  private String filename;

  @Column(name = "object_key", nullable = false, unique = true, length = 512)
  private String objectKey;

  @Column(name = "size_bytes", nullable = false)
  private long sizeBytes;

  @Column(name = "sha256", nullable = false, length = 64)
  private String sha256;

  @Column(name = "build_status", nullable = false, length = 30)
  private String buildStatus;

  @Column(name = "validation_type", nullable = false, length = 30)
  private String validationType;

  @Column(name = "expires_at", nullable = false)
  private LocalDateTime expiresAt;

  @Column(name = "download_count", nullable = false)
  private long downloadCount;

  @Column(name = "cleaned_at")
  private LocalDateTime cleanedAt;

  @Column(name = "created_at", nullable = false)
  private LocalDateTime createdAt;

  @Version
  @Column(name = "version_no", nullable = false)
  private long versionNo;

  protected ArtifactEntity() {}

  public ArtifactEntity(
      String id,
      String buildTaskId,
      String artifactType,
      String filename,
      String objectKey,
      long sizeBytes,
      String sha256,
      String buildStatus,
      String validationType,
      LocalDateTime expiresAt,
      long downloadCount,
      LocalDateTime cleanedAt,
      LocalDateTime createdAt) {
    this.id = id;
    this.buildTaskId = buildTaskId;
    this.artifactType = artifactType;
    this.filename = filename;
    this.objectKey = objectKey;
    this.sizeBytes = sizeBytes;
    this.sha256 = sha256;
    this.buildStatus = buildStatus;
    this.validationType = validationType;
    this.expiresAt = expiresAt;
    this.downloadCount = downloadCount;
    this.cleanedAt = cleanedAt;
    this.createdAt = createdAt;
  }

  public String getId() {
    return id;
  }

  public String getBuildTaskId() {
    return buildTaskId;
  }

  public String getArtifactType() {
    return artifactType;
  }

  public String getFilename() {
    return filename;
  }

  public String getObjectKey() {
    return objectKey;
  }

  public long getSizeBytes() {
    return sizeBytes;
  }

  public String getSha256() {
    return sha256;
  }

  public String getBuildStatus() {
    return buildStatus;
  }

  public String getValidationType() {
    return validationType;
  }

  public LocalDateTime getExpiresAt() {
    return expiresAt;
  }

  public long getDownloadCount() {
    return downloadCount;
  }

  public LocalDateTime getCleanedAt() {
    return cleanedAt;
  }

  public LocalDateTime getCreatedAt() {
    return createdAt;
  }

  public long getVersionNo() {
    return versionNo;
  }

  public void incrementDownloadCount() {
    downloadCount++;
  }

  public void markCleaned(LocalDateTime cleanedAt) {
    if (this.cleanedAt == null) {
      this.cleanedAt = cleanedAt;
    }
  }
}
