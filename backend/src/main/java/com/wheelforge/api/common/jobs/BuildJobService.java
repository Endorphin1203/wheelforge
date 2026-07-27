package com.wheelforge.api.common.jobs;

import com.wheelforge.api.contracts.JobPayload;
import java.time.Clock;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.UUID;
import java.util.function.Supplier;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import tools.jackson.databind.JsonNode;

@Service
public class BuildJobService {
  private final BuildJobRepository repository;
  private final Clock clock;
  private final Supplier<UUID> idSupplier;

  @Autowired
  public BuildJobService(BuildJobRepository repository) {
    this(repository, Clock.systemUTC(), UUID::randomUUID);
  }

  public BuildJobService(BuildJobRepository repository, Clock clock, Supplier<UUID> idSupplier) {
    this.repository = repository;
    this.clock = clock;
    this.idSupplier = idSupplier;
  }

  public BuildJobEntity enqueue(String jobType, UUID subjectId, JsonNode payload) {
    if (!TransactionSynchronizationManager.isActualTransactionActive()) {
      throw new IllegalStateException(
          "Build jobs must be enqueued inside the caller's transaction");
    }
    String createdAt = DateTimeFormatter.ISO_INSTANT.format(clock.instant());
    var wire =
        new JobPayload(
            JobPayload.VERSION, jobType, subjectId.toString(), createdAt, payload.deepCopy());
    String payloadJson;
    try {
      payloadJson = JobPayload.databaseWireMapper().writeValueAsString(wire);
    } catch (Exception exception) {
      throw new IllegalArgumentException("Could not serialize strict job payload", exception);
    }
    LocalDateTime now = LocalDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
    return repository.save(
        new BuildJobEntity(
            idSupplier.get().toString(),
            jobType,
            JobPayload.VERSION,
            subjectId.toString(),
            payloadJson,
            now));
  }

  public static JsonNode requirementParsePayload(
      String originalObjectKey, String normalizedObjectKey) {
    return JobPayload.databaseWireMapper()
        .createObjectNode()
        .put("originalObjectKey", originalObjectKey)
        .put("normalizedObjectKey", normalizedObjectKey);
  }
}
