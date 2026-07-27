package com.wheelforge.api.build;

import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.verify;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.wheelforge.api.common.ApiException;
import com.wheelforge.api.common.ApiExceptionHandler;
import com.wheelforge.api.security.SecurityConfig;
import com.wheelforge.api.security.TokenService;
import com.wheelforge.api.security.UserAccount;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Import;
import org.springframework.dao.OptimisticLockingFailureException;
import org.springframework.dao.PessimisticLockingFailureException;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(
    controllers = BuildTaskController.class,
    properties = "wheelforge.auth.token-secret=0123456789abcdef0123456789abcdef")
@Import({ApiExceptionHandler.class, SecurityConfig.class, TokenService.class})
class BuildTaskControllerTest {
  private static final UUID USER_ID = UUID.fromString("cae6ea8d-0afe-41df-aeea-fc0e5aceabfb");
  private static final UUID FILE_ID = UUID.fromString("2b0e75d6-c238-4f39-9bf9-5e246eb7c4cd");
  private static final UUID PROFILE_ID = UUID.fromString("d29ec24f-095c-45f6-ac96-acdef0e7d8ad");
  private static final UUID TASK_ID = UUID.fromString("fe3b9a09-e696-4104-beb7-d8fd1fb85d24");

  @Autowired private MockMvc mvc;
  @Autowired private TokenService tokenService;
  @MockitoBean private BuildTaskService service;

  @Test
  void createsBuildWithDefaultCompatibleMode() throws Exception {
    given(
            service.create(
                eq(USER_ID),
                eq(new BuildTaskService.CreateBuildTaskRequest(FILE_ID, PROFILE_ID, null))))
        .willReturn(view());

    mvc.perform(
            post("/api/build-tasks")
                .header(HttpHeaders.AUTHORIZATION, bearerToken())
                .contentType(MediaType.APPLICATION_JSON)
                .content(
                    "{\"requirementFileId\":\""
                        + FILE_ID
                        + "\",\"targetProfileId\":\""
                        + PROFILE_ID
                        + "\"}"))
        .andExpect(status().isCreated())
        .andExpect(jsonPath("$.id").value(TASK_ID.toString()))
        .andExpect(jsonPath("$.solveMode").value("COMPATIBLE"))
        .andExpect(jsonPath("$.targetSnapshot.profileCode").value("linux-aarch64-cp311"));
  }

  @Test
  void validatesCreateBodyAndReturnsNotFoundForCrossUserAccess() throws Exception {
    mvc.perform(
            post("/api/build-tasks")
                .header(HttpHeaders.AUTHORIZATION, bearerToken())
                .contentType(MediaType.APPLICATION_JSON)
                .content("{}"))
        .andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("VALIDATION_FAILED"));

    given(service.get(USER_ID, TASK_ID))
        .willThrow(ApiException.notFound("Build task was not found"));
    mvc.perform(
            get("/api/build-tasks/{id}", TASK_ID).header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isNotFound())
        .andExpect(jsonPath("$.code").value("NOT_FOUND"));
  }

  @Test
  void listsCancelsRetriesAndSoftDeletesOwnedTasks() throws Exception {
    given(service.list(USER_ID)).willReturn(List.of(view()));
    given(service.cancel(USER_ID, TASK_ID)).willReturn(view());
    given(service.retry(USER_ID, TASK_ID)).willReturn(view());

    mvc.perform(get("/api/build-tasks").header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$[0].id").value(TASK_ID.toString()));
    mvc.perform(
            post("/api/build-tasks/{id}/cancel", TASK_ID)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isOk());
    mvc.perform(
            post("/api/build-tasks/{id}/retry", TASK_ID)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isCreated());
    mvc.perform(
            delete("/api/build-tasks/{id}", TASK_ID)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isNoContent());
    verify(service).delete(USER_ID, TASK_ID);
  }

  @Test
  void mapsConcurrentModificationToStableConflictResponse() throws Exception {
    given(service.cancel(USER_ID, TASK_ID))
        .willThrow(new OptimisticLockingFailureException("stale build task"));

    mvc.perform(
            post("/api/build-tasks/{id}/cancel", TASK_ID)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isConflict())
        .andExpect(jsonPath("$.code").value("CONCURRENT_MODIFICATION"))
        .andExpect(jsonPath("$.message").value("The resource was modified concurrently"));
  }

  @Test
  void mapsDatabaseLockFailureToStableConflictResponse() throws Exception {
    given(service.cancel(USER_ID, TASK_ID))
        .willThrow(new PessimisticLockingFailureException("database lock timeout"));

    mvc.perform(
            post("/api/build-tasks/{id}/cancel", TASK_ID)
                .header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isConflict())
        .andExpect(jsonPath("$.code").value("CONCURRENT_MODIFICATION"))
        .andExpect(jsonPath("$.message").value("The resource was modified concurrently"));
  }

  private BuildTaskService.BuildTaskView view() {
    return new BuildTaskService.BuildTaskView(
        TASK_ID,
        FILE_ID,
        PROFILE_ID,
        null,
        BuildStatus.QUEUED,
        0,
        null,
        SolveMode.COMPATIBLE,
        new BuildTaskEntity.TargetSnapshot(
            PROFILE_ID.toString(),
            "linux-aarch64-cp311",
            "LINUX",
            "AARCH64",
            "CPYTHON",
            "3.11",
            "3.11.9",
            "manylinux2014_aarch64",
            List.of("cp311", "abi3", "none"),
            "STATIC",
            "wheel-tags-v1",
            0),
        false,
        null,
        null,
        LocalDateTime.of(2026, 7, 23, 1, 2, 3),
        null,
        null);
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
