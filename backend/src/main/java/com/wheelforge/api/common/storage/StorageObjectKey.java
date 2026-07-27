package com.wheelforge.api.common.storage;

import java.nio.file.Path;

record StorageObjectKey(String value, Path relative, Path parent, Path fileName) {
  static StorageObjectKey parse(String key) {
    if (key == null || key.isBlank() || key.indexOf('\\') >= 0) {
      throw new IllegalArgumentException("Object key must be a non-empty portable relative path");
    }
    Path relative = Path.of(key);
    Path normalized = relative.normalize();
    if (relative.isAbsolute()
        || !relative.equals(normalized)
        || normalized.startsWith("..")
        || normalized.getFileName() == null) {
      throw new IllegalArgumentException("Object key must be normalized within the storage root");
    }
    return new StorageObjectKey(key, normalized, normalized.getParent(), normalized.getFileName());
  }
}
