package com.wheelforge.api.common.storage;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.channels.Channels;
import java.nio.channels.SeekableByteChannel;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;

final class StorageObjectWriter {
  private static final int BUFFER_SIZE = 8192;

  private StorageObjectWriter() {}

  static WriteResult write(SeekableByteChannel channel, InputStream input, long expectedSize)
      throws IOException {
    if (expectedSize < 0) {
      throw new IllegalArgumentException("Expected size must not be negative");
    }
    try {
      MessageDigest digest = MessageDigest.getInstance("SHA-256");
      long written = 0;
      byte[] buffer = new byte[BUFFER_SIZE];
      try (OutputStream output = Channels.newOutputStream(channel)) {
        int count;
        while ((count = input.read(buffer)) != -1) {
          written += count;
          if (written > expectedSize) {
            throw new LocalFileStorage.SizeMismatchException(expectedSize, written);
          }
          digest.update(buffer, 0, count);
          output.write(buffer, 0, count);
        }
      }
      if (written != expectedSize) {
        throw new LocalFileStorage.SizeMismatchException(expectedSize, written);
      }
      return new WriteResult(written, HexFormat.of().formatHex(digest.digest()));
    } catch (NoSuchAlgorithmException exception) {
      throw new IllegalStateException("SHA-256 is unavailable", exception);
    }
  }

  record WriteResult(long sizeBytes, String sha256) {}
}
