package com.wheelforge.api.common.storage;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

@Component
public class LocalFileStorage {
  private static final int BUFFER_SIZE = 8192;

  private final Path root;
  private final Path realRoot;

  @Autowired
  public LocalFileStorage(@Value("${wheelforge.storage.data-root}") String root) {
    this(Path.of(root));
  }

  public LocalFileStorage(Path root) {
    if (!root.isAbsolute()) {
      throw new IllegalArgumentException("Storage root must be an absolute path");
    }
    try {
      Path normalizedRoot = root.normalize();
      Files.createDirectories(normalizedRoot);
      this.realRoot = normalizedRoot.toRealPath();
      this.root = this.realRoot;
    } catch (IOException exception) {
      throw new StorageException("Could not initialize local storage", exception);
    }
  }

  public StoredObject putAtomically(String key, InputStream input, long expectedSize) {
    if (expectedSize < 0) {
      throw new IllegalArgumentException("Expected size must not be negative");
    }
    Path destination = resolveForWrite(key);
    Path temporary = null;
    try {
      Path parent = destination.getParent();
      createDirectoriesWithoutFollowingLinks(parent);
      temporary = Files.createTempFile(parent, ".wheelforge-", ".tmp");
      MessageDigest digest = MessageDigest.getInstance("SHA-256");
      long written = 0;
      byte[] buffer = new byte[BUFFER_SIZE];
      try (OutputStream output = Files.newOutputStream(temporary)) {
        int count;
        while ((count = input.read(buffer)) != -1) {
          written += count;
          if (written > expectedSize) {
            throw new SizeMismatchException(expectedSize, written);
          }
          digest.update(buffer, 0, count);
          output.write(buffer, 0, count);
        }
      }
      if (written != expectedSize) {
        throw new SizeMismatchException(expectedSize, written);
      }
      try {
        Files.move(temporary, destination, StandardCopyOption.ATOMIC_MOVE);
      } catch (AtomicMoveNotSupportedException exception) {
        throw new StorageException(
            "Storage filesystem does not support atomic publication", exception);
      }
      temporary = null;
      return new StoredObject(key, written, HexFormat.of().formatHex(digest.digest()));
    } catch (StorageException exception) {
      throw exception;
    } catch (IOException | NoSuchAlgorithmException exception) {
      throw new StorageException("Could not publish local object", exception);
    } finally {
      if (temporary != null) {
        try {
          Files.deleteIfExists(temporary);
        } catch (IOException ignored) {
          // The original storage failure remains the actionable error.
        }
      }
    }
  }

  public InputStream open(String key) {
    Path object = resolveExisting(key);
    try {
      return Files.newInputStream(object);
    } catch (IOException exception) {
      throw new StorageException("Could not open local object", exception);
    }
  }

  public void deleteIfExists(String key) {
    Path object = resolveForWrite(key);
    try {
      Path parent = object.getParent();
      if (!Files.exists(parent, LinkOption.NOFOLLOW_LINKS)) {
        return;
      }
      requireExistingDirectoriesWithoutLinks(parent);
      if (Files.isSymbolicLink(object)) {
        throw new IllegalArgumentException("Symbolic-link object keys are not allowed");
      }
      Files.deleteIfExists(object);
    } catch (IOException exception) {
      throw new StorageException("Could not delete local object", exception);
    }
  }

  private Path resolveForWrite(String key) {
    Path relative = validatedRelativeKey(key);
    Path resolved = root.resolve(relative).normalize();
    requireInsideRoot(resolved);
    return resolved;
  }

  private Path resolveExisting(String key) {
    Path resolved = resolveForWrite(key);
    try {
      Path real = resolved.toRealPath(LinkOption.NOFOLLOW_LINKS);
      requireInsideRoot(real);
      if (Files.isSymbolicLink(real)) {
        throw new IllegalArgumentException("Symbolic-link object keys are not allowed");
      }
      return real;
    } catch (IOException exception) {
      throw new StorageException("Local object does not exist", exception);
    }
  }

  private Path validatedRelativeKey(String key) {
    if (key == null || key.isBlank() || key.indexOf('\\') >= 0) {
      throw new IllegalArgumentException("Object key must be a non-empty portable relative path");
    }
    Path relative = Path.of(key);
    if (relative.isAbsolute() || relative.normalize().startsWith("..")) {
      throw new IllegalArgumentException("Object key escapes the storage root");
    }
    return relative.normalize();
  }

  private void createDirectoriesWithoutFollowingLinks(Path directory) throws IOException {
    Path current = root;
    for (Path segment : root.relativize(directory)) {
      current = current.resolve(segment);
      if (!Files.exists(current, LinkOption.NOFOLLOW_LINKS)) {
        try {
          Files.createDirectory(current);
        } catch (FileAlreadyExistsException ignored) {
          // A concurrent upload may have created this shared generated-key directory.
        }
      }
      if (Files.isSymbolicLink(current) || !Files.isDirectory(current, LinkOption.NOFOLLOW_LINKS)) {
        throw new IllegalArgumentException("Object key contains an unsafe directory");
      }
      requireInsideRoot(current.toRealPath(LinkOption.NOFOLLOW_LINKS));
    }
  }

  private void requireExistingDirectoriesWithoutLinks(Path directory) throws IOException {
    Path current = root;
    for (Path segment : root.relativize(directory)) {
      current = current.resolve(segment);
      if (Files.isSymbolicLink(current) || !Files.isDirectory(current, LinkOption.NOFOLLOW_LINKS)) {
        throw new IllegalArgumentException("Object key contains an unsafe directory");
      }
      requireInsideRoot(current.toRealPath(LinkOption.NOFOLLOW_LINKS));
    }
  }

  private void requireInsideRoot(Path path) {
    if (!path.normalize().startsWith(realRoot)) {
      throw new IllegalArgumentException("Object key escapes the storage root");
    }
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
