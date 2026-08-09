package com.wheelforge.api.common.storage;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;

class LocalFileStorageContextTest {
  @TempDir Path tempDir;

  @Test
  void applicationContextRefusesAProviderWithoutCompleteStorageSemantics() throws Exception {
    Path root = Files.createDirectory(tempDir.resolve("root"));
    var contextRunner =
        new ApplicationContextRunner()
            .withBean(
                LocalFileStorage.class,
                () -> new LocalFileStorage(root.toAbsolutePath(), ignored -> ordinaryStream(root)));

    contextRunner.run(
        context -> {
          assertThat(context).hasFailed();
          assertThat(context.getStartupFailure())
              .hasRootCauseInstanceOf(LocalFileStorage.StorageException.class)
              .hasRootCauseMessage(
                  "Local storage requires SecureDirectoryStream for complete storage semantics");
        });

    try (var entries = Files.list(root)) {
      assertThat(entries).isEmpty();
    }
  }

  private static DirectoryStream<Path> ordinaryStream(Path root) throws IOException {
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
  }
}
