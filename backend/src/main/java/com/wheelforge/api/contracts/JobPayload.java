package com.wheelforge.api.contracts;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import tools.jackson.databind.JsonNode;

import java.time.Instant;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;

@JsonIgnoreProperties(ignoreUnknown = false)
public record JobPayload(
    @JsonProperty("schemaVersion") int schemaVersion,
    @JsonProperty("jobType") String jobType,
    @JsonProperty("subjectId") String subjectId,
    @JsonProperty("createdAt") String createdAt,
    @JsonProperty("payload") JsonNode payload
) {
    public static final int VERSION = 1;
    private static final Set<String> SUPPORTED_JOB_TYPES = Set.of("REQUIREMENT_PARSE", "BUILD");

    public JobPayload {
        if (schemaVersion != VERSION) {
            throw new IllegalArgumentException("Unsupported job payload schema version: " + schemaVersion);
        }
        if (!SUPPORTED_JOB_TYPES.contains(jobType)) {
            throw new IllegalArgumentException("Unsupported job type: " + jobType);
        }
        Objects.requireNonNull(subjectId, "subjectId must not be null");
        Objects.requireNonNull(createdAt, "createdAt must not be null");
        Objects.requireNonNull(payload, "payload must not be null");
        UUID.fromString(subjectId);
        Instant.parse(createdAt);
        if (!payload.isObject()) {
            throw new IllegalArgumentException("payload must be a JSON object");
        }
    }

    public enum JobStatus {
        READY,
        RUNNING,
        COMPLETED,
        FAILED,
        CANCELLED
    }
}
