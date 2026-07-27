package com.wheelforge.api.requirements;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.util.List;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

@Entity
@Table(name = "requirement_items")
public class RequirementItemEntity {
  @Id
  @Column(name = "id", nullable = false, length = 36)
  private String id;

  @Column(name = "requirement_file_id", nullable = false, length = 36)
  private String requirementFileId;

  @Column(name = "line_no", nullable = false)
  private int lineNo;

  @Column(name = "normalized_name", nullable = false, length = 255)
  private String normalizedName;

  @JdbcTypeCode(SqlTypes.JSON)
  @Column(name = "extras_json", nullable = false, columnDefinition = "json")
  private List<String> extras;

  @Column(name = "specifier", nullable = false, length = 500)
  private String specifier;

  @Column(name = "marker_text", length = 1000)
  private String markerText;

  @Column(name = "original_text", nullable = false, length = 2000)
  private String originalText;

  @Column(name = "supported", nullable = false)
  private boolean supported;

  @Column(name = "error_code", length = 100)
  private String errorCode;

  @Column(name = "error_message", length = 2000)
  private String errorMessage;

  protected RequirementItemEntity() {}

  public RequirementItemEntity(
      String id,
      String requirementFileId,
      int lineNo,
      String normalizedName,
      List<String> extras,
      String specifier,
      String markerText,
      String originalText,
      boolean supported,
      String errorCode,
      String errorMessage) {
    this.id = id;
    this.requirementFileId = requirementFileId;
    this.lineNo = lineNo;
    this.normalizedName = normalizedName;
    this.extras = List.copyOf(extras);
    this.specifier = specifier;
    this.markerText = markerText;
    this.originalText = originalText;
    this.supported = supported;
    this.errorCode = errorCode;
    this.errorMessage = errorMessage;
  }

  public String getId() {
    return id;
  }

  public int getLineNo() {
    return lineNo;
  }

  public String getNormalizedName() {
    return normalizedName;
  }

  public List<String> getExtras() {
    return List.copyOf(extras);
  }

  public String getSpecifier() {
    return specifier;
  }

  public String getMarkerText() {
    return markerText;
  }

  public String getOriginalText() {
    return originalText;
  }

  public boolean isSupported() {
    return supported;
  }

  public String getErrorCode() {
    return errorCode;
  }

  public String getErrorMessage() {
    return errorMessage;
  }
}
