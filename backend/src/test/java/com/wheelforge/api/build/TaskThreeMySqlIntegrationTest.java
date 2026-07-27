package com.wheelforge.api.build;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doThrow;

import com.wheelforge.api.common.ApiException;
import com.wheelforge.api.common.jobs.BuildJobRepository;
import com.wheelforge.api.common.jobs.BuildJobService;
import com.wheelforge.api.requirements.RequirementFileEntity;
import com.wheelforge.api.requirements.RequirementFileRepository;
import com.wheelforge.api.security.UserAccount;
import com.wheelforge.api.security.UserAccountRepository;
import com.wheelforge.api.target.TargetProfileEntity;
import com.wheelforge.api.target.TargetProfileRepository;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.dao.OptimisticLockingFailureException;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

@SpringBootTest(
    properties = {
      "wheelforge.auth.token-secret=0123456789abcdef0123456789abcdef",
      "wheelforge.storage.data-root=${java.io.tmpdir}/wheelforge-task-three-test"
    })
@EnabledIfEnvironmentVariable(named = "WF_TEST_JDBC_URL", matches = ".+")
class TaskThreeMySqlIntegrationTest {
  @Autowired private BuildTaskService buildTaskService;
  @Autowired private BuildTaskRepository buildTaskRepository;
  @Autowired private BuildJobRepository buildJobRepository;
  @Autowired private UserAccountRepository userRepository;
  @Autowired private RequirementFileRepository requirementFileRepository;
  @Autowired private TargetProfileRepository targetProfileRepository;
  @Autowired private JdbcTemplate jdbcTemplate;
  @Autowired private PlatformTransactionManager transactionManager;
  @MockitoSpyBean private BuildJobService buildJobService;

  @DynamicPropertySource
  static void databaseProperties(DynamicPropertyRegistry registry) {
    registry.add("spring.datasource.url", () -> System.getenv("WF_TEST_JDBC_URL"));
    registry.add("spring.datasource.username", () -> System.getenv("WF_TEST_DATABASE_USER"));
    registry.add("spring.datasource.password", () -> System.getenv("WF_TEST_DATABASE_PASSWORD"));
  }

  @Test
  void persistsQueuedTaskAndReadyBuildJobInOneServiceCall() {
    TestInput input = seedParsedInput();

    var created =
        buildTaskService.create(
            input.userId(),
            new BuildTaskService.CreateBuildTaskRequest(
                input.fileId(), input.profileId(), "COMPATIBLE"));

    assertThat(buildTaskRepository.findById(created.id().toString())).isPresent();
    var job =
        buildJobRepository
            .findBySubjectIdAndJobType(created.id().toString(), "BUILD")
            .orElseThrow();
    assertThat(job.getStatus()).isEqualTo("READY");
  }

  @Test
  void rollsBackTaskWhenBuildJobEnqueueFails() {
    TestInput input = seedParsedInput();
    doThrow(new IllegalStateException("job persistence failed"))
        .when(buildJobService)
        .enqueue(eq("BUILD"), any(UUID.class), any());

    assertThatThrownBy(
            () ->
                buildTaskService.create(
                    input.userId(),
                    new BuildTaskService.CreateBuildTaskRequest(
                        input.fileId(), input.profileId(), null)))
        .isInstanceOf(IllegalStateException.class)
        .hasMessage("job persistence failed");

    assertThat(
            buildTaskRepository.findAllByUserIdAndDeletedAtIsNullOrderByCreatedAtDesc(
                input.userId().toString()))
        .isEmpty();
  }

  @Test
  void overlappingCancelFirstPreventsWorkerClaimWithoutDeadlock() throws Exception {
    TestInput input = seedParsedInput();
    var created = create(input);
    var cancelHasJobLock = new CountDownLatch(1);
    var workerClaimFinished = new CountDownLatch(1);
    doAnswer(
            invocation -> {
              int affected = (Integer) invocation.callRealMethod();
              if (affected == 1) {
                cancelHasJobLock.countDown();
                await(workerClaimFinished);
              }
              return affected;
            })
        .when(buildJobService)
        .cancelReadyBuildJob(eq(created.id().toString()), any(LocalDateTime.class));
    ExecutorService executor = Executors.newFixedThreadPool(2);

    try {
      Future<BuildTaskService.BuildTaskView> cancellation =
          executor.submit(() -> buildTaskService.cancel(input.userId(), created.id()));
      await(cancelHasJobLock);
      Future<Boolean> workerClaim =
          executor.submit(
              () -> {
                try {
                  return claimBuildJobAndTask(created.id(), () -> {});
                } finally {
                  workerClaimFinished.countDown();
                }
              });

      assertThat(workerClaim.get(10, TimeUnit.SECONDS)).isFalse();
      assertThat(cancellation.get(10, TimeUnit.SECONDS).status()).isEqualTo(BuildStatus.CANCELLED);
    } finally {
      executor.shutdownNow();
    }

    BuildTaskEntity task = buildTaskRepository.findById(created.id().toString()).orElseThrow();
    assertThat(task.getStatus()).isEqualTo(BuildStatus.CANCELLED.name());
    assertThat(task.isCancelRequested()).isTrue();
    assertThat(task.getFinishedAt()).isNotNull();
    var job =
        buildJobRepository
            .findBySubjectIdAndJobType(created.id().toString(), "BUILD")
            .orElseThrow();
    assertThat(job.getStatus()).isEqualTo("CANCELLED");
  }

  @Test
  void overlappingWorkerClaimFirstPreservesCancellationFlagWithoutDeadlock() throws Exception {
    TestInput input = seedParsedInput();
    var created = create(input);
    var workerHasJobLock = new CountDownLatch(1);
    var cancelReachedJobCas = new CountDownLatch(1);
    doAnswer(
            invocation -> {
              cancelReachedJobCas.countDown();
              return invocation.callRealMethod();
            })
        .when(buildJobService)
        .cancelReadyBuildJob(eq(created.id().toString()), any(LocalDateTime.class));
    ExecutorService executor = Executors.newFixedThreadPool(2);

    try {
      Future<Boolean> workerClaim =
          executor.submit(
              () ->
                  claimBuildJobAndTask(
                      created.id(),
                      () -> {
                        workerHasJobLock.countDown();
                        await(cancelReachedJobCas);
                      }));
      await(workerHasJobLock);
      Future<BuildTaskService.BuildTaskView> cancellation =
          executor.submit(() -> buildTaskService.cancel(input.userId(), created.id()));

      assertThat(workerClaim.get(10, TimeUnit.SECONDS)).isTrue();
      BuildTaskService.BuildTaskView cancelled = cancellation.get(10, TimeUnit.SECONDS);
      assertThat(cancelled.status()).isEqualTo(BuildStatus.RESOLVING);
      assertThat(cancelled.cancelRequested()).isTrue();
    } finally {
      executor.shutdownNow();
    }

    BuildTaskEntity task = buildTaskRepository.findById(created.id().toString()).orElseThrow();
    assertThat(task.getStatus()).isEqualTo(BuildStatus.RESOLVING.name());
    assertThat(task.isCancelRequested()).isTrue();
    assertThat(task.getFinishedAt()).isNull();
    var job =
        buildJobRepository
            .findBySubjectIdAndJobType(created.id().toString(), "BUILD")
            .orElseThrow();
    assertThat(job.getStatus()).isEqualTo("RUNNING");
  }

  @Test
  void staleJpaVersionCommitUsesSpringOptimisticLockingFailureException() throws Exception {
    TestInput input = seedParsedInput();
    var created = create(input);
    var firstLoaded = new CountDownLatch(1);
    var staleLoaded = new CountDownLatch(1);
    var firstCommitted = new CountDownLatch(1);
    ExecutorService executor = Executors.newFixedThreadPool(2);
    TransactionTemplate transaction = new TransactionTemplate(transactionManager);

    try {
      Future<?> firstWriter =
          executor.submit(
              () ->
                  transaction.executeWithoutResult(
                      ignored -> {
                        BuildTaskEntity task =
                            buildTaskRepository.findById(created.id().toString()).orElseThrow();
                        firstLoaded.countDown();
                        await(staleLoaded);
                        task.requestCancellation();
                      }));
      Future<?> staleWriter =
          executor.submit(
              () ->
                  transaction.executeWithoutResult(
                      ignored -> {
                        await(firstLoaded);
                        BuildTaskEntity task =
                            buildTaskRepository.findById(created.id().toString()).orElseThrow();
                        staleLoaded.countDown();
                        await(firstCommitted);
                        task.softDelete(LocalDateTime.now(ZoneOffset.UTC));
                      }));

      firstWriter.get(10, TimeUnit.SECONDS);
      firstCommitted.countDown();
      assertThatThrownBy(() -> staleWriter.get(10, TimeUnit.SECONDS))
          .isInstanceOf(ExecutionException.class)
          .hasCauseInstanceOf(OptimisticLockingFailureException.class);
    } finally {
      firstCommitted.countDown();
      executor.shutdownNow();
    }

    BuildTaskEntity task = buildTaskRepository.findById(created.id().toString()).orElseThrow();
    assertThat(task.isCancelRequested()).isTrue();
    assertThat(task.getDeletedAt()).isNull();
  }

  @Test
  void ownerQueriesHideCrossUserAndDeletedTasksAndListNewestFirst() {
    TestInput input = seedParsedInput();
    var first = create(input);
    jdbcTemplate.update(
        "update build_tasks set created_at = date_sub(created_at, interval 1 second) where id = ?",
        first.id().toString());
    var second = create(input);

    assertThat(buildTaskService.get(input.userId(), first.id()).id()).isEqualTo(first.id());
    assertThatThrownBy(() -> buildTaskService.get(UUID.randomUUID(), first.id()))
        .isInstanceOf(ApiException.class)
        .extracting(exception -> ((ApiException) exception).status())
        .isEqualTo(HttpStatus.NOT_FOUND);
    assertThat(buildTaskService.list(input.userId()))
        .extracting(BuildTaskService.BuildTaskView::id)
        .containsExactly(second.id(), first.id());

    buildTaskService.delete(input.userId(), second.id());

    assertThatThrownBy(() -> buildTaskService.get(input.userId(), second.id()))
        .isInstanceOf(ApiException.class)
        .extracting(exception -> ((ApiException) exception).status())
        .isEqualTo(HttpStatus.NOT_FOUND);
    assertThatThrownBy(() -> buildTaskService.retry(input.userId(), second.id()))
        .isInstanceOf(ApiException.class)
        .extracting(exception -> ((ApiException) exception).status())
        .isEqualTo(HttpStatus.NOT_FOUND);
    assertThat(buildTaskService.list(input.userId()))
        .extracting(BuildTaskService.BuildTaskView::id)
        .containsExactly(first.id());
  }

  private BuildTaskService.BuildTaskView create(TestInput input) {
    return buildTaskService.create(
        input.userId(),
        new BuildTaskService.CreateBuildTaskRequest(
            input.fileId(), input.profileId(), "COMPATIBLE"));
  }

  private boolean claimBuildJobAndTask(UUID taskId, Runnable afterJobLocked) {
    Boolean claimed =
        new TransactionTemplate(transactionManager)
            .execute(
                ignored -> {
                  List<String> jobIds =
                      jdbcTemplate.queryForList(
                          """
                          select id
                            from build_jobs
                           where subject_id = ?
                             and job_type = 'BUILD'
                             and status = 'READY'
                           for update skip locked
                          """,
                          String.class,
                          taskId.toString());
                  if (jobIds.isEmpty()) {
                    return false;
                  }
                  String executionId = UUID.randomUUID().toString();
                  assertThat(
                          jdbcTemplate.update(
                              """
                              update build_jobs
                                 set status = 'RUNNING',
                                     execution_id = ?,
                                     lease_owner = 'task-three-test-worker',
                                     lease_expires_at = date_add(current_timestamp(6), interval 1 minute),
                                     heartbeat_at = current_timestamp(6),
                                     started_at = current_timestamp(6),
                                     attempts = attempts + 1,
                                     version_no = version_no + 1
                               where id = ?
                                 and status = 'READY'
                              """,
                              executionId,
                              jobIds.getFirst()))
                      .isOne();
                  afterJobLocked.run();
                  assertThat(
                          jdbcTemplate.update(
                              """
                              update build_tasks
                                 set status = 'RESOLVING',
                                     execution_id = ?,
                                     started_at = current_timestamp(6),
                                     version_no = version_no + 1
                               where id = ?
                                 and status = 'QUEUED'
                                 and cancel_requested = false
                              """,
                              executionId,
                              taskId.toString()))
                      .isOne();
                  return true;
                });
    return Boolean.TRUE.equals(claimed);
  }

  private static void await(CountDownLatch latch) {
    try {
      assertThat(latch.await(10, TimeUnit.SECONDS)).isTrue();
    } catch (InterruptedException exception) {
      Thread.currentThread().interrupt();
      throw new AssertionError("Interrupted while coordinating database race", exception);
    }
  }

  private TestInput seedParsedInput() {
    UUID userId = UUID.randomUUID();
    UUID fileId = UUID.randomUUID();
    UUID profileId = UUID.randomUUID();
    LocalDateTime now = LocalDateTime.now(ZoneOffset.UTC);
    userRepository.saveAndFlush(
        new UserAccount(
            userId.toString(), "task-three-" + userId, "$argon2id$test", "USER", "ACTIVE", now));
    targetProfileRepository.saveAndFlush(
        new TargetProfileEntity(
            profileId.toString(),
            "task-three-" + profileId,
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
    requirementFileRepository.saveAndFlush(
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
    return new TestInput(userId, fileId, profileId);
  }

  private record TestInput(UUID userId, UUID fileId, UUID profileId) {}
}
