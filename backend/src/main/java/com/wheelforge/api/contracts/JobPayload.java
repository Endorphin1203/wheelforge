package com.wheelforge.api.contracts;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.cfg.CoercionAction;
import tools.jackson.databind.cfg.CoercionInputShape;
import tools.jackson.databind.type.LogicalType;

import java.time.Instant;
import java.math.BigDecimal;
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

    public static ObjectMapper databaseWireMapper() {
        return new ObjectMapper().rebuild()
            .enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES)
            .disable(DeserializationFeature.ACCEPT_FLOAT_AS_INT)
            .withCoercionConfig(LogicalType.Integer, config -> config
                .setCoercion(CoercionInputShape.Boolean, CoercionAction.Fail)
                .setCoercion(CoercionInputShape.String, CoercionAction.Fail))
            .withCoercionConfig(LogicalType.Textual, config -> config
                .setCoercion(CoercionInputShape.Boolean, CoercionAction.Fail)
                .setCoercion(CoercionInputShape.Float, CoercionAction.Fail)
                .setCoercion(CoercionInputShape.Integer, CoercionAction.Fail))
            .build();
    }

    @JsonCreator
    public static JobPayload fromDatabaseWire(
        @JsonProperty("schemaVersion") JsonNode schemaVersion,
        @JsonProperty("jobType") String jobType,
        @JsonProperty("subjectId") String subjectId,
        @JsonProperty("createdAt") String createdAt,
        @JsonProperty("payload") JsonNode payload
    ) {
        return new JobPayload(parseSchemaVersion(schemaVersion), jobType, subjectId, createdAt, payload);
    }

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
        UUID parsedSubjectId = UUID.fromString(subjectId);
        if (!parsedSubjectId.toString().equalsIgnoreCase(subjectId)) {
            throw new IllegalArgumentException("subjectId must be a canonical UUID");
        }
        Instant.parse(createdAt.replace(' ', 'T'));
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

    private static int parseSchemaVersion(JsonNode schemaVersion) {
        if (schemaVersion == null || !schemaVersion.isNumber()
            || schemaVersion.decimalValue().compareTo(BigDecimal.ONE) != 0) {
            throw new IllegalArgumentException("Unsupported job payload schema version");
        }
        return VERSION;
    }
}
