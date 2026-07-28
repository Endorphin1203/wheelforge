package com.wheelforge.api.target;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.verify;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
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
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(
    controllers = TargetProfileController.class,
    properties = "wheelforge.auth.token-secret=0123456789abcdef0123456789abcdef")
@Import({ApiExceptionHandler.class, SecurityConfig.class, TokenService.class})
class TargetProfileTest {
  private static final UUID PROFILE_ID = UUID.fromString("d29ec24f-095c-45f6-ac96-acdef0e7d8ad");

  @Autowired private MockMvc mvc;
  @Autowired private TokenService tokenService;
  @MockitoBean private TargetProfileService service;
  @MockitoBean private UserAccountRepository userAccountRepository;

  @Test
  void endpointReturnsEnabledTargetCombinations() throws Exception {
    given(service.enabledProfiles()).willReturn(List.of(view()));

    mvc.perform(get("/api/target-profiles").header(HttpHeaders.AUTHORIZATION, bearerToken()))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$[0].os").value("LINUX"))
        .andExpect(jsonPath("$[0].architecture").value("AARCH64"))
        .andExpect(jsonPath("$[0].pythonImplementation").value("CPYTHON"))
        .andExpect(jsonPath("$[0].pythonVersion").value("3.11"))
        .andExpect(jsonPath("$[0].abiTags[0]").value("cp311"));
  }

  @Test
  void serviceQueriesOnlyEnabledRows() {
    var repository = org.mockito.Mockito.mock(TargetProfileRepository.class);
    given(repository.findAllByEnabledTrueOrderByOsAscArchitectureAscPythonVersionAsc())
        .willReturn(List.of(entity()));
    var targetService = new TargetProfileService(repository);

    assertThat(targetService.enabledProfiles()).containsExactly(view());
    verify(repository).findAllByEnabledTrueOrderByOsAscArchitectureAscPythonVersionAsc();
  }

  private TargetProfileService.TargetProfileView view() {
    return new TargetProfileService.TargetProfileView(
        PROFILE_ID,
        "linux-aarch64-cp311",
        "LINUX",
        "AARCH64",
        "CPYTHON",
        "3.11",
        "3.11.9",
        "manylinux2014_aarch64",
        List.of("cp311", "abi3", "none"),
        "STATIC",
        "wheel-tags-v1");
  }

  private TargetProfileEntity entity() {
    return new TargetProfileEntity(
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
        true);
  }

  private String bearerToken() {
    var user =
        new UserAccount(
            "cae6ea8d-0afe-41df-aeea-fc0e5aceabfb",
            "alice",
            "$argon2id$encoded",
            "USER",
            "ACTIVE",
            LocalDateTime.now(ZoneOffset.UTC));
    given(userAccountRepository.findById(user.getId())).willReturn(java.util.Optional.of(user));
    return "Bearer " + tokenService.issue(user).accessToken();
  }
}
