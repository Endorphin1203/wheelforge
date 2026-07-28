package com.wheelforge.api.security;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verifyNoInteractions;

import jakarta.servlet.DispatcherType;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpHeaders;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;

class TokenAuthenticationFilterTest {
  private final TokenService tokenService = mock(TokenService.class);
  private final SecurityConfig.TokenAuthenticationFilter filter =
      new SecurityConfig.TokenAuthenticationFilter(tokenService);

  @AfterEach
  void clearSecurityContext() {
    SecurityContextHolder.clearContext();
  }

  @Test
  void authenticatesBearerTokenOnAsyncRedispatch() throws Exception {
    var currentUser = new CurrentUser(UUID.randomUUID(), "USER");
    given(tokenService.parse("valid-token")).willReturn(Optional.of(currentUser));
    var request = asyncRequest();
    request.addHeader(HttpHeaders.AUTHORIZATION, "Bearer valid-token");
    var authentication = new AtomicReference<Authentication>();

    filter.doFilter(
        request,
        new MockHttpServletResponse(),
        (ignoredRequest, ignoredResponse) ->
            authentication.set(SecurityContextHolder.getContext().getAuthentication()));

    assertThat(authentication.get()).isNotNull();
    assertThat(authentication.get().getPrincipal()).isEqualTo(currentUser);
  }

  @Test
  void leavesAsyncRedispatchUnauthenticatedWithoutBearerToken() throws Exception {
    var authentication = new AtomicReference<Authentication>();

    filter.doFilter(
        asyncRequest(),
        new MockHttpServletResponse(),
        (ignoredRequest, ignoredResponse) ->
            authentication.set(SecurityContextHolder.getContext().getAuthentication()));

    assertThat(authentication.get()).isNull();
    verifyNoInteractions(tokenService);
  }

  private MockHttpServletRequest asyncRequest() {
    var request = new MockHttpServletRequest();
    request.setDispatcherType(DispatcherType.ASYNC);
    return request;
  }
}
