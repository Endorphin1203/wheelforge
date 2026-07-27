package com.wheelforge.api.common.storage;

import static java.nio.charset.StandardCharsets.UTF_8;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class LocalFileStorageTest {
  @TempDir Path tempDir;

  @Test
  void publishesAtomicallyAndReturnsTheSinglePassDigest() throws Exception {
    var storage = new LocalFileStorage(tempDir.toAbsolutePath());
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
  }

  @Test
  void rejectsAbsoluteAndEscapingObjectKeys() {
    var storage = new LocalFileStorage(tempDir.toAbsolutePath());

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
    var storage = new LocalFileStorage(tempDir.toAbsolutePath());
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
    var storage = new LocalFileStorage(tempDir.toAbsolutePath());

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
    var storage = new LocalFileStorage(root.toAbsolutePath());

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
}
