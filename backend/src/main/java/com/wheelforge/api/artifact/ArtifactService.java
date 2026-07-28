package com.wheelforge.api.artifact;

import com.wheelforge.api.common.ApiException;
import com.wheelforge.api.common.storage.LocalFileStorage;
import java.io.IOException;
import java.io.OutputStream;
import java.time.Clock;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class ArtifactService {
  public static final String VALIDATION_MESSAGE =
      "Static compatibility checks passed; target installation was not verified.";

  private final ArtifactRepository artifactRepository;
  private final DownloadRecordRepository downloadRecordRepository;
  private final LocalFileStorage storage;
  private final Clock clock;

  @Autowired
  public ArtifactService(
      ArtifactRepository artifactRepository,
      DownloadRecordRepository downloadRecordRepository,
      LocalFileStorage storage) {
    this(artifactRepository, downloadRecordRepository, storage, Clock.systemUTC());
  }

  ArtifactService(
      ArtifactRepository artifactRepository,
      DownloadRecordRepository downloadRecordRepository,
      LocalFileStorage storage,
      Clock clock) {
    this.artifactRepository = artifactRepository;
    this.downloadRecordRepository = downloadRecordRepository;
    this.storage = storage;
    this.clock = clock;
  }

  @Transactional(readOnly = true)
  public List<ArtifactView> list(UUID userId) {
    return artifactRepository.findAllByOwner(userId.toString()).stream().map(this::view).toList();
  }

  @Transactional(readOnly = true)
  public ArtifactView get(UUID userId, UUID artifactId) {
    return view(findOwned(userId, artifactId));
  }

  @Transactional
  public DownloadTicket prepareDownload(
      UUID userId, UUID artifactId, String ipAddress, String userAgent) {
    ArtifactEntity artifact =
        artifactRepository
            .findByIdAndOwnerForUpdate(artifactId.toString(), userId.toString())
            .orElseThrow(() -> ApiException.notFound("Artifact was not found"));
    LocalDateTime now = LocalDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
    if (!artifact.getExpiresAt().isAfter(now) || artifact.getCleanedAt() != null) {
      throw ApiException.gone("ARTIFACT_UNAVAILABLE", "Artifact is no longer available");
    }

    artifact.incrementDownloadCount();
    String recordId = UUID.randomUUID().toString();
    downloadRecordRepository.save(
        new DownloadRecordEntity(
            recordId,
            artifact.getId(),
            userId.toString(),
            bounded(ipAddress, 45),
            boundedNullable(userAgent, 1000),
            false,
            now));
    return new DownloadTicket(
        UUID.fromString(recordId),
        artifact.getObjectKey(),
        artifact.getFilename(),
        artifact.getSizeBytes());
  }

  @Transactional
  public void stream(DownloadTicket ticket, OutputStream output) throws IOException {
    DownloadRecordEntity record =
        downloadRecordRepository
            .findById(ticket.recordId().toString())
            .orElseThrow(() -> new IllegalStateException("Download audit record was not found"));
    try (var input = storage.open(ticket.objectKey())) {
      input.transferTo(output);
    }
    record.markCompleted();
  }

  private ArtifactEntity findOwned(UUID userId, UUID artifactId) {
    return artifactRepository
        .findByIdAndOwner(artifactId.toString(), userId.toString())
        .orElseThrow(() -> ApiException.notFound("Artifact was not found"));
  }

  private ArtifactView view(ArtifactEntity artifact) {
    return new ArtifactView(
        UUID.fromString(artifact.getId()),
        UUID.fromString(artifact.getBuildTaskId()),
        artifact.getArtifactType(),
        artifact.getFilename(),
        artifact.getSizeBytes(),
        artifact.getSha256(),
        artifact.getBuildStatus(),
        artifact.getExpiresAt(),
        artifact.getDownloadCount(),
        artifact.getCreatedAt(),
        "STATIC",
        false,
        VALIDATION_MESSAGE);
  }

  private String bounded(String value, int maximumLength) {
    return boundedNullable(value == null ? "" : value, maximumLength);
  }

  private String boundedNullable(String value, int maximumLength) {
    return value == null ? null : value.substring(0, Math.min(value.length(), maximumLength));
  }

  public record ArtifactView(
      UUID id,
      UUID buildTaskId,
      String artifactType,
      String filename,
      long sizeBytes,
      String sha256,
      String buildStatus,
      LocalDateTime expiresAt,
      long downloadCount,
      LocalDateTime createdAt,
      String validationLevel,
      boolean installVerified,
      String validationMessage) {}

  public record DownloadTicket(UUID recordId, String objectKey, String filename, long sizeBytes) {}
}
