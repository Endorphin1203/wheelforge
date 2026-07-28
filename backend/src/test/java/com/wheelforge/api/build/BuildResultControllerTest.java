package com.wheelforge.api.build;

import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.verify;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
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
import tools.jackson.databind.ObjectMapper;

@WebMvcTest(
    controllers = BuildResultController.class,
    properties = "wheelforge.auth.token-secret=0123456789abcdef0123456789abcdef")
@Import({ApiExceptionHandler.class, SecurityConfig.class, TokenService.class})
class BuildResultControllerTest {
  private static final UUID USER_ID = UUID.fromString("cae6ea8d-0afe-41df-aeea-fc0e5aceabfb");
  private static final UUID TASK_ID = UUID.fromString("fe3b9a09-e696-4104-beb7-d8fd1fb85d24");
  private static final LocalDateTime CREATED_AT = LocalDateTime.of(2026, 7, 23, 1, 2, 3);

  @Autowired private MockMvc mvc;
  @Autowired private TokenService tokenService;
  @MockitoBean private BuildResultService service;
  @MockitoBean private UserAccountRepository userAccountRepository;

  @Test
  void returnsLogsAfterTheExplicitCursorWithStaticValidation() throws Exception {
    var context = new ObjectMapper().readTree("{\"candidate\":\"2.32.4\",\"cached\":true}");
    given(service.logs(USER_ID, TASK_ID, 7))
        .willReturn(
            List.of(
                new BuildResultService.BuildLogView(
                    8, "DOWNLOAD", "INFO", "Wheel selected", context, CREATED_AT)));

    mvc.perform(
            get("/api/build-tasks/{id}/logs", TASK_ID)
                .queryParam("afterSequence", "7")
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isOk())
        .andExpect(header().string(BuildResultController.VALIDATION_HEADER, "STATIC"))
        .andExpect(jsonPath("$[0].sequence").value(8))
        .andExpect(jsonPath("$[0].context.candidate").value("2.32.4"))
        .andExpect(jsonPath("$[0].id").doesNotExist());
  }

  @Test
  void defaultsTheLogCursorToZeroAndRejectsANegativeCursorWithGlobalErrorShape() throws Exception {
    given(service.logs(USER_ID, TASK_ID, 0)).willReturn(List.of());

    mvc.perform(
            get("/api/build-tasks/{id}/logs", TASK_ID)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isOk());
    verify(service).logs(USER_ID, TASK_ID, 0);

    given(service.logs(USER_ID, TASK_ID, -1))
        .willThrow(
            ApiException.badRequest("INVALID_LOG_CURSOR", "afterSequence must be non-negative"));
    mvc.perform(
            get("/api/build-tasks/{id}/logs", TASK_ID)
                .queryParam("afterSequence", "-1")
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("INVALID_LOG_CURSOR"))
        .andExpect(jsonPath("$.fieldErrors").isMap())
        .andExpect(jsonPath("$.traceId").isString());
  }

  @Test
  void mapsResolvedPackageJsonWithoutExposingPersistenceOrHostFields() throws Exception {
    var attempts =
        new ObjectMapper().readTree("[{\"version\":\"2.32.4\",\"outcome\":\"SELECTED\"}]");
    given(service.resolvedPackages(USER_ID, TASK_ID))
        .willReturn(
            List.of(
                new BuildResultService.ResolvedPackageView(
                    "requests",
                    "2.32.4",
                    "DIRECT",
                    ">=2.31",
                    "2.31.0",
                    "UPGRADE",
                    "Selected compatible Wheel",
                    attempts,
                    "requests-2.32.4-py3-none-any.whl",
                    List.of("py3-none-any"),
                    "PYPI",
                    "a".repeat(64),
                    "STATIC_PASSED",
                    null)));

    mvc.perform(
            get("/api/build-tasks/{id}/resolved-packages", TASK_ID)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isOk())
        .andExpect(header().string(BuildResultController.VALIDATION_HEADER, "STATIC"))
        .andExpect(jsonPath("$[0].normalizedName").value("requests"))
        .andExpect(jsonPath("$[0].candidateAttempts[0].outcome").value("SELECTED"))
        .andExpect(jsonPath("$[0].wheelTags[0]").value("py3-none-any"))
        .andExpect(jsonPath("$[0].id").doesNotExist())
        .andExpect(jsonPath("$[0].buildTaskId").doesNotExist())
        .andExpect(jsonPath("$[0].hostPath").doesNotExist())
        .andExpect(jsonPath("$[0].storagePath").doesNotExist());
  }

  @Test
  void exposesExactVersionComparisonFieldsWithStaticValidation() throws Exception {
    given(service.versionComparison(USER_ID, TASK_ID))
        .willReturn(
            List.of(
                new VersionComparisonRow(
                    "requests",
                    "DIRECT",
                    ">=2.31",
                    "2.31.0",
                    "2.32.4",
                    "UPGRADE",
                    "Selected compatible Wheel",
                    "STATIC_PASSED",
                    "PYPI")));

    mvc.perform(
            get("/api/build-tasks/{id}/version-comparison", TASK_ID)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isOk())
        .andExpect(header().string(BuildResultController.VALIDATION_HEADER, "STATIC"))
        .andExpect(jsonPath("$[0].packageName").value("requests"))
        .andExpect(jsonPath("$[0].dependencyType").value("DIRECT"))
        .andExpect(jsonPath("$[0].originalConstraint").value(">=2.31"))
        .andExpect(jsonPath("$[0].changeDirection").value("UPGRADE"))
        .andExpect(jsonPath("$[0].wheelStatus").value("STATIC_PASSED"))
        .andExpect(jsonPath("$[0].id").doesNotExist());
  }

  @Test
  void hidesCrossUserUnknownAndSoftDeletedTasksBehindTheSameNotFoundShape() throws Exception {
    given(service.logs(USER_ID, TASK_ID, 0))
        .willThrow(ApiException.notFound("Build task was not found"));
    given(service.resolvedPackages(USER_ID, TASK_ID))
        .willThrow(ApiException.notFound("Build task was not found"));
    given(service.versionComparison(USER_ID, TASK_ID))
        .willThrow(ApiException.notFound("Build task was not found"));

    for (String suffix : List.of("logs", "resolved-packages", "version-comparison")) {
      mvc.perform(
              get("/api/build-tasks/{id}/" + suffix, TASK_ID)
                  .header(HttpHeaders.AUTHORIZATION, bearerToken()))
          .andExpect(status().isNotFound())
          .andExpect(jsonPath("$.code").value("NOT_FOUND"))
          .andExpect(jsonPath("$.message").value("Build task was not found"))
          .andExpect(jsonPath("$.fieldErrors").isMap())
          .andExpect(jsonPath("$.traceId").isString());
    }
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
