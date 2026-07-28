package com.wheelforge.api.build;

import static org.assertj.core.api.Assertions.assertThat;

import com.wheelforge.api.requirements.RequirementFileEntity;
import com.wheelforge.api.requirements.RequirementFileRepository;
import com.wheelforge.api.security.UserAccount;
import com.wheelforge.api.security.UserAccountRepository;
import com.wheelforge.api.target.TargetProfileEntity;
import com.wheelforge.api.target.TargetProfileRepository;
import jakarta.persistence.EntityManager;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.ObjectMapper;

@SpringBootTest(
    properties = {
      "wheelforge.auth.token-secret=0123456789abcdef0123456789abcdef",
      "wheelforge.storage.data-root=${java.io.tmpdir}/wheelforge-task-four-test"
    })
@EnabledIfEnvironmentVariable(named = "WF_TEST_JDBC_URL", matches = ".+")
class TaskFourMySqlIntegrationTest {
  @Autowired private UserAccountRepository userRepository;
  @Autowired private TargetProfileRepository targetProfileRepository;
  @Autowired private RequirementFileRepository requirementFileRepository;
  @Autowired private BuildTaskRepository taskRepository;
  @Autowired private BuildLogRepository logRepository;
  @Autowired private ResolvedPackageRepository resolvedRepository;
  @Autowired private BuildResultService resultService;
  @Autowired private EntityManager entityManager;
  @Autowired private ObjectMapper objectMapper;

  @DynamicPropertySource
  static void databaseProperties(DynamicPropertyRegistry registry) {
    registry.add("spring.datasource.url", () -> System.getenv("WF_TEST_JDBC_URL"));
    registry.add("spring.datasource.username", () -> System.getenv("WF_TEST_DATABASE_USER"));
    registry.add("spring.datasource.password", () -> System.getenv("WF_TEST_DATABASE_PASSWORD"));
  }

  @Test
  @Transactional
  void roundTripsStructuredJsonAndAppliesTheRealMySqlLogWindow() {
    TestInput input = seedTask();
    var structuredContext = objectMapper.createObjectNode();
    structuredContext.put("candidate", "2.32.4");
    structuredContext.put("cached", true);

    List<BuildLogEntity> logs = new ArrayList<>(501);
    for (int sequence = 501; sequence >= 1; sequence--) {
      logs.add(
          new BuildLogEntity(
              UUID.randomUUID().toString(),
              input.taskId().toString(),
              sequence,
              "DOWNLOAD",
              "INFO",
              "log-" + sequence,
              sequence == 1 ? structuredContext : null,
              input.now().plusNanos(sequence * 1_000L)));
    }
    logRepository.saveAll(logs);

    var selectedAttempts = objectMapper.createArrayNode();
    selectedAttempts.addObject().put("version", "2.32.4").put("outcome", "SELECTED");
    var missingAttempts = objectMapper.createArrayNode();
    missingAttempts.addObject().put("version", "1.0.0").put("outcome", "NO_WHEEL");
    resolvedRepository.saveAll(
        List.of(
            resolvedPackage(
                input.taskId(),
                "requests",
                selectedAttempts,
                List.of("py3-none-any"),
                "STATIC_PASSED"),
            resolvedPackage(input.taskId(), "unavailable", missingAttempts, null, "MISSING")));

    entityManager.flush();
    entityManager.clear();

    List<BuildResultService.BuildLogView> firstWindow =
        resultService.logs(input.userId(), input.taskId(), 0);
    assertThat(firstWindow).hasSize(500);
    assertThat(firstWindow)
        .extracting(BuildResultService.BuildLogView::sequence)
        .containsExactlyElementsOf(
            java.util.stream.LongStream.rangeClosed(1, 500).boxed().toList());
    assertThat(firstWindow.getFirst().context().path("candidate").asText()).isEqualTo("2.32.4");
    assertThat(firstWindow.getFirst().context().path("cached").asBoolean()).isTrue();
    assertThat(firstWindow.get(1).context()).isNull();

    assertThat(resultService.logs(input.userId(), input.taskId(), 500))
        .extracting(BuildResultService.BuildLogView::sequence)
        .containsExactly(501L);

    List<ResolvedPackageEntity> persistedPackages =
        resolvedRepository.findAllByBuildTaskIdOrderByNormalizedNameAsc(input.taskId().toString());
    assertThat(persistedPackages).hasSize(2);
    assertThat(persistedPackages.getFirst().getCandidateAttempts().isArray()).isTrue();
    assertThat(persistedPackages.getFirst().getCandidateAttempts().path(0).path("outcome").asText())
        .isEqualTo("SELECTED");
    assertThat(persistedPackages.getFirst().getWheelTags()).containsExactly("py3-none-any");
    assertThat(persistedPackages.get(1).getCandidateAttempts().isArray()).isTrue();
    assertThat(persistedPackages.get(1).getWheelTags()).isNull();

    assertThat(resultService.resolvedPackages(input.userId(), input.taskId()))
        .extracting(BuildResultService.ResolvedPackageView::normalizedName)
        .containsExactly("requests", "unavailable");
  }

  private TestInput seedTask() {
    UUID userId = UUID.randomUUID();
    UUID profileId = UUID.randomUUID();
    UUID fileId = UUID.randomUUID();
    UUID taskId = UUID.randomUUID();
    LocalDateTime now = LocalDateTime.now(ZoneOffset.UTC);
    userRepository.save(
        new UserAccount(
            userId.toString(), "task-four-" + userId, "$argon2id$test", "USER", "ACTIVE", now));
    TargetProfileEntity profile =
        targetProfileRepository.save(
            new TargetProfileEntity(
                profileId.toString(),
                "task-four-" + profileId,
                "LINUX",
                "AARCH64",
                "CPYTHON",
                "3.11",
                "3.11.9",
                "manylinux2014_aarch64",
                List.of("cp311", "abi3", "none"),
                "STATIC",
                "wheel-tags-v1",
                true));
    requirementFileRepository.save(
        new RequirementFileEntity(
            fileId.toString(),
            userId.toString(),
            "requirements.txt",
            17,
            "0".repeat(64),
            "users/" + userId + "/requirements/" + fileId + "/original.txt",
            "users/" + userId + "/requirements/" + fileId + "/normalized.txt",
            "PARSED",
            now));
    taskRepository.save(
        new BuildTaskEntity(
            taskId.toString(),
            userId.toString(),
            fileId.toString(),
            profileId.toString(),
            null,
            BuildStatus.SUCCESS,
            SolveMode.COMPATIBLE,
            BuildTaskEntity.TargetSnapshot.from(profile, profile.getVersionNo()),
            now,
            null));
    return new TestInput(userId, taskId, now);
  }

  private ResolvedPackageEntity resolvedPackage(
      UUID taskId,
      String normalizedName,
      tools.jackson.databind.JsonNode attempts,
      List<String> wheelTags,
      String wheelStatus) {
    return new ResolvedPackageEntity(
        UUID.randomUUID().toString(),
        taskId.toString(),
        normalizedName,
        "2.32.4",
        "DIRECT",
        ">=2.31",
        "2.31.0",
        "UPGRADE",
        "MySQL round-trip test",
        attempts,
        normalizedName + "-2.32.4-py3-none-any.whl",
        wheelTags,
        "PYPI",
        "a".repeat(64),
        wheelStatus,
        null);
  }

  private record TestInput(UUID userId, UUID taskId, LocalDateTime now) {}
}
