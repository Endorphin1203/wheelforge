package com.wheelforge.api.artifact;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.reset;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;

import com.wheelforge.api.admin.SystemConfigEntity;
import com.wheelforge.api.admin.SystemConfigRepository;
import com.wheelforge.api.common.storage.LocalFileStorage;
import java.time.Clock;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

@ExtendWith(MockitoExtension.class)
class ArtifactRetentionJobTest {
  @Mock private ArtifactRepository artifactRepository;
  @Mock private LocalFileStorage storage;
  @Mock private SystemConfigRepository configRepository;

  @Test
  void cleansOnlyTheClaimedBoundedBatchAndIsIdempotent() {
    ArtifactEntity first = artifact("first.zip");
    ArtifactEntity second = artifact("second.zip");
    LocalDateTime now = LocalDateTime.of(2026, 7, 28, 1, 2, 3);
    given(artifactRepository.claimExpired(now, now.minusMinutes(5), 2))
        .willReturn(List.of(first, second), List.of());
    var job = job(false, now);

    assertThat(job.cleanupBatch(now)).isEqualTo(2);
    assertThat(first.getCleanedAt()).isEqualTo(now);
    assertThat(second.getCleanedAt()).isEqualTo(now);
    assertThat(job.cleanupBatch(now)).isZero();
  }

  @Test
  void storageFailureLeavesArtifactRetryableAndDoesNotBlockOtherClaims() {
    ArtifactEntity failed = artifact("failed.zip");
    ArtifactEntity succeeded = artifact("succeeded.zip");
    LocalDateTime now = LocalDateTime.of(2026, 7, 28, 1, 2, 3);
    given(artifactRepository.claimExpired(now, now.minusMinutes(5), 2))
        .willReturn(List.of(failed, succeeded));
    doThrow(new LocalFileStorage.StorageException("delete failed", new java.io.IOException()))
        .when(storage)
        .deleteIfExists(failed.getObjectKey());

    int cleaned = job(false, now).cleanupBatch(now);

    assertThat(cleaned).isEqualTo(1);
    assertThat(failed.getCleanedAt()).isNull();
    assertThat(succeeded.getCleanedAt()).isEqualTo(now);
  }

  @Test
  void malformedObjectKeyRemainsRetryableWithoutBlockingLaterRows() {
    ArtifactEntity malformed = artifact("bad.zip", "../outside.zip");
    ArtifactEntity succeeded = artifact("succeeded.zip");
    LocalDateTime now = LocalDateTime.of(2026, 7, 28, 1, 2, 3);
    given(artifactRepository.claimExpired(now, now.minusMinutes(5), 2))
        .willReturn(List.of(malformed, succeeded));
    doThrow(new IllegalArgumentException("invalid object key"))
        .when(storage)
        .deleteIfExists(malformed.getObjectKey());

    assertThat(job(false, now).cleanupBatch(now)).isEqualTo(1);
    assertThat(malformed.getCleanedAt()).isNull();
    assertThat(succeeded.getCleanedAt()).isEqualTo(now);
  }

  @Test
  void scheduledCleanupRequiresInfrastructureAndRuntimeDatabaseSwitches() throws Exception {
    LocalDateTime now = LocalDateTime.of(2026, 7, 28, 1, 2, 3);

    for (boolean infrastructureEnabled : new boolean[] {false, true}) {
      for (boolean databaseEnabled : new boolean[] {false, true}) {
        reset(artifactRepository, configRepository, storage);
        given(configRepository.findById("retentionEnabled"))
            .willReturn(java.util.Optional.of(retentionConfig(databaseEnabled, now)));
        given(artifactRepository.claimExpired(now, now.minusMinutes(5), 2)).willReturn(List.of());

        job(infrastructureEnabled, now).scheduledCleanup();

        verify(artifactRepository, times(infrastructureEnabled && databaseEnabled ? 1 : 0))
            .claimExpired(now, now.minusMinutes(5), 2);
      }
    }
  }

  private ArtifactEntity artifact(String filename) {
    return artifact(filename, "artifacts/" + filename);
  }

  private ArtifactEntity artifact(String filename, String objectKey) {
    return new ArtifactEntity(
        UUID.randomUUID().toString(),
        UUID.randomUUID().toString(),
        "OFFLINE_ZIP",
        filename,
        objectKey,
        4,
        "a".repeat(64),
        "SUCCESS",
        "STATIC",
        LocalDateTime.of(2026, 7, 1, 0, 0),
        0,
        null,
        LocalDateTime.of(2026, 6, 1, 0, 0));
  }

  private ArtifactRetentionJob job(boolean infrastructureEnabled, LocalDateTime now) {
    return new ArtifactRetentionJob(
        artifactRepository,
        storage,
        configRepository,
        2,
        infrastructureEnabled,
        300,
        Clock.fixed(now.toInstant(ZoneOffset.UTC), ZoneOffset.UTC));
  }

  private SystemConfigEntity retentionConfig(boolean enabled, LocalDateTime now) throws Exception {
    return new SystemConfigEntity(
        "retentionEnabled",
        new tools.jackson.databind.ObjectMapper().readTree(Boolean.toString(enabled)),
        "Retention enabled",
        null,
        now);
  }
}
