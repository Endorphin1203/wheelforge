package com.wheelforge.api.requirements;

import static com.wheelforge.api.common.storage.SecureDirectoryStreamTestSupport.secureStorage;
import static java.nio.charset.StandardCharsets.UTF_8;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

import com.wheelforge.api.common.ApiException;
import com.wheelforge.api.common.jobs.BuildJobService;
import com.wheelforge.api.common.storage.LocalFileStorage;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.Arrays;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.io.TempDir;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

@ExtendWith(MockitoExtension.class)
class RequirementFileServiceTest {
  private static final UUID USER_ID = UUID.fromString("cae6ea8d-0afe-41df-aeea-fc0e5aceabfb");
  private static final UUID FILE_ID = UUID.fromString("2b0e75d6-c238-4f39-9bf9-5e246eb7c4cd");
  private static final Instant NOW = Instant.parse("2026-07-23T01:00:00Z");

  @TempDir Path tempDir;
  @Mock RequirementFileRepository fileRepository;
  @Mock RequirementItemRepository itemRepository;
  @Mock BuildJobService buildJobService;

  private RequirementFileService service;
  private LocalFileStorage storage;

  @BeforeEach
  void setUp() {
    storage = secureStorage(tempDir.toAbsolutePath());
    service =
        new RequirementFileService(
            fileRepository,
            itemRepository,
            buildJobService,
            storage,
            Clock.fixed(NOW, ZoneOffset.UTC),
            () -> FILE_ID);
  }

  @AfterEach
  void clearTransactionState() {
    if (TransactionSynchronizationManager.isSynchronizationActive()) {
      TransactionSynchronizationManager.clearSynchronization();
    }
    TransactionSynchronizationManager.setActualTransactionActive(false);
  }

  @Test
  void storesOncePersistsPendingRecordAndEnqueuesStrictParsePayload() throws Exception {
    beginTransactionSynchronization();
    var upload =
        new MockMultipartFile(
            "file", "requirements.txt", "text/plain", "requests==2.32.4\n".getBytes(UTF_8));
    given(fileRepository.save(any())).willAnswer(invocation -> invocation.getArgument(0));

    var result = service.upload(USER_ID, upload);

    assertThat(result.id()).isEqualTo(FILE_ID);
    assertThat(result.parseStatus()).isEqualTo("PENDING");
    assertThat(
            Files.readString(
                tempDir.resolve(
                    "users/cae6ea8d-0afe-41df-aeea-fc0e5aceabfb/requirements/2b0e75d6-c238-4f39-9bf9-5e246eb7c4cd/original.txt")))
        .isEqualTo("requests==2.32.4\n");

    var entityCaptor = ArgumentCaptor.forClass(RequirementFileEntity.class);
    verify(fileRepository).save(entityCaptor.capture());
    assertThat(entityCaptor.getValue().getUserId()).isEqualTo(USER_ID.toString());
    assertThat(entityCaptor.getValue().getNormalizedObjectKey()).endsWith("/normalized.txt");
    verify(buildJobService)
        .enqueue(
            "REQUIREMENT_PARSE",
            FILE_ID,
            BuildJobService.requirementParsePayload(
                "users/cae6ea8d-0afe-41df-aeea-fc0e5aceabfb/requirements/2b0e75d6-c238-4f39-9bf9-5e246eb7c4cd/original.txt",
                "users/cae6ea8d-0afe-41df-aeea-fc0e5aceabfb/requirements/2b0e75d6-c238-4f39-9bf9-5e246eb7c4cd/normalized.txt"));
  }

  @Test
  void deletesPublishedObjectWhenTheTransactionDoesNotCommit() throws Exception {
    beginTransactionSynchronization();
    var upload =
        new MockMultipartFile(
            "file", "requirements.txt", "text/plain", "requests==2.32.4\n".getBytes(UTF_8));
    given(fileRepository.save(any())).willAnswer(invocation -> invocation.getArgument(0));

    service.upload(USER_ID, upload);
    TransactionSynchronizationManager.getSynchronizations()
        .forEach(sync -> sync.afterCompletion(TransactionSynchronization.STATUS_ROLLED_BACK));

    assertThat(Files.walk(tempDir).filter(Files::isRegularFile)).isEmpty();
  }

  @Test
  void rejectsInvalidNamesEmptyOversizedAndNulContentBeforeDatabaseWrites() {
    beginTransactionSynchronization();
    List<MockMultipartFile> invalidFiles =
        List.of(
            new MockMultipartFile("file", "requirements.in", "text/plain", "a==1".getBytes(UTF_8)),
            new MockMultipartFile("file", "requirements.txt", "text/plain", new byte[0]),
            new MockMultipartFile(
                "file",
                "requirements.txt",
                "text/plain",
                new byte[RequirementFileService.MAX_BYTES + 1]),
            new MockMultipartFile(
                "file", "requirements.txt", "text/plain", new byte[] {'a', '=', '=', '1', 0}));

    for (MockMultipartFile invalidFile : invalidFiles) {
      assertThatThrownBy(() -> service.upload(USER_ID, invalidFile))
          .isInstanceOf(ApiException.class);
    }

    verify(fileRepository, never()).save(any());
    verify(buildJobService, never()).enqueue(any(), any(), any());
  }

  @Test
  void rejectsAStreamThatExceedsItsReportedSize() {
    beginTransactionSynchronization();
    byte[] actual = new byte[RequirementFileService.MAX_BYTES + 1];
    Arrays.fill(actual, (byte) 'a');
    var misleading =
        new MockMultipartFile("file", "requirements.txt", "text/plain", actual) {
          @Override
          public long getSize() {
            return 1;
          }

          @Override
          public InputStream getInputStream() throws IOException {
            return new ByteArrayInputStream(actual);
          }
        };

    assertThatThrownBy(() -> service.upload(USER_ID, misleading))
        .isInstanceOf(ApiException.class)
        .extracting("status")
        .isEqualTo(org.springframework.http.HttpStatus.PAYLOAD_TOO_LARGE);
    verify(fileRepository, never()).save(any());
  }

  @Test
  void scopesDetailAndItemsLookupsToTheAuthenticatedOwner() {
    UUID anotherFile = UUID.fromString("2034b2b6-deef-407e-8dca-4d7745766c0d");
    given(fileRepository.findByIdAndUserId(anotherFile.toString(), USER_ID.toString()))
        .willReturn(Optional.empty());

    assertThatThrownBy(() -> service.get(USER_ID, anotherFile))
        .isInstanceOf(ApiException.class)
        .extracting("status")
        .isEqualTo(org.springframework.http.HttpStatus.NOT_FOUND);
    assertThatThrownBy(() -> service.items(USER_ID, anotherFile))
        .isInstanceOf(ApiException.class)
        .extracting("status")
        .isEqualTo(org.springframework.http.HttpStatus.NOT_FOUND);
    verify(itemRepository, never()).findAllByRequirementFileIdOrderByLineNoAsc(any());
  }

  @Test
  void closesTheMultipartInputStreamAfterPublication() throws Exception {
    beginTransactionSynchronization();
    byte[] content = "requests==2.32.4\n".getBytes(UTF_8);
    var stream = new CloseTrackingInputStream(content);
    var upload =
        new MockMultipartFile("file", "requirements.txt", "text/plain", content) {
          @Override
          public InputStream getInputStream() {
            return stream;
          }
        };
    given(fileRepository.save(any())).willAnswer(invocation -> invocation.getArgument(0));

    service.upload(USER_ID, upload);

    assertThat(stream.closed).isTrue();
  }

  @Test
  void acceptsAnOriginalFilenameOfExactly255JavaCharacters() throws Exception {
    beginTransactionSynchronization();
    String filename = "a".repeat(251) + ".txt";
    var upload =
        new MockMultipartFile("file", filename, "text/plain", "requests==2.32.4\n".getBytes(UTF_8));
    given(fileRepository.save(any())).willAnswer(invocation -> invocation.getArgument(0));

    var result = service.upload(USER_ID, upload);

    assertThat(filename).hasSize(255);
    assertThat(result.originalName()).isEqualTo(filename);
    assertThat(Files.walk(tempDir).filter(Files::isRegularFile)).hasSize(1);
  }

  @Test
  void rejectsAnOriginalFilenameOf256JavaCharactersBeforePublishing() throws Exception {
    beginTransactionSynchronization();
    String filename = "a".repeat(252) + ".txt";
    var upload =
        new MockMultipartFile("file", filename, "text/plain", "requests==2.32.4\n".getBytes(UTF_8));

    assertThat(filename).hasSize(256);
    assertThatThrownBy(() -> service.upload(USER_ID, upload))
        .isInstanceOf(ApiException.class)
        .extracting("status")
        .isEqualTo(org.springframework.http.HttpStatus.BAD_REQUEST);
    assertThat(Files.walk(tempDir).filter(Files::isRegularFile)).isEmpty();
    verify(fileRepository, never()).save(any());
    verify(buildJobService, never()).enqueue(any(), any(), any());
  }

  private void beginTransactionSynchronization() {
    TransactionSynchronizationManager.setActualTransactionActive(true);
    TransactionSynchronizationManager.initSynchronization();
  }

  private static final class CloseTrackingInputStream extends ByteArrayInputStream {
    private boolean closed;

    private CloseTrackingInputStream(byte[] content) {
      super(content);
    }

    @Override
    public void close() throws IOException {
      closed = true;
      super.close();
    }
  }
}
