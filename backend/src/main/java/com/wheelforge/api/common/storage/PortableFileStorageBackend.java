package com.wheelforge.api.common.storage;

import java.io.IOException;
import java.io.InputStream;
import java.nio.channels.Channels;
import java.nio.channels.SeekableByteChannel;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.NoSuchFileException;
import java.nio.file.OpenOption;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.Set;
import java.util.UUID;

final class PortableFileStorageBackend implements LocalStorageBackend {
  private static final int TEMP_NAME_ATTEMPTS = 5;
  private static final Set<OpenOption> CREATE_OPTIONS =
      Set.of(StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE, LinkOption.NOFOLLOW_LINKS);
  private static final Set<OpenOption> READ_OPTIONS =
      Set.of(StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS);

  private final Path realRoot;

  PortableFileStorageBackend(Path realRoot) {
    this.realRoot = realRoot;
  }

  @Override
  public LocalFileStorage.StoredObject putAtomically(
      String key, InputStream input, long expectedSize) {
    if (expectedSize < 0) {
      throw new IllegalArgumentException("Expected size must not be negative");
    }
    StorageObjectKey objectKey = StorageObjectKey.parse(key);
    Path parent = createAndValidateParent(objectKey.parent());
    TemporaryFile temporary = createTemporaryFile(parent);
    boolean published = false;
    try {
      StorageObjectWriter.WriteResult result =
          StorageObjectWriter.write(temporary.channel(), input, expectedSize);
      Path validatedParent = validateExistingParent(objectKey.parent());
      Path destination = resolveWithinRoot(validatedParent, objectKey.fileName());
      rejectSymbolicLink(destination);
      Files.move(temporary.path(), destination, StandardCopyOption.ATOMIC_MOVE);
      published = true;
      return new LocalFileStorage.StoredObject(key, result.sizeBytes(), result.sha256());
    } catch (LocalFileStorage.StorageException exception) {
      throw exception;
    } catch (IOException exception) {
      throw new LocalFileStorage.StorageException("Could not publish local object", exception);
    } finally {
      if (!published) {
        closeQuietly(temporary.channel());
        deleteTemporaryIfPresent(temporary.path());
      }
    }
  }

  @Override
  public InputStream open(String key) {
    StorageObjectKey objectKey = StorageObjectKey.parse(key);
    try {
      Path parent = validateExistingParent(objectKey.parent());
      Path object = resolveWithinRoot(parent, objectKey.fileName());
      requireRegularObject(object);
      return Channels.newInputStream(Files.newByteChannel(object, READ_OPTIONS));
    } catch (IllegalArgumentException exception) {
      throw exception;
    } catch (IOException exception) {
      throw new LocalFileStorage.StorageException("Local object does not exist", exception);
    }
  }

  @Override
  public void deleteIfExists(String key) {
    StorageObjectKey objectKey = StorageObjectKey.parse(key);
    try {
      Path parent = validateExistingParent(objectKey.parent());
      Path object = resolveWithinRoot(parent, objectKey.fileName());
      if (!Files.exists(object, LinkOption.NOFOLLOW_LINKS)) {
        return;
      }
      requireRegularObject(object);
      Files.delete(object);
    } catch (NoSuchFileException ignored) {
      // Deletion is idempotent.
    } catch (IllegalArgumentException exception) {
      throw exception;
    } catch (IOException exception) {
      throw new LocalFileStorage.StorageException("Could not delete local object", exception);
    }
  }

  @Override
  public void close() {
    // This fallback does not retain provider handles.
  }

  private Path createAndValidateParent(Path relativeParent) {
    validateRoot();
    Path current = realRoot;
    if (relativeParent == null) {
      return current;
    }
    try {
      for (Path segment : relativeParent) {
        current = resolveWithinRoot(current, segment);
        if (!Files.exists(current, LinkOption.NOFOLLOW_LINKS)) {
          try {
            Files.createDirectory(current);
          } catch (FileAlreadyExistsException ignored) {
            // A concurrent upload may have created the same generated-key directory.
          }
        }
        requireSafeDirectory(current);
      }
      return current;
    } catch (IllegalArgumentException exception) {
      throw exception;
    } catch (IOException exception) {
      throw new LocalFileStorage.StorageException(
          "Could not create local storage directories", exception);
    }
  }

  private Path validateExistingParent(Path relativeParent) throws IOException {
    validateRoot();
    Path current = realRoot;
    if (relativeParent != null) {
      for (Path segment : relativeParent) {
        current = resolveWithinRoot(current, segment);
        requireSafeDirectory(current);
      }
    }
    return current;
  }

  private void validateRoot() {
    try {
      requireSafeDirectory(realRoot);
      if (!realRoot.toRealPath().equals(realRoot)) {
        throw new IllegalArgumentException("Storage root no longer resolves to its startup path");
      }
    } catch (IllegalArgumentException exception) {
      throw exception;
    } catch (IOException exception) {
      throw new LocalFileStorage.StorageException(
          "Could not validate local storage root", exception);
    }
  }

  private Path resolveWithinRoot(Path parent, Path child) {
    Path resolved = parent.resolve(child).normalize();
    if (!resolved.startsWith(realRoot)) {
      throw new IllegalArgumentException("Object key escapes the storage root");
    }
    return resolved;
  }

  private void requireSafeDirectory(Path directory) throws IOException {
    BasicFileAttributes attributes =
        Files.readAttributes(directory, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
    if (attributes.isSymbolicLink() || !attributes.isDirectory()) {
      throw new IllegalArgumentException("Object key contains an unsafe directory");
    }
    if (!directory.toRealPath().startsWith(realRoot)) {
      throw new IllegalArgumentException("Object key escapes the storage root");
    }
  }

  private void requireRegularObject(Path object) throws IOException {
    BasicFileAttributes attributes =
        Files.readAttributes(object, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
    if (attributes.isSymbolicLink() || !attributes.isRegularFile()) {
      throw new IllegalArgumentException("Object key refers to an unsafe object");
    }
    if (!object.toRealPath().startsWith(realRoot)) {
      throw new IllegalArgumentException("Object key escapes the storage root");
    }
  }

  private void rejectSymbolicLink(Path object) {
    if (Files.isSymbolicLink(object)) {
      throw new IllegalArgumentException("Object key refers to an unsafe object");
    }
  }

  private TemporaryFile createTemporaryFile(Path parent) {
    for (int attempt = 0; attempt < TEMP_NAME_ATTEMPTS; attempt++) {
      Path candidate = parent.resolve(".wheelforge-" + UUID.randomUUID() + ".tmp");
      try {
        return new TemporaryFile(candidate, Files.newByteChannel(candidate, CREATE_OPTIONS));
      } catch (FileAlreadyExistsException ignored) {
        // Try another generated name.
      } catch (IOException exception) {
        throw new LocalFileStorage.StorageException(
            "Could not create local storage temporary file", exception);
      }
    }
    throw new LocalFileStorage.StorageException(
        "Could not allocate a unique storage temporary name", null);
  }

  private static void closeQuietly(SeekableByteChannel channel) {
    try {
      channel.close();
    } catch (IOException ignored) {
      // The original publication failure remains the actionable error.
    }
  }

  private static void deleteTemporaryIfPresent(Path temporary) {
    try {
      Files.deleteIfExists(temporary);
    } catch (IOException ignored) {
      // The original publication failure remains the actionable error.
    }
  }

  private record TemporaryFile(Path path, SeekableByteChannel channel) {}
}
