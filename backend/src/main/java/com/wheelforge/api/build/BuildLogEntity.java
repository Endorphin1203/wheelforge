package com.wheelforge.api.build;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.LocalDateTime;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;
import tools.jackson.databind.JsonNode;

@Entity
@Table(name = "build_logs")
public class BuildLogEntity {
  @Id
  @Column(name = "id", nullable = false, length = 36)
  private String id;

  @Column(name = "build_task_id", nullable = false, length = 36)
  private String buildTaskId;

  @Column(name = "sequence_no", nullable = false)
  private long sequenceNo;

  @Column(name = "stage", nullable = false, length = 50)
  private String stage;

  @Column(name = "level", nullable = false, length = 20)
  private String level;

  @Column(name = "message", nullable = false, length = 4000)
  private String message;

  @JdbcTypeCode(SqlTypes.JSON)
  @Column(name = "context_json", columnDefinition = "json")
  private JsonNode context;

  @Column(name = "created_at", nullable = false)
  private LocalDateTime createdAt;

  protected BuildLogEntity() {}

  public BuildLogEntity(
      String id,
      String buildTaskId,
      long sequenceNo,
      String stage,
      String level,
      String message,
      JsonNode context,
      LocalDateTime createdAt) {
    this.id = id;
    this.buildTaskId = buildTaskId;
    this.sequenceNo = sequenceNo;
    this.stage = stage;
    this.level = level;
    this.message = message;
    this.context = context;
    this.createdAt = createdAt;
  }

  public long getSequenceNo() {
    return sequenceNo;
  }

  public String getStage() {
    return stage;
  }

  public String getLevel() {
    return level;
  }

  public String getMessage() {
    return message;
  }

  public JsonNode getContext() {
    return context;
  }

  public LocalDateTime getCreatedAt() {
    return createdAt;
  }
}
