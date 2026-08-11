package com.wheelforge.api.common.storage;

import java.io.IOException;
import java.io.InputStream;
import java.nio.channels.Channels;
import java.nio.channels.SeekableByteChannel;
import java.nio.file.DirectoryNotEmptyException;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.NoSuchFileException;
import java.nio.file.OpenOption;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.Objects;
import java.util.Set;

final class PortableFileStorageBackend implements LocalStorageBackend {
  private static final Set<OpenOption> READ_OPTIONS =
      Set.of(StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS);
  private static final Set<OpenOption> WRITE_OPTIONS =
      Set.of(StandardOpenOption.WRITE, LinkOption.NOFOLLOW_LINKS);

  private final Path realRoot;
  private final Object rootIdentity;

  PortableFileStorageBackend(Path realRoot) {
    this.realRoot = realRoot;
    try {
      BasicFileAttributes attributes = identifiedDirectory(realRoot);
      rootIdentity = attributes.fileKey();
    } catch (IOException exception) {
      throw new LocalFileStorage.StorageException(
          "Portable storage requires stable filesystem identities", exception);
    }
  }

  @Override
  public LocalFileStorage.StoredObject putAtomically(
      String key, InputStream input, long expectedSize) {
    if (expectedSize < 0) {
      throw new IllegalArgumentException("Expected size must not be negative");
    }
    StorageObjectKey objectKey = StorageObjectKey.parse(key);
    Path temporary = null;
    boolean published = false;
    try {
      Path parent = createAndValidateParent(objectKey.parent());
      Object parentIdentity = identifiedDirectory(parent).fileKey();
      Path object = resolveWithinRoot(parent, objectKey.fileName());
      requireRegularObjectIfPresent(object);
      temporary = Files.createTempFile(parent, ".wheelforge-", ".tmp");
      requireRegularObject(temporary);
      StorageObjectWriter.WriteResult result;
      try (SeekableByteChannel channel = Files.newByteChannel(temporary, WRITE_OPTIONS)) {
        result = StorageObjectWriter.write(channel, input, expectedSize);
      }
      requireSameDirectory(parent, parentIdentity);
      requireRegularObjectIfPresent(object);
      Files.move(
          temporary, object, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
      published = true;
      requireRegularObject(object);
      return new LocalFileStorage.StoredObject(key, result.sizeBytes(), result.sha256());
    } catch (LocalFileStorage.StorageException | IllegalArgumentException exception) {
      throw exception;
    } catch (IOException exception) {
      throw new LocalFileStorage.StorageException("Could not publish local object", exception);
    } finally {
      if (!published && temporary != null) {
        deleteTemporaryIfSafe(temporary);
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
      Object parentIdentity = identifiedDirectory(parent).fileKey();
      Path object = resolveWithinRoot(parent, objectKey.fileName());
      if (requireRegularObjectIfPresent(object)) {
        requireSameDirectory(parent, parentIdentity);
        Files.delete(object);
      }
      pruneGeneratedArtifactDirectories(objectKey);
    } catch (NoSuchFileException ignored) {
      // Deleting a missing object is idempotent.
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

  private Path createAndValidateParent(Path relativeParent) throws IOException {
    validateRoot();
    Path current = realRoot;
    if (relativeParent != null) {
      for (Path segment : relativeParent) {
        current = resolveWithinRoot(current, segment);
        try {
          Files.createDirectory(current);
        } catch (FileAlreadyExistsException ignored) {
          // A concurrent upload may have created the same generated-key directory.
        }
        requireSafeDirectory(current);
      }
    }
    return current;
  }

  private void validateRoot() {
    try {
      BasicFileAttributes current = identifiedDirectory(realRoot);
      if (!Objects.equals(current.fileKey(), rootIdentity)) {
        throw new IllegalArgumentException("Storage root identity changed");
      }
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
    identifiedDirectory(directory);
    if (!directory.toRealPath().startsWith(realRoot)) {
      throw new IllegalArgumentException("Object key escapes the storage root");
    }
  }

  private static BasicFileAttributes identifiedDirectory(Path directory) throws IOException {
    BasicFileAttributes attributes =
        Files.readAttributes(directory, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
    if (attributes.isSymbolicLink() || !attributes.isDirectory()) {
      throw new IllegalArgumentException("Object key contains an unsafe directory");
    }
    if (attributes.fileKey() == null) {
      throw new IllegalArgumentException("Portable storage requires stable filesystem identities");
    }
    return attributes;
  }

  private void requireRegularObject(Path object) throws IOException {
    if (!requireRegularObjectIfPresent(object)) {
      throw new NoSuchFileException(object.toString());
    }
  }

  private boolean requireRegularObjectIfPresent(Path object) throws IOException {
    BasicFileAttributes attributes = readAttributesIfPresent(object);
    if (attributes == null) {
      return false;
    }
    if (attributes.isSymbolicLink() || !attributes.isRegularFile()) {
      throw new IllegalArgumentException("Object key refers to an unsafe object");
    }
    if (!object.toRealPath().startsWith(realRoot)) {
      throw new IllegalArgumentException("Object key escapes the storage root");
    }
    return true;
  }

  private void requireSameDirectory(Path directory, Object expectedIdentity) throws IOException {
    validateRoot();
    Object currentIdentity = identifiedDirectory(directory).fileKey();
    if (!Objects.equals(currentIdentity, expectedIdentity)
        || !directory.toRealPath().startsWith(realRoot)) {
      throw new IllegalArgumentException("Storage directory identity changed during operation");
    }
  }

  private void pruneGeneratedArtifactDirectories(StorageObjectKey objectKey) throws IOException {
    GeneratedArtifactPath generated = GeneratedArtifactPath.parse(objectKey.relative());
    if (generated == null) {
      return;
    }
    deleteDirectoryIfEmpty(
        realRoot.resolve("artifacts").resolve(generated.task()).resolve(generated.execution()));
    deleteDirectoryIfEmpty(realRoot.resolve("artifacts").resolve(generated.task()));
  }

  private void deleteDirectoryIfEmpty(Path directory) throws IOException {
    try {
      requireSafeDirectory(directory);
      Files.delete(directory);
    } catch (NoSuchFileException | DirectoryNotEmptyException ignored) {
      // Concurrent or sibling content keeps the generated directory live.
    }
  }

  private void deleteTemporaryIfSafe(Path temporary) {
    try {
      if (temporary.normalize().startsWith(realRoot) && requireRegularObjectIfPresent(temporary)) {
        Files.deleteIfExists(temporary);
      }
    } catch (IOException | IllegalArgumentException ignored) {
      // The publication failure remains the actionable error.
    }
  }

  private static BasicFileAttributes readAttributesIfPresent(Path path) throws IOException {
    try {
      return Files.readAttributes(path, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
    } catch (NoSuchFileException ignored) {
      return null;
    }
  }
}
