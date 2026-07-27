package com.wheelforge.api.common.jobs;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.verify;

import com.wheelforge.api.contracts.JobPayload;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.transaction.support.TransactionSynchronizationManager;

@ExtendWith(MockitoExtension.class)
class BuildJobServiceTest {
  private static final Instant NOW = Instant.parse("2026-07-23T01:02:03Z");
  private static final UUID SUBJECT_ID = UUID.fromString("2b0e75d6-c238-4f39-9bf9-5e246eb7c4cd");

  @Mock BuildJobRepository repository;

  @AfterEach
  void clearTransactionState() {
    TransactionSynchronizationManager.setActualTransactionActive(false);
  }

  @Test
  void writesAReadyJobUsingTheStrictSharedPayloadContract() throws Exception {
    TransactionSynchronizationManager.setActualTransactionActive(true);
    given(repository.save(any())).willAnswer(invocation -> invocation.getArgument(0));
    var service =
        new BuildJobService(repository, Clock.fixed(NOW, ZoneOffset.UTC), () -> UUID.randomUUID());
    var payload =
        BuildJobService.requirementParsePayload(
            "users/user/requirements/file/original.txt",
            "users/user/requirements/file/normalized.txt");

    service.enqueue("REQUIREMENT_PARSE", SUBJECT_ID, payload);

    var captor = ArgumentCaptor.forClass(BuildJobEntity.class);
    verify(repository).save(captor.capture());
    BuildJobEntity job = captor.getValue();
    assertThat(job.getJobType()).isEqualTo("REQUIREMENT_PARSE");
    assertThat(job.getPayloadVersion()).isEqualTo(1);
    assertThat(job.getStatus()).isEqualTo("READY");
    assertThat(job.getAvailableAt()).isEqualTo(NOW.atOffset(ZoneOffset.UTC).toLocalDateTime());
    JobPayload wire =
        JobPayload.databaseWireMapper().readValue(job.getPayloadJson(), JobPayload.class);
    assertThat(wire.subjectId()).isEqualTo(SUBJECT_ID.toString());
    assertThat(wire.createdAt()).isEqualTo("2026-07-23T01:02:03Z");
    assertThat(wire.payload()).isEqualTo(payload);
  }

  @Test
  void refusesToCreateAJobOutsideTheCallersTransaction() {
    var service =
        new BuildJobService(repository, Clock.fixed(NOW, ZoneOffset.UTC), () -> UUID.randomUUID());

    assertThatThrownBy(
            () ->
                service.enqueue(
                    "REQUIREMENT_PARSE",
                    SUBJECT_ID,
                    BuildJobService.requirementParsePayload("original", "normalized")))
        .isInstanceOf(IllegalStateException.class)
        .hasMessageContaining("transaction");
  }
}
