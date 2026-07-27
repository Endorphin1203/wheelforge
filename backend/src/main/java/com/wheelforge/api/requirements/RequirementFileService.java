package com.wheelforge.api.requirements;

import com.wheelforge.api.common.ApiException;
import com.wheelforge.api.common.jobs.BuildJobService;
import com.wheelforge.api.common.storage.LocalFileStorage;
import java.io.FilterInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.time.Clock;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.function.Supplier;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.web.multipart.MultipartFile;

@Service
public class RequirementFileService {
  public static final int MAX_BYTES = 512 * 1024;

  private final RequirementFileRepository fileRepository;
  private final RequirementItemRepository itemRepository;
  private final BuildJobService buildJobService;
  private final LocalFileStorage storage;
  private final Clock clock;
  private final Supplier<UUID> idSupplier;

  @Autowired
  public RequirementFileService(
      RequirementFileRepository fileRepository,
      RequirementItemRepository itemRepository,
      BuildJobService buildJobService,
      LocalFileStorage storage) {
    this(
        fileRepository,
        itemRepository,
        buildJobService,
        storage,
        Clock.systemUTC(),
        UUID::randomUUID);
  }

  public RequirementFileService(
      RequirementFileRepository fileRepository,
      RequirementItemRepository itemRepository,
      BuildJobService buildJobService,
      LocalFileStorage storage,
      Clock clock,
      Supplier<UUID> idSupplier) {
    this.fileRepository = fileRepository;
    this.itemRepository = itemRepository;
    this.buildJobService = buildJobService;
    this.storage = storage;
    this.clock = clock;
    this.idSupplier = idSupplier;
  }

  @Transactional
  public RequirementFileView upload(UUID userId, MultipartFile upload) {
    requireActiveTransaction();
    validateMetadata(upload);

    UUID fileId = idSupplier.get();
    String baseKey = "users/" + userId + "/requirements/" + fileId;
    String originalKey = baseKey + "/original.txt";
    String normalizedKey = baseKey + "/normalized.txt";
    LocalFileStorage.StoredObject stored;
    boolean published = false;
    boolean rollbackCleanupRegistered = false;
    try (InputStream input = new NulRejectingInputStream(upload.getInputStream())) {
      stored = storage.putAtomically(originalKey, input, upload.getSize());
      published = true;
      registerRollbackCleanup(originalKey);
      rollbackCleanupRegistered = true;
    } catch (NulByteException exception) {
      cleanupBeforeRegistration(originalKey, published, rollbackCleanupRegistered, exception);
      throw ApiException.badRequest(
          "INVALID_REQUIREMENT_FILE", "Requirements files must not contain NUL bytes");
    } catch (LocalFileStorage.SizeMismatchException exception) {
      cleanupBeforeRegistration(originalKey, published, rollbackCleanupRegistered, exception);
      throw ApiException.payloadTooLarge("Requirements file exceeds 512 KiB");
    } catch (IOException exception) {
      cleanupBeforeRegistration(originalKey, published, rollbackCleanupRegistered, exception);
      throw ApiException.badRequest(
          "INVALID_REQUIREMENT_FILE", "Requirements file could not be read");
    } catch (RuntimeException | Error exception) {
      cleanupBeforeRegistration(originalKey, published, rollbackCleanupRegistered, exception);
      throw exception;
    }

    LocalDateTime createdAt = LocalDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
    var entity =
        new RequirementFileEntity(
            fileId.toString(),
            userId.toString(),
            upload.getOriginalFilename(),
            stored.sizeBytes(),
            stored.sha256(),
            originalKey,
            normalizedKey,
            "PENDING",
            createdAt);
    RequirementFileEntity saved = fileRepository.save(entity);
    buildJobService.enqueue(
        "REQUIREMENT_PARSE",
        fileId,
        BuildJobService.requirementParsePayload(originalKey, normalizedKey));
    return toView(saved);
  }

  @Transactional(readOnly = true)
  public RequirementFileView get(UUID userId, UUID fileId) {
    return toView(requireOwned(userId, fileId));
  }

  @Transactional(readOnly = true)
  public List<RequirementItemView> items(UUID userId, UUID fileId) {
    RequirementFileEntity owned = requireOwned(userId, fileId);
    return itemRepository.findAllByRequirementFileIdOrderByLineNoAsc(owned.getId()).stream()
        .map(RequirementFileService::toItemView)
        .toList();
  }

  private void validateMetadata(MultipartFile upload) {
    String filename = upload.getOriginalFilename();
    if (filename != null && filename.length() > 255) {
      throw ApiException.badRequest(
          "INVALID_REQUIREMENT_FILE", "Original filename must not exceed 255 characters");
    }
    if (filename == null || !filename.toLowerCase(Locale.ROOT).endsWith(".txt")) {
      throw ApiException.badRequest(
          "INVALID_REQUIREMENT_FILE", "Only .txt requirements files are supported");
    }
    if (upload.getSize() == 0) {
      throw ApiException.badRequest(
          "INVALID_REQUIREMENT_FILE", "Requirements file must not be empty");
    }
    if (upload.getSize() > MAX_BYTES) {
      throw ApiException.payloadTooLarge("Requirements file exceeds 512 KiB");
    }
  }

  private RequirementFileEntity requireOwned(UUID userId, UUID fileId) {
    return fileRepository
        .findByIdAndUserId(fileId.toString(), userId.toString())
        .orElseThrow(() -> ApiException.notFound("Requirement file was not found"));
  }

  private void registerRollbackCleanup(String objectKey) {
    if (!TransactionSynchronizationManager.isSynchronizationActive()) {
      throw new IllegalStateException("Upload transaction synchronization is not active");
    }
    TransactionSynchronizationManager.registerSynchronization(
        new TransactionSynchronization() {
          @Override
          public void afterCompletion(int status) {
            if (status != STATUS_COMMITTED) {
              storage.deleteIfExists(objectKey);
            }
          }
        });
  }

  private void cleanupBeforeRegistration(
      String objectKey, boolean published, boolean cleanupRegistered, Throwable originalFailure) {
    if (!published || cleanupRegistered) {
      return;
    }
    try {
      storage.deleteIfExists(objectKey);
    } catch (RuntimeException cleanupFailure) {
      originalFailure.addSuppressed(cleanupFailure);
    }
  }

  private void requireActiveTransaction() {
    if (!TransactionSynchronizationManager.isActualTransactionActive()) {
      throw new IllegalStateException("Requirement uploads require an active transaction");
    }
  }

  private static RequirementFileView toView(RequirementFileEntity entity) {
    return new RequirementFileView(
        UUID.fromString(entity.getId()),
        entity.getOriginalName(),
        entity.getSizeBytes(),
        entity.getSha256(),
        entity.getParseStatus(),
        entity.getParseError(),
        entity.getCreatedAt().toInstant(ZoneOffset.UTC));
  }

  private static RequirementItemView toItemView(RequirementItemEntity entity) {
    return new RequirementItemView(
        UUID.fromString(entity.getId()),
        entity.getLineNo(),
        entity.getNormalizedName(),
        entity.getExtras(),
        entity.getSpecifier(),
        entity.getMarkerText(),
        entity.getOriginalText(),
        entity.isSupported(),
        entity.getErrorCode(),
        entity.getErrorMessage());
  }

  public record RequirementFileView(
      UUID id,
      String originalName,
      long sizeBytes,
      String sha256,
      String parseStatus,
      String parseError,
      Instant createdAt) {}

  public record RequirementItemView(
      UUID id,
      int lineNo,
      String normalizedName,
      List<String> extras,
      String specifier,
      String marker,
      String originalText,
      boolean supported,
      String errorCode,
      String errorMessage) {}

  private static final class NulRejectingInputStream extends FilterInputStream {
    private NulRejectingInputStream(InputStream input) {
      super(input);
    }

    @Override
    public int read() throws IOException {
      int value = super.read();
      rejectNul(value);
      return value;
    }

    @Override
    public int read(byte[] bytes, int offset, int length) throws IOException {
      int count = super.read(bytes, offset, length);
      for (int index = offset; index < offset + Math.max(count, 0); index++) {
        rejectNul(bytes[index] & 0xff);
      }
      return count;
    }

    private static void rejectNul(int value) {
      if (value == 0) {
        throw new NulByteException();
      }
    }
  }

  private static final class NulByteException extends RuntimeException {}
}
