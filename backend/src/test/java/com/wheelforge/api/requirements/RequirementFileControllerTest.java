package com.wheelforge.api.requirements;

import static java.nio.charset.StandardCharsets.UTF_8;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.verify;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.wheelforge.api.common.ApiException;
import com.wheelforge.api.common.ApiExceptionHandler;
import com.wheelforge.api.security.SecurityConfig;
import com.wheelforge.api.security.TokenService;
import com.wheelforge.api.security.UserAccount;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpHeaders;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(
    controllers = RequirementFileController.class,
    properties = "wheelforge.auth.token-secret=0123456789abcdef0123456789abcdef")
@Import({ApiExceptionHandler.class, SecurityConfig.class, TokenService.class})
class RequirementFileControllerTest {
  private static final UUID USER_ID = UUID.fromString("cae6ea8d-0afe-41df-aeea-fc0e5aceabfb");
  private static final UUID FILE_ID = UUID.fromString("2b0e75d6-c238-4f39-9bf9-5e246eb7c4cd");

  @Autowired private MockMvc mvc;
  @Autowired private TokenService tokenService;
  @MockitoBean private RequirementFileService requirementFileService;

  @Test
  void uploadsRequirementsAndQueuesParsingForAuthenticatedOwner() throws Exception {
    var file =
        new MockMultipartFile(
            "file", "requirements.txt", "text/plain", "requests==2.32.4\n".getBytes(UTF_8));
    given(requirementFileService.upload(eq(USER_ID), any())).willReturn(fileView("PENDING"));

    mvc.perform(
            multipart("/api/requirement-files")
                .file(file)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isAccepted())
        .andExpect(jsonPath("$.id").value(FILE_ID.toString()))
        .andExpect(jsonPath("$.parseStatus").value("PENDING"));

    verify(requirementFileService).upload(eq(USER_ID), any());
  }

  @Test
  void returnsPayloadTooLargeUsingTheGlobalErrorShape() throws Exception {
    var file =
        new MockMultipartFile(
            "file",
            "requirements.txt",
            "text/plain",
            new byte[RequirementFileService.MAX_BYTES + 1]);
    given(requirementFileService.upload(eq(USER_ID), any()))
        .willThrow(ApiException.payloadTooLarge("Requirements file exceeds 512 KiB"));

    mvc.perform(
            multipart("/api/requirement-files")
                .file(file)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isPayloadTooLarge())
        .andExpect(jsonPath("$.code").value("FILE_TOO_LARGE"))
        .andExpect(jsonPath("$.fieldErrors").isMap())
        .andExpect(jsonPath("$.traceId").isString());
  }

  @Test
  void rejectsAMissingMultipartFileUsingTheGlobalErrorShape() throws Exception {
    mvc.perform(
            multipart("/api/requirement-files").header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("VALIDATION_FAILED"))
        .andExpect(jsonPath("$.fieldErrors").isMap())
        .andExpect(jsonPath("$.traceId").isString());
  }

  @Test
  void returnsDetailAndParsedItemsForAuthenticatedOwner() throws Exception {
    given(requirementFileService.get(USER_ID, FILE_ID)).willReturn(fileView("PARSED"));
    given(requirementFileService.items(USER_ID, FILE_ID))
        .willReturn(
            List.of(
                new RequirementFileService.RequirementItemView(
                    UUID.fromString("5c01d75f-e092-4387-ad90-65347d37dcaa"),
                    1,
                    "requests",
                    List.of("security"),
                    "==2.32.4",
                    null,
                    "requests[security]==2.32.4",
                    true,
                    null,
                    null)));

    mvc.perform(
            get("/api/requirement-files/{id}", FILE_ID)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.parseStatus").value("PARSED"));
    mvc.perform(
            get("/api/requirement-files/{id}/items", FILE_ID)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$[0].normalizedName").value("requests"))
        .andExpect(jsonPath("$[0].extras[0]").value("security"));
  }

  @Test
  void hidesAnotherUsersRequirementFileAsNotFound() throws Exception {
    given(requirementFileService.get(USER_ID, FILE_ID))
        .willThrow(ApiException.notFound("Requirement file was not found"));

    mvc.perform(
            get("/api/requirement-files/{id}", FILE_ID)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isNotFound())
        .andExpect(jsonPath("$.code").value("NOT_FOUND"));
  }

  private RequirementFileService.RequirementFileView fileView(String parseStatus) {
    return new RequirementFileService.RequirementFileView(
        FILE_ID,
        "requirements.txt",
        17,
        "61b547565054050327bdb8618040d14e5f5e40af571975ad5bf63c36f545c9d7",
        parseStatus,
        null,
        Instant.parse("2026-07-23T01:00:00Z"));
  }

  private String bearerToken() {
    var user =
        new UserAccount(
            USER_ID.toString(),
            "alice",
            "$argon2id$encoded",
            "USER",
            "ACTIVE",
            LocalDateTime.now(ZoneOffset.UTC));
    return "Bearer " + tokenService.issue(user).accessToken();
  }
}
