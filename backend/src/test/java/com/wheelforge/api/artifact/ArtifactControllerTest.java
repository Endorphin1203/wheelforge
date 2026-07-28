package com.wheelforge.api.artifact;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.BDDMockito.given;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.asyncDispatch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.wheelforge.api.common.ApiException;
import com.wheelforge.api.common.ApiExceptionHandler;
import com.wheelforge.api.security.SecurityConfig;
import com.wheelforge.api.security.TokenService;
import com.wheelforge.api.security.UserAccount;
import com.wheelforge.api.security.UserAccountRepository;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpHeaders;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;

@WebMvcTest(
    controllers = ArtifactController.class,
    properties = "wheelforge.auth.token-secret=0123456789abcdef0123456789abcdef")
@Import({ApiExceptionHandler.class, SecurityConfig.class, TokenService.class})
class ArtifactControllerTest {
  private static final UUID USER_ID = UUID.fromString("cae6ea8d-0afe-41df-aeea-fc0e5aceabfb");
  private static final UUID TASK_ID = UUID.fromString("fe3b9a09-e696-4104-beb7-d8fd1fb85d24");
  private static final UUID ARTIFACT_ID = UUID.fromString("2b0e75d6-c238-4f39-9bf9-5e246eb7c4cd");
  private static final UUID RECORD_ID = UUID.fromString("d29ec24f-095c-45f6-ac96-acdef0e7d8ad");

  @Autowired private MockMvc mvc;
  @Autowired private TokenService tokenService;
  @MockitoBean private ArtifactService service;
  @MockitoBean private UserAccountRepository userAccountRepository;

  @Test
  void listAndDetailExposeStaticMetadataWithoutStoragePaths() throws Exception {
    given(service.list(USER_ID, null, null, 50)).willReturn(List.of(view()));
    given(service.get(USER_ID, ARTIFACT_ID)).willReturn(view());

    mvc.perform(get("/api/artifacts").header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$[0].id").value(ARTIFACT_ID.toString()))
        .andExpect(jsonPath("$[0].buildTaskId").value(TASK_ID.toString()))
        .andExpect(jsonPath("$[0].filename").value("wheelhouse.zip"))
        .andExpect(jsonPath("$[0].validationLevel").value("STATIC"))
        .andExpect(jsonPath("$[0].installVerified").value(false))
        .andExpect(jsonPath("$[0].objectKey").doesNotExist())
        .andExpect(jsonPath("$[0].absolutePath").doesNotExist());

    mvc.perform(
            get("/api/artifacts/{id}", ARTIFACT_ID)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isOk())
        .andExpect(
            jsonPath("$.validationMessage")
                .value("Static compatibility checks passed; target installation was not verified."))
        .andExpect(jsonPath("$.objectKey").doesNotExist());
  }

  @Test
  void streamsOwnedArtifactWithFixedZipHeadersAndExactLength() throws Exception {
    var ticket =
        new ArtifactService.DownloadTicket(
            RECORD_ID,
            "users/internal/artifacts/object.zip",
            "../../unsafe\r\nname.zip",
            4,
            "9f64a747e1b97f131fabb6b447296c9b6f0201e79fb3c5356e6c77e89b6a806a");
    given(service.prepareDownload(any(), any(), any(), any())).willReturn(ticket);
    org.mockito.Mockito.doAnswer(
        invocation -> {
          invocation.<java.io.OutputStream>getArgument(1).write(new byte[] {1, 2, 3, 4});
          return null;
        })
        .when(service)
        .stream(eq(ticket), org.mockito.ArgumentMatchers.any());

    MvcResult pending =
        mvc.perform(
                get("/api/artifacts/{id}/download", ARTIFACT_ID)
                    .header(HttpHeaders.AUTHORIZATION, bearerToken())
                    .header(HttpHeaders.USER_AGENT, "WheelForge test"))
            .andExpect(request().asyncStarted())
            .andReturn();

    mvc.perform(asyncDispatch(pending))
        .andExpect(status().isOk())
        .andExpect(header().string(HttpHeaders.CONTENT_TYPE, "application/zip"))
        .andExpect(header().string(HttpHeaders.CONTENT_LENGTH, "4"))
        .andExpect(
            header()
                .string(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=\"wheelhouse.zip\""))
        .andExpect(header().doesNotExist(HttpHeaders.LOCATION))
        .andExpect(content().bytes(new byte[] {1, 2, 3, 4}));
  }

  @Test
  void unknownCrossUserExpiredAndCleanedDownloadsUseStable404Or410Responses() throws Exception {
    given(service.get(USER_ID, ARTIFACT_ID))
        .willThrow(ApiException.notFound("Artifact was not found"));
    mvc.perform(
            get("/api/artifacts/{id}", ARTIFACT_ID)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isNotFound())
        .andExpect(jsonPath("$.code").value("NOT_FOUND"));

    given(service.prepareDownload(any(), any(), any(), any()))
        .willThrow(ApiException.gone("ARTIFACT_UNAVAILABLE", "Artifact is no longer available"));
    mvc.perform(
            get("/api/artifacts/{id}/download", ARTIFACT_ID)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isGone())
        .andExpect(jsonPath("$.code").value("ARTIFACT_UNAVAILABLE"));
  }

  private ArtifactService.ArtifactView view() {
    return new ArtifactService.ArtifactView(
        ARTIFACT_ID,
        TASK_ID,
        "OFFLINE_ZIP",
        "wheelhouse.zip",
        4,
        "a".repeat(64),
        "SUCCESS",
        LocalDateTime.of(2026, 8, 23, 1, 2, 3),
        7,
        LocalDateTime.of(2026, 7, 23, 1, 2, 3),
        "STATIC",
        false,
        "Static compatibility checks passed; target installation was not verified.");
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
    given(userAccountRepository.findById(USER_ID.toString()))
        .willReturn(java.util.Optional.of(user));
    return "Bearer " + tokenService.issue(user).accessToken();
  }
}
