package com.wheelforge.api.requirements;

import static org.assertj.core.api.Assertions.assertThat;

import com.wheelforge.api.common.jobs.BuildJobService;
import com.wheelforge.api.common.storage.LocalFileStorage;
import com.wheelforge.api.contracts.JobPayload;
import com.wheelforge.api.security.UserAccount;
import com.wheelforge.api.security.UserAccountRepository;
import com.wheelforge.api.target.TargetProfileEntity;
import com.wheelforge.api.target.TargetProfileRepository;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.transaction.annotation.Transactional;

@SpringBootTest(
    properties = {
      "wheelforge.auth.token-secret=0123456789abcdef0123456789abcdef",
      "wheelforge.storage.data-root=${java.io.tmpdir}/wheelforge-task-two-test"
    })
@EnabledIfEnvironmentVariable(named = "WF_TEST_JDBC_URL", matches = ".+")
class TaskTwoMySqlIntegrationTest {
  @Autowired private UserAccountRepository userRepository;
  @Autowired private RequirementFileRepository requirementFileRepository;
  @Autowired private TargetProfileRepository targetProfileRepository;
  @Autowired private BuildJobService buildJobService;
  @Autowired private JdbcTemplate jdbcTemplate;
  @MockitoBean private LocalFileStorage localFileStorage;

  @DynamicPropertySource
  static void databaseProperties(DynamicPropertyRegistry registry) {
    registry.add("spring.datasource.url", () -> System.getenv("WF_TEST_JDBC_URL"));
    registry.add("spring.datasource.username", () -> System.getenv("WF_TEST_DATABASE_USER"));
    registry.add("spring.datasource.password", () -> System.getenv("WF_TEST_DATABASE_PASSWORD"));
  }

  @Test
  @Transactional
  void persistsTaskTwoEntitiesAndReadsTheStrictJobPayloadFromMySqlJson() throws Exception {
    UUID userId = UUID.randomUUID();
    UUID fileId = UUID.randomUUID();
    UUID profileId = UUID.randomUUID();
    LocalDateTime now = LocalDateTime.now(ZoneOffset.UTC);

    userRepository.saveAndFlush(
        new UserAccount(
            userId.toString(), "mysql-" + userId, "$argon2id$test", "USER", "ACTIVE", now));
    targetProfileRepository.saveAndFlush(
        new TargetProfileEntity(
            profileId.toString(),
            "mysql-" + profileId,
            "LINUX",
            "ARM64",
            "CPYTHON",
            "3.11",
            "3.11.9",
            "manylinux2014_aarch64",
            List.of("cp311", "abi3", "none"),
            "STATIC",
            "v1",
            true));
    requirementFileRepository.saveAndFlush(
        new RequirementFileEntity(
            fileId.toString(),
            userId.toString(),
            "requirements.txt",
            17,
            "0".repeat(64),
            "users/" + userId + "/requirements/" + fileId + "/original.txt",
            "users/" + userId + "/requirements/" + fileId + "/normalized.txt",
            "PENDING",
            now));

    var job =
        buildJobService.enqueue(
            "REQUIREMENT_PARSE",
            fileId,
            BuildJobService.requirementParsePayload(
                "users/" + userId + "/requirements/" + fileId + "/original.txt",
                "users/" + userId + "/requirements/" + fileId + "/normalized.txt"));
    requirementFileRepository.flush();

    String payloadJson =
        jdbcTemplate.queryForObject(
            "select cast(payload_json as char) from build_jobs where id = ?",
            String.class,
            job.getId());
    JobPayload payload = JobPayload.databaseWireMapper().readValue(payloadJson, JobPayload.class);

    assertThat(payload.jobType()).isEqualTo("REQUIREMENT_PARSE");
    assertThat(payload.subjectId()).isEqualTo(fileId.toString());
    assertThat(payload.payload().get("originalObjectKey").asText()).endsWith("/original.txt");
    assertThat(targetProfileRepository.findById(profileId.toString()).orElseThrow().getAbiTags())
        .containsExactly("cp311", "abi3", "none");
    assertThat(requirementFileRepository.findByIdAndUserId(fileId.toString(), userId.toString()))
        .isPresent();
  }
}
