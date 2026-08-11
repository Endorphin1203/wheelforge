package com.wheelforge.api.security;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.wheelforge.api.common.ApiExceptionHandler;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.web.bind.annotation.RestController;

@WebMvcTest(
    controllers = StaticAssetSecurityTest.NoopController.class,
    properties = "wheelforge.auth.token-secret=0123456789abcdef0123456789abcdef")
@Import({ApiExceptionHandler.class, SecurityConfig.class, TokenService.class})
class StaticAssetSecurityTest {
  @Autowired private MockMvc mvc;
  @MockitoBean private UserAccountRepository userAccountRepository;

  @Test
  void servesTheStaticEntryPointWithoutOpeningApiRoutes() throws Exception {
    mvc.perform(get("/index.html"))
        .andExpect(status().isOk())
        .andExpect(content().string(org.hamcrest.Matchers.containsString("WheelForge test shell")));

    mvc.perform(get("/api/build-tasks"))
        .andExpect(status().isUnauthorized())
        .andExpect(jsonPath("$.code").value("UNAUTHENTICATED"));
  }

  @RestController
  static class NoopController {}
}
