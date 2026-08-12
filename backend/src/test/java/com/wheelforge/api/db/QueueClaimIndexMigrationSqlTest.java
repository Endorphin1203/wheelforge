package com.wheelforge.api.db;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.Test;

class QueueClaimIndexMigrationSqlTest {
  @Test
  void replacesReadyIndexWithPriorityOrderedClaimIndex() throws IOException {
    String migration = readMigration();

    assertThat(migration).containsIgnoringCase("dropindexix_job_readyonbuild_jobs");
    assertThat(migration)
        .containsIgnoringCase(
            "createindexix_job_readyonbuild_jobs"
                + "(status,priority_no,created_at,id,available_at)");
  }

  private String readMigration() throws IOException {
    try (InputStream input =
        getClass().getResourceAsStream("/db/migration/V4__queue_claim_index.sql")) {
      assertThat(input).as("queue claim index migration resource").isNotNull();
      return new String(input.readAllBytes(), StandardCharsets.UTF_8).replaceAll("\\s+", "");
    }
  }
}
