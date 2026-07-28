package com.wheelforge.api.admin;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import java.time.LocalDateTime;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;
import tools.jackson.databind.JsonNode;

@Entity
@Table(name = "system_config")
public class SystemConfigEntity {
  @Id
  @Column(name = "config_key", nullable = false, length = 100)
  private String configKey;

  @JdbcTypeCode(SqlTypes.JSON)
  @Column(name = "config_value", nullable = false, columnDefinition = "json")
  private JsonNode configValue;

  @Column(name = "description", nullable = false, length = 500)
  private String description;

  @Column(name = "updated_by", length = 36)
  private String updatedBy;

  @Column(name = "updated_at", nullable = false)
  private LocalDateTime updatedAt;

  @Version
  @Column(name = "version_no", nullable = false)
  private long versionNo;

  protected SystemConfigEntity() {}

  public SystemConfigEntity(
      String configKey,
      JsonNode configValue,
      String description,
      String updatedBy,
      LocalDateTime updatedAt) {
    this.configKey = configKey;
    this.configValue = configValue;
    this.description = description;
    this.updatedBy = updatedBy;
    this.updatedAt = updatedAt;
  }

  public String getConfigKey() {
    return configKey;
  }

  public JsonNode getConfigValue() {
    return configValue;
  }

  public String getDescription() {
    return description;
  }

  public String getUpdatedBy() {
    return updatedBy;
  }

  public LocalDateTime getUpdatedAt() {
    return updatedAt;
  }

  public long getVersionNo() {
    return versionNo;
  }

  public void update(JsonNode value, String updatedBy, LocalDateTime now) {
    this.configValue = value;
    this.updatedBy = updatedBy;
    this.updatedAt = now;
  }
}
