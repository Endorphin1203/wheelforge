package com.wheelforge.api.artifact;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.doThrow;

import com.wheelforge.api.common.storage.LocalFileStorage;
import java.time.LocalDateTime;
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

  @Test
  void cleansOnlyTheClaimedBoundedBatchAndIsIdempotent() {
    ArtifactEntity first = artifact("first.zip");
    ArtifactEntity second = artifact("second.zip");
    LocalDateTime now = LocalDateTime.of(2026, 7, 28, 1, 2, 3);
    given(artifactRepository.claimExpired(now, 2)).willReturn(List.of(first, second), List.of());
    var job = new ArtifactRetentionJob(artifactRepository, storage, 2);

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
    given(artifactRepository.claimExpired(now, 2)).willReturn(List.of(failed, succeeded));
    doThrow(new LocalFileStorage.StorageException("delete failed", new java.io.IOException()))
        .when(storage)
        .deleteIfExists(failed.getObjectKey());

    int cleaned = new ArtifactRetentionJob(artifactRepository, storage, 2).cleanupBatch(now);

    assertThat(cleaned).isEqualTo(1);
    assertThat(failed.getCleanedAt()).isNull();
    assertThat(succeeded.getCleanedAt()).isEqualTo(now);
  }

  private ArtifactEntity artifact(String filename) {
    return new ArtifactEntity(
        UUID.randomUUID().toString(),
        UUID.randomUUID().toString(),
        "OFFLINE_ZIP",
        filename,
        "artifacts/" + filename,
        4,
        "a".repeat(64),
        "SUCCESS",
        "STATIC",
        LocalDateTime.of(2026, 7, 1, 0, 0),
        0,
        null,
        LocalDateTime.of(2026, 6, 1, 0, 0));
  }
}
