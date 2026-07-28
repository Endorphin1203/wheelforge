package com.wheelforge.api.artifact;

import com.wheelforge.api.common.storage.LocalFileStorage;
import java.time.Clock;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

@Component
public class ArtifactRetentionJob {
  private static final Logger logger = LoggerFactory.getLogger(ArtifactRetentionJob.class);

  private final ArtifactRepository artifactRepository;
  private final LocalFileStorage storage;
  private final int batchSize;
  private final boolean schedulingEnabled;
  private final Clock clock;

  @Autowired
  public ArtifactRetentionJob(
      ArtifactRepository artifactRepository,
      LocalFileStorage storage,
      @Value("${wheelforge.retention.batch-size:100}") int batchSize,
      @Value("${wheelforge.retention.enabled:false}") boolean schedulingEnabled) {
    this(artifactRepository, storage, batchSize, schedulingEnabled, Clock.systemUTC());
  }

  ArtifactRetentionJob(
      ArtifactRepository artifactRepository, LocalFileStorage storage, int batchSize) {
    this(artifactRepository, storage, batchSize, false, Clock.systemUTC());
  }

  ArtifactRetentionJob(
      ArtifactRepository artifactRepository,
      LocalFileStorage storage,
      int batchSize,
      boolean schedulingEnabled,
      Clock clock) {
    if (batchSize < 1 || batchSize > 1000) {
      throw new IllegalArgumentException("Retention batch size must be between 1 and 1000");
    }
    this.artifactRepository = artifactRepository;
    this.storage = storage;
    this.batchSize = batchSize;
    this.schedulingEnabled = schedulingEnabled;
    this.clock = clock;
  }

  @Scheduled(fixedDelayString = "${wheelforge.retention.fixed-delay-ms:60000}")
  @Transactional
  public void scheduledCleanup() {
    if (schedulingEnabled) {
      cleanupClaimed(LocalDateTime.ofInstant(clock.instant(), ZoneOffset.UTC));
    }
  }

  @Transactional
  public int cleanupBatch(LocalDateTime now) {
    return cleanupClaimed(now);
  }

  private int cleanupClaimed(LocalDateTime now) {
    int cleaned = 0;
    for (ArtifactEntity artifact : artifactRepository.claimExpired(now, batchSize)) {
      try {
        storage.deleteIfExists(artifact.getObjectKey());
        artifact.markCleaned(now);
        cleaned++;
      } catch (LocalFileStorage.StorageException exception) {
        logger.warn("Artifact retention delete failed artifactId={}", artifact.getId(), exception);
      }
    }
    return cleaned;
  }
}
