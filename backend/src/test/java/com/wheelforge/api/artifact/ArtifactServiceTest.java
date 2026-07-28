package com.wheelforge.api.artifact;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.BDDMockito.given;

import com.wheelforge.api.common.ApiException;
import com.wheelforge.api.common.storage.LocalFileStorage;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.security.MessageDigest;
import java.time.Clock;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.HexFormat;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.data.domain.Pageable;

@ExtendWith(MockitoExtension.class)
class ArtifactServiceTest {
  private static final UUID USER_ID = UUID.fromString("cae6ea8d-0afe-41df-aeea-fc0e5aceabfb");
  private static final UUID OTHER_USER_ID = UUID.fromString("b9aaf244-5ee2-44a8-872f-18d181f124cc");
  private static final UUID TASK_ID = UUID.fromString("fe3b9a09-e696-4104-beb7-d8fd1fb85d24");
  private static final UUID ARTIFACT_ID = UUID.fromString("2b0e75d6-c238-4f39-9bf9-5e246eb7c4cd");
  private static final Instant NOW = Instant.parse("2026-07-28T01:02:03Z");
  private static final byte[] CONTENT = {1, 2, 3, 4};
  private static final String CONTENT_SHA256 = sha256(CONTENT);

  @Mock private ArtifactRepository artifactRepository;
  @Mock private DownloadRecordRepository downloadRecordRepository;
  @Mock private LocalFileStorage storage;
  private ArtifactService service;

  @BeforeEach
  void setUp() {
    service =
        new ArtifactService(
            artifactRepository,
            downloadRecordRepository,
            storage,
            Clock.fixed(NOW, ZoneOffset.UTC));
  }

  @Test
  void listsAndReadsOwnedArtifactsEvenWhenTheirTaskWasSoftDeleted() {
    ArtifactEntity artifact = artifact(NOW.plusSeconds(60), null);
    given(artifactRepository.findPageByOwner(USER_ID.toString(), null, null, Pageable.ofSize(50)))
        .willReturn(List.of(artifact));
    given(artifactRepository.findByIdAndOwner(ARTIFACT_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(artifact));

    assertThat(service.list(USER_ID, null, null, 50))
        .singleElement()
        .extracting(ArtifactService.ArtifactView::id)
        .isEqualTo(ARTIFACT_ID);
    assertThat(service.get(USER_ID, ARTIFACT_ID).filename()).isEqualTo("wheelhouse.zip");
  }

  @Test
  void artifactListRequiresPairedCursorAndHardBoundedLimit() {
    assertThatThrownBy(
            () -> service.list(USER_ID, NOW.atZone(ZoneOffset.UTC).toLocalDateTime(), null, 50))
        .isInstanceOf(ApiException.class);
    assertThatThrownBy(() -> service.list(USER_ID, null, ARTIFACT_ID, 50))
        .isInstanceOf(ApiException.class);
    assertThatThrownBy(() -> service.list(USER_ID, null, null, 0)).isInstanceOf(ApiException.class);
    assertThatThrownBy(() -> service.list(USER_ID, null, null, 101))
        .isInstanceOf(ApiException.class);
  }

  @Test
  void hidesUnknownAndCrossUserArtifactsBehindTheSameNotFoundError() {
    given(artifactRepository.findByIdAndOwner(ARTIFACT_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.empty());
    given(artifactRepository.findByIdAndOwner(ARTIFACT_ID.toString(), OTHER_USER_ID.toString()))
        .willReturn(Optional.empty());

    assertNotFound(() -> service.get(USER_ID, ARTIFACT_ID));
    assertNotFound(() -> service.get(OTHER_USER_ID, ARTIFACT_ID));
  }

  @Test
  void createsIncompleteAuditAndIncrementsCountBeforeStreamingWithBoundedClientMetadata() {
    ArtifactEntity artifact = artifact(NOW.plusSeconds(60), null);
    given(artifactRepository.findByIdAndOwnerForUpdate(ARTIFACT_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(artifact));

    ArtifactService.DownloadTicket ticket =
        service.prepareDownload(
            USER_ID, ARTIFACT_ID, "2001:db8:" + "1".repeat(80), "agent/" + "x".repeat(1200));

    assertThat(ticket.objectKey()).isEqualTo("users/internal/artifacts/object.zip");
    assertThat(ticket.sha256()).isEqualTo("a".repeat(64));
    assertThat(artifact.getDownloadCount()).isEqualTo(8);
    ArgumentCaptor<DownloadRecordEntity> record =
        ArgumentCaptor.forClass(DownloadRecordEntity.class);
    org.mockito.Mockito.verify(downloadRecordRepository).save(record.capture());
    assertThat(record.getValue().isCompleted()).isFalse();
    assertThat(record.getValue().getIpAddress()).hasSize(45);
    assertThat(record.getValue().getUserAgent()).hasSize(1000);
  }

  @Test
  void marksAuditCompleteOnlyAfterStorageStreamCopiesAndClosesSuccessfully() throws Exception {
    DownloadRecordEntity record = record();
    given(downloadRecordRepository.findById(record.getId())).willReturn(Optional.of(record));
    given(storage.open("users/internal/artifacts/object.zip"))
        .willReturn(new ByteArrayInputStream(CONTENT));
    var output = new java.io.ByteArrayOutputStream();

    service.stream(ticket(record), output);

    assertThat(output.toByteArray()).containsExactly(1, 2, 3, 4);
    assertThat(record.isCompleted()).isTrue();
  }

  @Test
  void shortLongAndSameLengthDigestCorruptionLeaveAuditIncomplete() {
    assertCorrupt(new byte[] {1, 2, 3}, 4, CONTENT_SHA256);
    assertCorrupt(new byte[] {1, 2, 3, 4, 5}, 4, CONTENT_SHA256);
    assertCorrupt(new byte[] {1, 2, 3, 9}, 4, CONTENT_SHA256);
  }

  @Test
  void flushFailureAfterAcceptedWritesLeavesAuditIncompleteAndDoesNotCloseOutput() {
    DownloadRecordEntity record = record();
    given(downloadRecordRepository.findById(record.getId())).willReturn(Optional.of(record));
    given(storage.open("users/internal/artifacts/object.zip"))
        .willReturn(new ByteArrayInputStream(CONTENT));
    var output = new FlushFailingOutputStream();

    assertThatThrownBy(() -> service.stream(ticket(record), output))
        .isInstanceOf(IOException.class);

    assertThat(output.written()).containsExactly(CONTENT);
    assertThat(output.closed()).isFalse();
    assertThat(record.isCompleted()).isFalse();
  }

  @Test
  void failedOrAbortedStreamLeavesAuditIncomplete() {
    DownloadRecordEntity record = record();
    given(downloadRecordRepository.findById(record.getId())).willReturn(Optional.of(record));
    given(storage.open("users/internal/artifacts/object.zip")).willReturn(new FailingInputStream());

    assertThatThrownBy(() -> service.stream(ticket(record), new java.io.ByteArrayOutputStream()))
        .isInstanceOf(IOException.class);
    assertThat(record.isCompleted()).isFalse();
  }

  @Test
  void expiredAndCleanedArtifactsCannotStartADownloadButMetadataRemainsReadable() {
    ArtifactEntity expired = artifact(NOW.minusSeconds(1), null);
    ArtifactEntity cleaned =
        artifact(NOW.plusSeconds(60), LocalDateTime.ofInstant(NOW, ZoneOffset.UTC));
    given(artifactRepository.findByIdAndOwner(ARTIFACT_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(expired));
    assertThat(service.get(USER_ID, ARTIFACT_ID).id()).isEqualTo(ARTIFACT_ID);

    given(artifactRepository.findByIdAndOwnerForUpdate(ARTIFACT_ID.toString(), USER_ID.toString()))
        .willReturn(Optional.of(expired), Optional.of(cleaned));
    assertGone(() -> service.prepareDownload(USER_ID, ARTIFACT_ID, "127.0.0.1", null));
    assertGone(() -> service.prepareDownload(USER_ID, ARTIFACT_ID, "127.0.0.1", null));
  }

  private ArtifactEntity artifact(Instant expiresAt, LocalDateTime cleanedAt) {
    return new ArtifactEntity(
        ARTIFACT_ID.toString(),
        TASK_ID.toString(),
        "OFFLINE_ZIP",
        "wheelhouse.zip",
        "users/internal/artifacts/object.zip",
        4,
        "a".repeat(64),
        "SUCCESS",
        "STATIC",
        LocalDateTime.ofInstant(expiresAt, ZoneOffset.UTC),
        7,
        cleanedAt,
        LocalDateTime.of(2026, 7, 23, 1, 2, 3));
  }

  private DownloadRecordEntity record() {
    return new DownloadRecordEntity(
        UUID.randomUUID().toString(),
        ARTIFACT_ID.toString(),
        USER_ID.toString(),
        "127.0.0.1",
        "test",
        false,
        LocalDateTime.ofInstant(NOW, ZoneOffset.UTC));
  }

  private ArtifactService.DownloadTicket ticket(DownloadRecordEntity record) {
    return new ArtifactService.DownloadTicket(
        UUID.fromString(record.getId()),
        "users/internal/artifacts/object.zip",
        "wheelhouse.zip",
        4,
        CONTENT_SHA256);
  }

  private void assertCorrupt(byte[] stored, long expectedSize, String expectedSha256) {
    DownloadRecordEntity record = record();
    given(downloadRecordRepository.findById(record.getId())).willReturn(Optional.of(record));
    given(storage.open("users/internal/artifacts/object.zip"))
        .willReturn(new ByteArrayInputStream(stored));
    var ticket =
        new ArtifactService.DownloadTicket(
            UUID.fromString(record.getId()),
            "users/internal/artifacts/object.zip",
            "wheelhouse.zip",
            expectedSize,
            expectedSha256);

    assertThatThrownBy(() -> service.stream(ticket, new java.io.ByteArrayOutputStream()))
        .isInstanceOf(IOException.class);
    assertThat(record.isCompleted()).isFalse();
  }

  private static String sha256(byte[] value) {
    try {
      return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value));
    } catch (java.security.NoSuchAlgorithmException exception) {
      throw new IllegalStateException(exception);
    }
  }

  private void assertNotFound(Runnable operation) {
    assertThatThrownBy(operation::run)
        .isInstanceOf(ApiException.class)
        .satisfies(failure -> assertThat(((ApiException) failure).status().value()).isEqualTo(404));
  }

  private void assertGone(Runnable operation) {
    assertThatThrownBy(operation::run)
        .isInstanceOf(ApiException.class)
        .satisfies(
            failure -> {
              ApiException apiFailure = (ApiException) failure;
              assertThat(apiFailure.status().value()).isEqualTo(410);
              assertThat(apiFailure.code()).isEqualTo("ARTIFACT_UNAVAILABLE");
            });
  }

  private static final class FailingInputStream extends InputStream {
    @Override
    public int read() throws IOException {
      throw new IOException("client stream failed");
    }
  }

  private static final class FlushFailingOutputStream extends OutputStream {
    private final java.io.ByteArrayOutputStream delegate = new java.io.ByteArrayOutputStream();
    private boolean closed;

    @Override
    public void write(int value) {
      delegate.write(value);
    }

    @Override
    public void flush() throws IOException {
      throw new IOException("client disconnected during flush");
    }

    @Override
    public void close() {
      closed = true;
    }

    byte[] written() {
      return delegate.toByteArray();
    }

    boolean closed() {
      return closed;
    }
  }
}
