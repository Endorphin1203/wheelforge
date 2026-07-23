package com.wheelforge.api.security;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.wheelforge.api.common.ApiExceptionHandler;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.Base64;
import java.util.Map;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpHeaders;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.ObjectMapper;

@WebMvcTest(
    controllers = {SecurityFilterChainTest.ProtectedController.class},
    properties = "wheelforge.auth.token-secret=0123456789abcdef0123456789abcdef")
@Import({
  ApiExceptionHandler.class,
  SecurityConfig.class,
  TokenService.class,
  SecurityFilterChainTest.ProtectedController.class
})
class SecurityFilterChainTest {
  private static final String TOKEN_SECRET = "0123456789abcdef0123456789abcdef";
  private static final String USER_ID = "d290f1ee-6c54-4b01-90e6-d701748f0851";

  @Autowired private MockMvc mvc;
  @Autowired private TokenService tokenService;

  @Test
  void acceptsAValidBearerTokenAndExposesTheCurrentUser() throws Exception {
    mvc.perform(
            get("/api/test/current-user").header(HttpHeaders.AUTHORIZATION, bearer(validToken())))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.userId").value(USER_ID))
        .andExpect(jsonPath("$.admin").value(true));
  }

  @Test
  void rejectsTamperedExpiredAndInvalidRoleTokensWithTheGlobalErrorShape() throws Exception {
    String validToken = validToken();
    long now = Instant.now().getEpochSecond();

    for (String token :
        new String[] {
          validToken + "x",
          signedToken(now - 60, now - 1, "USER"),
          signedToken(now, now + 60, "AUDITOR")
        }) {
      mvc.perform(get("/api/test/current-user").header(HttpHeaders.AUTHORIZATION, bearer(token)))
          .andExpect(status().isUnauthorized())
          .andExpect(jsonPath("$.code").value("UNAUTHENTICATED"))
          .andExpect(jsonPath("$.message").isString())
          .andExpect(jsonPath("$.fieldErrors").isMap())
          .andExpect(jsonPath("$.traceId").isString());
    }
  }

  @Test
  void returnsGlobalErrorsForCommonMvcFailures() throws Exception {
    String authorization = bearer(validToken());

    mvc.perform(get("/api/test/missing").header(HttpHeaders.AUTHORIZATION, authorization))
        .andExpect(status().isNotFound())
        .andExpect(jsonPath("$.code").value("NOT_FOUND"))
        .andExpect(jsonPath("$.fieldErrors").isMap())
        .andExpect(jsonPath("$.traceId").isString());
    mvc.perform(get("/api/test/method").header(HttpHeaders.AUTHORIZATION, authorization))
        .andExpect(status().isMethodNotAllowed())
        .andExpect(jsonPath("$.code").value("METHOD_NOT_ALLOWED"));
    mvc.perform(
            post("/api/test/content")
                .header(HttpHeaders.AUTHORIZATION, authorization)
                .contentType("text/plain")
                .content("not json"))
        .andExpect(status().isUnsupportedMediaType())
        .andExpect(jsonPath("$.code").value("UNSUPPORTED_MEDIA_TYPE"));
    mvc.perform(get("/api/test/required").header(HttpHeaders.AUTHORIZATION, authorization))
        .andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("VALIDATION_FAILED"));
    mvc.perform(get("/api/test/boom").header(HttpHeaders.AUTHORIZATION, authorization))
        .andExpect(status().isInternalServerError())
        .andExpect(jsonPath("$.code").value("INTERNAL_ERROR"))
        .andExpect(jsonPath("$.message").isString())
        .andExpect(jsonPath("$.fieldErrors").isMap())
        .andExpect(jsonPath("$.traceId").isString());
  }

  private String validToken() {
    return tokenService
        .issue(
            new UserAccount(
                USER_ID,
                "admin",
                "$argon2id$encoded",
                "ADMIN",
                "ACTIVE",
                LocalDateTime.now(ZoneOffset.UTC)))
        .accessToken();
  }

  private String bearer(String token) {
    return "Bearer " + token;
  }

  private String signedToken(long issuedAt, long expiresAt, String role) throws Exception {
    Base64.Encoder encoder = Base64.getUrlEncoder().withoutPadding();
    String header = encoder.encodeToString("{\"alg\":\"HS256\",\"typ\":\"JWT\"}".getBytes());
    String payload =
        encoder.encodeToString(
            new ObjectMapper()
                .writeValueAsBytes(
                    Map.of("sub", USER_ID, "role", role, "iat", issuedAt, "exp", expiresAt)));
    Mac mac = Mac.getInstance("HmacSHA256");
    mac.init(new SecretKeySpec(TOKEN_SECRET.getBytes(), "HmacSHA256"));
    return header
        + "."
        + payload
        + "."
        + encoder.encodeToString(mac.doFinal((header + "." + payload).getBytes()));
  }

  @RestController
  @RequestMapping("/api/test")
  public static class ProtectedController {
    @GetMapping("/current-user")
    Map<String, Object> currentUser(@AuthenticationPrincipal CurrentUser currentUser) {
      return Map.of(
          "userId", currentUser.requireUserId().toString(), "admin", currentUser.isAdmin());
    }

    @PostMapping(value = "/content", consumes = "application/json")
    void content() {}

    @PostMapping("/method")
    void method() {}

    @GetMapping("/required")
    void required(@RequestParam String value) {}

    @GetMapping("/boom")
    void boom() {
      throw new IllegalStateException("unexpected");
    }
  }
}
