package com.wheelforge.api.common.storage;

import java.io.InputStream;

interface LocalStorageBackend extends AutoCloseable {
  LocalFileStorage.StoredObject putAtomically(String key, InputStream input, long expectedSize);

  InputStream open(String key);

  void deleteIfExists(String key);

  @Override
  void close();
}
