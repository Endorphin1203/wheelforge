package com.wheelforge.api.requirements;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import java.time.LocalDateTime;

@Entity
@Table(name = "requirement_files")
public class RequirementFileEntity {
  @Id
  @Column(name = "id", nullable = false, length = 36)
  private String id;

  @Column(name = "user_id", nullable = false, length = 36)
  private String userId;

  @Column(name = "original_name", nullable = false, length = 255)
  private String originalName;

  @Column(name = "detected_encoding", length = 20)
  private String detectedEncoding;

  @Column(name = "size_bytes", nullable = false)
  private long sizeBytes;

  @Column(name = "sha256", nullable = false, length = 64)
  private String sha256;

  @Column(name = "original_object_key", nullable = false, length = 512)
  private String originalObjectKey;

  @Column(name = "normalized_object_key", length = 512)
  private String normalizedObjectKey;

  @Column(name = "parse_status", nullable = false, length = 20)
  private String parseStatus;

  @Column(name = "parse_error", length = 2000)
  private String parseError;

  @Column(name = "created_at", nullable = false)
  private LocalDateTime createdAt;

  @Version
  @Column(name = "version_no", nullable = false)
  private long versionNo;

  protected RequirementFileEntity() {}

  public RequirementFileEntity(
      String id,
      String userId,
      String originalName,
      long sizeBytes,
      String sha256,
      String originalObjectKey,
      String normalizedObjectKey,
      String parseStatus,
      LocalDateTime createdAt) {
    this.id = id;
    this.userId = userId;
    this.originalName = originalName;
    this.sizeBytes = sizeBytes;
    this.sha256 = sha256;
    this.originalObjectKey = originalObjectKey;
    this.normalizedObjectKey = normalizedObjectKey;
    this.parseStatus = parseStatus;
    this.createdAt = createdAt;
  }

  public String getId() {
    return id;
  }

  public String getUserId() {
    return userId;
  }

  public String getOriginalName() {
    return originalName;
  }

  public String getDetectedEncoding() {
    return detectedEncoding;
  }

  public long getSizeBytes() {
    return sizeBytes;
  }

  public String getSha256() {
    return sha256;
  }

  public String getOriginalObjectKey() {
    return originalObjectKey;
  }

  public String getNormalizedObjectKey() {
    return normalizedObjectKey;
  }

  public String getParseStatus() {
    return parseStatus;
  }

  public String getParseError() {
    return parseError;
  }

  public LocalDateTime getCreatedAt() {
    return createdAt;
  }
}
