package com.wheelforge.api.artifact;

import com.wheelforge.api.admin.SystemConfigRepository;
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
  private final SystemConfigRepository configRepository;
  private final int batchSize;
  private final boolean schedulingEnabled;
  private final int activeDownloadLeaseSeconds;
  private final Clock clock;

  @Autowired
  public ArtifactRetentionJob(
      ArtifactRepository artifactRepository,
      LocalFileStorage storage,
      SystemConfigRepository configRepository,
      @Value("${wheelforge.retention.batch-size:100}") int batchSize,
      @Value("${wheelforge.retention.enabled:false}") boolean schedulingEnabled,
      @Value("${wheelforge.retention.active-download-lease-seconds:300}")
          int activeDownloadLeaseSeconds) {
    this(
        artifactRepository,
        storage,
        configRepository,
        batchSize,
        schedulingEnabled,
        activeDownloadLeaseSeconds,
        Clock.systemUTC());
  }

  ArtifactRetentionJob(
      ArtifactRepository artifactRepository,
      LocalFileStorage storage,
      SystemConfigRepository configRepository,
      int batchSize,
      boolean schedulingEnabled,
      int activeDownloadLeaseSeconds,
      Clock clock) {
    if (batchSize < 1 || batchSize > 1000) {
      throw new IllegalArgumentException("Retention batch size must be between 1 and 1000");
    }
    if (activeDownloadLeaseSeconds < 1 || activeDownloadLeaseSeconds > 3600) {
      throw new IllegalArgumentException(
          "Active download lease must be between 1 and 3600 seconds");
    }
    this.artifactRepository = artifactRepository;
    this.storage = storage;
    this.configRepository = configRepository;
    this.batchSize = batchSize;
    this.schedulingEnabled = schedulingEnabled;
    this.activeDownloadLeaseSeconds = activeDownloadLeaseSeconds;
    this.clock = clock;
  }

  @Scheduled(fixedDelayString = "${wheelforge.retention.fixed-delay-ms:60000}")
  @Transactional
  public void scheduledCleanup() {
    if (schedulingEnabled && databaseRetentionEnabled()) {
      cleanupClaimed(LocalDateTime.ofInstant(clock.instant(), ZoneOffset.UTC));
    }
  }

  @Transactional
  public int cleanupBatch(LocalDateTime now) {
    return cleanupClaimed(now);
  }

  private int cleanupClaimed(LocalDateTime now) {
    int cleaned = 0;
    LocalDateTime activeDownloadCutoff = now.minusSeconds(activeDownloadLeaseSeconds);
    for (ArtifactEntity artifact :
        artifactRepository.claimExpired(now, activeDownloadCutoff, batchSize)) {
      try {
        storage.deleteIfExists(artifact.getObjectKey());
        artifact.markCleaned(now);
        cleaned++;
      } catch (LocalFileStorage.StorageException | IllegalArgumentException exception) {
        logger.warn("Artifact retention delete failed artifactId={}", artifact.getId(), exception);
      }
    }
    return cleaned;
  }

  private boolean databaseRetentionEnabled() {
    return configRepository
        .findById("retentionEnabled")
        .map(config -> config.getConfigValue())
        .filter(value -> value != null && value.isBoolean())
        .map(value -> value.booleanValue())
        .orElse(false);
  }
}
