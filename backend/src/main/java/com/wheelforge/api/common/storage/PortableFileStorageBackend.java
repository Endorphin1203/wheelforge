package com.wheelforge.api.common.storage;

import java.io.IOException;
import java.io.InputStream;
import java.nio.channels.Channels;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.OpenOption;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.Objects;
import java.util.Set;

final class PortableFileStorageBackend implements LocalStorageBackend {
  private static final Set<OpenOption> READ_OPTIONS =
      Set.of(StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS);

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
    throw unsupportedMutation();
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
    throw unsupportedMutation();
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
    BasicFileAttributes attributes =
        Files.readAttributes(object, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
    if (attributes.isSymbolicLink() || !attributes.isRegularFile()) {
      throw new IllegalArgumentException("Object key refers to an unsafe object");
    }
    if (!object.toRealPath().startsWith(realRoot)) {
      throw new IllegalArgumentException("Object key escapes the storage root");
    }
  }

  private static LocalFileStorage.StorageException unsupportedMutation() {
    return new LocalFileStorage.StorageException(
        "Portable storage provider cannot provide complete storage semantics", null);
  }
}
