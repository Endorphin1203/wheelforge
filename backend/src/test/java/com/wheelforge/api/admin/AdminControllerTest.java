package com.wheelforge.api.admin;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.BDDMockito.given;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

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
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(
    controllers = AdminController.class,
    properties = "wheelforge.auth.token-secret=0123456789abcdef0123456789abcdef")
@Import({ApiExceptionHandler.class, SecurityConfig.class, TokenService.class})
class AdminControllerTest {
  private static final UUID ADMIN_ID = UUID.fromString("cae6ea8d-0afe-41df-aeea-fc0e5aceabfb");
  private static final UUID USER_ID = UUID.fromString("fe3b9a09-e696-4104-beb7-d8fd1fb85d24");
  private static final UUID SOURCE_ID = UUID.fromString("2b0e75d6-c238-4f39-9bf9-5e246eb7c4cd");

  @Autowired private MockMvc mvc;
  @Autowired private TokenService tokenService;
  @MockitoBean private AdminService service;
  @MockitoBean private UserAccountRepository userAccountRepository;

  @Test
  void aRealUserTokenReceivesTheGlobal403ForEveryAdminEndpoint() throws Exception {
    var requests =
        List.of(
            get("/api/admin/users"),
            post("/api/admin/users")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"username\":\"new-user\",\"role\":\"USER\"}"),
            put("/api/admin/users/{id}/status", USER_ID)
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"status\":\"DISABLED\"}"),
            get("/api/admin/package-sources"),
            put("/api/admin/package-sources/{id}", SOURCE_ID)
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"enabled\":true,\"priorityNo\":10,\"timeoutSeconds\":30}"),
            get("/api/admin/system-config"),
            put("/api/admin/system-config")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"artifactRetentionDays\":{\"value\":30,\"version\":0}}"));

    for (var request : requests) {
      mvc.perform(request.header(HttpHeaders.AUTHORIZATION, token("USER")))
          .andExpect(status().isForbidden())
          .andExpect(jsonPath("$.code").value("FORBIDDEN"))
          .andExpect(jsonPath("$.fieldErrors").isMap())
          .andExpect(jsonPath("$.traceId").isString());
    }
  }

  @Test
  void adminCreatesUserAndGeneratedPasswordIsReturnedOnceWithoutAHash() throws Exception {
    given(service.createUser(any(), any()))
        .willReturn(
            new AdminService.UserView(
                USER_ID,
                "new-user",
                "USER",
                "ACTIVE",
                LocalDateTime.of(2026, 7, 28, 1, 2, 3),
                "generated-once"));

    mvc.perform(
            post("/api/admin/users")
                .header(HttpHeaders.AUTHORIZATION, token("ADMIN"))
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"username\":\"new-user\",\"role\":\"USER\"}"))
        .andExpect(status().isCreated())
        .andExpect(jsonPath("$.username").value("new-user"))
        .andExpect(jsonPath("$.initialPassword").value("generated-once"))
        .andExpect(jsonPath("$.passwordHash").doesNotExist());
  }

  @Test
  void adminListsUsersWithoutHashesAndUpdatesAnotherUsersStatus() throws Exception {
    var disabled =
        new AdminService.UserView(
            USER_ID,
            "existing-user",
            "USER",
            "DISABLED",
            LocalDateTime.of(2026, 7, 28, 1, 2, 3),
            null);
    given(service.users()).willReturn(List.of(disabled));
    given(
            service.updateUserStatus(
                eq(ADMIN_ID), eq(USER_ID), any(AdminService.UpdateUserStatusRequest.class)))
        .willReturn(disabled);

    mvc.perform(get("/api/admin/users").header(HttpHeaders.AUTHORIZATION, token("ADMIN")))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$[0].status").value("DISABLED"))
        .andExpect(jsonPath("$[0].passwordHash").doesNotExist());
    mvc.perform(
            put("/api/admin/users/{id}/status", USER_ID)
                .header(HttpHeaders.AUTHORIZATION, token("ADMIN"))
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"status\":\"DISABLED\"}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.status").value("DISABLED"));
  }

  @Test
  void adminEndpointsMapOnlyTheMutableSourceFieldsAndStructuredConfigValues() throws Exception {
    given(service.packageSources())
        .willReturn(
            List.of(
                new AdminService.PackageSourceView(
                    SOURCE_ID,
                    "TSINGHUA",
                    "Tsinghua PyPI",
                    "https://pypi.tuna.tsinghua.edu.cn/simple",
                    10,
                    true,
                    30,
                    0,
                    LocalDateTime.of(2026, 7, 28, 1, 2, 3),
                    0)));
    given(service.systemConfig())
        .willReturn(
            List.of(
                new AdminService.SystemConfigView(
                    "artifactRetentionDays",
                    new tools.jackson.databind.ObjectMapper().readTree("30"),
                    "Artifact retention in days",
                    null,
                    LocalDateTime.of(2026, 7, 28, 1, 2, 3),
                    0)));

    mvc.perform(get("/api/admin/package-sources").header(HttpHeaders.AUTHORIZATION, token("ADMIN")))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$[0].code").value("TSINGHUA"))
        .andExpect(jsonPath("$[0].baseUrl").value("https://pypi.tuna.tsinghua.edu.cn/simple"));
    mvc.perform(get("/api/admin/system-config").header(HttpHeaders.AUTHORIZATION, token("ADMIN")))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$[0].value").value(30))
        .andExpect(jsonPath("$[0].version").value(0));
  }

  @Test
  void adminUpdatesSourceAndConfigThroughTheirTypedRequestShapes() throws Exception {
    var source =
        new AdminService.PackageSourceView(
            SOURCE_ID,
            "TSINGHUA",
            "Tsinghua PyPI",
            "https://pypi.tuna.tsinghua.edu.cn/simple",
            20,
            false,
            45,
            0,
            LocalDateTime.of(2026, 7, 28, 1, 2, 3),
            1);
    var config =
        new AdminService.SystemConfigView(
            "artifactRetentionDays",
            new tools.jackson.databind.ObjectMapper().readTree("45"),
            "Artifact retention in days",
            ADMIN_ID,
            LocalDateTime.of(2026, 7, 28, 1, 2, 3),
            1);
    given(service.updatePackageSource(eq(SOURCE_ID), any())).willReturn(source);
    given(service.updateSystemConfig(eq(ADMIN_ID), any())).willReturn(List.of(config));

    mvc.perform(
            put("/api/admin/package-sources/{id}", SOURCE_ID)
                .header(HttpHeaders.AUTHORIZATION, token("ADMIN"))
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"enabled\":false,\"priorityNo\":20,\"timeoutSeconds\":45}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.enabled").value(false))
        .andExpect(jsonPath("$.baseUrl").value("https://pypi.tuna.tsinghua.edu.cn/simple"));
    mvc.perform(
            put("/api/admin/system-config")
                .header(HttpHeaders.AUTHORIZATION, token("ADMIN"))
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"artifactRetentionDays\":{\"value\":45,\"version\":0}}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$[0].value").value(45))
        .andExpect(jsonPath("$[0].version").value(1));
  }

  @Test
  void adminRequestsRejectUnknownAlternateAndDuplicateJsonFields() throws Exception {
    String authorization = token("ADMIN");
    for (String body :
        List.of(
            "{\"enabled\":true,\"priorityNo\":10,\"timeoutSeconds\":30,\"url\":\"https://evil.example\"}",
            "{\"enabled\":true,\"priorityNo\":10,\"timeoutSeconds\":30,\"base_url\":\"https://evil.example\"}",
            "{\"enabled\":true,\"priorityN0\":10,\"timeoutSeconds\":30}",
            "{\"enabled\":true,\"priorityNo\":10,\"timeoutSeconds\":30,\"unknown\":1}",
            "{\"enabled\":true,\"enabled\":false,\"priorityNo\":10,\"timeoutSeconds\":30}")) {
      mvc.perform(
              put("/api/admin/package-sources/{id}", SOURCE_ID)
                  .header(HttpHeaders.AUTHORIZATION, authorization)
                  .contentType(MediaType.APPLICATION_JSON)
                  .content(body))
          .andExpect(status().isBadRequest());
    }

    mvc.perform(
            post("/api/admin/users")
                .header(HttpHeaders.AUTHORIZATION, authorization)
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"username\":\"new-user\",\"role\":\"USER\",\"rol\":\"ADMIN\"}"))
        .andExpect(status().isBadRequest());
    mvc.perform(
            put("/api/admin/system-config")
                .header(HttpHeaders.AUTHORIZATION, authorization)
                .contentType(MediaType.APPLICATION_JSON)
                .content(
                    "{\"artifactRetentionDays\":{\"value\":30,\"version\":0,\"unknown\":true}}"))
        .andExpect(status().isBadRequest());
  }

  private String token(String role) {
    var user =
        new UserAccount(
            ADMIN_ID.toString(),
            "principal",
            "$argon2id$encoded",
            role,
            "ACTIVE",
            LocalDateTime.now(ZoneOffset.UTC));
    given(userAccountRepository.findById(ADMIN_ID.toString()))
        .willReturn(java.util.Optional.of(user));
    return "Bearer " + tokenService.issue(user).accessToken();
  }
}
