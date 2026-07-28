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
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;

class TokenAuthenticationFilterTest {
  private final TokenService tokenService = mock(TokenService.class);
  private final UserAccountRepository userAccountRepository = mock(UserAccountRepository.class);
  private final SecurityConfig.TokenAuthenticationFilter filter =
      new SecurityConfig.TokenAuthenticationFilter(tokenService, userAccountRepository);

  @AfterEach
  void clearSecurityContext() {
    SecurityContextHolder.clearContext();
  }

  @Test
  void authenticatesBearerTokenOnAsyncRedispatch() throws Exception {
    var currentUser = new CurrentUser(UUID.randomUUID(), "USER");
    given(tokenService.parse("valid-token")).willReturn(Optional.of(currentUser));
    given(userAccountRepository.findById(currentUser.userId().toString()))
        .willReturn(Optional.of(account(currentUser, "ACTIVE", "USER")));
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
    verifyNoInteractions(userAccountRepository);
  }

  @Test
  void rejectsRealTokenOnAsyncRedispatchAfterAccountDisable() throws Exception {
    UUID userId = UUID.randomUUID();
    var account =
        new UserAccount(
            userId.toString(),
            "async-user",
            "$argon2id$encoded",
            "USER",
            "ACTIVE",
            java.time.LocalDateTime.now());
    var realTokenService =
        new TokenService(
            "0123456789abcdef0123456789abcdef", new tools.jackson.databind.ObjectMapper());
    String token = realTokenService.issue(account).accessToken();
    given(userAccountRepository.findById(userId.toString())).willReturn(Optional.of(account));
    var realFilter =
        new SecurityConfig.TokenAuthenticationFilter(realTokenService, userAccountRepository);

    assertThat(authenticationSeen(realFilter, token)).isNotNull();
    account.updateStatus("DISABLED");
    assertThat(authenticationSeen(realFilter, token)).isNull();
  }

  @Test
  void revokedTokenClearsAnyAuthenticationRestoredForAsyncRedispatch() throws Exception {
    var currentUser = new CurrentUser(UUID.randomUUID(), "ADMIN");
    given(tokenService.parse("revoked-token")).willReturn(Optional.of(currentUser));
    given(userAccountRepository.findById(currentUser.userId().toString()))
        .willReturn(Optional.of(account(currentUser, "DISABLED", "ADMIN")));
    SecurityContextHolder.getContext()
        .setAuthentication(
            UsernamePasswordAuthenticationToken.authenticated(
                currentUser, null, java.util.List.of()));
    var request = asyncRequest();
    request.addHeader(HttpHeaders.AUTHORIZATION, "Bearer revoked-token");
    var authentication = new AtomicReference<Authentication>();

    filter.doFilter(
        request,
        new MockHttpServletResponse(),
        (ignoredRequest, ignoredResponse) ->
            authentication.set(SecurityContextHolder.getContext().getAuthentication()));

    assertThat(authentication.get()).isNull();
  }

  private MockHttpServletRequest asyncRequest() {
    var request = new MockHttpServletRequest();
    request.setDispatcherType(DispatcherType.ASYNC);
    return request;
  }

  private Authentication authenticationSeen(
      SecurityConfig.TokenAuthenticationFilter candidate, String token) throws Exception {
    SecurityContextHolder.clearContext();
    var request = asyncRequest();
    request.addHeader(HttpHeaders.AUTHORIZATION, "Bearer " + token);
    var authentication = new AtomicReference<Authentication>();
    candidate.doFilter(
        request,
        new MockHttpServletResponse(),
        (ignoredRequest, ignoredResponse) ->
            authentication.set(SecurityContextHolder.getContext().getAuthentication()));
    return authentication.get();
  }

  private UserAccount account(CurrentUser user, String status, String role) {
    return new UserAccount(
        user.userId().toString(),
        "user",
        "$argon2id$encoded",
        role,
        status,
        java.time.LocalDateTime.now());
  }
}
