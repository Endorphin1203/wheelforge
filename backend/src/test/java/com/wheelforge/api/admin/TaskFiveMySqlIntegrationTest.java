package com.wheelforge.api.admin;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.asyncDispatch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.wheelforge.api.artifact.ArtifactEntity;
import com.wheelforge.api.artifact.ArtifactRepository;
import com.wheelforge.api.artifact.ArtifactService;
import com.wheelforge.api.artifact.DownloadRecordRepository;
import com.wheelforge.api.build.BuildStatus;
import com.wheelforge.api.build.BuildTaskEntity;
import com.wheelforge.api.build.BuildTaskRepository;
import com.wheelforge.api.build.SolveMode;
import com.wheelforge.api.common.storage.LocalFileStorage;
import com.wheelforge.api.requirements.RequirementFileEntity;
import com.wheelforge.api.requirements.RequirementFileRepository;
import com.wheelforge.api.security.TokenService;
import com.wheelforge.api.security.UserAccount;
import com.wheelforge.api.security.UserAccountRepository;
import com.wheelforge.api.target.TargetProfileEntity;
import com.wheelforge.api.target.TargetProfileRepository;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.security.MessageDigest;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.HexFormat;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.data.domain.Pageable;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.ObjectMapper;

@SpringBootTest(
    properties = {
      "wheelforge.auth.token-secret=0123456789abcdef0123456789abcdef",
      "wheelforge.storage.data-root=${java.io.tmpdir}/wheelforge-task-five-test",
      "wheelforge.retention.enabled=false"
    })
@AutoConfigureMockMvc
@EnabledIfEnvironmentVariable(named = "WF_TEST_JDBC_URL", matches = ".+")
class TaskFiveMySqlIntegrationTest {
  @Autowired private MockMvc mvc;
  @Autowired private ObjectMapper objectMapper;
  @Autowired private TokenService tokenService;
  @Autowired private UserAccountRepository userRepository;
  @Autowired private TargetProfileRepository targetProfileRepository;
  @Autowired private RequirementFileRepository requirementFileRepository;
  @Autowired private BuildTaskRepository taskRepository;
  @Autowired private ArtifactRepository artifactRepository;
  @Autowired private ArtifactService artifactService;
  @Autowired private DownloadRecordRepository downloadRecordRepository;
  @Autowired private PackageSourceRepository packageSourceRepository;
  @Autowired private LocalFileStorage storage;
  @Autowired private JdbcTemplate jdbcTemplate;
  @Autowired private PlatformTransactionManager transactionManager;

  @DynamicPropertySource
  static void databaseProperties(DynamicPropertyRegistry registry) {
    registry.add("spring.datasource.url", () -> System.getenv("WF_TEST_JDBC_URL"));
    registry.add("spring.datasource.username", () -> System.getenv("WF_TEST_DATABASE_USER"));
    registry.add("spring.datasource.password", () -> System.getenv("WF_TEST_DATABASE_PASSWORD"));
  }

  @BeforeEach
  void clearArtifactRows() {
    jdbcTemplate.update("delete from download_records");
    jdbcTemplate.update("delete from artifacts");
  }

  @Test
  void realControllersStreamOwnedArtifactAuditCompletionAndRoundTripTypedConfig() throws Exception {
    TestInput input = seedArtifact();
    byte[] zip = {80, 75, 3, 4};
    storage.putAtomically(input.objectKey(), new ByteArrayInputStream(zip), zip.length);

    mvc.perform(get("/api/artifacts").header(HttpHeaders.AUTHORIZATION, input.authorization()))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$[0].objectKey").doesNotExist());
    MvcResult pending =
        mvc.perform(
                get("/api/artifacts/{id}/download", input.artifactId())
                    .header(HttpHeaders.AUTHORIZATION, input.authorization())
                    .header(HttpHeaders.USER_AGENT, "native-mysql-test"))
            .andExpect(request().asyncStarted())
            .andReturn();
    mvc.perform(asyncDispatch(pending)).andExpect(status().isOk()).andExpect(content().bytes(zip));

    ArtifactEntity downloaded =
        artifactRepository.findById(input.artifactId().toString()).orElseThrow();
    assertThat(downloaded.getDownloadCount()).isEqualTo(1);
    assertThat(downloadRecordRepository.findAll())
        .filteredOn(record -> record.getArtifactId().equals(input.artifactId().toString()))
        .singleElement()
        .satisfies(record -> assertThat(record.isCompleted()).isTrue());

    MvcResult sources =
        mvc.perform(
                get("/api/admin/package-sources")
                    .header(HttpHeaders.AUTHORIZATION, input.authorization()))
            .andExpect(status().isOk())
            .andReturn();
    assertThat(objectMapper.readTree(sources.getResponse().getContentAsByteArray()))
        .extracting(node -> node.get("code").asText())
        .containsExactlyInAnyOrder("TSINGHUA", "ALIYUN", "PYPI");
    var sourceArray = objectMapper.readTree(sources.getResponse().getContentAsByteArray());
    var tsinghua =
        java.util.stream.StreamSupport.stream(sourceArray.spliterator(), false)
            .filter(node -> node.get("code").asText().equals("TSINGHUA"))
            .findFirst()
            .orElseThrow();
    String sourceId = tsinghua.get("id").asText();
    long sourceVersion = tsinghua.get("version").asLong();
    mvc.perform(
            put("/api/admin/package-sources/{id}", sourceId)
                .header(HttpHeaders.AUTHORIZATION, input.authorization())
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"enabled\":false,\"priorityNo\":9,\"timeoutSeconds\":45}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.version").value(sourceVersion + 1));
    assertThat(packageSourceRepository.findById(sourceId).orElseThrow().getVersionNo())
        .isEqualTo(sourceVersion + 1);

    MvcResult configs =
        mvc.perform(
                get("/api/admin/system-config")
                    .header(HttpHeaders.AUTHORIZATION, input.authorization()))
            .andExpect(status().isOk())
            .andReturn();
    var configArray = objectMapper.readTree(configs.getResponse().getContentAsByteArray());
    var retention =
        java.util.stream.StreamSupport.stream(configArray.spliterator(), false)
            .filter(node -> node.get("key").asText().equals("artifactRetentionDays"))
            .findFirst()
            .orElseThrow();
    long oldVersion = retention.get("version").asLong();

    mvc.perform(
            put("/api/admin/system-config")
                .header(HttpHeaders.AUTHORIZATION, input.authorization())
                .contentType(MediaType.APPLICATION_JSON)
                .content(
                    "{\"artifactRetentionDays\":{\"value\":45,\"version\":" + oldVersion + "}}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$[0].value").value(45))
        .andExpect(jsonPath("$[0].version").value(oldVersion + 1));
  }

  @Test
  void expiredArtifactClaimUsesSkipLockedAcrossTransactions() throws Exception {
    TestInput input = seedArtifact();
    LocalDateTime claimAt = LocalDateTime.of(2100, 1, 1, 0, 0);
    var firstClaimed = new CountDownLatch(1);
    var releaseFirst = new CountDownLatch(1);
    var executor = Executors.newFixedThreadPool(2);
    try {
      var first =
          executor.submit(
              () ->
                  new TransactionTemplate(transactionManager)
                      .execute(
                          ignored -> {
                            List<ArtifactEntity> rows =
                                artifactRepository.claimExpired(
                                    claimAt, claimAt.minusMinutes(5), 1);
                            firstClaimed.countDown();
                            await(releaseFirst);
                            return rows;
                          }));
      assertThat(firstClaimed.await(5, TimeUnit.SECONDS)).isTrue();
      var second =
          executor.submit(
              () ->
                  new TransactionTemplate(transactionManager)
                      .execute(
                          ignored ->
                              artifactRepository.claimExpired(
                                  claimAt, claimAt.minusMinutes(5), 1)));

      assertThat(second.get(5, TimeUnit.SECONDS)).isEmpty();
      releaseFirst.countDown();
      assertThat(first.get(5, TimeUnit.SECONDS))
          .singleElement()
          .extracting(ArtifactEntity::getId)
          .isEqualTo(input.artifactId().toString());
    } finally {
      releaseFirst.countDown();
      executor.shutdownNow();
    }
  }

  @Test
  void flushFailureLeavesThePersistedDownloadAuditIncomplete() throws Exception {
    TestInput input = seedArtifact();
    byte[] zip = {80, 75, 3, 4};
    storage.putAtomically(input.objectKey(), new ByteArrayInputStream(zip), zip.length);
    ArtifactService.DownloadTicket ticket =
        artifactService.prepareDownload(
            input.userId(), input.artifactId(), "127.0.0.1", "native-flush-test");

    assertThatThrownBy(() -> artifactService.stream(ticket, new FlushFailingOutputStream()))
        .isInstanceOf(IOException.class)
        .hasMessageContaining("flush failed");

    assertThat(downloadRecordRepository.findById(ticket.recordId().toString()).orElseThrow())
        .extracting(record -> record.isCompleted())
        .isEqualTo(false);
  }

  @Test
  void recentIncompleteDownloadLeaseBlocksRetentionButOldLeaseExpires() {
    TestInput input = seedArtifact();
    ArtifactEntity artifact =
        artifactRepository.findById(input.artifactId().toString()).orElseThrow();
    LocalDateTime claimAt = artifact.getExpiresAt().plusSeconds(1);
    String recordId = UUID.randomUUID().toString();
    assertThat(
            jdbcTemplate.update(
                """
                insert into download_records
                  (id, artifact_id, user_id, ip_address, user_agent, completed, downloaded_at)
                select ?, artifact.id, ?, ?, ?, false, artifact.expires_at
                  from artifacts artifact
                 where artifact.id = ?
                """,
                recordId,
                input.userId().toString(),
                "127.0.0.1",
                "lease-test",
                input.artifactId().toString()))
        .isEqualTo(1);

    assertThat(claimExpired(claimAt, claimAt.minusMinutes(5), 10)).isEmpty();

    assertThat(
            jdbcTemplate.update(
                """
                update download_records download
                  join artifacts artifact on artifact.id = download.artifact_id
                   set download.downloaded_at = date_sub(artifact.expires_at, interval 6 minute)
                 where download.id = ?
                """,
                recordId))
        .isEqualTo(1);
    assertThat(claimExpired(claimAt, claimAt.minusMinutes(5), 10))
        .extracting(ArtifactEntity::getId)
        .contains(input.artifactId().toString());
  }

  @Test
  void artifactCursorPaginationIsStableWithoutDuplicates() {
    TestInput input = seedArtifact();
    ArtifactEntity original =
        artifactRepository.findById(input.artifactId().toString()).orElseThrow();
    for (String id :
        List.of("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222")) {
      artifactRepository.saveAndFlush(
          new ArtifactEntity(
              id,
              input.taskId().toString(),
              "OFFLINE_ZIP",
              "wheelhouse.zip",
              "users/" + input.userId() + "/artifacts/" + id + "/wheelhouse.zip",
              4,
              sha256(new byte[] {80, 75, 3, 4}),
              "SUCCESS",
              "STATIC",
              original.getExpiresAt(),
              0,
              null,
              original.getCreatedAt()));
    }

    List<ArtifactEntity> first =
        artifactRepository.findPageByOwner(
            input.userId().toString(), null, null, Pageable.ofSize(2));
    ArtifactEntity cursor = first.getLast();
    List<ArtifactEntity> second =
        artifactRepository.findPageByOwner(
            input.userId().toString(), cursor.getCreatedAt(), cursor.getId(), Pageable.ofSize(2));

    assertThat(first).hasSize(2);
    assertThat(second).hasSize(1);
    assertThat(first).doesNotContainAnyElementsOf(second);
  }

  private TestInput seedArtifact() {
    UUID userId = UUID.randomUUID();
    UUID profileId = UUID.randomUUID();
    UUID fileId = UUID.randomUUID();
    UUID taskId = UUID.randomUUID();
    UUID artifactId = UUID.randomUUID();
    LocalDateTime now = LocalDateTime.now(ZoneOffset.UTC);
    UserAccount user =
        userRepository.save(
            new UserAccount(
                userId.toString(),
                "task-five-" + userId,
                "$argon2id$test",
                "ADMIN",
                "ACTIVE",
                now));
    TargetProfileEntity profile =
        targetProfileRepository.save(
            new TargetProfileEntity(
                profileId.toString(),
                "task-five-" + profileId,
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
            1,
            "0".repeat(64),
            "users/" + userId + "/requirements/original.txt",
            "users/" + userId + "/requirements/normalized.txt",
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
            now));
    String objectKey = "users/" + userId + "/artifacts/" + artifactId + "/wheelhouse.zip";
    artifactRepository.saveAndFlush(
        new ArtifactEntity(
            artifactId.toString(),
            taskId.toString(),
            "OFFLINE_ZIP",
            "wheelhouse.zip",
            objectKey,
            4,
            sha256(new byte[] {80, 75, 3, 4}),
            "SUCCESS",
            "STATIC",
            now.plusDays(30),
            0,
            null,
            now));
    return new TestInput(
        userId, taskId, artifactId, objectKey, "Bearer " + tokenService.issue(user).accessToken());
  }

  private static String sha256(byte[] value) {
    try {
      return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value));
    } catch (java.security.NoSuchAlgorithmException exception) {
      throw new IllegalStateException(exception);
    }
  }

  private List<ArtifactEntity> claimExpired(
      LocalDateTime now, LocalDateTime activeDownloadCutoff, int batchSize) {
    return new TransactionTemplate(transactionManager)
        .execute(ignored -> artifactRepository.claimExpired(now, activeDownloadCutoff, batchSize));
  }

  private void await(CountDownLatch latch) {
    try {
      if (!latch.await(5, TimeUnit.SECONDS)) {
        throw new AssertionError("Timed out waiting for concurrent transaction");
      }
    } catch (InterruptedException exception) {
      Thread.currentThread().interrupt();
      throw new AssertionError(exception);
    }
  }

  private record TestInput(
      UUID userId, UUID taskId, UUID artifactId, String objectKey, String authorization) {}

  private static final class FlushFailingOutputStream extends OutputStream {
    @Override
    public void write(int value) {}

    @Override
    public void flush() throws IOException {
      throw new IOException("flush failed");
    }
  }
}
