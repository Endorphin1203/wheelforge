package com.wheelforge.api.common.storage;

import static com.wheelforge.api.common.storage.SecureDirectoryStreamTestSupport.secureStorage;
import static java.nio.charset.StandardCharsets.UTF_8;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class LocalFileStorageTest {
  @TempDir Path tempDir;

  @Test
  void publishesAtomicallyAndReturnsTheSinglePassDigest() throws Exception {
    var storage = secureStorage(tempDir.toAbsolutePath());
    byte[] content = "requests==2.32.4\n".getBytes(UTF_8);

    var stored =
        storage.putAtomically(
            "users/user-id/requirements/file-id/original.txt",
            new ByteArrayInputStream(content),
            content.length);

    assertThat(stored.sizeBytes()).isEqualTo(content.length);
    assertThat(stored.sha256())
        .isEqualTo("7bb1058b98b6f0c0e8b11afa38862bcdd7291084a35cd0684a3dfc095aa17dbe");
    assertThat(storage.open("users/user-id/requirements/file-id/original.txt").readAllBytes())
        .isEqualTo(content);

    storage.deleteIfExists("users/user-id/requirements/file-id/original.txt");
    storage.deleteIfExists("users/user-id/requirements/file-id/original.txt");

    assertThat(tempDir.resolve("users/user-id/requirements/file-id/original.txt")).doesNotExist();
  }

  @Test
  void rejectsAbsoluteAndEscapingObjectKeys() {
    var storage = secureStorage(tempDir.toAbsolutePath());

    assertThatThrownBy(
            () ->
                storage.putAtomically(
                    "../outside.txt", new ByteArrayInputStream(new byte[] {1}), 1))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(() -> storage.open(tempDir.resolve("outside.txt").toString()))
        .isInstanceOf(IllegalArgumentException.class);
  }

  @Test
  void removesTemporaryFileAndDoesNotPublishWhenStreamingFails() throws Exception {
    var storage = secureStorage(tempDir.toAbsolutePath());
    InputStream failing =
        new InputStream() {
          private int reads;

          @Override
          public int read() throws IOException {
            if (reads++ == 2) {
              throw new IOException("source failed");
            }
            return 'x';
          }
        };

    assertThatThrownBy(
            () ->
                storage.putAtomically(
                    "users/user-id/requirements/file-id/original.txt", failing, 4))
        .isInstanceOf(LocalFileStorage.StorageException.class);
    assertThat(Files.walk(tempDir).filter(Files::isRegularFile)).isEmpty();
  }

  @Test
  void rejectsContentWhoseActualSizeDiffersFromDeclaredSize() {
    var storage = secureStorage(tempDir.toAbsolutePath());

    assertThatThrownBy(
            () ->
                storage.putAtomically(
                    "users/user-id/requirements/file-id/original.txt",
                    new ByteArrayInputStream(new byte[] {1, 2}),
                    1))
        .isInstanceOf(LocalFileStorage.SizeMismatchException.class);
  }

  @Test
  void rejectsSymlinkedParentsForPublicationAndRollbackDeletion() throws Exception {
    Path root = Files.createDirectory(tempDir.resolve("root"));
    Path outside = Files.createDirectory(tempDir.resolve("outside"));
    Files.createSymbolicLink(root.resolve("users"), outside);
    Files.writeString(outside.resolve("existing.txt"), "must remain");
    var storage = secureStorage(root.toAbsolutePath());

    assertThatThrownBy(
            () ->
                storage.putAtomically(
                    "users/nested/original.txt", new ByteArrayInputStream(new byte[] {1}), 1))
        .isInstanceOf(IllegalArgumentException.class);
    assertThat(outside.resolve("nested")).doesNotExist();

    assertThatThrownBy(() -> storage.deleteIfExists("users/existing.txt"))
        .isInstanceOf(IllegalArgumentException.class);
    assertThat(outside.resolve("existing.txt")).hasContent("must remain");
  }

  @Test
  void publishesWithinTheOpenedParentWhenItsPathIsReplacedDuringStreaming() throws Exception {
    Path root = Files.createDirectory(tempDir.resolve("root"));
    Path parent = root.resolve("users/user-id/requirements/file-id");
    Files.createDirectories(parent);
    Path heldParent = tempDir.resolve("held-parent");
    Path outside = Files.createDirectory(tempDir.resolve("outside"));
    var storage = secureStorage(root.toAbsolutePath());
    var stream = new BlockingInputStream("safe-content".getBytes(UTF_8));
    var executor = Executors.newSingleThreadExecutor();

    try {
      var publication =
          executor.submit(
              () ->
                  storage.putAtomically(
                      "users/user-id/requirements/file-id/original.txt",
                      stream,
                      "safe-content".length()));
      assertThat(stream.readStarted.await(5, TimeUnit.SECONDS)).isTrue();

      Files.move(parent, heldParent);
      Files.createSymbolicLink(parent, outside);
      String temporaryName;
      try (var files = Files.list(heldParent)) {
        temporaryName =
            files
                .map(path -> path.getFileName().toString())
                .filter(name -> name.startsWith(".wheelforge-") && name.endsWith(".tmp"))
                .findFirst()
                .orElseThrow();
      }
      Files.writeString(outside.resolve(temporaryName), "attacker-content");
      stream.continueRead.countDown();

      assertThat(publication.get(5, TimeUnit.SECONDS).sha256()).hasSize(64);
      assertThat(heldParent.resolve("original.txt")).hasContent("safe-content");
      assertThat(outside.resolve("original.txt")).doesNotExist();
      assertThat(outside.resolve(temporaryName)).hasContent("attacker-content");
    } finally {
      stream.continueRead.countDown();
      executor.shutdownNow();
      assertThat(executor.awaitTermination(5, TimeUnit.SECONDS)).isTrue();
    }
  }

  @Test
  void opensFromTheOriginalRootAfterTheConfiguredPathIsReplaced() throws Exception {
    Path root = Files.createDirectory(tempDir.resolve("root"));
    String key = "users/user-id/requirements/file-id/original.txt";
    Path object = root.resolve(key);
    Files.createDirectories(object.getParent());
    Files.writeString(object, "safe-content");
    var storage = secureStorage(root.toAbsolutePath());
    Path heldRoot = tempDir.resolve("held-root");
    Path outside = Files.createDirectory(tempDir.resolve("outside"));
    Path outsideObject = outside.resolve(key);
    Files.createDirectories(outsideObject.getParent());
    Files.writeString(outsideObject, "attacker-content");

    Files.move(root, heldRoot);
    Files.createSymbolicLink(root, outside);

    assertThat(new String(storage.open(key).readAllBytes(), UTF_8)).isEqualTo("safe-content");
  }

  @Test
  void deletesFromTheOriginalRootAfterTheConfiguredPathIsReplaced() throws Exception {
    Path root = Files.createDirectory(tempDir.resolve("root"));
    String key = "users/user-id/requirements/file-id/original.txt";
    Path object = root.resolve(key);
    Files.createDirectories(object.getParent());
    Files.writeString(object, "safe-content");
    var storage = secureStorage(root.toAbsolutePath());
    Path heldRoot = tempDir.resolve("held-root");
    Path outside = Files.createDirectory(tempDir.resolve("outside"));
    Path outsideObject = outside.resolve(key);
    Files.createDirectories(outsideObject.getParent());
    Files.writeString(outsideObject, "attacker-content");

    Files.move(root, heldRoot);
    Files.createSymbolicLink(root, outside);
    storage.deleteIfExists(key);

    assertThat(heldRoot.resolve(key)).doesNotExist();
    assertThat(outsideObject).hasContent("attacker-content");
  }

  @Test
  void keepsAnOpenedReadValidAfterItsParentPathIsReplaced() throws Exception {
    Path root = Files.createDirectory(tempDir.resolve("root"));
    String key = "users/user-id/requirements/file-id/original.txt";
    Path object = root.resolve(key);
    Files.createDirectories(object.getParent());
    Files.writeString(object, "safe-content");
    var storage = secureStorage(root.toAbsolutePath());

    InputStream opened = storage.open(key);
    Path heldParent = tempDir.resolve("held-parent");
    Files.move(object.getParent(), heldParent);
    Files.createSymbolicLink(object.getParent(), Files.createDirectory(tempDir.resolve("outside")));

    assertThat(new String(opened.readAllBytes(), UTF_8)).isEqualTo("safe-content");
  }

  @Test
  void defaultProviderCompletesPutOpenAndDelete() throws Exception {
    Path root = Files.createDirectory(tempDir.resolve("default-provider-root"));
    String key = "users/user-id/requirements/file-id/original.txt";
    byte[] content = "requests==2.32.4\n".getBytes(UTF_8);

    try (var storage = new LocalFileStorage(root.toAbsolutePath())) {
      storage.putAtomically(key, new ByteArrayInputStream(content), content.length);

      assertThat(storage.open(key).readAllBytes()).isEqualTo(content);

      storage.deleteIfExists(key);
      assertThat(root.resolve(key)).doesNotExist();
    }
  }

  @Test
  void fallsBackWhenTheProviderDoesNotExposeASecureDirectoryStream() throws Exception {
    Path root = Files.createDirectory(tempDir.resolve("root"));
    String key = "users/user-id/requirements/file-id/original.txt";
    byte[] content = "fallback-content".getBytes(UTF_8);

    try (var storage = ordinaryStorage(root)) {
      storage.putAtomically(key, new ByteArrayInputStream(content), content.length);
      assertThat(storage.open(key).readAllBytes()).isEqualTo(content);

      storage.deleteIfExists(key);
      assertThat(root.resolve(key)).doesNotExist();
    }
  }

  @Test
  void fallbackRejectsEscapingKeysAndStaticSymlinkParents() throws Exception {
    Path root = Files.createDirectory(tempDir.resolve("fallback-root"));
    Path outside = Files.createDirectory(tempDir.resolve("fallback-outside"));
    Files.createSymbolicLink(root.resolve("users"), outside);

    try (var storage = ordinaryStorage(root)) {
      assertThatThrownBy(
              () ->
                  storage.putAtomically(
                      "../outside.txt", new ByteArrayInputStream(new byte[] {1}), 1))
          .isInstanceOf(IllegalArgumentException.class);
      assertThatThrownBy(
              () ->
                  storage.putAtomically(
                      "users/./original.txt", new ByteArrayInputStream(new byte[] {1}), 1))
          .isInstanceOf(IllegalArgumentException.class);
      assertThatThrownBy(
              () ->
                  storage.putAtomically(
                      "users/nested/original.txt", new ByteArrayInputStream(new byte[] {1}), 1))
          .isInstanceOf(IllegalArgumentException.class);
    }

    assertThat(outside.resolve("nested")).doesNotExist();
  }

  @Test
  void fallbackRejectsSymbolicLinkObjects() throws Exception {
    Path root = Files.createDirectory(tempDir.resolve("fallback-object-root"));
    String key = "users/user-id/requirements/file-id/original.txt";
    Path object = root.resolve(key);
    Files.createDirectories(object.getParent());
    Path outside = Files.writeString(tempDir.resolve("outside-object.txt"), "must remain");
    Files.createSymbolicLink(object, outside);

    try (var storage = ordinaryStorage(root)) {
      assertThatThrownBy(() -> storage.open(key)).isInstanceOf(IllegalArgumentException.class);
      assertThatThrownBy(() -> storage.deleteIfExists(key))
          .isInstanceOf(IllegalArgumentException.class);
      assertThatThrownBy(
              () ->
                  storage.putAtomically(
                      key, new ByteArrayInputStream("replacement".getBytes(UTF_8)), 11))
          .isInstanceOf(IllegalArgumentException.class);
    }

    assertThat(outside).hasContent("must remain");
    try (var files = Files.list(object.getParent())) {
      assertThat(files.map(path -> path.getFileName().toString())).containsExactly("original.txt");
    }
  }

  @Test
  void secureProviderRejectsSymbolicLinkObjectsForEveryOperation() throws Exception {
    Path root = Files.createDirectory(tempDir.resolve("secure-object-root"));
    String key = "users/user-id/requirements/file-id/original.txt";
    Path object = root.resolve(key);
    Files.createDirectories(object.getParent());
    Path outside = Files.writeString(tempDir.resolve("secure-outside-object.txt"), "must remain");
    Files.createSymbolicLink(object, outside);

    try (var storage = secureStorage(root.toAbsolutePath())) {
      assertThatThrownBy(
              () ->
                  storage.putAtomically(
                      key, new ByteArrayInputStream("replacement".getBytes(UTF_8)), 11))
          .isInstanceOf(IllegalArgumentException.class);
      assertThatThrownBy(() -> storage.open(key)).isInstanceOf(IllegalArgumentException.class);
      assertThatThrownBy(() -> storage.deleteIfExists(key))
          .isInstanceOf(IllegalArgumentException.class);
    }

    assertThat(object).isSymbolicLink();
    assertThat(outside).hasContent("must remain");
  }

  @Test
  void secureProviderRejectsNonRegularObjectsForEveryOperation() throws Exception {
    Path root = Files.createDirectory(tempDir.resolve("secure-directory-object-root"));
    String key = "users/user-id/requirements/file-id/original.txt";
    Path object = root.resolve(key);
    Files.createDirectories(object);

    try (var storage = secureStorage(root.toAbsolutePath())) {
      assertThatThrownBy(
              () ->
                  storage.putAtomically(
                      key, new ByteArrayInputStream("replacement".getBytes(UTF_8)), 11))
          .isInstanceOf(IllegalArgumentException.class);
      assertThatThrownBy(() -> storage.open(key)).isInstanceOf(IllegalArgumentException.class);
      assertThatThrownBy(() -> storage.deleteIfExists(key))
          .isInstanceOf(IllegalArgumentException.class);
    }

    assertThat(object).isDirectory();
  }

  @Test
  void rejectsOperationsAfterTheRootDirectoryHandleIsClosed() throws Exception {
    Path root = Files.createDirectory(tempDir.resolve("root"));
    var storage = secureStorage(root.toAbsolutePath());

    storage.close();

    assertThatThrownBy(() -> storage.open("users/user/original.txt"))
        .isInstanceOf(LocalFileStorage.StorageException.class)
        .hasMessageContaining("closed");
  }

  private static final class BlockingInputStream extends ByteArrayInputStream {
    private final CountDownLatch readStarted = new CountDownLatch(1);
    private final CountDownLatch continueRead = new CountDownLatch(1);
    private boolean blocked;

    private BlockingInputStream(byte[] content) {
      super(content);
    }

    @Override
    public synchronized int read(byte[] bytes, int offset, int length) {
      if (!blocked) {
        blocked = true;
        readStarted.countDown();
        try {
          if (!continueRead.await(5, TimeUnit.SECONDS)) {
            throw new AssertionError("Timed out waiting for directory replacement");
          }
        } catch (InterruptedException exception) {
          Thread.currentThread().interrupt();
          throw new AssertionError(exception);
        }
      }
      return super.read(bytes, offset, length);
    }
  }

  private LocalFileStorage ordinaryStorage(Path root) {
    return new LocalFileStorage(
        root.toAbsolutePath(),
        ignored -> {
          DirectoryStream<Path> delegate = Files.newDirectoryStream(root);
          return new DirectoryStream<>() {
            @Override
            public java.util.Iterator<Path> iterator() {
              return delegate.iterator();
            }

            @Override
            public void close() throws IOException {
              delegate.close();
            }
          };
        });
  }
}
