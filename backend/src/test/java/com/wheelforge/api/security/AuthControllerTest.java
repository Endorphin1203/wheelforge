package com.wheelforge.api.security;

import static org.mockito.BDDMockito.given;
import static org.springframework.http.MediaType.APPLICATION_JSON;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.wheelforge.api.common.ApiExceptionHandler;
import java.time.Instant;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(AuthController.class)
@AutoConfigureMockMvc
@Import({ApiExceptionHandler.class, SecurityConfig.class})
class AuthControllerTest {
  @Autowired private MockMvc mvc;
  @MockitoBean private AuthService authService;
  @MockitoBean private TokenService tokenService;

  @Test
  void returnsTokenForValidCredentials() throws Exception {
    given(authService.login("alice", "correct horse battery staple"))
        .willReturn(
            new AuthService.TokenResponse("signed-token", Instant.parse("2026-07-23T00:30:00Z")));

    mvc.perform(
            post("/api/auth/login")
                .contentType(APPLICATION_JSON)
                .content("{\"username\":\"alice\",\"password\":\"correct horse battery staple\"}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.accessToken").isString())
        .andExpect(jsonPath("$.expiresAt").isString());
  }

  @Test
  void rejectsInvalidCredentialsWithoutRevealingUsernameExistence() throws Exception {
    given(authService.login("alice", "wrong"))
        .willThrow(new AuthService.AuthenticationFailedException());

    mvc.perform(
            post("/api/auth/login")
                .contentType(APPLICATION_JSON)
                .content("{\"username\":\"alice\",\"password\":\"wrong\"}"))
        .andExpect(status().isUnauthorized())
        .andExpect(jsonPath("$.code").value("AUTHENTICATION_FAILED"));
  }

  @Test
  void rejectsUnauthorizedResourcesUsingTheGlobalErrorShape() throws Exception {
    mvc.perform(get("/api/private-resource"))
        .andExpect(status().isUnauthorized())
        .andExpect(jsonPath("$.code").value("UNAUTHENTICATED"))
        .andExpect(jsonPath("$.message").isString())
        .andExpect(jsonPath("$.fieldErrors").isMap())
        .andExpect(jsonPath("$.traceId").isString());
  }
}
