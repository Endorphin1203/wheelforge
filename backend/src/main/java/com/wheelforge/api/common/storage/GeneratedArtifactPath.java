package com.wheelforge.api.common.storage;

import java.nio.file.Path;
import java.util.UUID;

record GeneratedArtifactPath(String task, String execution) {
  static GeneratedArtifactPath parse(Path relative) {
    if (relative.getNameCount() != 4 || !relative.getName(0).toString().equals("artifacts")) {
      return null;
    }
    String task = canonicalUuid(relative.getName(1).toString());
    String execution = canonicalUuid(relative.getName(2).toString());
    String filename = relative.getName(3).toString();
    if (task == null || execution == null || !filename.endsWith(".zip")) {
      return null;
    }
    String artifact = canonicalUuid(filename.substring(0, filename.length() - 4));
    return artifact == null ? null : new GeneratedArtifactPath(task, execution);
  }

  private static String canonicalUuid(String value) {
    try {
      UUID parsed = UUID.fromString(value);
      return parsed.toString().equals(value) ? value : null;
    } catch (IllegalArgumentException exception) {
      return null;
    }
  }
}
