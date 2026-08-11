package com.wheelforge.api.common.storage;

import jakarta.annotation.PreDestroy;
import java.io.IOException;
import java.io.InputStream;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.SecureDirectoryStream;
import java.util.concurrent.locks.ReentrantReadWriteLock;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

@Component
public class LocalFileStorage implements AutoCloseable {
  private final LocalStorageBackend backend;
  private final ReentrantReadWriteLock lifecycleLock = new ReentrantReadWriteLock();
  private boolean closed;

  @Autowired
  public LocalFileStorage(
      @Value("${wheelforge.storage.data-root}") String root,
      @Value("${wheelforge.storage.allow-portable-fallback:false}") boolean allowPortableFallback) {
    this(Path.of(root), Files::newDirectoryStream, allowPortableFallback);
  }

  public LocalFileStorage(Path root) {
    this(root, Files::newDirectoryStream, false);
  }

  LocalFileStorage(Path root, DirectoryStreamOpener directoryStreamOpener) {
    this(root, directoryStreamOpener, false);
  }

  LocalFileStorage(
      Path root, DirectoryStreamOpener directoryStreamOpener, boolean allowPortableFallback) {
    if (!root.isAbsolute()) {
      throw new IllegalArgumentException("Storage root must be an absolute path");
    }
    DirectoryStream<Path> openedDirectory = null;
    try {
      Path normalizedRoot = root.normalize();
      Files.createDirectories(normalizedRoot);
      Path realRoot = normalizedRoot.toRealPath();
      openedDirectory = directoryStreamOpener.open(realRoot);
      if (openedDirectory instanceof SecureDirectoryStream<?> secureDirectory) {
        @SuppressWarnings("unchecked")
        SecureDirectoryStream<Path> typedDirectory = (SecureDirectoryStream<Path>) secureDirectory;
        backend = new SecureDirectoryStorageBackend(realRoot, typedDirectory);
        openedDirectory = null;
      } else {
        openedDirectory.close();
        openedDirectory = null;
        if (!allowPortableFallback) {
          throw new StorageException(
              "Local storage requires SecureDirectoryStream for complete storage semantics", null);
        }
        backend = new PortableFileStorageBackend(realRoot);
      }
    } catch (IOException exception) {
      throw new StorageException("Could not initialize local storage", exception);
    } finally {
      if (openedDirectory != null) {
        try {
          openedDirectory.close();
        } catch (IOException ignored) {
          // Initialization already failed; retain the actionable cause.
        }
      }
    }
  }

  public StoredObject putAtomically(String key, InputStream input, long expectedSize) {
    lifecycleLock.readLock().lock();
    try {
      requireOpen();
      return backend.putAtomically(key, input, expectedSize);
    } finally {
      lifecycleLock.readLock().unlock();
    }
  }

  public InputStream open(String key) {
    lifecycleLock.readLock().lock();
    try {
      requireOpen();
      return backend.open(key);
    } finally {
      lifecycleLock.readLock().unlock();
    }
  }

  public void deleteIfExists(String key) {
    lifecycleLock.readLock().lock();
    try {
      requireOpen();
      backend.deleteIfExists(key);
    } finally {
      lifecycleLock.readLock().unlock();
    }
  }

  @Override
  @PreDestroy
  public void close() {
    lifecycleLock.writeLock().lock();
    try {
      if (closed) {
        return;
      }
      try {
        backend.close();
      } finally {
        closed = true;
      }
    } finally {
      lifecycleLock.writeLock().unlock();
    }
  }

  private void requireOpen() {
    if (closed) {
      throw new StorageException("Local storage root handle is closed", null);
    }
  }

  @FunctionalInterface
  interface DirectoryStreamOpener {
    DirectoryStream<Path> open(Path root) throws IOException;
  }

  public record StoredObject(String key, long sizeBytes, String sha256) {}

  public static class StorageException extends RuntimeException {
    public StorageException(String message, Throwable cause) {
      super(message, cause);
    }
  }

  public static final class SizeMismatchException extends StorageException {
    public SizeMismatchException(long expected, long actual) {
      super("Object size mismatch: expected " + expected + " bytes but read " + actual, null);
    }
  }
}
