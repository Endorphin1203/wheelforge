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
import java.nio.file.SecureDirectoryStream;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributeView;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.UUID;

final class SecureDirectoryStorageBackend implements LocalStorageBackend {
  private static final int TEMP_NAME_ATTEMPTS = 5;
  private static final Set<OpenOption> CREATE_OPTIONS =
      Set.of(StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE, LinkOption.NOFOLLOW_LINKS);
  private static final Set<OpenOption> READ_OPTIONS =
      Set.of(StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS);

  private final Path root;
  private final SecureDirectoryStream<Path> rootDirectory;

  SecureDirectoryStorageBackend(Path root, SecureDirectoryStream<Path> rootDirectory) {
    this.root = root;
    this.rootDirectory = rootDirectory;
  }

  @Override
  public LocalFileStorage.StoredObject putAtomically(
      String key, InputStream input, long expectedSize) {
    if (expectedSize < 0) {
      throw new IllegalArgumentException("Expected size must not be negative");
    }
    StorageObjectKey objectKey = StorageObjectKey.parse(key);
    createDirectoriesWithoutFollowingLinks(objectKey.parent());
    try (OpenedDirectory parent = openDirectory(objectKey.parent())) {
      TemporaryFile temporary = createTemporaryFile(parent.directory());
      boolean published = false;
      try {
        StorageObjectWriter.WriteResult result =
            StorageObjectWriter.write(temporary.channel(), input, expectedSize);
        requireRegularObjectIfPresent(parent.directory(), objectKey.fileName());
        parent.directory().move(temporary.name(), parent.directory(), objectKey.fileName());
        published = true;
        return new LocalFileStorage.StoredObject(key, result.sizeBytes(), result.sha256());
      } catch (LocalFileStorage.StorageException exception) {
        throw exception;
      } catch (IOException exception) {
        throw new LocalFileStorage.StorageException("Could not publish local object", exception);
      } finally {
        if (!published) {
          closeQuietly(temporary.channel());
          deleteTemporaryIfPresent(parent.directory(), temporary.name());
        }
      }
    } catch (LocalFileStorage.StorageException | IllegalArgumentException exception) {
      throw exception;
    } catch (IOException exception) {
      throw new LocalFileStorage.StorageException("Could not publish local object", exception);
    }
  }

  @Override
  public InputStream open(String key) {
    StorageObjectKey objectKey = StorageObjectKey.parse(key);
    SeekableByteChannel channel;
    try (OpenedDirectory parent = openDirectory(objectKey.parent())) {
      requireRegularObject(parent.directory(), objectKey.fileName());
      channel = parent.directory().newByteChannel(objectKey.fileName(), READ_OPTIONS);
    } catch (IllegalArgumentException exception) {
      throw exception;
    } catch (IOException exception) {
      throw new LocalFileStorage.StorageException("Local object does not exist", exception);
    }
    return Channels.newInputStream(channel);
  }

  @Override
  public void deleteIfExists(String key) {
    StorageObjectKey objectKey = StorageObjectKey.parse(key);
    try (OpenedDirectory parent = openDirectory(objectKey.parent())) {
      if (!requireRegularObjectIfPresent(parent.directory(), objectKey.fileName())) {
        return;
      }
      parent.directory().deleteFile(objectKey.fileName());
    } catch (NoSuchFileException ignored) {
      return;
    } catch (IllegalArgumentException exception) {
      throw exception;
    } catch (IOException exception) {
      throw new LocalFileStorage.StorageException("Could not delete local object", exception);
    }
  }

  @Override
  public void close() {
    try {
      rootDirectory.close();
    } catch (IOException exception) {
      throw new LocalFileStorage.StorageException(
          "Could not close local storage root handle", exception);
    }
  }

  private TemporaryFile createTemporaryFile(SecureDirectoryStream<Path> parent) throws IOException {
    for (int attempt = 0; attempt < TEMP_NAME_ATTEMPTS; attempt++) {
      Path candidate = Path.of(".wheelforge-" + UUID.randomUUID() + ".tmp");
      try {
        return new TemporaryFile(candidate, parent.newByteChannel(candidate, CREATE_OPTIONS));
      } catch (FileAlreadyExistsException ignored) {
        // Try another generated name.
      }
    }
    throw new FileAlreadyExistsException("Could not allocate a unique storage temporary name");
  }

  private OpenedDirectory openDirectory(Path relativeDirectory) throws IOException {
    List<SecureDirectoryStream<Path>> opened = new ArrayList<>();
    SecureDirectoryStream<Path> current = rootDirectory;
    try {
      if (relativeDirectory != null) {
        for (Path segment : relativeDirectory) {
          BasicFileAttributeView view =
              current.getFileAttributeView(
                  segment, BasicFileAttributeView.class, LinkOption.NOFOLLOW_LINKS);
          BasicFileAttributes attributes = view.readAttributes();
          if (!attributes.isDirectory() || attributes.isSymbolicLink()) {
            throw new IllegalArgumentException("Object key contains an unsafe directory");
          }
          try {
            current = current.newDirectoryStream(segment, LinkOption.NOFOLLOW_LINKS);
          } catch (NoSuchFileException exception) {
            throw exception;
          } catch (IOException exception) {
            throw new IllegalArgumentException(
                "Object key contains an unsafe directory", exception);
          }
          opened.add(current);
        }
      }
      return new OpenedDirectory(current, opened);
    } catch (IOException | RuntimeException exception) {
      closeOpenedDirectories(opened);
      throw exception;
    }
  }

  private void requireRegularObject(SecureDirectoryStream<Path> parent, Path fileName)
      throws IOException {
    if (!requireRegularObjectIfPresent(parent, fileName)) {
      throw new NoSuchFileException(fileName.toString());
    }
  }

  private boolean requireRegularObjectIfPresent(SecureDirectoryStream<Path> parent, Path fileName)
      throws IOException {
    BasicFileAttributeView view =
        parent.getFileAttributeView(
            fileName, BasicFileAttributeView.class, LinkOption.NOFOLLOW_LINKS);
    try {
      BasicFileAttributes attributes = view.readAttributes();
      if (attributes.isSymbolicLink() || !attributes.isRegularFile()) {
        throw new IllegalArgumentException("Object key refers to an unsafe object");
      }
      return true;
    } catch (NoSuchFileException ignored) {
      return false;
    }
  }

  private void createDirectoriesWithoutFollowingLinks(Path relativeDirectory) {
    if (relativeDirectory == null) {
      return;
    }
    Path current = root;
    try {
      for (Path segment : relativeDirectory) {
        current = current.resolve(segment);
        if (!Files.exists(current, LinkOption.NOFOLLOW_LINKS)) {
          try {
            Files.createDirectory(current);
          } catch (FileAlreadyExistsException ignored) {
            // A concurrent upload may have created the same generated-key directory.
          }
        }
        if (Files.isSymbolicLink(current)
            || !Files.isDirectory(current, LinkOption.NOFOLLOW_LINKS)) {
          throw new IllegalArgumentException("Object key contains an unsafe directory");
        }
      }
    } catch (IOException exception) {
      throw new LocalFileStorage.StorageException(
          "Could not create local storage directories", exception);
    }
  }

  private static void closeQuietly(SeekableByteChannel channel) {
    try {
      channel.close();
    } catch (IOException ignored) {
      // The original publication failure remains the actionable error.
    }
  }

  private static void deleteTemporaryIfPresent(
      SecureDirectoryStream<Path> parent, Path temporaryName) {
    try {
      parent.deleteFile(temporaryName);
    } catch (NoSuchFileException ignored) {
      // Publication may have completed before a later bookkeeping failure.
    } catch (IOException ignored) {
      // The original publication failure remains the actionable error.
    }
  }

  private static void closeOpenedDirectories(List<SecureDirectoryStream<Path>> opened) {
    for (int index = opened.size() - 1; index >= 0; index--) {
      try {
        opened.get(index).close();
      } catch (IOException ignored) {
        // The traversal failure remains the actionable error.
      }
    }
  }

  private record TemporaryFile(Path name, SeekableByteChannel channel) {}

  private record OpenedDirectory(
      SecureDirectoryStream<Path> directory, List<SecureDirectoryStream<Path>> opened)
      implements AutoCloseable {
    @Override
    public void close() {
      closeOpenedDirectories(opened);
    }
  }
}
