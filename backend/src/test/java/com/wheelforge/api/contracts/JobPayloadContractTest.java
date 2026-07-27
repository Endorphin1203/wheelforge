package com.wheelforge.api.contracts;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.wheelforge.api.build.BuildStatus;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

class JobPayloadContractTest {
  private final ObjectMapper objectMapper = JobPayload.databaseWireMapper();

  @Test
  void readsBuildFixture() throws Exception {
    var payload = objectMapper.readValue(readFixture("build-v1.json"), JobPayload.class);

    assertThat(payload.schemaVersion()).isEqualTo(1);
    assertThat(payload.jobType()).isEqualTo("BUILD");
    assertThat(payload.payload().path("solveMode").asText()).isEqualTo("COMPATIBLE");
    assertThat(payload.payload().path("targetSnapshot").path("architecture").asText())
        .isEqualTo("AARCH64");
    assertThat(payload.payload().path("targetSnapshot").path("platformTag").asText())
        .isEqualTo("manylinux2014_aarch64");
    assertThat(payload.payload().path("targetSnapshot").path("abiTags").toString())
        .isEqualTo("[\"cp311\",\"abi3\",\"none\"]");
    assertThat(payload.payload().path("targetSnapshot").path("pythonVersion").asText())
        .isEqualTo("3.11");
    assertThat(payload.payload().propertyNames())
        .containsExactlyInAnyOrder(
            "requirementFileId", "normalizedObjectKey", "solveMode", "targetSnapshot");
    assertThat(payload.payload().path("targetSnapshot").propertyNames())
        .containsExactlyInAnyOrderElementsOf(
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
                "profileVersion"));
  }

  @Test
  void readsRequirementParseFixture() throws Exception {
    var payload =
        objectMapper.readValue(readFixture("requirement-parse-v1.json"), JobPayload.class);

    assertThat(payload.jobType()).isEqualTo("REQUIREMENT_PARSE");
    assertThat(payload.payload().path("originalObjectKey").asText()).endsWith("/original.txt");
    assertThat(payload.payload().propertyNames())
        .containsExactlyInAnyOrder("originalObjectKey", "normalizedObjectKey");
  }

  @Test
  void rejectsMissingUnknownMutableAndMistypedInnerPayloadFields() throws Exception {
    ObjectNode build = (ObjectNode) objectMapper.readTree(readFixture("build-v1.json"));
    ObjectNode missingBuildField = build.deepCopy();
    ((ObjectNode) missingBuildField.path("payload")).remove("normalizedObjectKey");
    ObjectNode unknownBuildField = build.deepCopy();
    ((ObjectNode) unknownBuildField.path("payload")).put("unknown", true);
    ObjectNode mutableBuildField = build.deepCopy();
    ((ObjectNode) mutableBuildField.path("payload")).put("attempts", 1);
    ObjectNode missingSnapshotField = build.deepCopy();
    ((ObjectNode) missingSnapshotField.path("payload").path("targetSnapshot")).remove("profileId");
    ObjectNode unknownSnapshotField = build.deepCopy();
    ((ObjectNode) unknownSnapshotField.path("payload").path("targetSnapshot"))
        .put("leaseOwner", "worker-1");
    ObjectNode mistypedSnapshotField = build.deepCopy();
    ((ObjectNode) mistypedSnapshotField.path("payload").path("targetSnapshot"))
        .put("profileVersion", "1");
    ObjectNode requirementParse =
        (ObjectNode) objectMapper.readTree(readFixture("requirement-parse-v1.json"));
    ((ObjectNode) requirementParse.path("payload")).put("status", "READY");

    for (ObjectNode invalid :
        List.of(
            missingBuildField,
            unknownBuildField,
            mutableBuildField,
            missingSnapshotField,
            unknownSnapshotField,
            mistypedSnapshotField,
            requirementParse)) {
      assertThatThrownBy(
              () ->
                  objectMapper.readValue(
                      objectMapper.writeValueAsString(invalid), JobPayload.class))
          .isInstanceOf(Exception.class);
    }
  }

  @Test
  void readsSchemaValidEdgeFixtures() throws Exception {
    for (String fixture : listFixtureNames("valid")) {
      var payload = objectMapper.readValue(readValidFixture(fixture), JobPayload.class);

      assertThat(payload.schemaVersion()).isEqualTo(1);
    }
  }

  @Test
  void rejectsUnsupportedSchemaVersionDuringDeserialization() {
    assertThatThrownBy(
            () ->
                objectMapper.readValue(
                    """
            {"schemaVersion":2,"jobType":"BUILD","subjectId":"fe3b9a09-e696-4104-beb7-d8fd1fb85d24","createdAt":"2026-07-22T10:05:00Z","payload":{}}
            """,
                    JobPayload.class))
        .hasRootCauseInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("Unsupported job payload schema version");
  }

  @Test
  void rejectsUnsupportedJobTypeDuringConstruction() {
    assertThatThrownBy(
            () ->
                new JobPayload(
                    1,
                    "EXPORT",
                    "fe3b9a09-e696-4104-beb7-d8fd1fb85d24",
                    "2026-07-22T10:05:00Z",
                    objectMapper.createObjectNode()))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("Unsupported job type");
  }

  @Test
  void rejectsMalformedSubjectIdDuringConstruction() {
    assertThatThrownBy(
            () ->
                new JobPayload(
                    1,
                    "BUILD",
                    "not-a-uuid",
                    "2026-07-22T10:05:00Z",
                    objectMapper.createObjectNode()))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("Invalid UUID string");
  }

  @Test
  void rejectsMalformedCreatedAtDuringConstruction() {
    assertThatThrownBy(
            () ->
                new JobPayload(
                    1,
                    "BUILD",
                    "fe3b9a09-e696-4104-beb7-d8fd1fb85d24",
                    "2026-07-22T10:05:00",
                    objectMapper.createObjectNode()))
        .isInstanceOf(IllegalArgumentException.class);
  }

  @Test
  void rejectsInvalidSharedFixturesDuringDeserialization() throws Exception {
    for (String fixture : listFixtureNames("invalid")) {
      assertThatThrownBy(
              () -> objectMapper.readValue(readInvalidFixture(fixture), JobPayload.class))
          .isInstanceOf(Exception.class);
    }
  }

  @Test
  void fixturePayloadsExcludeMutableDatabaseRowFields() throws Exception {
    for (String fixture : List.of("requirement-parse-v1.json", "build-v1.json")) {
      var document = objectMapper.readTree(readFixture(fixture));

      assertThat(document.propertyNames())
          .doesNotContain(
              "id",
              "jobId",
              "executionId",
              "attempts",
              "leaseOwner",
              "leaseExpiresAt",
              "heartbeatAt");
    }
  }

  @Test
  void keepsBuildTaskAndDatabaseJobStateSetsSeparate() {
    assertThat(BuildStatus.values())
        .containsExactly(
            BuildStatus.CREATED,
            BuildStatus.PARSING,
            BuildStatus.QUEUED,
            BuildStatus.RESOLVING,
            BuildStatus.DOWNLOADING,
            BuildStatus.VALIDATING,
            BuildStatus.PACKAGING,
            BuildStatus.SUCCESS,
            BuildStatus.PARTIAL_SUCCESS,
            BuildStatus.FAILED,
            BuildStatus.CANCELLED);
    assertThat(JobPayload.JobStatus.values())
        .containsExactly(
            JobPayload.JobStatus.READY,
            JobPayload.JobStatus.RUNNING,
            JobPayload.JobStatus.COMPLETED,
            JobPayload.JobStatus.FAILED,
            JobPayload.JobStatus.CANCELLED);
  }

  private String readFixture(String fileName) throws Exception {
    return Files.readString(Path.of("..", "contracts", "examples", fileName));
  }

  private String readInvalidFixture(String fileName) throws Exception {
    return Files.readString(Path.of("..", "contracts", "examples", "invalid", fileName));
  }

  private String readValidFixture(String fileName) throws Exception {
    return Files.readString(Path.of("..", "contracts", "examples", "valid", fileName));
  }

  private List<String> listFixtureNames(String directory) throws Exception {
    try (var fixtures = Files.list(Path.of("..", "contracts", "examples", directory))) {
      return fixtures
          .filter(path -> path.getFileName().toString().endsWith(".json"))
          .map(path -> path.getFileName().toString())
          .sorted()
          .toList();
    }
  }
}
