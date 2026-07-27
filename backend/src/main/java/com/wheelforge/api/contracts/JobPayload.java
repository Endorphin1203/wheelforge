package com.wheelforge.api.contracts;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.math.BigDecimal;
import java.time.DateTimeException;
import java.time.LocalDate;
import java.util.HashSet;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.cfg.CoercionAction;
import tools.jackson.databind.cfg.CoercionInputShape;
import tools.jackson.databind.type.LogicalType;

@JsonIgnoreProperties(ignoreUnknown = false)
public record JobPayload(
    @JsonProperty("schemaVersion") int schemaVersion,
    @JsonProperty("jobType") String jobType,
    @JsonProperty("subjectId") String subjectId,
    @JsonProperty("createdAt") String createdAt,
    @JsonProperty("payload") JsonNode payload) {
  public static final int VERSION = 1;
  private static final Set<String> SUPPORTED_JOB_TYPES = Set.of("REQUIREMENT_PARSE", "BUILD");
  private static final Pattern RFC3339_DATE_TIME =
      Pattern.compile(
          "^(?<year>\\d{4})-(?<month>\\d{2})-(?<day>\\d{2})[Tt](?:[01]\\d|2[0-3]):[0-5]\\d:[0-5]\\d(?:\\.\\d+)?(?:[Zz]|[+-](?:[01]\\d|2[0-3]):[0-5]\\d)$");

  public static ObjectMapper databaseWireMapper() {
    return new ObjectMapper()
        .rebuild()
        .enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES)
        .enable(DeserializationFeature.USE_BIG_DECIMAL_FOR_FLOATS)
        .disable(DeserializationFeature.ACCEPT_FLOAT_AS_INT)
        .withCoercionConfig(
            LogicalType.Integer,
            config ->
                config
                    .setCoercion(CoercionInputShape.Boolean, CoercionAction.Fail)
                    .setCoercion(CoercionInputShape.String, CoercionAction.Fail))
        .withCoercionConfig(
            LogicalType.Textual,
            config ->
                config
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
      @JsonProperty("payload") JsonNode payload) {
    return new JobPayload(
        parseSchemaVersion(schemaVersion), jobType, subjectId, createdAt, payload);
  }

  public JobPayload {
    if (schemaVersion != VERSION) {
      throw new IllegalArgumentException(
          "Unsupported job payload schema version: " + schemaVersion);
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
    validateCreatedAt(createdAt);
    if (!payload.isObject()) {
      throw new IllegalArgumentException("payload must be a JSON object");
    }
    validateInnerPayload(jobType, payload);
  }

  public enum JobStatus {
    READY,
    RUNNING,
    COMPLETED,
    FAILED,
    CANCELLED
  }

  private static int parseSchemaVersion(JsonNode schemaVersion) {
    if (schemaVersion == null
        || !schemaVersion.isNumber()
        || schemaVersion.decimalValue().compareTo(BigDecimal.ONE) != 0) {
      throw new IllegalArgumentException("Unsupported job payload schema version");
    }
    return VERSION;
  }

  private static void validateInnerPayload(String jobType, JsonNode payload) {
    if ("REQUIREMENT_PARSE".equals(jobType)) {
      requireExactKeys(payload, Set.of("originalObjectKey", "normalizedObjectKey"), "payload");
      requireText(payload, "originalObjectKey");
      requireText(payload, "normalizedObjectKey");
      return;
    }

    requireExactKeys(
        payload,
        Set.of("requirementFileId", "normalizedObjectKey", "solveMode", "targetSnapshot"),
        "payload");
    requireCanonicalUuid(requireText(payload, "requirementFileId"), "requirementFileId");
    requireText(payload, "normalizedObjectKey");
    if (!"COMPATIBLE".equals(requireText(payload, "solveMode"))) {
      throw new IllegalArgumentException("solveMode must be COMPATIBLE");
    }
    JsonNode snapshot = payload.path("targetSnapshot");
    requireExactKeys(
        snapshot,
        Set.of(
            "profileId",
            "profileCode",
            "os",
            "architecture",
            "pythonImplementation",
            "pythonVersion",
            "pythonFullVersion",
            "platformTag",
            "abiTags",
            "validationType",
            "validationPolicyVersion",
            "profileVersion"),
        "targetSnapshot");
    requireCanonicalUuid(requireText(snapshot, "profileId"), "profileId");
    for (String field :
        Set.of(
            "profileCode",
            "os",
            "architecture",
            "pythonImplementation",
            "pythonVersion",
            "pythonFullVersion",
            "platformTag",
            "validationType",
            "validationPolicyVersion")) {
      requireText(snapshot, field);
    }
    JsonNode abiTags = snapshot.path("abiTags");
    if (!abiTags.isArray() || abiTags.isEmpty()) {
      throw new IllegalArgumentException("abiTags must be a non-empty array");
    }
    for (JsonNode abiTag : abiTags) {
      if (!abiTag.isTextual() || abiTag.asText().isBlank()) {
        throw new IllegalArgumentException("abiTags must contain non-empty strings");
      }
    }
    JsonNode profileVersion = snapshot.path("profileVersion");
    if (!profileVersion.isNumber()
        || profileVersion.decimalValue().signum() < 0
        || profileVersion.decimalValue().stripTrailingZeros().scale() > 0) {
      throw new IllegalArgumentException("profileVersion must be a non-negative integer");
    }
  }

  private static void requireExactKeys(JsonNode object, Set<String> expected, String field) {
    if (!object.isObject()) {
      throw new IllegalArgumentException(field + " must be a JSON object");
    }
    Set<String> actual = new HashSet<>();
    object.propertyNames().forEach(actual::add);
    if (!actual.equals(expected)) {
      throw new IllegalArgumentException(field + " must contain exactly the V1 fields");
    }
  }

  private static String requireText(JsonNode object, String field) {
    JsonNode value = object.path(field);
    if (!value.isTextual() || value.asText().isBlank()) {
      throw new IllegalArgumentException(field + " must be a non-empty string");
    }
    return value.asText();
  }

  private static void requireCanonicalUuid(String value, String field) {
    UUID parsed = UUID.fromString(value);
    if (!parsed.toString().equalsIgnoreCase(value)) {
      throw new IllegalArgumentException(field + " must be a canonical UUID");
    }
  }

  private static void validateCreatedAt(String createdAt) {
    Matcher matcher = RFC3339_DATE_TIME.matcher(createdAt);
    if (!matcher.matches()) {
      throw new IllegalArgumentException(
          "createdAt must be an RFC3339 date-time string with a timezone");
    }
    int year = Integer.parseInt(matcher.group("year"));
    if (year == 0) {
      throw new IllegalArgumentException("createdAt year must be between 0001 and 9999");
    }
    try {
      LocalDate.of(
          year, Integer.parseInt(matcher.group("month")), Integer.parseInt(matcher.group("day")));
    } catch (DateTimeException exception) {
      throw new IllegalArgumentException("createdAt must contain a real calendar date", exception);
    }
  }
}
