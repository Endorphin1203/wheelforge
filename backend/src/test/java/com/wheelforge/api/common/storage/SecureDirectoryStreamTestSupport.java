package com.wheelforge.api.common.storage;

import java.io.IOException;
import java.nio.channels.SeekableByteChannel;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.NoSuchFileException;
import java.nio.file.OpenOption;
import java.nio.file.Path;
import java.nio.file.SecureDirectoryStream;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.nio.file.attribute.FileAttribute;
import java.nio.file.attribute.FileAttributeView;
import java.util.Iterator;
import java.util.List;
import java.util.Objects;
import java.util.Set;

public final class SecureDirectoryStreamTestSupport {
  private SecureDirectoryStreamTestSupport() {}

  public static LocalFileStorage secureStorage(Path root) {
    Path searchRoot = root.toAbsolutePath().normalize().getParent();
    return new LocalFileStorage(
        root.toAbsolutePath(), path -> new FileKeySecureDirectoryStream(path, searchRoot));
  }

  private static final class FileKeySecureDirectoryStream implements SecureDirectoryStream<Path> {
    private final Path originalPath;
    private final Path searchRoot;
    private final Object fileKey;
    private boolean closed;

    private FileKeySecureDirectoryStream(Path directory, Path searchRoot) throws IOException {
      this.originalPath = directory.toAbsolutePath().normalize();
      this.searchRoot = searchRoot;
      this.fileKey = attributes(this.originalPath).fileKey();
      if (fileKey == null) {
        throw new IOException("Test filesystem does not expose directory file keys");
      }
    }

    @Override
    public SecureDirectoryStream<Path> newDirectoryStream(Path path, LinkOption... options)
        throws IOException {
      Path child = currentPath().resolve(path);
      BasicFileAttributes attributes = attributes(child);
      if (attributes.isSymbolicLink() || !attributes.isDirectory()) {
        throw new IOException("Directory traversal refused a non-directory or symbolic link");
      }
      return new FileKeySecureDirectoryStream(child, searchRoot);
    }

    @Override
    public SeekableByteChannel newByteChannel(
        Path path, Set<? extends OpenOption> options, FileAttribute<?>... attrs)
        throws IOException {
      return Files.newByteChannel(currentPath().resolve(path), options, attrs);
    }

    @Override
    public void deleteFile(Path path) throws IOException {
      Files.delete(currentPath().resolve(path));
    }

    @Override
    public void deleteDirectory(Path path) throws IOException {
      Files.delete(currentPath().resolve(path));
    }

    @Override
    public void move(Path source, SecureDirectoryStream<Path> targetDirectory, Path target)
        throws IOException {
      if (!(targetDirectory instanceof FileKeySecureDirectoryStream targetHandle)) {
        throw new IOException("Mismatched test directory provider");
      }
      Files.move(
          currentPath().resolve(source),
          targetHandle.currentPath().resolve(target),
          StandardCopyOption.ATOMIC_MOVE);
    }

    @Override
    public <V extends FileAttributeView> V getFileAttributeView(Class<V> type) {
      try {
        return Files.getFileAttributeView(currentPath(), type);
      } catch (IOException exception) {
        throw new IllegalStateException(exception);
      }
    }

    @Override
    public <V extends FileAttributeView> V getFileAttributeView(
        Path path, Class<V> type, LinkOption... options) {
      try {
        return Files.getFileAttributeView(currentPath().resolve(path), type, options);
      } catch (IOException exception) {
        throw new IllegalStateException(exception);
      }
    }

    @Override
    public Iterator<Path> iterator() {
      try (var paths = Files.list(currentPath())) {
        List<Path> snapshot = paths.toList();
        return snapshot.iterator();
      } catch (IOException exception) {
        throw new IllegalStateException(exception);
      }
    }

    @Override
    public void close() {
      closed = true;
    }

    private Path currentPath() throws IOException {
      if (closed) {
        throw new java.nio.file.ClosedDirectoryStreamException();
      }
      if (Files.exists(originalPath, LinkOption.NOFOLLOW_LINKS)
          && Objects.equals(attributes(originalPath).fileKey(), fileKey)) {
        return originalPath;
      }
      try (var candidates =
          Files.find(
              searchRoot,
              32,
              (path, candidateAttributes) ->
                  candidateAttributes.isDirectory()
                      && Objects.equals(candidateAttributes.fileKey(), fileKey))) {
        return candidates
            .findFirst()
            .orElseThrow(() -> new NoSuchFileException(originalPath.toString()));
      }
    }

    private static BasicFileAttributes attributes(Path path) throws IOException {
      return Files.readAttributes(path, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
    }
  }
}
